import { useState, type ReactNode } from "react";
import {
  Archive,
  Menu,
  Search,
  Settings,
  SquarePen,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import type {
  ChatSummary,
  SidebarViewState,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import type { ViewRegistration } from "@/views/registry";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  onTogglePin: (key: string) => void;
  onRequestRename: (key: string, label: string) => void;
  onToggleArchive: (key: string) => void;
  onToggleGroup: (groupId: string) => void;
  onRequestRenameProject: (projectKey: string, label: string) => void;
  onNewChatInProject: (projectPath: string, projectName: string) => void;
  // 声明式视图导航：由 App.tsx 从 VIEW_REGISTRY 传入
  navItems: ViewRegistration[];
  onNavigate: (key: string) => void;
  // settings 单独处理（位于底部，且 onOpenSettings 可携带 section 参数）
  onOpenSettings: () => void;
  onOpenSearch: () => void;
  onToggleArchived: () => void;
  onCollapse: () => void;
  onExpand?: () => void;
  containActionMenus?: boolean;
  collapsed?: boolean;
  pinnedKeys?: string[];
  archivedKeys?: string[];
  titleOverrides?: Record<string, string>;
  projectNameOverrides?: Record<string, string>;
  collapsedGroups?: Record<string, boolean>;
  runningChatIds?: string[];
  completedChatIds?: string[];
  viewState?: SidebarViewState;
  showArchived?: boolean;
  archivedCount?: number;
  defaultWorkspacePath?: string | null;
  hostChromeInset?: boolean;
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  const [menuPortalContainer, setMenuPortalContainer] =
    useState<HTMLElement | null>(null);
  const collapsed = Boolean(props.collapsed);

  return (
    <nav
      ref={props.containActionMenus ? setMenuPortalContainer : undefined}
      aria-label={t("sidebar.navigation")}
      className={cn(
        "flex h-full w-full min-w-0 flex-col bg-sidebar text-sidebar-foreground",
        !props.hostChromeInset && "border-r border-sidebar-border/60",
      )}
    >
      {/* 品牌区：折叠态仅显示 logo，展开态显示 logo + 文字 */}
      <div
        className={cn(
          "flex items-center gap-2 px-3",
          collapsed ? "h-12 justify-center" : "h-12",
          props.hostChromeInset && "pt-[2.85rem]",
        )}
      >
        <img
          src="/logo.svg"
          alt={t("app.brand")}
          className="h-7 w-7 shrink-0 rounded-md"
        />
        {!collapsed && (
          <span className="text-sm font-semibold text-sidebar-foreground truncate">
            {t("app.brand")}
          </span>
        )}
      </div>
      <div
        className={cn(
          "space-y-1.5 px-2",
          props.hostChromeInset ? "pt-[2.85rem]" : "pt-3",
          collapsed && "flex w-14 flex-col items-center px-0",
        )}
      >
        {!collapsed && !props.hostChromeInset ? (
          <Button
            type="button"
            variant="ghost"
            aria-label={t("sidebar.collapse")}
            onClick={props.onCollapse}
            className="h-8 w-full justify-start rounded-xl px-3 text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
          >
            <Menu className="h-4 w-4" />
          </Button>
        ) : null}
        {collapsed && props.onExpand ? (
          <SidebarActionButton
            collapsed
            label={t("sidebar.expand")}
            onClick={props.onExpand}
            icon={<Menu className="h-4 w-4" />}
          />
        ) : null}
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.newChat")}
          onClick={props.onNewChat}
          icon={<SquarePen className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.search")}
          onClick={props.onOpenSearch}
          icon={<Search className="h-4 w-4" />}
        />
        {/* 声明式视图导航按钮：由 VIEW_REGISTRY 驱动，新增视图只需在 registry 加一项 */}
        {props.navItems.map((item) => {
          const Icon = item.icon;
          return (
            <SidebarActionButton
              key={item.key}
              collapsed={collapsed}
              label={t(item.labelKey)}
              onClick={() => props.onNavigate(item.key)}
              icon={<Icon className="h-4 w-4" />}
            />
          );
        })}
        {props.archivedCount ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={props.showArchived ? t("chat.hideArchived") : t("chat.showArchived")}
            onClick={props.onToggleArchived}
            icon={<Archive className="h-4 w-4" />}
          />
        ) : null}
      </div>
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden transition-opacity duration-200",
          collapsed && "pointer-events-none opacity-0",
        )}
      >
        {!collapsed && (
          <ChatList
            sessions={props.sessions}
            activeKey={props.activeKey}
            loading={props.loading}
            emptyLabel={t("chat.noSessions")}
            onSelect={props.onSelect}
            onRequestDelete={props.onRequestDelete}
            onTogglePin={props.onTogglePin}
            onRequestRename={props.onRequestRename}
            onToggleArchive={props.onToggleArchive}
            onToggleGroup={props.onToggleGroup}
            onRequestRenameProject={props.onRequestRenameProject}
            onNewChatInProject={props.onNewChatInProject}
            pinnedKeys={props.pinnedKeys}
            archivedKeys={props.archivedKeys}
            titleOverrides={props.titleOverrides}
            projectNameOverrides={props.projectNameOverrides}
            collapsedGroups={props.collapsedGroups}
            runningChatIds={props.runningChatIds}
            completedChatIds={props.completedChatIds}
            density={props.viewState?.density}
            showPreviews={props.viewState?.show_previews}
            showTimestamps={props.viewState?.show_timestamps}
            sort={props.viewState?.sort}
            showArchived={props.showArchived}
            defaultWorkspacePath={props.defaultWorkspacePath}
            actionMenuPortalContainer={
              props.containActionMenus ? menuPortalContainer : undefined
            }
          />
        )}
      </div>
      <Separator className="bg-sidebar-border/50" />
      <div
        className={cn(
          "flex items-center gap-1 px-2.5 py-2.5 text-xs",
          collapsed && "w-14 flex-col px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.settings")}
          onClick={() => props.onOpenSettings()}
          className={collapsed ? undefined : "flex-1"}
          icon={<Settings className="h-4 w-4" />}
        />
        <ConnectionBadge />
      </div>
    </nav>
  );
}

function SidebarActionButton({
  collapsed,
  label,
  icon,
  onClick,
  active = false,
  className,
}: {
  collapsed: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  active?: boolean;
  className?: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      aria-label={label}
      aria-current={active ? "page" : undefined}
      title={collapsed ? label : undefined}
      onClick={onClick}
      className={cn(
        "group h-8 min-w-0 gap-2 overflow-hidden rounded-full font-medium text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
        "transition-[width,padding,border-radius,color,background-color] duration-300 ease-out",
        collapsed
          ? "w-9 justify-center gap-0 rounded-xl px-0"
          : "w-full justify-start gap-2 px-3 text-[12.5px]",
        active && "bg-sidebar-accent text-sidebar-foreground shadow-[inset_0_0_0_1px_hsl(var(--sidebar-border)/0.55)]",
        className,
      )}
    >
      <span
        className={cn(
          "flex shrink-0 items-center justify-center transition-transform duration-300 ease-out",
          "translate-x-0",
        )}
        aria-hidden
      >
        {icon}
      </span>
      <span
        className={cn(
          "min-w-0 overflow-hidden truncate whitespace-nowrap transition-[max-width,opacity,transform] duration-200 ease-out",
          collapsed
            ? "max-w-0 -translate-x-1 opacity-0"
            : "max-w-[12rem] translate-x-0 opacity-100",
        )}
      >
        {label}
      </span>
    </Button>
  );
}
