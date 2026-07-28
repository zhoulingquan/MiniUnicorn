// 顶层视图注册表（声明式）
//
// 新增顶层视图只需：
//   1. 新建组件文件（如 webui/src/components/foo/FooView.tsx）
//   2. 在下方 VIEW_REGISTRY 加一项（含 lazy 组件 + render 函数 + 图标 + i18n key）
//   3. 如需在 Sidebar 顶部按钮区显示，order 字段决定排列顺序
//
// 设计说明：
//   - 用 render 函数模式而非 component 字段，因为各 View 的 props 差异较大
//     （settings 无 token 但有 themeMode/onRestart 等；apps 无 token；agents 有 onUseAgent）
//   - lazy 组件在模块顶层定义，确保 Suspense 只触发一次懒加载
//   - showBoundary 控制是否包裹 ErrorBoundary（tools/channels/apps 原本无 ErrorBoundary，保持不变）
//   - settings/chat 是特殊视图：chat 不在此注册（需保持挂载不卸载），settings 在此注册但 render 较复杂

import { lazy, type ComponentType, type ReactNode } from "react";
import {
  CalendarClock,
  LayoutGrid,
  MessageSquare,
  Package,
  PlugZap,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";
import type { SettingsSectionKey } from "@/components/settings/types";
import type { SettingsPayload } from "@/lib/types";
import type { ThemeMode } from "@/hooks/useTheme";

// 各 View 的 lazy 组件（模块顶层定义，确保只懒加载一次）
const LazySettingsView = lazy(() => import("@/components/settings/SettingsView").then(m => ({ default: m.SettingsView })));
const LazyMcpView = lazy(() => import("@/components/mcp/McpView").then(m => ({ default: m.McpView })));
const LazySkillsView = lazy(() => import("@/components/skills/SkillsView").then(m => ({ default: m.SkillsView })));
const LazyAgentsView = lazy(() => import("@/components/agents/AgentsView").then(m => ({ default: m.AgentsView })));
const LazyCronView = lazy(() => import("@/components/cron/CronView").then(m => ({ default: m.CronView })));
const LazyToolsView = lazy(() => import("@/components/tools/ToolsView").then(m => ({ default: m.ToolsView })));
const LazyChannelsView = lazy(() => import("@/components/channels/ChannelsView").then(m => ({ default: m.ChannelsView })));
const LazyAppsView = lazy(() => import("@/components/apps/AppsView").then(m => ({ default: m.AppsView })));

// 渲染上下文：聚合所有 View 可能需要的 props，由 App.tsx 统一传入
export interface ViewRenderContext {
  token: string;
  onBack: () => void;
  // agents 专用
  onUseAgent: (agentId: string) => void;
  // settings 专用
  themeMode: ThemeMode;
  initialSection: SettingsSectionKey;
  showSidebar: boolean;
  onSetThemeMode: (mode: ThemeMode) => void;
  onModelNameChange: (name: string | null) => void;
  onSettingsChange?: (payload: SettingsPayload) => void;
  onRestart?: () => void;
  isRestarting: boolean;
  hostChromeInset: boolean;
  /** 侧边栏折叠态(供 settings 等有独立 sidebar 的视图使用)。 */
  sidebarCollapsed?: boolean;
  /** 切换侧边栏折叠态。 */
  onToggleSidebar?: () => void;
}

export interface ViewRegistration {
  key: string;
  labelKey: string;          // i18n key，如 "sidebar.mcp"
  icon: ComponentType<{ className?: string }>;
  render: (ctx: ViewRenderContext) => ReactNode;
  showBoundary?: boolean;    // 是否包 ErrorBoundary，默认 true
  order: number;              // sidebar 顶部按钮区排列顺序
}

export const VIEW_REGISTRY: ViewRegistration[] = [
  {
    key: "skills",
    labelKey: "sidebar.skills",
    icon: Sparkles,
    showBoundary: true,
    order: 0,
    render: (ctx) => <LazySkillsView onBack={ctx.onBack} token={ctx.token} />,
  },
  {
    key: "tools",
    labelKey: "sidebar.tools",
    icon: Package,
    showBoundary: false,      // 保持原有行为：无 ErrorBoundary
    order: 1,
    render: (ctx) => <LazyToolsView onBack={ctx.onBack} token={ctx.token} />,
  },
  {
    key: "agents",
    labelKey: "sidebar.agents",
    icon: Users,
    showBoundary: true,
    order: 2,
    render: (ctx) => <LazyAgentsView onBack={ctx.onBack} token={ctx.token} onUseAgent={ctx.onUseAgent} />,
  },
  {
    key: "mcp",
    labelKey: "sidebar.mcp",
    icon: PlugZap,
    showBoundary: true,
    order: 3,
    render: (ctx) => <LazyMcpView onBack={ctx.onBack} token={ctx.token} />,
  },
  {
    key: "channels",
    labelKey: "sidebar.channels",
    icon: MessageSquare,
    showBoundary: false,      // 保持原有行为：无 ErrorBoundary
    order: 4,
    render: (ctx) => <LazyChannelsView onBack={ctx.onBack} token={ctx.token} />,
  },
  {
    key: "apps",
    labelKey: "sidebar.apps",
    icon: LayoutGrid,
    showBoundary: false,      // 保持原有行为：无 ErrorBoundary
    order: 5,
    render: (ctx) => <LazyAppsView onBack={ctx.onBack} />,
  },
  {
    key: "cron",
    labelKey: "sidebar.cron",
    icon: CalendarClock,
    showBoundary: true,
    order: 6,
    render: (ctx) => <LazyCronView onBack={ctx.onBack} token={ctx.token} />,
  },
  {
    // settings 特殊：props 与其他 view 完全不同，且通过 useClient() 自取 token
    key: "settings",
    labelKey: "sidebar.settings",
    icon: Settings,
    showBoundary: true,
    order: 7,
    render: (ctx) => (
      <LazySettingsView
        themeMode={ctx.themeMode}
        initialSection={ctx.initialSection}
        showSidebar={ctx.showSidebar}
        onSetThemeMode={ctx.onSetThemeMode}
        onBackToChat={ctx.onBack}
        onModelNameChange={ctx.onModelNameChange}
        onSettingsChange={ctx.onSettingsChange}
        onRestart={ctx.onRestart}
        isRestarting={ctx.isRestarting}
        hostChromeInset={ctx.hostChromeInset}
        sidebarCollapsed={ctx.sidebarCollapsed}
        onToggleSidebar={ctx.onToggleSidebar}
      />
    ),
  },
];

/** 获取所有在 Sidebar 顶部按钮区显示的视图（按 order 排序） */
export function getSidebarNavItems(): ViewRegistration[] {
  return [...VIEW_REGISTRY].sort((a, b) => a.order - b.order);
}

/** 按 key 查找视图注册项 */
export function getView(key: string): ViewRegistration | undefined {
  return VIEW_REGISTRY.find((v) => v.key === key);
}

/** 所有已注册视图的 key（用于类型推导，chat 不在其中） */
export const VIEW_KEYS = VIEW_REGISTRY.map((v) => v.key);
