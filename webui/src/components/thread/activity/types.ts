import type { UIFileEdit, UIMessage } from "@/lib/types";

export interface ActivityCounts {
  reasoningSteps: number;
  toolCalls: number;
  cliCount: number;
  mcpCount: number;
  fileCount: number;
  added: number;
  deleted: number;
  hasDiffStats: boolean;
  hasEditingFiles: boolean;
  hasFailedFiles: boolean;
  hasDeletedFiles: boolean;
  primaryFilePath?: string;
  primaryFileTooltipPath?: string;
  primaryCliName?: string;
  primaryCliStatus?: CliRunStatus;
  primaryMcpName?: string;
  primaryMcpDisplayName?: string;
  primaryMcpStatus?: McpRunStatus;
}

export interface FileEditSummary {
  key: string;
  path: string;
  absolute_path?: string | null;
  added: number;
  deleted: number;
  approximate: boolean;
  binary: boolean;
  status: UIFileEdit["status"];
  operation?: UIFileEdit["operation"];
  pending: boolean;
  error?: string;
}

export interface CliRunSummary {
  key: string;
  name: string;
  args: string[];
  json: boolean;
  workingDir?: string;
  status: CliRunStatus;
  error?: string;
}

export type CliRunStatus = "running" | "done" | "error";
export type McpRunStatus = "running" | "done" | "error";

export interface McpRunSummary {
  key: string;
  presetName: string;
  displayName: string;
  toolName: string;
  argsPreview: string;
  status: McpRunStatus;
  error?: string;
}

export interface TraceDescription {
  kind: "search" | "tool" | "done" | "trace";
  label: string;
  detail: string;
  url?: string;
  host?: string;
}

export function isReasoningOnlyAssistant(m: UIMessage): boolean {
  if (m.role !== "assistant" || m.kind === "trace") return false;
  if (m.content.trim().length > 0) return false;
  return !!(m.reasoning?.length || m.reasoningStreaming || m.isStreaming);
}

export function isAgentActivityMember(m: UIMessage): boolean {
  return isReasoningOnlyAssistant(m) || m.kind === "trace";
}

export function traceLines(message: UIMessage): string[] {
  if (message.traces?.length) return message.traces;
  return message.content.trim() ? [message.content] : [];
}

export function previewScalar(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

export function previewMcpArgs(argsObject: unknown): string {
  if (!argsObject || typeof argsObject !== "object" || Array.isArray(argsObject)) {
    return previewScalar(argsObject) ?? "";
  }
  const record = argsObject as Record<string, unknown>;
  for (const key of ["url", "query", "q", "path", "name", "id", "title", "message", "text"]) {
    const preview = previewScalar(record[key]);
    if (preview) return `${key}: ${preview}`;
  }
  const entries = Object.entries(record)
    .filter(([, value]) => previewScalar(value) !== null)
    .slice(0, 2)
    .map(([key, value]) => `${key}: ${previewScalar(value)}`);
  return entries.join(" · ");
}

export function activityDurationMs(
  messages: UIMessage[],
  active: boolean,
  now: number,
  completedLatencyMs?: number,
): number {
  if (!active && Number.isFinite(completedLatencyMs) && completedLatencyMs! >= 0) {
    return Math.round(completedLatencyMs!);
  }
  const timestamps = messages
    .map((message) => message.createdAt)
    .filter((value) => Number.isFinite(value));
  if (!timestamps.length) return 0;
  const first = Math.min(...timestamps);
  const last = active && first > 1_000_000_000_000
    ? now
    : Math.max(...timestamps);
  return Math.max(0, last - first);
}

export function formatActivityDuration(ms: number): string {
  const seconds = ms > 0 && ms < 1000 ? 1 : Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}
