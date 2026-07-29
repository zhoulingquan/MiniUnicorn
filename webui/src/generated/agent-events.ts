/* Generated from Python Pydantic models. Do not edit. */

export type InboundEvent =
  | ReadyEvent
  | AttachedEvent
  | MessageEvent
  | FileEditEvent
  | DeltaEvent
  | StreamEndEvent
  | ReasoningDeltaEvent
  | ReasoningEndEvent
  | RuntimeModelUpdatedEvent
  | TurnEndEvent
  | GoalStatusEvent
  | GoalStateEvent
  | SessionUpdatedEvent
  | SubagentActivityEvent
  | ErrorEvent;
export type ChatId = string;
export type ClientId = string;
export type Event = "ready";
export type ProtocolVersion = 1;
export type ChatId1 = string;
export type Event1 = "attached";
export type ProtocolVersion1 = 1;
export type RequestId = string | null;
export type AgentUi = {
  [k: string]: unknown;
} | null;
export type ChatId2 = string;
export type Event2 = "message";
export type Kind = ("tool_hint" | "progress" | "reasoning") | null;
export type LatencyMs = number | null;
export type Media = string[] | null;
export type MediaUrls =
  | {
      [k: string]: string;
    }[]
  | null;
export type ProtocolVersion2 = 1;
export type ReplyTo = string | null;
export type Text = string;
export type ToolEvents = ToolProgressEvent[] | null;
export type CallId = string;
export type Embeds = unknown[];
export type Error = string | null;
export type Files = unknown[];
export type Name = string;
export type Phase = "start" | "end" | "error";
export type Version = 1;
export type ChatId3 = string;
export type AbsolutePath = string | null;
export type Added = number;
export type Approximate = boolean | null;
export type Binary = boolean | null;
export type CallId1 = string;
export type Deleted = number;
export type Error1 = string | null;
export type Operation = string | null;
export type Path = string;
export type Pending = boolean | null;
export type Phase1 = string | null;
export type Status = "editing" | "done" | "error";
export type Tool = string;
export type Version1 = number | null;
export type Edits = FileEditPayload[];
export type Event3 = "file_edit";
export type ProtocolVersion3 = 1;
export type ChatId4 = string;
export type Event4 = "delta";
export type ProtocolVersion4 = 1;
export type StreamId = string | null;
export type Text1 = string;
export type ChatId5 = string;
export type Event5 = "stream_end";
export type ProtocolVersion5 = 1;
export type StreamId1 = string | null;
export type Text2 = string | null;
export type ChatId6 = string;
export type Event6 = "reasoning_delta";
export type ProtocolVersion6 = 1;
export type StreamId2 = string | null;
export type Text3 = string;
export type ChatId7 = string;
export type Event7 = "reasoning_end";
export type ProtocolVersion7 = 1;
export type StreamId3 = string | null;
export type Event8 = "runtime_model_updated";
export type ModelName = string;
export type ModelPreset = string | null;
export type ProtocolVersion8 = 1;
export type ChatId8 = string;
export type CachedTokens = number;
export type CompletionTokens = number;
export type PromptTokens = number;
export type TotalTokens = number;
export type Event9 = "turn_end";
export type Active = boolean;
export type Objective = string | null;
export type UiSummary = string | null;
export type LatencyMs1 = number | null;
export type ProtocolVersion9 = 1;
export type ChatId9 = string;
export type Event10 = "goal_status";
export type ProtocolVersion10 = 1;
export type StartedAt = number | null;
export type Status1 = "running" | "idle";
export type ChatId10 = string;
export type Event11 = "goal_state";
export type ProtocolVersion11 = 1;
export type ChatId11 = string;
export type Event12 = "session_updated";
export type ProtocolVersion12 = 1;
export type Scope = string | null;
export type AccessMode = "restricted" | "full";
export type ProjectName = string | null;
export type ProjectPath = string;
export type RestrictToWorkspace = boolean | null;
export type Enforced = boolean;
export type Level = string;
export type Provider = string;
export type ProviderLabel = string;
export type RestrictToWorkspace1 = boolean;
export type Summary = string;
export type WorkspaceRoot = string;
export type ChatId12 = string;
export type Content = string;
export type Event13 = "subagent_activity";
export type Label = string | null;
export type ProtocolVersion13 = 1;
export type TaskId = string | null;
export type ChatId13 = string | null;
export type Detail = string | null;
export type Event14 = "error";
export type ProtocolVersion14 = 1;
export type Reason = string | null;

/**
 * Initial frame sent right after the WebSocket handshake completes.
 */
export interface ReadyEvent {
  chat_id: ChatId;
  client_id: ClientId;
  event?: Event;
  protocol_version?: ProtocolVersion;
}
/**
 * Ack for ``new_chat`` / ``attach`` envelopes.
 */
export interface AttachedEvent {
  chat_id: ChatId1;
  event?: Event1;
  protocol_version?: ProtocolVersion1;
  request_id?: RequestId;
}
/**
 * Conversational assistant message (final or intermediate breadcrumb).
 *
 * ``kind`` disambiguates intermediate breadcrumbs (``tool_hint`` /
 * ``progress`` / ``reasoning``) from final replies, which omit the field.
 */
export interface MessageEvent {
  agent_ui?: AgentUi;
  chat_id: ChatId2;
  event?: Event2;
  kind?: Kind;
  latency_ms?: LatencyMs;
  media?: Media;
  media_urls?: MediaUrls;
  protocol_version?: ProtocolVersion2;
  reply_to?: ReplyTo;
  text: Text;
  tool_events?: ToolEvents;
}
/**
 * One tool-call lifecycle breadcrumb.
 *
 * This is both a standalone event payload (embedded in ``MessageEvent``)
 * and the shape persisted in the WebUI transcript for tool-call traces.
 * ``version`` is fixed at ``1`` for this protocol generation.
 */
export interface ToolProgressEvent {
  arguments?: Arguments;
  call_id: CallId;
  embeds?: Embeds;
  error?: Error;
  files?: Files;
  name: Name;
  phase: Phase;
  result?: unknown;
  version?: Version;
}
export interface Arguments {
  [k: string]: unknown;
}
/**
 * Batched file-edit notification (one or more edits).
 */
export interface FileEditEvent {
  chat_id: ChatId3;
  edits: Edits;
  event?: Event3;
  protocol_version?: ProtocolVersion3;
}
/**
 * One file-edit event emitted by editing tools.
 */
export interface FileEditPayload {
  absolute_path?: AbsolutePath;
  added?: Added;
  approximate?: Approximate;
  binary?: Binary;
  call_id: CallId1;
  deleted?: Deleted;
  error?: Error1;
  operation?: Operation;
  path: Path;
  pending?: Pending;
  phase?: Phase1;
  status: Status;
  tool: Tool;
  version?: Version1;
}
/**
 * One streaming text chunk for the active assistant bubble.
 */
export interface DeltaEvent {
  chat_id: ChatId4;
  event?: Event4;
  protocol_version?: ProtocolVersion4;
  stream_id?: StreamId;
  text: Text1;
}
/**
 * Close of a streaming text segment (final rewritten text optional).
 */
export interface StreamEndEvent {
  chat_id: ChatId5;
  event?: Event5;
  protocol_version?: ProtocolVersion5;
  stream_id?: StreamId1;
  text?: Text2;
}
/**
 * One streaming chunk of model reasoning.
 */
export interface ReasoningDeltaEvent {
  chat_id: ChatId6;
  event?: Event6;
  protocol_version?: ProtocolVersion6;
  stream_id?: StreamId2;
  text: Text3;
}
/**
 * Close of the current reasoning stream segment.
 */
export interface ReasoningEndEvent {
  chat_id: ChatId7;
  event?: Event7;
  protocol_version?: ProtocolVersion7;
  stream_id?: StreamId3;
}
/**
 * Broadcast that the active model/preset changed at runtime.
 */
export interface RuntimeModelUpdatedEvent {
  event?: Event8;
  model_name: ModelName;
  model_preset?: ModelPreset;
  protocol_version?: ProtocolVersion8;
}
/**
 * Signal that the agent fully finished processing the current turn.
 */
export interface TurnEndEvent {
  chat_id: ChatId8;
  context_usage?: ContextUsagePayload | null;
  event?: Event9;
  goal_state?: GoalStatePayload | null;
  latency_ms?: LatencyMs1;
  protocol_version?: ProtocolVersion9;
}
/**
 * Token-usage snapshot for a completed turn.
 */
export interface ContextUsagePayload {
  cached_tokens?: CachedTokens;
  completion_tokens?: CompletionTokens;
  prompt_tokens?: PromptTokens;
  total_tokens?: TotalTokens;
}
/**
 * Persisted sustained-goal snapshot replayed on reconnect.
 */
export interface GoalStatePayload {
  active: Active;
  objective?: Objective;
  ui_summary?: UiSummary;
}
/**
 * Wall-clock hint that a turn started or finished.
 */
export interface GoalStatusEvent {
  chat_id: ChatId9;
  event?: Event10;
  protocol_version?: ProtocolVersion10;
  started_at?: StartedAt;
  status: Status1;
}
/**
 * Persisted goal-state snapshot pushed to freshly subscribed clients.
 */
export interface GoalStateEvent {
  chat_id: ChatId10;
  event?: Event11;
  goal_state: GoalStatePayload;
  protocol_version?: ProtocolVersion11;
}
/**
 * Notify clients that session metadata changed outside the main turn.
 */
export interface SessionUpdatedEvent {
  chat_id: ChatId11;
  event?: Event12;
  protocol_version?: ProtocolVersion12;
  scope?: Scope;
  workspace_scope?: WorkspaceScopePayload | null;
}
/**
 * Workspace scope attached to ``session_updated`` and ``attached`` events.
 */
export interface WorkspaceScopePayload {
  access_mode: AccessMode;
  project_name?: ProjectName;
  project_path: ProjectPath;
  restrict_to_workspace?: RestrictToWorkspace;
  sandbox_status?: SandboxStatusPayload | null;
}
/**
 * Sandbox enforcement snapshot attached to workspace scope events.
 */
export interface SandboxStatusPayload {
  enforced: Enforced;
  level: Level;
  provider: Provider;
  provider_label: ProviderLabel;
  restrict_to_workspace: RestrictToWorkspace1;
  summary: Summary;
  workspace_root: WorkspaceRoot;
}
/**
 * Subagent breadcrumb (tool call, reasoning, completion).
 */
export interface SubagentActivityEvent {
  chat_id: ChatId12;
  content: Content;
  event?: Event13;
  label?: Label;
  protocol_version?: ProtocolVersion13;
  task_id?: TaskId;
}
/**
 * Channel-level error frame (invalid envelope, media rejection, …).
 */
export interface ErrorEvent {
  chat_id?: ChatId13;
  detail?: Detail;
  event?: Event14;
  protocol_version?: ProtocolVersion14;
  reason?: Reason;
}
