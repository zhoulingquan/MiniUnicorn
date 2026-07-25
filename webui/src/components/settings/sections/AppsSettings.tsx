// Apps section:CLI Apps + MCP Presets 一体化管理。
// 参考 ChannelsView 的两段式列表模式：已启用（CLI installed / MCP configured）
// 在上半部分，可用项在下半部分。无 drawer，仅有 install/uninstall/update/test
// 四种原子动作；执行后通过 toast 展示 last_action.message。

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Loader2, Package, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { LoadingSpinner } from "@/components/ui/loading-spinner";
import { RefreshIconButton } from "@/components/ui/refresh-icon-button";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "@/components/settings/components/SettingsRow";
import {
  fetchCliApps,
  fetchMcpPresets,
  runCliAppAction,
  runMcpPresetAction,
} from "@/lib/api";
import { notifyCliAppsChanged } from "@/lib/cli-app-events";
import { notifyMcpPresetsChanged } from "@/lib/mcp-preset-events";
import type {
  CliAppInfo,
  CliAppsPayload,
  McpPresetInfo,
  McpPresetsPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

interface AppsSettingsProps {
  /** 可选：从父组件注入的 cli-apps payload。SettingsView 通过 fetch
   * `/api/settings/cli-apps` 已经拉取过一次时传入，避免重复请求。 */
  initialCliApps?: CliAppsPayload | null;
  initialMcpPresets?: McpPresetsPayload | null;
  /** 隐藏顶部的 "Apps" section 标题。AppsView 已通过 ViewShell 提供 h1，
   * 渲染重复标题会导致 `findByRole("heading", { name: "Apps" })` 匹配多项。 */
  hideTitle?: boolean;
}

type AppKind = "cli" | "mcp";

interface UnifiedAppRow {
  kind: AppKind;
  name: string;
  displayName: string;
  description: string;
  category: string;
  /** CLI: installed；MCP: configured。决定是否出现在"已启用"区域。 */
  enabled: boolean;
  /** CLI: install_supported；MCP: install_supported。决定是否可执行动作。 */
  installSupported: boolean;
  /** CLI: available；MCP: available。决定是否可安装。 */
  available: boolean;
  status: string;
  brandColor: string | null;
  logoUrl: string | null;
  /** 透传原始信息，便于动作回调使用。 */
  raw: CliAppInfo | McpPresetInfo;
}

function toRow(kind: AppKind, info: CliAppInfo | McpPresetInfo): UnifiedAppRow {
  if (kind === "cli") {
    const app = info as CliAppInfo;
    return {
      kind,
      name: app.name,
      displayName: app.display_name,
      description: app.description,
      category: app.category,
      enabled: app.installed,
      installSupported: app.install_supported,
      available: app.available,
      status: app.status,
      brandColor: app.brand_color ?? null,
      logoUrl: app.logo_url ?? null,
      raw: app,
    };
  }
  const preset = info as McpPresetInfo;
  return {
    kind,
    name: preset.name,
    displayName: preset.display_name,
    description: preset.description,
    category: preset.category,
    enabled: preset.installed && preset.configured,
    installSupported: preset.install_supported,
    available: preset.available,
    status: preset.status,
    brandColor: preset.brand_color ?? null,
    logoUrl: preset.logo_url ?? null,
    raw: preset,
  };
}

/** 简单首字母色块头像，与 ChannelCard 视觉一致。 */
function AppAvatar({ name, displayName }: { name: string; displayName: string }) {
  const ch = (displayName || name).trim().charAt(0).toUpperCase() || "?";
  return (
    <div
      className={cn(
        "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
        "bg-foreground/5 text-sm font-semibold text-foreground/70",
        "ring-1 ring-inset ring-foreground/10",
      )}
      aria-hidden
    >
      {ch}
    </div>
  );
}

/** 已启用应用卡片：头像 + 名称 + 类型标签 + 状态点 + 描述 + 动作按钮。 */
function InstalledAppCard({
  row,
  acting,
  onUninstall,
  onUpdate,
  onTest,
}: {
  row: UnifiedAppRow;
  acting: boolean;
  onUninstall: () => void;
  onUpdate: () => void;
  onTest: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "group relative flex flex-col gap-2.5 rounded-xl border border-foreground/15 bg-card p-3.5 text-left shadow-sm transition-all",
        "hover:bg-accent/30 hover:shadow-md",
      )}
    >
      <div className="flex items-start gap-2.5">
        <AppAvatar name={row.name} displayName={row.displayName} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-sm font-semibold text-foreground">
              {row.displayName}
            </span>
            <span
              className={cn(
                "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                row.kind === "cli"
                  ? "bg-blue-500/15 text-blue-600 dark:text-blue-300"
                  : "bg-violet-500/15 text-violet-600 dark:text-violet-300",
              )}
            >
              {row.kind === "cli"
                ? t("settings.apps.cliLabel")
                : t("settings.apps.mcpLabel")}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            <span>{t("settings.values.configured")}</span>
          </div>
        </div>
        {acting ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        ) : null}
      </div>
      {row.description ? (
        <p className="line-clamp-2 text-xs text-muted-foreground/80">
          {row.description}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          disabled={acting}
          onClick={onUninstall}
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
            "bg-foreground text-background hover:scale-105 transition-transform",
            "disabled:opacity-50 disabled:cursor-not-allowed",
          )}
        >
          {row.kind === "cli"
            ? t("settings.cliApps.uninstall")
            : t("settings.cliApps.uninstall")}
        </button>
        {row.kind === "cli" ? (
          <button
            type="button"
            disabled={acting}
            onClick={onUpdate}
            className={cn(
              "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
              "border border-border/60 bg-card text-foreground hover:bg-accent/30",
              "disabled:opacity-50 disabled:cursor-not-allowed",
            )}
          >
            {t("settings.cliApps.update")}
          </button>
        ) : null}
        <button
          type="button"
          disabled={acting}
          onClick={onTest}
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
            "border border-border/60 bg-card text-foreground hover:bg-accent/30",
            "disabled:opacity-50 disabled:cursor-not-allowed",
          )}
        >
          {t("settings.cliApps.test")}
        </button>
      </div>
    </div>
  );
}

/** 可用应用列表项：精简展示 + 启用按钮。 */
function AvailableAppItem({
  row,
  acting,
  onInstall,
}: {
  row: UnifiedAppRow;
  acting: boolean;
  onInstall: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "group flex items-center gap-2.5 rounded-lg border border-dashed border-foreground/15 bg-card/50 p-2.5",
        "hover:bg-accent/20 hover:border-foreground/25 transition-all",
      )}
    >
      <AppAvatar name={row.name} displayName={row.displayName} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-xs font-semibold text-foreground">
            {row.displayName}
          </span>
          <span
            className={cn(
              "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
              row.kind === "cli"
                ? "bg-blue-500/15 text-blue-600 dark:text-blue-300"
                : "bg-violet-500/15 text-violet-600 dark:text-violet-300",
            )}
          >
            {row.kind === "cli"
              ? t("settings.apps.cliLabel")
              : t("settings.apps.mcpLabel")}
          </span>
        </div>
        {row.description ? (
          <span className="truncate text-[11px] text-muted-foreground/70">
            {row.description}
          </span>
        ) : null}
      </div>
      {row.installSupported ? (
        <button
          type="button"
          disabled={acting}
          onClick={onInstall}
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
            "bg-foreground text-background hover:scale-105 transition-transform",
            "disabled:opacity-50 disabled:cursor-not-allowed",
          )}
        >
          {acting ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          {row.kind === "cli"
            ? t("settings.cliApps.install")
            : t("settings.cliApps.install")}
        </button>
      ) : (
        <span className="shrink-0 text-[10px] text-muted-foreground/70">
          {t("settings.cliApps.unsupported")}
        </span>
      )}
    </div>
  );
}

export function AppsSettings({
  initialCliApps,
  initialMcpPresets,
  hideTitle = false,
}: AppsSettingsProps = {}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [cliApps, setCliApps] = useState<CliAppInfo[]>(
    initialCliApps?.apps ?? [],
  );
  const [mcpPresets, setMcpPresets] = useState<McpPresetInfo[]>(
    initialMcpPresets?.presets ?? [],
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actingName, setActingName] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cliData, mcpData] = await Promise.all([
        fetchCliApps(token),
        fetchMcpPresets(token),
      ]);
      setCliApps(cliData.apps);
      setMcpPresets(mcpData.presets);
      if (cliData.last_action?.message) {
        setToast(cliData.last_action.message);
      } else if (mcpData.last_action?.message) {
        setToast(mcpData.last_action.message);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5_000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const unifiedRows = useMemo<UnifiedAppRow[]>(() => {
    const cliRows = cliApps.map((app) => toRow("cli", app));
    const mcpRows = mcpPresets.map((preset) => toRow("mcp", preset));
    return [...cliRows, ...mcpRows];
  }, [cliApps, mcpPresets]);

  const enabledApps = useMemo(
    () => unifiedRows.filter((row) => row.enabled),
    [unifiedRows],
  );
  const availableApps = useMemo(
    () => unifiedRows.filter((row) => !row.enabled),
    [unifiedRows],
  );

  /** 应用 CLI 动作（install/update/uninstall/test）并合并返回的 payload。 */
  const runCliAction = useCallback(
    async (
      action: "install" | "update" | "uninstall" | "test",
      name: string,
    ) => {
      if (actingName) return;
      setActingName(name);
      try {
        const payload = await runCliAppAction(token, action, name);
        setCliApps(payload.apps);
        if (payload.last_action?.message) {
          setToast(payload.last_action.message);
        }
        notifyCliAppsChanged(payload);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setActingName(null);
      }
    },
    [actingName, token],
  );

  /** 应用 MCP 动作（enable/remove/test）。 */
  const runMcpAction = useCallback(
    async (
      action: "enable" | "remove" | "test",
      name: string,
    ) => {
      if (actingName) return;
      setActingName(name);
      try {
        const payload = await runMcpPresetAction(token, action, name);
        setMcpPresets(payload.presets);
        if (payload.last_action?.message) {
          setToast(payload.last_action.message);
        }
        notifyMcpPresetsChanged(payload);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setActingName(null);
      }
    },
    [actingName, token],
  );

  const handleAction = useCallback(
    (row: UnifiedAppRow, action: "install" | "update" | "uninstall" | "test" | "enable" | "remove") => {
      if (row.kind === "cli") {
        const cliAction = action === "enable" ? "install" : action === "remove" ? "uninstall" : action;
        void runCliAction(cliAction as "install" | "update" | "uninstall" | "test", row.name);
      } else {
        const mcpAction = action === "install" ? "enable" : action === "uninstall" ? "remove" : action === "update" ? "test" : action;
        void runMcpAction(mcpAction as "enable" | "remove" | "test", row.name);
      }
    },
    [runCliAction, runMcpAction],
  );

  const cliInstalledCount = cliApps.filter((app) => app.installed).length;
  const mcpConfiguredCount = mcpPresets.filter(
    (preset) => preset.installed && preset.configured,
  ).length;

  return (
    <div className="space-y-7">
      <section>
        {!hideTitle ? (
          <SettingsSectionTitle>
            {t("settings.sections.apps")}
          </SettingsSectionTitle>
        ) : null}
        <SettingsGroup>
          <SettingsRow
            title={t("settings.apps.description")}
            description={t("settings.apps.caption", {
              cli: cliInstalledCount,
              mcp: mcpConfiguredCount,
            })}
          >
            <RefreshIconButton
              onClick={load}
              loading={loading}
              title={t("settings.cliApps.allCategories")}
            />
          </SettingsRow>
          <details className="px-4 py-3.5 text-[11px] leading-relaxed text-muted-foreground/80 sm:px-5">
            <summary className="cursor-pointer select-none font-medium text-foreground/70 hover:text-foreground">
              CLI 应用实现机制
            </summary>
            <div className="mt-2 space-y-1.5">
              <p>
                <span className="font-medium text-foreground/70">【数据来源】</span>
                所有 CLI 应用条目均来自 3 个远程 JSON registry(harness/public 必选,extensions 可选),无本地内置 catalog。本地缓存 TTL 由 catalogTtlSeconds 控制(默认 3600s)。
              </p>
              <p>
                <span className="font-medium text-foreground/70">【安装策略】</span>
                bundled(随父应用捆绑,只检测不安装)、npm、brew、uv、pip 五种;其他视为 unsupported。
              </p>
              <p>
                <span className="font-medium text-foreground/70">【状态判定】</span>
                installed(已登记+可执行)、missing(已登记+找不到)、available(未登记+系统已有)、not_installed(未登记+找不到)、unsupported。
              </p>
              <p>
                <span className="font-medium text-foreground/70">【不支持自定义 CLI】</span>
                UI/API/配置/核心逻辑四层均封闭:get_app() 只查远程 catalog,无 create/edit/delete 接口,config.tools.cliApps 仅含 enable 与超时字段。
              </p>
              <p>
                <span className="font-medium text-foreground/70">【替代方案】</span>
                运行任意 CLI 用 exec 工具;集成自定义服务用 tools.mcpServers 声明。
              </p>
            </div>
          </details>
        </SettingsGroup>
      </section>

      {error ? (
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <AlertCircle className="h-6 w-6 opacity-50" />
          <p>{error}</p>
          <Button variant="outline" size="sm" onClick={load}>
            {t("settings.cliApps.allCategories")}
          </Button>
        </div>
      ) : loading && unifiedRows.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
          <LoadingSpinner />
          {t("settings.apps.loading")}
        </div>
      ) : unifiedRows.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <Package className="h-8 w-8 opacity-40" />
          <p>{t("settings.apps.empty")}</p>
        </div>
      ) : (
        <div className="space-y-6">
          {/* 已启用应用 */}
          <section className="flex flex-col gap-2.5">
            <div className="flex items-center gap-2 px-1">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              <h2 className="text-xs font-semibold text-foreground/80">
                {t("settings.values.configured")}
              </h2>
              <span className="text-[11px] text-muted-foreground/60">
                ({enabledApps.length})
              </span>
            </div>
            {enabledApps.length === 0 ? (
              <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-foreground/15 bg-card/30 py-8 text-sm text-muted-foreground">
                <Sparkles className="h-5 w-5 opacity-50" />
                <p>{t("settings.apps.empty")}</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                {enabledApps.map((row) => (
                  <InstalledAppCard
                    key={`${row.kind}-${row.name}`}
                    row={row}
                    acting={actingName === row.name}
                    onUninstall={() => handleAction(row, row.kind === "cli" ? "uninstall" : "remove")}
                    onUpdate={() => handleAction(row, "update")}
                    onTest={() => handleAction(row, "test")}
                  />
                ))}
              </div>
            )}
          </section>

          {/* 可用应用 */}
          {availableApps.length > 0 ? (
            <section className="flex flex-col gap-2.5">
              <div className="flex items-center gap-2 px-1">
                <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
                <h2 className="text-xs font-semibold text-foreground/80">
                  {t("settings.apps.searchPlaceholder")}
                </h2>
                <span className="text-[11px] text-muted-foreground/60">
                  ({availableApps.length})
                </span>
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {availableApps.map((row) => (
                  <AvailableAppItem
                    key={`${row.kind}-${row.name}`}
                    row={row}
                    acting={actingName === row.name}
                    onInstall={() => handleAction(row, row.kind === "cli" ? "install" : "enable")}
                  />
                ))}
              </div>
            </section>
          ) : null}
        </div>
      )}

      {toast ? (
        <div
          role="status"
          className="pointer-events-none fixed bottom-4 left-1/2 z-50 -translate-x-1/2 flex items-center gap-3 rounded-full border border-border/70 bg-popover px-4 py-2 text-xs font-medium text-popover-foreground shadow-lg"
        >
          <span>{toast}</span>
          <button
            type="button"
            onClick={() => setToast(null)}
            className={cn(
              "pointer-events-auto inline-flex h-6 items-center rounded-full px-2 text-[11px] font-medium",
              "text-muted-foreground hover:text-foreground",
            )}
          >
            {t("common.dismiss")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
