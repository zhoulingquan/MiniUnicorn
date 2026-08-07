// Settings 侧边栏:导航 + 退出按钮。
// 从 SettingsView.tsx 拆分而来。
// 支持桌面端折叠(由 PanelLeft 按钮控制):折叠时仅显示图标列。

import { LogOut } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  SETTINGS_NAV_ITEMS,
  type SettingsSectionKey,
} from "../types";

export function SettingsSidebar({
  activeSection,
  onSelectSection,
  onLogout,
  hostChromeInset,
  collapsed = false,
  onToggleSidebar,
}: {
  activeSection: SettingsSectionKey;
  onSelectSection: (section: SettingsSectionKey) => void;
  onLogout?: () => void;
  hostChromeInset?: boolean;
  /** 桌面端折叠态:仅显示图标列。 */
  collapsed?: boolean;
  /** 点击折叠态遮罩或展开按钮时调用(展开侧边栏)。 */
  onToggleSidebar?: () => void;
}) {
  const { t } = useTranslation();
  const toggleable = Boolean(collapsed && onToggleSidebar);
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- aside is conditionally interactive; role/tabIndex/handlers are set only when toggleable
    <aside
      role={toggleable ? "button" : undefined}
      tabIndex={toggleable ? 0 : undefined}
      className={cn(
        "flex w-full shrink-0 flex-col border-b border-border/55 bg-card/62 px-4 pb-3 shadow-[inset_0_-1px_0_rgba(255,255,255,0.55)] backdrop-blur-xl dark:bg-card/45 dark:shadow-none md:border-b-0 md:border-r md:px-3 md:pb-4 md:shadow-[inset_-1px_0_0_rgba(255,255,255,0.55)]",
        // 折叠态:窄宽度,居中对齐图标
        collapsed
          ? "md:w-[3.5rem] md:items-center md:px-1.5"
          : "md:w-[17rem]",
        // 折叠态:点击空白处展开
        toggleable ? "md:cursor-pointer" : "",
        hostChromeInset ? "pt-[4.25rem] md:pt-[4.25rem]" : "pt-4 md:pt-4",
      )}
      onClick={toggleable ? onToggleSidebar : undefined}
      onKeyDown={
        toggleable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onToggleSidebar?.();
              }
            }
          : undefined
      }
    >
      <div className={cn("mb-3 px-1 pt-1 md:mb-4 md:px-2", collapsed && "md:hidden")}>
        <h2 className="text-[21px] font-semibold tracking-[-0.02em] text-foreground">
          {t("settings.sidebar.title")}
        </h2>
      </div>

      <nav
        aria-label={t("settings.sidebar.ariaLabel")}
        className={cn(
          "-mx-1 flex gap-2 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:mx-0 md:block md:space-y-1 md:overflow-visible md:px-0 md:pb-0",
          collapsed && "md:space-y-1",
        )}
      >
        {SETTINGS_NAV_ITEMS.map(({ key, icon: Icon, fallback }) => {
          const active = key === activeSection;
          return (
            <button
              key={key}
              type="button"
              aria-current={active ? "page" : undefined}
              title={collapsed ? t(`settings.nav.${key}`, { defaultValue: fallback }) : undefined}
              onClick={(e) => {
                e.stopPropagation();
                onSelectSection(key);
              }}
              className={cn(
                "flex h-9 w-auto shrink-0 items-center gap-2 rounded-full px-3 text-left text-[13px] font-medium transition-colors md:w-full md:rounded-[10px] md:px-2.5",
                collapsed && "md:mx-auto md:w-9 md:justify-center md:px-0",
                active
                  ? "bg-muted/90 text-foreground shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)]"
                  : "text-muted-foreground/78 hover:bg-muted/45 hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" strokeWidth={2} aria-hidden />
              {!collapsed ? (
                <span className="truncate">{t(`settings.nav.${key}`, { defaultValue: fallback })}</span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div className="hidden md:mt-auto md:block md:pt-4">
        {onLogout && !hostChromeInset ? (
          <Button
            type="button"
            variant="ghost"
            onClick={(e) => {
              e.stopPropagation();
              onLogout();
            }}
            title={collapsed ? t("app.account.logout") : undefined}
            className={cn(
              "h-9 w-full justify-start gap-2 rounded-[10px] px-2.5 text-[13px] font-medium text-muted-foreground hover:bg-destructive/8 hover:text-destructive",
              collapsed && "md:mx-auto md:w-9 md:justify-center md:px-0",
            )}
          >
            <LogOut className="h-4 w-4" aria-hidden />
            {!collapsed ? <span>{t("app.account.logout")}</span> : null}
          </Button>
        ) : null}
      </div>
    </aside>
  );
}
