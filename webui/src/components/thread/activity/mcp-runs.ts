import { formatToolCallTrace } from "@/lib/tool-traces";
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

import {
  cliRunError,
  cliRunStatusFromPhase,
  parseToolEventArguments,
  toolEventName,
} from "@/components/thread/activity/cli-runs";
import {
  previewMcpArgs,
  traceLines,
} from "@/components/thread/activity/types";
import type { McpRunStatus, McpRunSummary } from "@/components/thread/activity/types";

const MCP_RUN_STATUS_RANK: Record<McpRunStatus, number> = { running: 1, done: 2, error: 3 };
const MCP_TOOL_NAME_RE = /^mcp_([a-z0-9_-]+?)_(.+)$/i;

export function isMcpRunTraceLine(line: string): boolean {
  return MCP_TOOL_NAME_RE.test(line.trim().split("(", 1)[0] ?? "");
}

function titleFromPresetName(name: string): string {
  return name
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ") || name;
}

function mcpRunFromToolName(
  toolName: string,
  argsObject: unknown,
  options: { key: string; status: McpRunStatus; error?: string },
): McpRunSummary | null {
  const match = MCP_TOOL_NAME_RE.exec(toolName);
  if (!match) return null;
  const presetName = match[1].toLowerCase();
  return {
    key: options.key,
    presetName,
    displayName: titleFromPresetName(presetName),
    toolName: match[2],
    argsPreview: previewMcpArgs(argsObject),
    status: options.status,
    error: options.error,
  };
}

export function parseMcpRunTrace(line: string, status: McpRunStatus = "running"): McpRunSummary | null {
  const match = /^([a-z0-9_-]+)\((.*)\)$/i.exec(line.trim());
  if (!match || !MCP_TOOL_NAME_RE.test(match[1])) return null;
  const argsText = match[2].trim();
  let argsObject: unknown = {};
  if (argsText) {
    try {
      argsObject = JSON.parse(argsText);
    } catch {
      argsObject = argsText;
    }
  }
  return mcpRunFromToolName(match[1], argsObject, { key: line, status });
}

function mcpRunFromEvent(event: ToolProgressEvent): McpRunSummary | null {
  const name = toolEventName(event);
  if (!MCP_TOOL_NAME_RE.test(name)) return null;
  const argsObject = parseToolEventArguments(event);
  const key = event.call_id ? `call:${event.call_id}` : `${name}:${JSON.stringify(argsObject)}`;
  return mcpRunFromToolName(name, argsObject, {
    key,
    status: cliRunStatusFromPhase(event.phase),
    error: cliRunError(event),
  });
}

export function mcpRunMapByTraceLine(message: UIMessage): Map<string, McpRunSummary> {
  const runsByLine = new Map<string, McpRunSummary>();
  for (const event of message.toolEvents ?? []) {
    const run = mcpRunFromEvent(event);
    if (!run) continue;
    const line = formatToolCallTrace(event);
    if (!line) continue;
    runsByLine.set(line, mergeMcpRun(runsByLine.get(line), run));
  }
  return runsByLine;
}

function mergeMcpRun(existing: McpRunSummary | undefined, incoming: McpRunSummary): McpRunSummary {
  if (!existing) return incoming;
  return MCP_RUN_STATUS_RANK[incoming.status] >= MCP_RUN_STATUS_RANK[existing.status]
    ? { ...existing, ...incoming }
    : existing;
}

export function collectMcpRuns(messages: UIMessage[]): McpRunSummary[] {
  const runsByKey = new Map<string, McpRunSummary>();
  for (const message of messages) {
    if (message.kind !== "trace") continue;
    let hasStructuredMcpRun = false;
    for (const event of message.toolEvents ?? []) {
      const run = mcpRunFromEvent(event);
      if (!run) continue;
      hasStructuredMcpRun = true;
      runsByKey.set(run.key, mergeMcpRun(runsByKey.get(run.key), run));
    }
    if (hasStructuredMcpRun) continue;
    for (const line of traceLines(message)) {
      const run = parseMcpRunTrace(line);
      if (!run || runsByKey.has(run.key)) continue;
      runsByKey.set(run.key, run);
    }
  }
  return [...runsByKey.values()];
}

export function mcpActivitySummaryKey(status: McpRunStatus | undefined, active: boolean): string {
  if (status === "error") return "message.mcpActivityFailedOne";
  return active && status === "running" ? "message.mcpActivityRunningOne" : "message.mcpActivityRanOne";
}

export function mcpActivitySummaryDefault(status: McpRunStatus | undefined, active: boolean): string {
  if (status === "error") return "Failed {{name}}";
  return `${active && status === "running" ? "Using" : "Used"} {{name}}`;
}

export function mcpActivityManySummaryKey(runs: McpRunSummary[], active: boolean): string {
  if (runs.some((run) => run.status === "error")) return "message.mcpActivityFailedMany";
  return active && runs.some((run) => run.status === "running")
    ? "message.mcpActivityRunningMany"
    : "message.mcpActivityRanMany";
}

export function mcpActivityManySummaryDefault(runs: McpRunSummary[], active: boolean): string {
  if (runs.some((run) => run.status === "error")) return "{{count}} MCP calls failed";
  return `${active && runs.some((run) => run.status === "running") ? "Using" : "Used"} {{count}} MCP tools`;
}

export function mcpRunLabelKey(run: McpRunSummary, active: boolean): string {
  if (run.status === "error") return "message.mcpRunFailed";
  return active && run.status === "running" ? "message.mcpRunRunning" : "message.mcpRunRan";
}

export function mcpRunLabelDefault(run: McpRunSummary, active: boolean): string {
  if (run.status === "error") return "Failed";
  return active && run.status === "running" ? "Using" : "Used";
}
