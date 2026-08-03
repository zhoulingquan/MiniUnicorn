import { formatToolCallTrace } from "@/lib/tool-traces";
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

import { traceLines } from "@/components/thread/activity/types";
import type { CliRunStatus, CliRunSummary } from "@/components/thread/activity/types";

const CLI_RUN_TOOL_NAMES = new Set(["run_cli_app", "cli_anything_run"]);
const CLI_RUN_STATUS_RANK: Record<CliRunStatus, number> = { running: 1, done: 2, error: 3 };

export function isCliRunTraceLine(line: string): boolean {
  return /^(run_cli_app|cli_anything_run)\(/.test(line.trim());
}

export function parseCliRunTrace(line: string, status: CliRunStatus = "running"): CliRunSummary | null {
  const match = /^(run_cli_app|cli_anything_run)\((.*)\)$/.exec(line.trim());
  if (!match) return null;
  const argsText = match[2].trim();
  let argsObject: unknown = {};
  if (argsText) {
    try {
      argsObject = JSON.parse(argsText);
    } catch {
      return {
        key: line,
        name: "cli",
        args: [argsText],
        json: false,
        status,
      };
    }
  }
  return cliRunFromArguments(argsObject, { key: line, status });
}

export function parseToolEventArguments(event: ToolProgressEvent): unknown {
  const fnArgs = (event as { function?: { arguments?: unknown } }).function?.arguments;
  const raw = fnArgs ?? event.arguments;
  if (typeof raw !== "string") return raw ?? {};
  if (!raw.trim()) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return { args: [raw] };
  }
}

export function cliRunStatusFromPhase(phase: unknown): CliRunStatus {
  if (phase === "error") return "error";
  if (phase === "end") return "done";
  return "running";
}

export function cliRunError(event: ToolProgressEvent): string | undefined {
  const error = event.error;
  if (typeof error === "string") return error;
  if (error && typeof error === "object") return JSON.stringify(error);
  return undefined;
}

export function toolEventName(event: ToolProgressEvent): string {
  return typeof (event as { function?: { name?: unknown } }).function?.name === "string"
    ? String((event as { function?: { name?: unknown } }).function?.name)
    : typeof event.name === "string"
      ? event.name
      : "";
}

function cliRunFromArguments(
  argsObject: unknown,
  options: { key: string; status: CliRunStatus; error?: string },
): CliRunSummary {
  if (!argsObject || typeof argsObject !== "object" || Array.isArray(argsObject)) {
    return {
      key: options.key,
      name: "cli",
      args: [],
      json: false,
      status: options.status,
      error: options.error,
    };
  }
  const record = argsObject as Record<string, unknown>;
  const appName = typeof record.name === "string" && record.name.trim()
    ? record.name.trim()
    : "cli";
  const rawArgs = Array.isArray(record.args) ? record.args : [];
  const cliArgs = rawArgs.filter((item): item is string => typeof item === "string");
  return {
    key: options.key,
    name: appName,
    args: cliArgs,
    json: record.json === true || record.json === "true",
    workingDir: typeof record.working_dir === "string" ? record.working_dir : undefined,
    status: options.status,
    error: options.error,
  };
}

function cliRunFromEvent(event: ToolProgressEvent): CliRunSummary | null {
  const name = toolEventName(event);
  if (!CLI_RUN_TOOL_NAMES.has(name)) return null;
  const argsObject = parseToolEventArguments(event);
  const key = event.call_id ? `call:${event.call_id}` : `${name}:${JSON.stringify(argsObject)}`;
  return cliRunFromArguments(argsObject, {
    key,
    status: cliRunStatusFromPhase(event.phase),
    error: cliRunError(event),
  });
}

export function cliRunMapByTraceLine(message: UIMessage): Map<string, CliRunSummary> {
  const runsByLine = new Map<string, CliRunSummary>();
  for (const event of message.toolEvents ?? []) {
    const run = cliRunFromEvent(event);
    if (!run) continue;
    const line = formatToolCallTrace(event);
    if (!line) continue;
    runsByLine.set(line, mergeCliRun(runsByLine.get(line), run));
  }
  return runsByLine;
}

function mergeCliRun(existing: CliRunSummary | undefined, incoming: CliRunSummary): CliRunSummary {
  if (!existing) return incoming;
  return CLI_RUN_STATUS_RANK[incoming.status] >= CLI_RUN_STATUS_RANK[existing.status]
    ? { ...existing, ...incoming }
    : existing;
}

export function collectCliRuns(messages: UIMessage[]): CliRunSummary[] {
  const runsByKey = new Map<string, CliRunSummary>();
  for (const message of messages) {
    if (message.kind !== "trace") continue;
    let hasStructuredCliRun = false;
    for (const event of message.toolEvents ?? []) {
      const run = cliRunFromEvent(event);
      if (!run) continue;
      hasStructuredCliRun = true;
      runsByKey.set(run.key, mergeCliRun(runsByKey.get(run.key), run));
    }
    if (hasStructuredCliRun) continue;
    for (const line of traceLines(message)) {
      const run = parseCliRunTrace(line);
      if (!run || runsByKey.has(run.key)) continue;
      runsByKey.set(run.key, run);
    }
  }
  return [...runsByKey.values()];
}

export function displayCliArg(arg: string): string {
  return /\s/.test(arg) ? JSON.stringify(arg) : arg;
}

export function formatCliArgs(run: CliRunSummary): string {
  const args = [...(run.json ? ["--json"] : []), ...run.args].map(displayCliArg);
  return args.join(" ");
}

export function cliActivitySummaryKey(status: CliRunStatus | undefined, active: boolean): string {
  if (status === "error") return "message.cliActivityFailedOne";
  return active && status === "running" ? "message.cliActivityRunningOne" : "message.cliActivityRanOne";
}

export function cliActivitySummaryDefault(status: CliRunStatus | undefined, active: boolean): string {
  if (status === "error") return "Failed @{{name}}";
  return `${active && status === "running" ? "Using" : "Used"} @{{name}}`;
}

export function cliActivityManySummaryKey(runs: CliRunSummary[], active: boolean): string {
  if (runs.some((run) => run.status === "error")) return "message.cliActivityFailedMany";
  return active && runs.some((run) => run.status === "running")
    ? "message.cliActivityRunningMany"
    : "message.cliActivityRanMany";
}

export function cliActivityManySummaryDefault(runs: CliRunSummary[], active: boolean): string {
  if (runs.some((run) => run.status === "error")) return "{{count}} CLI apps failed";
  return `${active && runs.some((run) => run.status === "running") ? "Using" : "Used"} {{count}} CLI apps`;
}

export function cliRunLabelKey(run: CliRunSummary, active: boolean): string {
  if (run.status === "error") return "message.cliRunFailed";
  return active && run.status === "running" ? "message.cliRunRunning" : "message.cliRunRan";
}

export function cliRunLabelDefault(run: CliRunSummary, active: boolean): string {
  if (run.status === "error") return "Failed";
  return active && run.status === "running" ? "Using" : "Used";
}
