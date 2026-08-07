import { useState } from "react";
import { Loader2, Settings2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ToggleSwitch } from "@/components/ui/toggle-switch";
import type { ChannelPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ChannelCardProps {
  channel: ChannelPayload;
  /** 已启用 = configured（在 config 中有显式配置）。 */
  enabled: boolean;
  /** 显示名称（已 i18n 解析）。 */
  displayName: string;
  /** 描述（已 i18n 解析）。 */
  description: string;
  acting: boolean;
  onToggle: () => void;
  onClick: () => void;
}

/** 频道卡片首字母头像（参考 QwenPaw ChannelIcon 字母回退）。 */
function ChannelAvatar({ name, displayName }: { name: string; displayName: string }) {
  // 取显示名首个字符（中英文皆可），降级取 name 首字母
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

/**
 * 已启用频道卡片：图标 + 名称 + 标签（内置/插件）+ 描述 + 状态点 + 配置按钮 + ToggleSwitch。
 * 点击卡片本体或配置按钮打开抽屉；右侧 ToggleSwitch 控制启用/停用。
 * 参考 QwenPaw ChannelCard 的两段式布局。
 */
export function ChannelCard({
  channel,
  enabled,
  displayName,
  description,
  acting,
  onToggle,
  onClick,
}: ChannelCardProps) {
  const { t } = useTranslation();
  const [hovered, setHovered] = useState(false);

  return (
    <div
      role="button"
      tabIndex={0}
      className={cn(
        "group relative flex flex-col gap-2.5 rounded-xl border bg-card p-3.5 text-left shadow-sm transition-all",
        "hover:bg-accent/30 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-ring",
        enabled && "border-foreground/15",
        !enabled && "border-dashed border-foreground/15",
      )}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* 顶部：头像 + 状态点 + 标签 */}
      <div className="flex items-start gap-2.5">
        <ChannelAvatar name={channel.name} displayName={displayName} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-sm font-semibold text-foreground">
              {displayName}
            </span>
            <span
              className={cn(
                "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                channel.is_builtin
                  ? "bg-violet-500/15 text-violet-600 dark:text-violet-300"
                  : "bg-blue-500/15 text-blue-600 dark:text-blue-300",
              )}
            >
              {channel.is_builtin ? t("channels.builtin") : t("channels.custom")}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                enabled
                  ? channel.has_credentials
                    ? "bg-emerald-500"
                    : "bg-amber-500"
                  : "bg-muted-foreground/40",
              )}
            />
            <span>
              {enabled
                ? channel.has_credentials
                  ? t("channels.badge.configured")
                  : t("channels.badge.needsConfig")
                : t("channels.badge.unconfigured")}
            </span>
          </div>
        </div>
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
        >
          <ToggleSwitch
            checked={enabled}
            disabled={acting}
            onClick={onToggle}
            ariaLabel={t("channels.toggle.aria", { name: displayName })}
          />
        </span>
      </div>

      {/* 中部：描述 */}
      {description ? (
        <p className="line-clamp-2 text-xs text-muted-foreground/80">{description}</p>
      ) : null}

      {/* 底部：配置按钮（hover 时显形） */}
      <div className="flex items-center justify-between">
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors",
            "text-muted-foreground hover:bg-foreground/5 hover:text-foreground",
            hovered ? "opacity-100" : "opacity-60",
          )}
          onClick={(e) => {
            e.stopPropagation();
            onClick();
          }}
        >
          <Settings2 className="h-3 w-3" />
          {t("channels.configure")}
        </button>
        {acting ? <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" /> : null}
      </div>
    </div>
  );
}

/**
 * 可用频道列表项：更精简的展示，仅头像 + 名称 + 标签 + 启用按钮。
 * 用于"可用频道"区域（未配置的频道）。
 */
export function ChannelAvailableItem({
  channel,
  displayName,
  description,
  acting,
  onEnable,
  onClick,
}: {
  channel: ChannelPayload;
  displayName: string;
  description: string;
  acting: boolean;
  onEnable: () => void;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      role="button"
      tabIndex={0}
      className={cn(
        "group flex items-center gap-2.5 rounded-lg border border-dashed border-foreground/15 bg-card/50 p-2.5",
        "hover:bg-accent/20 hover:border-foreground/25 focus:outline-none focus:ring-2 focus:ring-ring",
        "transition-all cursor-pointer",
      )}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
    >
      <ChannelAvatar name={channel.name} displayName={displayName} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-xs font-semibold text-foreground">{displayName}</span>
          <span
            className={cn(
              "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
              channel.is_builtin
                ? "bg-violet-500/15 text-violet-600 dark:text-violet-300"
                : "bg-blue-500/15 text-blue-600 dark:text-blue-300",
            )}
          >
            {channel.is_builtin ? t("channels.builtin") : t("channels.custom")}
          </span>
        </div>
        {description ? (
          <span className="truncate text-[11px] text-muted-foreground/70">{description}</span>
        ) : null}
      </div>
      <button
        type="button"
        disabled={acting}
        onClick={(e) => {
          e.stopPropagation();
          onEnable();
        }}
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
          "bg-foreground text-background hover:scale-105 transition-transform",
          "disabled:opacity-50 disabled:cursor-not-allowed",
        )}
      >
        {acting ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        {t("channels.enable")}
      </button>
    </div>
  );
}
