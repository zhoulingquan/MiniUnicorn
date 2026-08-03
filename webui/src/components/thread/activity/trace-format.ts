import type { UIMessage } from "@/lib/types";

import { hasVisibleDiffStats } from "@/components/thread/activity/file-edits";
import { isCliRunTraceLine } from "@/components/thread/activity/cli-runs";
import { isMcpRunTraceLine } from "@/components/thread/activity/mcp-runs";
import {
  isReasoningOnlyAssistant,
  previewMcpArgs,
  traceLines,
} from "@/components/thread/activity/types";
import type {
  ActivityCounts,
  CliRunSummary,
  FileEditSummary,
  McpRunSummary,
  TraceDescription,
} from "@/components/thread/activity/types";

export function countActivity(
  messages: UIMessage[],
  fileEdits: FileEditSummary[],
  cliRuns: CliRunSummary[],
  mcpRuns: McpRunSummary[],
): ActivityCounts {
  let reasoningSteps = 0;
  let toolCalls = 0;
  const cliCount = cliRuns.length;
  const mcpCount = mcpRuns.length;
  const primaryCli = cliRuns[cliRuns.length - 1];
  const primaryCliName = primaryCli?.name;
  const primaryCliStatus = primaryCli?.status;
  const primaryMcp = mcpRuns[mcpRuns.length - 1];
  for (const m of messages) {
    if (isReasoningOnlyAssistant(m)) {
      reasoningSteps += 1;
      continue;
    }
    if (m.kind === "trace") {
      const lines = traceLines(m);
      for (const line of lines) {
        if (!isCliRunTraceLine(line) && !isMcpRunTraceLine(line)) {
          toolCalls += 1;
        }
      }
    }
  }
  let added = 0;
  let deleted = 0;
  let hasDiffStats = false;
  let hasEditingFiles = false;
  let failedFileCount = 0;
  let deletedFileCount = 0;
  let primaryFilePath: string | undefined;
  let primaryFileTooltipPath: string | undefined;
  for (const edit of fileEdits) {
    primaryFilePath = edit.path;
    primaryFileTooltipPath = edit.absolute_path || edit.path;
    if (edit.status === "editing") {
      hasEditingFiles = true;
    }
    if (edit.status === "error") {
      failedFileCount += 1;
    }
    if (edit.operation === "delete") {
      deletedFileCount += 1;
    }
    if (edit.status === "error" || edit.binary) {
      continue;
    }
    if (!hasVisibleDiffStats(edit)) {
      continue;
    }
    hasDiffStats = true;
    added += edit.added;
    deleted += edit.deleted;
  }
  return {
    reasoningSteps,
    toolCalls,
    cliCount,
    mcpCount,
    fileCount: fileEdits.length,
    added,
    deleted,
    hasDiffStats,
    hasEditingFiles,
    hasFailedFiles: fileEdits.length > 0 && failedFileCount === fileEdits.length,
    hasDeletedFiles: fileEdits.length > 0 && deletedFileCount === fileEdits.length,
    primaryFilePath,
    primaryFileTooltipPath,
    primaryCliName,
    primaryCliStatus,
    primaryMcpName: primaryMcp?.presetName,
    primaryMcpDisplayName: primaryMcp?.displayName,
    primaryMcpStatus: primaryMcp?.status,
  };
}

export function describeTraceLine(line: string): TraceDescription {
  const trimmed = line.trim();
  const functionMatch = /^([a-zA-Z0-9_.-]+)\((.*)\)$/.exec(trimmed);
  const name = functionMatch?.[1] ?? "";
  const args = functionMatch?.[2] ?? "";
  const parsedUrl = traceUrlFromArgs(args, trimmed);
  const webDetail = parsedUrl ? formatTraceUrl(parsedUrl) : "";
  const plainWebReadTrace =
    !!parsedUrl && /\b(fetch(?:ing|ed)?|read(?:ing)?|opened?|opening)\b/i.test(trimmed);
  if (/search/i.test(name)) {
    return { kind: "search", label: "Searching", detail: previewTraceDetail(args, trimmed) };
  }
  if (/fetch|read|open/i.test(name) || plainWebReadTrace) {
    return {
      kind: "tool",
      label: "Reading",
      detail: webDetail || previewTraceDetail(args, trimmed),
      url: parsedUrl?.href,
      host: parsedUrl ? displayHost(parsedUrl.hostname) : undefined,
    };
  }
  if (isShellTraceName(name)) {
    return {
      kind: "tool",
      label: "Shell",
      detail: previewShellTraceDetail(args, trimmed),
    };
  }
  if (name) {
    return { kind: "tool", label: "Using", detail: name };
  }
  if (/done|complete|success/i.test(trimmed)) {
    return { kind: "done", label: "Done", detail: trimmed };
  }
  return { kind: "trace", label: "Working", detail: trimmed };
}

function isShellTraceName(name: string): boolean {
  const compact = name.toLowerCase().split(".").pop() || name.toLowerCase();
  return new Set([
    "exec",
    "exec_command",
    "execute_command",
    "run_command",
    "run_shell",
    "shell",
    "terminal",
    "bash",
    "sh",
  ]).has(compact);
}

function previewShellTraceDetail(args: string, fallback: string): string {
  const command = shellCommandFromArgs(args) || fallback;
  return summarizeShellCommand(command);
}

function shellCommandFromArgs(args: string): string {
  const compactArgs = args.trim();
  if (!compactArgs) return "";
  try {
    const parsed = JSON.parse(compactArgs) as unknown;
    if (typeof parsed === "string") return parsed;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return "";
    const record = parsed as Record<string, unknown>;
    for (const key of ["command", "cmd", "script", "input"]) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) return value;
    }
  } catch {
    return compactArgs.replace(/^["']|["']$/g, "");
  }
  return "";
}

function summarizeShellCommand(command: string): string {
  const redacted = redactShellCommand(command.replace(/\r\n/g, "\n"));
  const lines = redacted
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const firstLine = compactShellPath(lines[0] || "command");
  const firstPreview = truncateMiddle(firstLine, 92);
  if (lines.length <= 1) return firstPreview;
  return `${firstPreview} · script, ${lines.length} lines`;
}

function redactShellCommand(command: string): string {
  return command
    .replace(/\b((?:[A-Z0-9_]*)(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)(?:[A-Z0-9_]*))=(?:"[^"]*"|'[^']*'|[^\s]+)/gi, "$1=••••")
    .replace(/\b(Bearer)\s+[A-Za-z0-9._~+/=-]+/gi, "$1 ••••")
    .replace(/(--(?:api-?key|token|secret|password)(?:=|\s+))(?:"[^"]*"|'[^']*'|[^\s]+)/gi, "$1••••")
    .replace(/([?&](?:api_?key|token|secret|password)=)[^&\s]+/gi, "$1••••");
}

function compactShellPath(value: string): string {
  return value
    .replace(/\/Users\/[^/\s"']+/g, "~")
    .replace(/\/private\/tmp\/[^\s"']+/g, "/tmp/…")
    .replace(/\/var\/folders\/[^\s"']+/g, "/var/folders/…");
}

function truncateMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  const head = Math.ceil((maxLength - 1) * 0.62);
  const tail = Math.floor((maxLength - 1) * 0.38);
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

function traceUrlFromArgs(args: string, fallback: string): URL | null {
  const candidates: string[] = [];
  const compactArgs = args.trim();
  if (compactArgs) {
    try {
      collectUrlCandidates(JSON.parse(compactArgs), candidates);
    } catch {
      candidates.push(compactArgs.replace(/^["']|["']$/g, ""));
    }
  }
  candidates.push(fallback);
  for (const candidate of candidates) {
    const url = parsePublicHttpUrl(candidate);
    if (url) return url;
    const embedded = candidate.match(/https?:\/\/[^\s"'<>),]+/i)?.[0];
    if (embedded) {
      const embeddedUrl = parsePublicHttpUrl(embedded);
      if (embeddedUrl) return embeddedUrl;
    }
  }
  return null;
}

function collectUrlCandidates(value: unknown, candidates: string[]) {
  if (typeof value === "string") {
    candidates.push(value);
    return;
  }
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 6)) collectUrlCandidates(item, candidates);
    return;
  }
  const record = value as Record<string, unknown>;
  for (const key of ["url", "uri", "href", "link"]) {
    if (typeof record[key] === "string") candidates.push(record[key]);
  }
}

function parsePublicHttpUrl(value: string): URL | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (isPrivateHostname(url.hostname)) return null;
    return url;
  } catch {
    return null;
  }
}

function isPrivateHostname(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!host || host === "localhost" || host.endsWith(".local")) return true;
  if (!host.includes(".") && !host.includes(":")) return true;
  const ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (ipv4) {
    const [, aText, bText] = ipv4;
    const a = Number(aText);
    const b = Number(bText);
    return (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168)
    );
  }
  return host === "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80:");
}

function displayHost(hostname: string): string {
  return hostname.replace(/^www\./i, "").toLowerCase();
}

function formatTraceUrl(url: URL): string {
  const host = displayHost(url.hostname);
  const path = url.pathname && url.pathname !== "/" ? url.pathname : "";
  return `${host}${path}`;
}

function previewTraceDetail(args: string, fallback: string): string {
  const compactArgs = args.trim();
  if (!compactArgs) return fallback;
  try {
    const parsed = JSON.parse(compactArgs) as unknown;
    const preview = previewMcpArgs(parsed);
    if (preview) return preview;
  } catch {
    // Keep the original trace text for non-JSON progress hints.
  }
  return compactArgs.replace(/^["']|["']$/g, "");
}
