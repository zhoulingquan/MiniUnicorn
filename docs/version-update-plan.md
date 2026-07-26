# MiniUnicorn 多部署方式版本更新提示改造方案

> 版本：v1.0
> 日期：2026-07-26
> 状态：Phase 1 已实施，Phase 2/3 待实施

## 一、背景与目标

### 1.1 现状

MiniUnicorn 当前支持多种部署方式：

| 部署方式 | 安装命令 | 适用场景 |
|---|---|---|
| **pip 安装** | `pip install miniunicorn` | 普通用户，开箱即用 |
| **源码安装** | `git clone && pip install -e .` | 开发者，需自定义 |
| **Docker** | `docker pull agentscope/miniunicorn` | 容器化部署 |
| **Web 模式** | 浏览器访问 `http://127.0.0.1:8088` | 本地或云端网关 |
| **Tauri 桌面端**（规划中） | 下载安装包 | 桌面原生体验 |

### 1.2 问题

- 用户无法感知新版本发布，需主动查看 GitHub Releases
- 不同部署方式升级命令不同，用户容易混淆
- 后期接入 Tauri 桌面端后，需要支持自动更新（签名验证 + 一键安装）

### 1.3 目标

- **统一版本元数据源**：一份 `updater.json`，多端共用
- **Web 端**：版本号红点提示 + Modal 显示升级命令（已实施）
- **Docker 端**：容器启动时检测 + Web UI 复用提示
- **Tauri 端**：原生 updater 插件 + Ed25519 签名验证 + 自动下载安装

### 1.4 参考项目

| 项目 | 模式 | 借鉴点 |
|---|---|---|
| **QwenPaw**（[PR #715](https://github.com/agentscope-ai/QwenPaw/pull/715)） | 前端 fetch PyPI + 红点 Badge + Modal | UI 交互、多部署命令展示 |
| **Reasonix**（[desktop-v1.0.0](https://github.com/esengine/DeepSeek-Reasonix/releases/tag/desktop-v1.0.0)） | Tauri updater + Ed25519 签名 + 自建镜像 | 桌面端自动更新、签名验证、大陆加速 |

## 二、整体架构

```
┌─────────────────────────────────────────────────────────┐
│            GitHub Releases（单一真相源）                 │
│  ├─ Tauri 安装包（.dmg/.exe/.deb/.AppImage）            │
│  ├─ Tauri .sig 签名文件                                 │
│  ├─ PyPI / npm 包                                      │
│  └─ Docker 镜像                                         │
└──────────────────┬──────────────────────────────────────┘
                   │ GitHub Actions 自动同步
                   ▼
┌─────────────────────────────────────────────────────────┐
│   updater.json（托管：GitHub Pages / 自建镜像）         │
│   { version, notes, platforms, signature, urls }        │
└──────────────────┬──────────────────────────────────────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────────┐
│  Web 端  │ │ Docker   │ │ Tauri 桌面端 │
│ 红点提示 │ │ 启动检测 │ │ 自动下载     │
│ 命令复制 │ │ UI 复用  │ │ 签名验证     │
│ 手动升级 │ │ 提示     │ │ 一键安装     │
└──────────┘ └──────────┘ └──────────────┘
```

**核心原则**：
1. 一份 `updater.json` 元数据，多端共用
2. Web/Docker 走"提示 + 命令复制"模式（无法自动升级）
3. Tauri 走"自动下载 + 签名验证 + 一键安装"模式
4. 静默失败：版本检查失败不影响主功能

## 三、统一版本元数据（updater.json）

### 3.1 文件结构

```json
{
  "version": "0.4.0",
  "pub_date": "2026-07-26T10:00:00Z",
  "notes": {
    "zh": "### v0.4.0\n\n- 新增 X 功能\n- 修复 Y 问题",
    "en": "### v0.4.0\n\n- Added X\n- Fixed Y"
  },
  "tauri": {
    "darwin-aarch64": {
      "url": "https://dl.miniunicorn.cn/v0.4.0/miniunicorn-darwin-arm64.app.tar.gz",
      "signature": "dW50cnVzdGVkIGNvbW1lbnQ6..."
    },
    "darwin-x86_64": { "url": "...", "signature": "..." },
    "linux-x86_64": { "url": "...", "signature": "..." },
    "windows-x86_64": { "url": "...", "signature": "..." }
  },
  "package": {
    "pypi": "0.4.0",
    "npm": "0.4.0",
    "docker": "agentscope/miniunicorn:0.4.0"
  },
  "min_required": "0.1.0",
  "release_url": "https://github.com/tuolaonainaiguomalu/mini-Unicorn/releases/tag/v0.4.0"
}
```

### 3.2 字段说明

| 字段 | 类型 | 用途 |
|---|---|---|
| `version` | string | 最新版本号（X.Y.Z 格式） |
| `pub_date` | string | 发布时间（ISO 8601） |
| `notes` | Record<string, string> | 多语言 release notes（Markdown） |
| `tauri` | object | Tauri 各平台安装包 URL + 签名 |
| `package` | object | 各包管理器对应版本（可与 version 不同步） |
| `min_required` | string | 最低兼容版本，低于此版本强制升级 |
| `release_url` | string | GitHub Release 页面 URL |

### 3.3 托管位置

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `https://dl.miniunicorn.cn/updater.json` | 自建镜像（大陆加速，Phase 4） |
| 2 | `https://github.com/tuolaonainaiguomalu/mini-Unicorn/releases/latest/download/updater.json` | GitHub Releases |
| 3 | 同源 `/updater.json` | Web 模式本地兜底（已实施） |

## 四、Web 端方案（已实施 ✅）

### 4.1 实现思路

参考 QwenPaw PR #715 模式：前端 fetch `/updater.json` + semver 比较 + 红点 Badge + Modal 显示升级命令。

### 4.2 已实施文件

| 文件 | 作用 |
|---|---|
| `webui/src/hooks/useVersionCheck.ts` | 版本检测 Hook（fetch + semver + 6h 轮询） |
| `webui/src/components/VersionBadge.tsx` | 版本号徽章 + Tooltip + Dialog |
| `webui/public/updater.json` | 本地兜底元数据模板 |
| `webui/src/components/thread/TopBar.tsx` | 集成 `<VersionBadge>` |
| `webui/src/i18n/locales/zh-CN/common.json` | 中文文案 |
| `webui/src/i18n/locales/en/common.json` | 英文文案 |

### 4.3 UI 交互

```
顶栏 logo 旁：MiniUnicorn v0.4.0●  （紫色高亮 + 红点）
                    ↓ 点击
┌──────────────────────────────────────────┐
│  发现新版本 v0.5.0                       │
│  当前版本 v0.4.0，最新版本 v0.5.0        │
│                                          │
│  ┌─ release notes（Markdown 渲染）─┐    │
│  │ ### v0.5.0                       │    │
│  │ - 新增 X 功能                    │    │
│  └──────────────────────────────────┘    │
│                                          │
│  选择你的部署方式：                      │
│  pip     [pip install --upgrade ...] 📋 │
│  npm     [npm install -g ...@latest] 📋 │
│  Docker  [docker pull ...:latest]    📋 │
│  源码    [git pull && pip install]   📋 │
│                                          │
│  升级后请重启服务：miniunicorn gateway   │
│                                          │
│              [查看发布说明]              │
└──────────────────────────────────────────┘
```

### 4.4 关键设计

- **静默失败**：fetch 失败时 `hasUpdate=false`，不弹错误对话框
- **6 小时轮询**：长会话场景下也能感知新版本
- **强制升级**：`min_required` 字段触发红点改为 destructive 红色
- **Tauri 环境探测**：`__TAURI_INTERNALS__ in window` 为 Phase 3 预留分支
- **复制按钮**：2000ms 反馈，符合用户偏好（200ms Tooltip delay）

## 五、Docker 部署方案（Phase 2 待实施）

### 5.1 问题分析

Docker 容器内的 MiniUnicorn 无法自行 `docker pull` 升级镜像，只能：
1. 在 Web UI 提示用户执行 `docker pull` + `docker run`
2. 容器启动时打印版本检查日志

### 5.2 改造点

#### 5.2.1 后端启动时检测（可选）

在 `miniunicorn/cli/_gateway_runner.py` 启动流程中加入版本检查：

```python
# 伪代码
async def check_remote_version():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            res = await client.get("https://dl.miniunicorn.cn/updater.json")
            data = res.json()
            if semver_lt(CURRENT_VERSION, data["version"]):
                logger.info(
                    f"发现新版本 {data['version']}（当前 {CURRENT_VERSION}），"
                    f"请执行：docker pull {data['package']['docker']}"
                )
    except Exception:
        pass  # 静默失败
```

#### 5.2.2 Web UI 复用提示

Docker 部署的 MiniUnicorn Web UI 自动复用 Phase 1 的 `<VersionBadge>` 组件，无需额外改造。用户在浏览器中看到红点提示，点击 Modal 显示：

```bash
docker pull agentscope/miniunicorn:latest
docker run -p 127.0.0.1:8088:8088 -v miniunicorn-data:/app/workspace agentscope/miniunicorn:latest
```

#### 5.2.3 Docker 镜像 LABEL

在 `Dockerfile` 中添加版本标签，便于 `docker inspect` 查询：

```dockerfile
LABEL org.miniunicorn.version="0.4.0"
LABEL org.miniunicorn.updater-url="https://dl.miniunicorn.cn/updater.json"
```

### 5.3 升级流程

```
容器启动 → 后端打印版本检查日志（可选）
    ↓
用户访问 Web UI → 看到 v0.4.0 红点提示
    ↓
点击版本号 → Modal 显示 docker pull 命令
    ↓
用户在宿主机执行：
  docker pull agentscope/miniunicorn:latest
  docker stop miniunicorn && docker rm miniunicorn
  docker run ... agentscope/miniunicorn:latest
    ↓
重新访问 Web UI → 版本号更新
```

## 六、Tauri 桌面端方案（Phase 3 待实施）

### 6.1 实现思路

参考 Reasonix 桌面端（[desktop-v1.0.0](https://github.com/esengine/DeepSeek-Reasonix/releases/tag/desktop-v1.0.0)）：使用 Tauri 官方 `@tauri-apps/plugin-updater` 插件 + Ed25519 签名验证 + GitHub Releases 多平台构建。

### 6.2 项目结构

```
mini-Unicorn/
├── src-tauri/                    # 新增 Tauri 后端
│   ├── tauri.conf.json
│   ├── Cargo.toml
│   ├── src/main.rs
│   └── icons/
├── webui/                        # 现有前端（复用）
│   └── src/components/VersionBadge.tsx  # 已预留 isTauri 分支
├── .github/workflows/
│   ├── release.yml               # 新增：Tauri 多平台构建
│   └── pages.yml                 # 新增：同步 updater.json
└── ...
```

### 6.3 tauri.conf.json 关键配置

```json
{
  "version": "0.4.0",
  "plugins": {
    "updater": {
      "active": true,
      "endpoints": [
        "https://dl.miniunicorn.cn/updater.json",
        "https://github.com/tuolaonainaiguomalu/mini-Unicorn/releases/latest/download/updater.json"
      ],
      "pubkey": "你的Ed25519公钥",
      "dialog": true
    }
  },
  "bundle": {
    "active": true,
    "targets": ["app", "dmg", "deb", "appimage", "nsis"],
    "icon": ["icons/icon.icns", "icons/icon.ico", "icons/icon.png"]
  }
}
```

### 6.4 签名密钥生成（一次性）

```bash
# 生成密钥对
npx @tauri-apps/cli signer generate -w ~/.tauri/miniunicorn.key

# 输出示例：
# Private key: /Users/xxx/.tauri/miniunicorn.key
# Public key: dW50cnVzdGVkIGNvbW1lbnQ6IG1pbml1bmljb3JuIHM...
```

- **私钥**：保管好，配置到 GitHub Secrets（`TAURI_SIGNING_PRIVATE_KEY`）
- **公钥**：填入 `tauri.conf.json` 的 `pubkey` 字段

### 6.5 前端改造（VersionBadge.tsx）

在现有 `VersionBadge.tsx` 的 `handleClick` 中加入 Tauri 分支：

```typescript
const handleClick = async () => {
  if (!hasUpdate && !requiresForce) return;

  // Tauri 桌面端：调用原生 updater 自动下载安装
  if (updateInfo?.isTauri) {
    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const { relaunch } = await import("@tauri-apps/plugin-process");
      const update = await check();
      if (update) {
        // 可选：显示下载进度
        await update.downloadAndInstall((event) => {
          if (event.event === "Started") {
            console.log(`下载中：${event.data.contentLength} 字节`);
          } else if (event.event === "Progress") {
            console.log(`已下载 ${event.data.chunkLength} 字节`);
          } else if (event.event === "Finished") {
            console.log("下载完成");
          }
        });
        await relaunch();
      }
    } catch (e) {
      console.error("自动更新失败:", e);
      // 回退到 Web 模式：打开 Modal 显示手动升级命令
      setOpen(true);
    }
    return;
  }

  // Web 模式：打开 Modal 显示命令
  setOpen(true);
};
```

### 6.6 GitHub Actions 发布流水线

```yaml
# .github/workflows/release.yml
name: Release
on:
  push:
    tags: ["v*"]

jobs:
  publish-tauri:
    permissions: { contents: write }
    strategy:
      fail-fast: false
      matrix:
        include:
          - { platform: macos-latest, args: "--target aarch64-apple-darwin" }
          - { platform: macos-latest, args: "--target x86_64-apple-darwin" }
          - { platform: ubuntu-22.04, args: "" }
          - { platform: windows-latest, args: "" }
    runs-on: ${{ matrix.platform }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: lts/* }
      - uses: dtolnay/rust-toolchain@stable
        with:
          targets: ${{ matrix.platform == 'macos-latest' && 'aarch64-apple-darwin,x86_64-apple-darwin' || '' }}
      - uses: swatinem/rust-cache@v2
        with: { workspaces: "./src-tauri -> target" }
      - run: npm ci
        working-directory: webui
      - uses: tauri-apps/tauri-action@v0
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
          TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}
        with:
          tagName: v__VERSION__
          releaseName: "v__VERSION__"
          releaseDraft: false
          prerelease: false
          args: ${{ matrix.args }}
          updaterJsonPreferNsis: true

  update-updater-json:
    needs: publish-tauri
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: 读取版本号
        id: version
        run: echo "version=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')" >> $GITHUB_OUTPUT
      - name: 生成 updater.json
        run: |
          cat > updater.json << EOF
          {
            "version": "${{ steps.version.outputs.version }}",
            "pub_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
            "notes": { "zh": "...", "en": "..." },
            "tauri": { ... },
            "package": { "pypi": "...", "npm": "...", "docker": "..." },
            "min_required": "0.1.0",
            "release_url": "..."
          }
          EOF
      - name: 推送到 GitHub Pages
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: .
          publish_branch: gh-pages
          keep_files: true
```

### 6.7 更新流程

```
应用启动 → Tauri updater 插件检查 endpoints
    ↓
比对版本号 → 有更新则弹原生对话框
    ↓
用户点击"下载并安装"
    ↓
后台下载 .tar.gz + .sig 文件
    ↓
Ed25519 签名验证（公钥内置在 tauri.conf.json）
    ↓
验证通过 → 替换二进制 → 提示重启
    ↓
用户重启 → 新版本启动
```

### 6.8 安全考虑

| 风险 | 缓解措施 |
|---|---|
| 中间人攻击替换安装包 | Ed25519 签名验证（强制） |
| 私钥泄露 | 私钥仅存于 GitHub Secrets，不入仓库 |
| 公钥被篡改 | 公钥编译进二进制，无法运行时修改 |
| 回滚攻击 | 不支持降级，仅允许升级到更高版本 |

## 七、多部署方式适配表

| 部署方式 | 检测源 | 提示方式 | 升级动作 | 实施阶段 |
|---|---|---|---|---|
| **Web 模式** | `/updater.json`（同源） | 版本号红点 + Modal | 显示命令复制 | Phase 1 ✅ |
| **pip 安装** | `/updater.json` | 版本号红点 + Modal | `pip install --upgrade` 复制 | Phase 1 ✅ |
| **npm 安装** | `/updater.json` | 版本号红点 + Modal | `npm i -g ...@latest` 复制 | Phase 1 ✅ |
| **源码部署** | `/updater.json` | 版本号红点 + Modal | `git pull && pip install -e .` 复制 | Phase 1 ✅ |
| **Docker 部署** | `/updater.json` | 版本号红点 + Modal | `docker pull` + `docker run` 复制 | Phase 1 ✅ |
| **Tauri 桌面** | `updater.json`（远程） | 系统原生对话框 | 自动下载+签名验证+一键安装重启 | Phase 3 ⏳ |

## 八、实施路线

### Phase 1：Web 端版本检测（已实施 ✅）

- [x] 创建 `webui/src/hooks/useVersionCheck.ts`
- [x] 创建 `webui/src/components/VersionBadge.tsx`
- [x] 在 `TopBar.tsx` 集成 `<VersionBadge>`
- [x] 创建 `webui/public/updater.json` 模板
- [x] 添加 zh-CN / en i18n 文案
- [x] TypeScript 类型检查通过
- [x] Vite 构建成功
- [x] 单元测试通过（app-layout + i18n）

### Phase 2：CI 自动化 updater.json（待实施 ⏳）

- [ ] 写 GitHub Action，发 tag 时自动从 `pyproject.toml` 读版本号
- [ ] 生成 `updater.json` 推送到 GitHub Pages（`gh-pages` 分支）
- [ ] 同步 PyPI / npm / Docker 镜像版本号到 `package` 字段
- [ ] 从 GitHub Release body 提取 release notes 填入 `notes` 字段

### Phase 3：Tauri 桌面打包 + 自动更新（待实施 ⏳）

- [ ] `npm create tauri-app` 初始化 `src-tauri/`
- [ ] 生成 Ed25519 密钥对，公钥写入 `tauri.conf.json`
- [ ] 添加 `release.yml` GitHub Action（参考 6.6）
- [ ] 前端 `VersionBadge.tsx` 加入 `isTauri` 分支，调用 `@tauri-apps/plugin-updater`
- [ ] 多平台构建测试（macOS arm64/x64、Linux x64、Windows x64）
- [ ] 签名验证测试

### Phase 4：优化（可选 ⏳）

- [ ] 自建 `dl.miniunicorn.cn` 下载镜像（大陆加速，参考 Reasonix #3926）
- [ ] 加入"忽略此版本"功能
- [ ] 加入更新检查频率配置（每日/每周/手动）
- [ ] 后端启动时打印版本检查日志（Docker 场景）
- [ ] Docker 镜像添加 `LABEL org.miniunicorn.version`

## 九、关键设计决策

### 9.1 为何不依赖 PyPI/npm API

参考 QwenPaw 直接 fetch PyPI JSON API 的做法存在局限：
- 仅能获取 PyPI 版本，无法承载 Tauri 签名、多语言 notes
- 简单字符串包含比较（`version !== latestVersion`）无法处理预发布版本

**决策**：自建 `updater.json`，同时承载所有元数据。

### 9.2 为何 Web 端不自动升级

浏览器沙箱限制无法直接执行系统命令（`pip install` / `docker pull`），所以走"展示命令 + 复制"模式。这是 QwenPaw 的做法，也是行业标准实践。

### 9.3 为何 Tauri 端强制签名

参考 Reasonix #2467：
- Tauri 与 Electron 不同，强制要求更新包签名（Ed25519）
- 防止中间人攻击替换安装包
- 公钥预置在 `tauri.conf.json`，编译进二进制，无法运行时篡改

### 9.4 为何静默失败

版本检查是"锦上添花"功能，不应影响主功能：
- 网络异常时 `hasUpdate=false`，不弹错误对话框
- 仅在 `console.warn` 记录日志
- 与 QwenPaw 的 `.catch(() => {})` 一致

### 9.5 为何 6 小时轮询

- 用户长时间开着 Web UI（如工作日全天），希望及时感知新版本
- 6 小时平衡了实时性与服务器压力
- 可在 Phase 4 改为可配置

## 十、文件清单

### 10.1 已实施文件（Phase 1）

| 文件路径 | 类型 | 作用 |
|---|---|---|
| `webui/src/hooks/useVersionCheck.ts` | 新增 | 版本检测 Hook |
| `webui/src/components/VersionBadge.tsx` | 新增 | 版本号徽章 + Dialog |
| `webui/public/updater.json` | 新增 | 本地兜底元数据模板 |
| `webui/src/components/thread/TopBar.tsx` | 修改 | 集成 `<VersionBadge>` |
| `webui/src/i18n/locales/zh-CN/common.json` | 修改 | 新增 `version.*` 中文文案 |
| `webui/src/i18n/locales/en/common.json` | 修改 | 新增 `version.*` 英文文案 |

### 10.2 待实施文件（Phase 2/3）

| 文件路径 | 类型 | 阶段 |
|---|---|---|
| `.github/workflows/release.yml` | 新增 | Phase 3 |
| `.github/workflows/pages.yml` | 新增 | Phase 2 |
| `src-tauri/tauri.conf.json` | 新增 | Phase 3 |
| `src-tauri/Cargo.toml` | 新增 | Phase 3 |
| `src-tauri/src/main.rs` | 新增 | Phase 3 |
| `src-tauri/icons/*` | 新增 | Phase 3 |
| `webui/src/components/VersionBadge.tsx` | 修改 | Phase 3（加入 isTauri 分支） |
| `Dockerfile` | 修改 | Phase 4（添加 LABEL） |

## 十一、验证清单

### Phase 1 验证（已完成 ✅）

- [x] `tsc --noEmit` 类型检查通过
- [x] `vite build` 构建成功（3.48s）
- [x] ESLint 新增代码无错误
- [x] `app-layout.test.tsx` 15/15 通过
- [x] `i18n.test.tsx` + `format.i18n.test.ts` 11/11 通过

### Phase 3 验证（待执行）

- [ ] macOS arm64 安装包可正常下载安装
- [ ] macOS x64 安装包可正常下载安装
- [ ] Linux x64 .deb 安装包可正常下载安装
- [ ] Linux x64 .AppImage 可正常运行
- [ ] Windows x64 .exe 安装包可正常下载安装
- [ ] 签名验证通过（篡改安装包后拒绝安装）
- [ ] 自动更新流程完整（旧版本 → 检测 → 下载 → 安装 → 重启 → 新版本）

## 十二、参考资料

- [QwenPaw PR #715 - Version notification & header jump](https://github.com/agentscope-ai/QwenPaw/pull/715/files)
- [Reasonix desktop-v1.0.0 Release](https://github.com/esengine/DeepSeek-Reasonix/releases/tag/desktop-v1.0.0)
- [Reasonix desktop-v1.6.0 - Add desktop update check controls](https://github.com/esengine/DeepSeek-Reasonix/releases/tag/desktop-v1.6.0)
- [Tauri GitHub 发布工作流](https://tauri.org.cn/distribute/pipelines/github/)
- [Tauri 应用更新机制深度解析](https://blog.csdn.net/a2b3c4d5e/article/details/154862272)
- [Tauri Updater 插件文档](https://v2.tauri.app/plugin/updater/)
