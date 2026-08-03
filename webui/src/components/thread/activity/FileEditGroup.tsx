import { AlertCircle, CheckCircle2, CircleDashed } from "lucide-react";
import { useTranslation } from "react-i18next";

import { FileReferenceChip } from "@/components/FileReferenceChip";
import { StreamingLabelSheen } from "@/components/MessageBubble";
import { cn } from "@/lib/utils";

import { AnimatedNumber } from "@/components/thread/activity/AnimatedNumber";
import {
  fileActivityVerb,
  formatFileEditError,
  hasVisibleDiffStats,
} from "@/components/thread/activity/file-edits";
import type { FileEditSummary } from "@/components/thread/activity/types";

export function FileEditGroup({ edits }: { edits: FileEditSummary[] }) {
  if (edits.length === 0) return null;
  return (
    <ul className="space-y-1">
      {edits.map((edit) => (
        <FileEditRow key={edit.key} edit={edit} />
      ))}
    </ul>
  );
}

function FileEditRow({ edit }: { edit: FileEditSummary }) {
  const { t } = useTranslation();
  const editing = edit.status === "editing";
  const failed = edit.status === "error";
  const hasCountedDiff = !failed && !edit.binary && hasVisibleDiffStats(edit);
  const failureDetail = failed
    ? formatFileEditError(edit.error)
      || t("message.fileEditFailedFallback", { defaultValue: "File change was not applied." })
    : "";
  return (
    <li
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-0.5 text-xs"
      title={failureDetail || edit.absolute_path || edit.path}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="grid h-5 w-5 shrink-0 place-items-center text-muted-foreground/50">
          {failed ? (
            <AlertCircle className="h-3.5 w-3.5 text-destructive/75" aria-hidden />
          ) : editing ? (
            <CircleDashed className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500/75" aria-hidden />
          )}
        </span>
        {edit.pending && !edit.path ? (
          <StreamingLabelSheen
            active={editing}
            className="min-w-0 text-[12px] font-medium text-muted-foreground"
          >
            {t("message.fileEditPreparing", { defaultValue: "Preparing file edit…" })}
          </StreamingLabelSheen>
        ) : (
          <FileReferenceChip
            path={edit.path}
            tooltipPath={edit.absolute_path ?? undefined}
            display="path"
            active={editing}
            className="min-w-0"
            textClassName="text-[12px]"
            testId="activity-file-reference"
          />
        )}
        {failed ? (
          <span className="min-w-0 truncate text-[11px] leading-4 text-destructive/75">
            {failureDetail}
          </span>
        ) : null}
      </div>
      {hasCountedDiff ? (
        <DiffPair added={edit.added} deleted={edit.deleted} />
      ) : null}
    </li>
  );
}

export function DiffPair({ added, deleted }: { added: number; deleted: number }) {
  return (
    <span
      className="inline-flex shrink-0 items-baseline gap-1.5 leading-[inherit] tabular-nums"
      data-testid="activity-diff-pair"
    >
      <DiffValue
        sign="+"
        value={added}
        className="text-emerald-600/75 dark:text-emerald-300/75"
      />
      <DiffValue
        sign="-"
        value={deleted}
        className="text-rose-600/70 dark:text-rose-300/75"
      />
    </span>
  );
}

function DiffValue({ sign, value, className }: { sign: string; value: number; className: string }) {
  const safeValue = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
  return (
    <span
      className={cn("inline-flex items-baseline leading-[inherit]", className)}
      aria-label={`${sign}${safeValue}`}
    >
      <span className="inline-flex items-baseline leading-none" aria-hidden>
        {sign}
        <AnimatedNumber value={safeValue} />
      </span>
      <span className="sr-only">{sign}{safeValue}</span>
    </span>
  );
}

export function FileEditFlatActivity({
  edits,
  active,
  hasBodyBelow,
  summary,
  singleFilePath,
  singleFileTooltipPath,
  hasLiveEditingFiles,
  hasFailedFiles,
  hasDeletedFiles,
  added,
  deleted,
  hasDiffStats,
}: {
  edits: FileEditSummary[];
  active: boolean;
  hasBodyBelow: boolean;
  summary: string;
  singleFilePath?: string;
  singleFileTooltipPath?: string;
  hasLiveEditingFiles: boolean;
  hasFailedFiles: boolean;
  hasDeletedFiles: boolean;
  added: number;
  deleted: number;
  hasDiffStats: boolean;
}) {
  const showRows = edits.length > 1 || edits.some((edit) => edit.status === "error" || edit.pending);
  return (
    <div className={cn("w-full", hasBodyBelow && "mb-2")} aria-label={summary}>
      <div
        className={cn(
          "flex max-w-full items-center gap-1.5 px-1 py-1",
          "text-[12.5px] text-muted-foreground/72",
        )}
      >
        <StreamingLabelSheen active={active} className="min-w-0">
          {singleFilePath
            ? fileActivityVerb(hasLiveEditingFiles, hasFailedFiles, hasDeletedFiles)
            : summary}
        </StreamingLabelSheen>
        {singleFilePath ? (
          <FileReferenceChip
            path={singleFilePath}
            tooltipPath={singleFileTooltipPath}
            active={hasLiveEditingFiles}
            className="-my-0.5 min-w-0"
            textClassName="text-xs"
            testId="activity-header-file-reference"
          />
        ) : null}
        {hasDiffStats ? (
          <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground/85">
            <DiffPair added={added} deleted={deleted} />
          </span>
        ) : null}
      </div>
      {showRows ? (
        <div className="mt-0.5 pl-4">
          <FileEditGroup edits={edits} />
        </div>
      ) : null}
    </div>
  );
}
