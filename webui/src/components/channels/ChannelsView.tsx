import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, MessageSquare } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { ChannelAvailableItem, ChannelCard } from "@/components/channels/ChannelCard";
import { ChannelDrawer } from "@/components/channels/ChannelDrawer";
import { LoadingSpinner } from "@/components/ui/loading-spinner";
import { RefreshIconButton } from "@/components/ui/refresh-icon-button";
import { ViewShell } from "@/components/ui/view-shell";
import {
  deleteChannelConfig,
  fetchChannels,
  updateChannelConfig,
} from "@/lib/api";
import type { ChannelPayload } from "@/lib/types";

interface ChannelsViewProps {
  onBack: () => void;
  token: string;
}

/** 优先用 i18n 的 channelNames 映射，缺失时回退到后端 display_name。
 * 参考 QwenPaw Console 的 getChannelLabel 模式。 */
function useChannelDisplayName() {
  const { t } = useTranslation();
  return (ch: ChannelPayload): string => {
    const translated = t(`channels.channelNames.${ch.name}`, { defaultValue: "" });
    return translated || ch.display_name;
  };
}

/** 频道卡片副标题（描述）的 i18n helper。
 * 查找顺序：channels.channelDescriptions.{name} → 后端 ch.description */
function useChannelDescription() {
  const { t } = useTranslation();
  return (ch: ChannelPayload): string => {
    const translated = t(`channels.channelDescriptions.${ch.name}`, { defaultValue: "" });
    return translated || ch.description;
  };
}

export function ChannelsView({ onBack, token }: ChannelsViewProps) {
  const { t } = useTranslation();
  const getDisplayName = useChannelDisplayName();
  const getDescription = useChannelDescription();
  const [channels, setChannels] = useState<ChannelPayload[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 抽屉状态
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [activeName, setActiveName] = useState<string | null>(null);
  const [actingName, setActingName] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchChannels(token);
      setChannels(data.channels);
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
    const timer = window.setTimeout(() => setToast(null), 2_500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // QwenPaw 两段式：已启用 vs 可用
  const enabledChannels = useMemo(
    () => channels.filter((c) => c.configured),
    [channels],
  );
  const availableChannels = useMemo(
    () => channels.filter((c) => !c.configured),
    [channels],
  );

  const activeChannel = useMemo(
    () => channels.find((c) => c.name === activeName) ?? null,
    [channels, activeName],
  );

  const handleToggleEnabled = async (ch: ChannelPayload) => {
    if (actingName) return;
    setActingName(ch.name);
    try {
      // 启用未配置 channel 时，前端先以 default_config 作为 payload 提交，
      // 与后端兜底（default_config()）保持一致，避免 "no existing config" 错误。
      const nextEnabled = !ch.configured;
      const configPayload =
        nextEnabled && !ch.config ? ch.default_config : ch.config;
      await updateChannelConfig(token, ch.name, configPayload, nextEnabled);
      setChannels((prev) =>
        prev.map((c) =>
          c.name === ch.name
            ? {
                ...c,
                configured: nextEnabled,
                has_credentials: nextEnabled ? c.has_credentials : false,
                config:
                  nextEnabled && !c.config && c.default_config
                    ? c.default_config
                    : c.config,
                enabled: nextEnabled,
              }
            : c,
        ),
      );
      setToast(t("channels.toast.toggled", { name: getDisplayName(ch) }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActingName(null);
    }
  };

  const handleEnableFromAvailable = async (ch: ChannelPayload) => {
    if (actingName) return;
    setActingName(ch.name);
    try {
      // 启用未配置 channel：用 default_config 作为初始 payload
      const payload = ch.default_config ?? {};
      await updateChannelConfig(token, ch.name, payload, true);
      setChannels((prev) =>
        prev.map((c) =>
          c.name === ch.name
            ? {
                ...c,
                configured: true,
                enabled: true,
                has_credentials: false,
                config: c.default_config ? { ...c.default_config } : c.config,
              }
            : c,
        ),
      );
      setToast(t("channels.toast.toggled", { name: getDisplayName(ch) }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActingName(null);
    }
  };

  const openDrawer = (ch: ChannelPayload) => {
    setActiveName(ch.name);
    setDrawerOpen(true);
  };

  const handleDrawerSave = async (payload: Record<string, unknown>) => {
    if (!activeChannel || actingName) return;
    setActingName(activeChannel.name);
    try {
      await updateChannelConfig(token, activeChannel.name, payload, true);
      setChannels((prev) =>
        prev.map((c) =>
          c.name === activeChannel.name
            ? {
                ...c,
                config: payload as Record<string, unknown>,
                configured: true,
                enabled: true,
                has_credentials: true,
              }
            : c,
        ),
      );
      setToast(t("channels.toast.saved", { name: getDisplayName(activeChannel) }));
      setDrawerOpen(false);
    } catch (e) {
      setToast((e as Error).message);
    } finally {
      setActingName(null);
    }
  };

  const handleDrawerDelete = async () => {
    if (!activeChannel || actingName) return;
    setActingName(activeChannel.name);
    try {
      await deleteChannelConfig(token, activeChannel.name);
      setChannels((prev) =>
        prev.map((c) =>
          c.name === activeChannel.name
            ? { ...c, config: null, configured: false, enabled: false, has_credentials: false }
            : c,
        ),
      );
      setToast(t("channels.toast.deleted", { name: getDisplayName(activeChannel) }));
      setDrawerOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActingName(null);
    }
  };

  /** 扫码登录成功后：用后端写入的最新 config 更新本地 state，并同步表单。 */
  const handleQrSuccess = (config: Record<string, unknown>) => {
    if (!activeChannel) return;
    setChannels((prev) =>
      prev.map((c) =>
        c.name === activeChannel.name
          ? {
              ...c,
              config,
              configured: true,
              enabled: true,
              has_credentials: true,
            }
          : c,
      ),
    );
    setToast(t("channels.qr.successToast", { name: getDisplayName(activeChannel) }));
  };

  const scrollToAvailable = () => {
    const el = document.getElementById("available-channels");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <ViewShell
      onBack={onBack}
      icon={<MessageSquare className="h-4 w-4 text-foreground/80" />}
      title={t("channels.title")}
      actions={
        <RefreshIconButton
          onClick={load}
          loading={loading}
          title={t("channels.refresh")}
        />
      }
      bodyClassName="py-4"
    >
      {error ? (
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <AlertCircle className="h-6 w-6 opacity-50" />
          <p>{error}</p>
          <Button variant="outline" size="sm" onClick={load}>
            {t("channels.retry")}
          </Button>
        </div>
      ) : loading && channels.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
          <LoadingSpinner />
          {t("channels.loading")}
        </div>
      ) : channels.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <MessageSquare className="h-8 w-8 opacity-40" />
          <p>{t("channels.empty")}</p>
        </div>
      ) : (
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          {/* 上半部分：已启用频道 */}
          <section className="flex flex-col gap-2.5">
            <div className="flex items-center gap-2 px-1">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              <h2 className="text-xs font-semibold text-foreground/80">
                {t("channels.enabledSection")}
              </h2>
              <span className="text-[11px] text-muted-foreground/60">
                ({enabledChannels.length})
              </span>
            </div>
            {enabledChannels.length === 0 ? (
              <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-foreground/15 bg-card/30 py-8 text-sm text-muted-foreground">
                <p>{t("channels.noEnabled")}</p>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 gap-1.5"
                  onClick={scrollToAvailable}
                >
                  {t("channels.goEnable")}
                </Button>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                {enabledChannels.map((ch) => (
                  <ChannelCard
                    key={ch.name}
                    channel={ch}
                    enabled={ch.configured}
                    displayName={getDisplayName(ch)}
                    description={getDescription(ch)}
                    acting={actingName === ch.name}
                    onToggle={() => handleToggleEnabled(ch)}
                    onClick={() => openDrawer(ch)}
                  />
                ))}
              </div>
            )}
          </section>

          {/* 下半部分：可用频道 */}
          <section
            id="available-channels"
            className="flex flex-col gap-2.5 scroll-mt-4"
          >
            <div className="flex items-center gap-2 px-1">
              <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
              <h2 className="text-xs font-semibold text-foreground/80">
                {t("channels.availableSection")}
              </h2>
              <span className="text-[11px] text-muted-foreground/60">
                ({availableChannels.length})
              </span>
            </div>
            {availableChannels.length === 0 ? (
              <div className="flex items-center justify-center rounded-xl border border-dashed border-foreground/15 bg-card/30 py-6 text-xs text-muted-foreground">
                {t("channels.noAvailable")}
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {availableChannels.map((ch) => (
                  <ChannelAvailableItem
                    key={ch.name}
                    channel={ch}
                    displayName={getDisplayName(ch)}
                    description={getDescription(ch)}
                    acting={actingName === ch.name}
                    onEnable={() => handleEnableFromAvailable(ch)}
                    onClick={() => openDrawer(ch)}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
      )}

      {/* 抽屉：编辑频道配置 */}
      <ChannelDrawer
        channel={activeChannel}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        displayName={activeChannel ? getDisplayName(activeChannel) : ""}
        description={activeChannel ? getDescription(activeChannel) : ""}
        acting={activeChannel ? actingName === activeChannel.name : false}
        token={token}
        onSave={handleDrawerSave}
        onDelete={handleDrawerDelete}
        onQrSuccess={handleQrSuccess}
      />

      {toast ? (
        <div
          role="status"
          className="pointer-events-none fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-full border border-border/70 bg-popover px-4 py-2 text-xs font-medium text-popover-foreground shadow-lg"
        >
          {toast}
        </div>
      ) : null}
    </ViewShell>
  );
}
