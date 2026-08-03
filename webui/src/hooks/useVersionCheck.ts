import { useEffect, useState } from "react";

/**
 * updater.json 元数据结构。
 * 由 GitHub Actions 发版时自动生成,托管在 public/updater.json(同源)或远程 CDN。
 * Tauri 桌面端后续接入 @tauri-apps/plugin-updater 时可复用同一份元数据。
 */
export interface UpdaterMeta {
  /** 最新版本号,例如 "1.2.0"。 */
  version: string;
  /** 发布时间,ISO 8601 字符串。 */
  pub_date?: string;
  /** 多语言 release notes,Markdown 文本。 */
  notes?: Record<string, string>;
  /** 各包管理器对应的最新版本(可与 version 不同步)。 */
  package?: {
    pypi?: string;
    npm?: string;
    docker?: string;
  };
  /** 最低兼容版本,低于此版本时强制升级提示。 */
  min_required?: string;
  /** GitHub Release 页面 URL。 */
  release_url?: string;
}

export interface UpdateInfo {
  /** 当前版本(来自后端 boot.version)。 */
  currentVersion: string;
  /** 远程最新版本。 */
  latestVersion: string;
  /** 是否有更新。 */
  hasUpdate: boolean;
  /** 当前版本是否低于 min_required(需要强制升级)。 */
  requiresForceUpdate: boolean;
  /** 多语言 release notes。 */
  notes: Record<string, string>;
  /** GitHub Release URL。 */
  releaseUrl: string;
  /** 是否运行在 Tauri 桌面端(后续接入原生 updater 时使用)。 */
  isTauri: boolean;
}

/** 远程 updater.json 地址(GitHub 仓库 main 分支,发版后自动反映最新版本)。 */
const REMOTE_UPDATER_URL =
  "https://raw.githubusercontent.com/zhoulingquan/MiniUnicorn/main/webui/public/updater.json";

/** 同源 fallback:远程不可达时(如离线/内网)用本地 updater.json。 */
const LOCAL_UPDATER_URL = "/updater.json";

/**
 * 检测是否有新版本。
 *
 * 设计参考 QwenPaw PR #715:前端 fetch 远程元数据 + semver 比较 + 红点 Badge 提示。
 * 与 QwenPaw 不同之处:
 *  1. 不依赖 PyPI JSON API,改用自建 updater.json(同时承载 Tauri 签名/多语言 notes)
 *  2. 使用 semver 语义化比较,而非简单字符串包含判断
 *  3. 静默失败(fetch 失败时 hasUpdate=false,不打扰用户)
 *
 * 优先 fetch 远程 updater.json(GitHub raw),失败时 fallback 到同源 /updater.json。
 * 这样旧版本实例无需 git pull 即可感知到 GitHub 上发布的新版本。
 *
 * @param currentVersion 当前版本号(来自后端 boot.version)
 * @param updaterUrl     updater.json 的 URL,默认远程 GitHub raw
 */
export function useVersionCheck(
  currentVersion: string | null,
  updaterUrl = REMOTE_UPDATER_URL,
): UpdateInfo | null {
  const [info, setInfo] = useState<UpdateInfo | null>(null);

  useEffect(() => {
    if (!currentVersion) return;
    // 锁定非 null 版本,避免闭包内 currentVersion 仍是 string | null
    const ver = currentVersion;

    let cancelled = false;
    const isTauri = "__TAURI_INTERNALS__" in window;

    // Tauri 桌面端后续接入 @tauri-apps/plugin-updater 时走原生检测
    // 当前 Phase 1 先统一走 fetch updater.json
    async function check() {
      try {
        let res = await fetch(updaterUrl, {
          cache: "no-cache",
          headers: { Accept: "application/json" },
        });
        // 远程不可达时 fallback 到同源本地 updater.json
        if (!res.ok && updaterUrl !== LOCAL_UPDATER_URL) {
          res = await fetch(LOCAL_UPDATER_URL, {
            cache: "no-cache",
            headers: { Accept: "application/json" },
          });
        }
        if (!res.ok) return;
        const data: UpdaterMeta = await res.json();
        if (cancelled || !data?.version) return;

        const hasUpdate = semverLt(ver, data.version);
        const requiresForceUpdate = data.min_required
          ? semverLt(ver, data.min_required)
          : false;

        setInfo({
          currentVersion: ver,
          latestVersion: data.version,
          hasUpdate,
          requiresForceUpdate,
          notes: data.notes ?? {},
          releaseUrl:
            data.release_url ??
            `https://github.com/zhoulingquan/MiniUnicorn/releases/tag/v${data.version}`,
          isTauri,
        });
      } catch {
        // 静默失败:版本检查失败不应影响主功能
      }
    }

    check();
    // 每 6 小时轮询一次(长会话场景下也能感知新版本)
    const timer = setInterval(check, 6 * 60 * 60 * 1000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [currentVersion, updaterUrl]);

  return info;
}

/**
 * 简易 semver "小于" 比较。
 * 仅处理 X.Y.Z 格式(忽略预发布标签),满足版本检测需求。
 * 不引入 semver 依赖以减小 bundle 体积。
 */
function semverLt(a: string, b: string): boolean {
  const pa = normalizeSemver(a);
  const pb = normalizeSemver(b);
  for (let i = 0; i < 3; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x < y) return true;
    if (x > y) return false;
  }
  return false;
}

function normalizeSemver(v: string): number[] {
  // 去掉可能的 "v" 前缀和预发布标签(如 "1.2.3-beta.1" → "1.2.3")
  const cleaned = v.replace(/^v/, "").split("-")[0].split(".");
  return cleaned.slice(0, 3).map((n) => {
    const num = parseInt(n, 10);
    return Number.isNaN(num) ? 0 : num;
  });
}
