// Apps section:MCP Presets 管理。
// 参考 ChannelsView 的两段式列表模式:已启用(configured)在上半部分,
// 可用项在下半部分。无 drawer,仅有 enable/remove/test 三种原子动作;
// 执行后通过 toast 展示 last_action.message。

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
import { fetchMcpPresets, runMcpPresetAction } from "@/lib/api";
import { notifyMcpPresetsChanged } from "@/lib/mcp-preset-events";
import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

interface AppsSettingsProps {
  /** 可选：从父组件注入的 mcp-presets payload。 */
  initialMcpPresets?: McpPresetsPayload | null;
  /** 隐藏顶部的 "Apps" section 标题。AppsView 已通过 ViewShell 提供 h1，
   渲染重复标题会导致 `findByRole("heading", { name: "Apps" })` 匹配多项。 */
  hideTitle?: boolean;
}

interface PresetRow {
  name: string;
  displayName: string;
  description: string;
  category: string;
  enabled: boolean;
  installSupported: boolean;
  status: string;
  raw: McpPresetInfo;
}

function toRow(preset: McpPresetInfo): PresetRow {
  return {
    name: preset.name,
    displayName: preset.display_name,
    description: preset.description,
    category: preset.category,
    enabled: preset.installed && preset.configured,
    installSupported: preset.install_supported,
    status: preset.status,
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

/** 已启用应用卡片：头像 + 名称 + 状态点 + 描述 + 动作按钮。 */
function InstalledAppCard({
  row,
  acting,
  onRemove,
  onTest,
}: {
  row: PresetRow;
  acting: boolean;
  onRemove: () => void;
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
                "bg-violet-500/15 text-violet-600 dark:text-violet-300",
              )}
            >
              {t("settings.apps.mcpLabel")}
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
          onClick={onRemove}
          className={cn(
            "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
            "bg-foreground text-background hover:scale-105 transition-transform",
            "disabled:opacity-50 disabled:cursor-not-allowed",
          )}
        >
          {t("settings.apps.uninstall")}
        </button>
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
          {t("settings.apps.test")}
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
  row: PresetRow;
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
              "bg-violet-500/15 text-violet-600 dark:text-violet-300",
            )}
          >
            {t("settings.apps.mcpLabel")}
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
          {t("settings.apps.enable")}
        </button>
      ) : (
        <span className="shrink-0 text-[10px] text-muted-foreground/70">
          {t("settings.apps.unsupported")}
        </span>
      )}
    </div>
  );
}

export function AppsSettings({
  initialMcpPresets,
  hideTitle = false,
}: AppsSettingsProps = {}) {
  const { t } = useTranslation();
  const { token } = useClient();
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
      const mcpData = await fetchMcpPresets(token);
      setMcpPresets(mcpData.presets);
      if (mcpData.last_action?.message) {
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

  const unifiedRows = useMemo<PresetRow[]>(
    () => mcpPresets.map(toRow),
    [mcpPresets],
  );

  const enabledApps = useMemo(
    () => unifiedRows.filter((row) => row.enabled),
    [unifiedRows],
  );
  const availableApps = useMemo(
    () => unifiedRows.filter((row) => !row.enabled),
    [unifiedRows],
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
              mcp: mcpConfiguredCount,
            })}
          >
            <RefreshIconButton
              onClick={load}
              loading={loading}
              title={t("settings.apps.refresh")}
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      {error ? (
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <AlertCircle className="h-6 w-6 opacity-50" />
          <p>{error}</p>
          <Button variant="outline" size="sm" onClick={load}>
            {t("settings.apps.refresh")}
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
                    key={row.name}
                    row={row}
                    acting={actingName === row.name}
                    onRemove={() => void runMcpAction("remove", row.name)}
                    onTest={() => void runMcpAction("test", row.name)}
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
                    key={row.name}
                    row={row}
                    acting={actingName === row.name}
                    onInstall={() => void runMcpAction("enable", row.name)}
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
