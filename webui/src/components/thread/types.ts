import type {
  AgentInfo,
  ContextUsagePayload,
  GoalStateWsPayload,
  SkillInfo,
  SlashCommand,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import type { SendImage, SendOptions } from "@/hooks/useErzaStream";

/** 主组件 ThreadComposer 的 props 类型。 */
export interface ThreadComposerProps {
  onSend: (content: string, images?: SendImage[], options?: SendOptions) => void;
  disabled?: boolean;
  placeholder?: string;
  isStreaming?: boolean;
  modelLabel?: string | null;
  modelProvider?: string | null;
  modelProviderLabel?: string | null;
  /** 当前 provider 的 api_base(用于 custom 动态 brand 图标生成)。 */
  modelApiBase?: string | null;
  /** 当前 provider 下可用模型列表(用于多模型下拉选择)。 */
  models?: string[];
  /** 用户在 composer 模型徽章弹出菜单中选择其他模型时触发。 */
  onSelectModel?: (model: string) => void;
  variant?: "thread" | "hero";
  slashCommands?: SlashCommand[];
  onStop?: () => void;
  /** Unix seconds from server; turn elapsed timer above input while set. */
  runStartedAt?: number | null;
  /** Sustained objective for this chat (WebSocket ``goal_state``). */
  goalState?: GoalStateWsPayload;
  workspaceScope?: WorkspaceScopePayload | null;
  workspaceDefaultScope?: WorkspaceScopePayload | null;
  workspaceControls?: WorkspacesPayload["controls"] | null;
  workspaceScopeDisabled?: boolean;
  workspaceError?: string | null;
  onWorkspaceScopeChange?: (scope: WorkspaceScopePayload) => void;
  /** Subagents available for ``@agent`` selection (from ``GET /api/agents``). */
  agents?: AgentInfo[];
  /** Currently selected subagent id (``agent.name``); routes outbound turns. */
  selectedAgentId?: string | null;
  /** Called when the user picks a subagent from the selector. */
  onSelectAgent?: (agentId: string) => void;
  /** Called when the user clears the active subagent selection. */
  onClearAgent?: () => void;
  /** 可用 skill 列表(从 ``GET /api/skills`` 拉取,已在前端过滤 available && !disabled)。
   * 列表非空时,在对话框左下角渲染 SkillSelectorButton。 */
  skills?: SkillInfo[];
  /** 当前会话消息条数(含 user/assistant,不含 trace 行)。 */
  messageCount?: number;
  /** 当前模型预设的上下文窗口大小(tokens),用于显示上下文预算。 */
  contextWindowTokens?: number | null;
  /** 最近一轮对话最后一次 LLM 调用的 token usage(从 turn_end 推送)。 */
  contextUsage?: ContextUsagePayload | null;
  /** Session key used to reset per-conversation refs (e.g. chipRefs) on switch. */
  conversationKey?: string | null;
  /** 当传入非空字符串时,将该文本填入输入框并聚焦(用于回退后编辑重发)。
   * 每次填入后应通过 ``onPrefillConsumed`` 通知父组件清空,避免重复填入。 */
  prefillText?: string | null;
  /** ``prefillText`` 被填入输入框后调用,父组件据此清空 prefill 状态。 */
  onPrefillConsumed?: () => void;
  /** 服务端 WebSocket 帧大小上限(字节)。Composer 在发送前对最终 JSON frame
   * 的 UTF-8 字节数做校验,超限直接拦截并保留草稿(见设计 §4.5)。 */
  maxMessageBytes?: number;
}

/** 斜杠命令面板的弹出位置(输入框上方或下方)。 */
export type SlashPalettePlacement = "above" | "below";

/** 斜杠命令面板的布局信息(位置 + 最大高度)。 */
export interface SlashPaletteLayout {
  placement: SlashPalettePlacement;
  maxHeight: number;
}

/** 斜杠命令面板中单条命令的展示模型,扩展自原始 SlashCommand。 */
export interface SlashPaletteCommand extends SlashCommand {
  detail: string;
  badge?: string;
  recent: boolean;
}
