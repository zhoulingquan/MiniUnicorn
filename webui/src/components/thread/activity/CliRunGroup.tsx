import { AlertCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { StreamingLabelSheen } from "@/components/MessageBubble";

import {
  cliRunLabelDefault,
  cliRunLabelKey,
  formatCliArgs,
} from "@/components/thread/activity/cli-runs";
import type { CliRunSummary } from "@/components/thread/activity/types";

export function CliRunGroup({
  runs,
  active,
}: {
  runs: CliRunSummary[];
  active: boolean;
}) {
  if (runs.length === 0) return null;
  return (
    <ul className="space-y-1" data-testid="activity-cli-runs">
      {runs.map((run) => (
        <CliRunRow
          key={run.key}
          run={run}
          active={active}
        />
      ))}
    </ul>
  );
}

function CliRunRow({ run, active }: { run: CliRunSummary; active: boolean }) {
  const { t } = useTranslation();
  const args = formatCliArgs(run);
  const failed = run.status === "error";
  const rowActive = active && run.status === "running";
  const label = t(cliRunLabelKey(run, active), {
    defaultValue: cliRunLabelDefault(run, active),
  });

  return (
    <li
      className="flex min-w-0 items-center gap-2 py-0.5 text-[13px] leading-5"
      title={`${label} @${run.name}${args ? ` ${args}` : ""}${run.error ? ` ${run.error}` : ""}`}
    >
      <span className="flex min-w-0 flex-1 items-baseline gap-1.5">
        <StreamingLabelSheen active={rowActive} className="shrink-0 font-medium text-muted-foreground/85">
          {label}
        </StreamingLabelSheen>
        <span className="max-w-[11rem] shrink-0 truncate font-mono text-[12.5px] font-semibold text-foreground/90">
          @{run.name}
        </span>
        {failed ? (
          <AlertCircle className="h-3 w-3 shrink-0 translate-y-[0.16em] text-destructive/75" aria-hidden />
        ) : null}
        {args ? (
          <>
            <span className="shrink-0 text-muted-foreground/36">·</span>
            <span className="min-w-0 truncate font-mono text-[12px] text-muted-foreground/72">
              {args}
            </span>
          </>
        ) : null}
        {run.error ? (
          <>
            <span className="shrink-0 text-muted-foreground/30">·</span>
            <span className="min-w-0 truncate text-[12px] text-destructive/72">
              {run.error}
            </span>
          </>
        ) : null}
        {run.workingDir && !run.error ? (
          <>
            <span className="shrink-0 text-muted-foreground/30">·</span>
            <span className="min-w-0 truncate text-[12px] text-muted-foreground/55">
              {run.workingDir}
            </span>
          </>
        ) : null}
      </span>
    </li>
  );
}
