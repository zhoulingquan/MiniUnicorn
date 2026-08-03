import { AlertCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { StreamingLabelSheen } from "@/components/MessageBubble";

import {
  mcpRunLabelDefault,
  mcpRunLabelKey,
} from "@/components/thread/activity/mcp-runs";
import type { McpRunSummary } from "@/components/thread/activity/types";

export function McpRunGroup({
  runs,
  active,
}: {
  runs: McpRunSummary[];
  active: boolean;
}) {
  if (runs.length === 0) return null;
  return (
    <ul className="space-y-1" data-testid="activity-mcp-runs">
      {runs.map((run) => (
        <McpRunRow
          key={run.key}
          run={run}
          active={active}
        />
      ))}
    </ul>
  );
}

function McpRunRow({ run, active }: { run: McpRunSummary; active: boolean }) {
  const { t } = useTranslation();
  const failed = run.status === "error";
  const rowActive = active && run.status === "running";
  const displayName = run.displayName;
  const label = t(mcpRunLabelKey(run, active), {
    defaultValue: mcpRunLabelDefault(run, active),
  });

  return (
    <li
      className="flex min-w-0 items-center gap-2 py-0.5 text-[13px] leading-5"
      title={`${label} ${displayName} ${run.toolName}${run.argsPreview ? ` ${run.argsPreview}` : ""}${run.error ? ` ${run.error}` : ""}`}
    >
      <span className="flex min-w-0 flex-1 items-baseline gap-1.5">
        <StreamingLabelSheen active={rowActive} className="shrink-0 font-medium text-muted-foreground/85">
          {label}
        </StreamingLabelSheen>
        <span className="max-w-[12rem] shrink-0 truncate text-[12.5px] font-semibold text-foreground/90">
          {displayName}
        </span>
        {failed ? (
          <AlertCircle className="h-3 w-3 shrink-0 translate-y-[0.16em] text-destructive/75" aria-hidden />
        ) : null}
        <span className="shrink-0 text-muted-foreground/36">·</span>
        <span className="min-w-0 truncate font-mono text-[12px] text-muted-foreground/72">
          {run.toolName}
          {run.argsPreview ? ` · ${run.argsPreview}` : ""}
        </span>
        {run.error ? (
          <>
            <span className="shrink-0 text-muted-foreground/30">·</span>
            <span className="min-w-0 truncate text-[12px] text-destructive/72">
              {run.error}
            </span>
          </>
        ) : null}
      </span>
    </li>
  );
}
