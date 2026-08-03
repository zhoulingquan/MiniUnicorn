import { type ReactNode } from "react";
import {
  CheckCircle2,
  Layers,
  Search,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import { faviconUrls } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";

import { CliRunGroup } from "@/components/thread/activity/CliRunGroup";
import { McpRunGroup } from "@/components/thread/activity/McpRunGroup";
import {
  cliRunMapByTraceLine,
  parseCliRunTrace,
} from "@/components/thread/activity/cli-runs";
import {
  mcpRunMapByTraceLine,
  parseMcpRunTrace,
} from "@/components/thread/activity/mcp-runs";
import { describeTraceLine } from "@/components/thread/activity/trace-format";
import { traceLines } from "@/components/thread/activity/types";
import type { TraceDescription } from "@/components/thread/activity/types";

function ActivityTraceList({
  lines,
  active,
}: {
  lines: string[];
  active: boolean;
}) {
  return (
    <ul className="space-y-1">
      {lines.map((line, index) => (
        <ActivityTraceRow
          key={`${line}-${index}`}
          line={line}
          active={active && index === lines.length - 1}
        />
      ))}
    </ul>
  );
}

export function ActivityTraceTimeline({
  message,
  active,
}: {
  message: UIMessage;
  active: boolean;
}) {
  const lines = traceLines(message);
  const cliRunsByLine = cliRunMapByTraceLine(message);
  const mcpRunsByLine = mcpRunMapByTraceLine(message);
  const renderedRunKeys = new Set<string>();
  const items: ReactNode[] = [];
  let normalLines: string[] = [];

  const flushNormalLines = (suffix: string) => {
    if (!normalLines.length) return;
    items.push(
      <ActivityTraceList
        key={`${message.id}:trace:${suffix}`}
        lines={normalLines}
        active={active}
      />,
    );
    normalLines = [];
  };

  lines.forEach((line, index) => {
    const cliRun = cliRunsByLine.get(line) ?? parseCliRunTrace(line);
    if (cliRun) {
      flushNormalLines(String(index));
      renderedRunKeys.add(cliRun.key);
      items.push(
        <CliRunGroup
          key={`${message.id}:cli:${cliRun.key}:${index}`}
          runs={[cliRun]}
          active={active}
        />,
      );
      return;
    }

    const mcpRun = mcpRunsByLine.get(line) ?? parseMcpRunTrace(line);
    if (mcpRun) {
      flushNormalLines(String(index));
      renderedRunKeys.add(mcpRun.key);
      items.push(
        <McpRunGroup
          key={`${message.id}:mcp:${mcpRun.key}:${index}`}
          runs={[mcpRun]}
          active={active}
        />,
      );
      return;
    }

    normalLines.push(line);
  });

  flushNormalLines("tail");

  for (const run of cliRunsByLine.values()) {
    if (renderedRunKeys.has(run.key)) continue;
    items.push(
      <CliRunGroup
        key={`${message.id}:cli:${run.key}:event`}
        runs={[run]}
        active={active}
      />,
    );
  }
  for (const run of mcpRunsByLine.values()) {
    if (renderedRunKeys.has(run.key)) continue;
    items.push(
      <McpRunGroup
        key={`${message.id}:mcp:${run.key}:event`}
        runs={[run]}
        active={active}
      />,
    );
  }

  return items.length ? <>{items}</> : null;
}

function ActivityTraceRow({ line, active }: { line: string; active: boolean }) {
  const trace = describeTraceLine(line);
  const Icon = trace.kind === "search"
    ? Search
    : trace.kind === "done"
      ? CheckCircle2
      : trace.kind === "tool"
        ? Wrench
        : Layers;
  return (
    <li className="flex min-w-0 items-start gap-2 py-0.5 text-[13px] leading-5">
      <TraceIconMark trace={trace} fallbackIcon={Icon} active={active} />
      <span className="min-w-0 flex-1">
        <span className="font-medium text-muted-foreground/85">{trace.label}</span>
        {trace.detail ? (
          <>
            <span className="text-muted-foreground/55"> </span>
            <span className="break-words text-foreground/82">{trace.detail}</span>
          </>
        ) : null}
      </span>
    </li>
  );
}

function TraceIconMark({
  trace,
  fallbackIcon: FallbackIcon,
  active,
}: {
  trace: TraceDescription;
  fallbackIcon: LucideIcon;
  active: boolean;
}) {
  const [faviconIndex, setFaviconIndex] = useState(0);
  const faviconUrl = trace.host ? faviconUrls(trace.host)[faviconIndex] : undefined;

  useEffect(() => setFaviconIndex(0), [trace.host]);

  if (trace.url && trace.host && faviconUrl) {
    return (
      <span
        data-testid={`activity-web-favicon-${trace.host}`}
        className={cn(
          "mt-0.5 grid h-4 w-4 shrink-0 place-items-center overflow-hidden rounded-[4px] border border-border/45 bg-background shadow-[inset_0_0_0_1px_rgba(0,0,0,0.02)]",
          active && "animate-pulse",
        )}
        aria-hidden
      >
        <img
          src={faviconUrl}
          alt=""
          className="h-3.5 w-3.5 object-contain"
          onError={() => setFaviconIndex((index) => index + 1)}
        />
      </span>
    );
  }

  return (
    <FallbackIcon
      className={cn(
        "mt-0.5 h-3.5 w-3.5 shrink-0",
        trace.kind === "done"
          ? "text-emerald-500/75"
          : active
            ? "text-muted-foreground/75"
            : "text-muted-foreground/45",
      )}
      aria-hidden
    />
  );
}
