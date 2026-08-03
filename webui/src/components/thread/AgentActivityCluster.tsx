import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronRight, CircleDashed } from "lucide-react";
import { useTranslation } from "react-i18next";

import { FileReferenceChip } from "@/components/FileReferenceChip";
import { MarkdownText, preloadMarkdownText } from "@/components/MarkdownText";
import { StreamingLabelSheen } from "@/components/MessageBubble";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";

import { ActivityTraceTimeline } from "@/components/thread/activity/ActivityTraceTimeline";
import { DiffPair, FileEditFlatActivity, FileEditGroup } from "@/components/thread/activity/FileEditGroup";
import {
  cliActivityManySummaryDefault,
  cliActivityManySummaryKey,
  cliActivitySummaryDefault,
  cliActivitySummaryKey,
  collectCliRuns,
} from "@/components/thread/activity/cli-runs";
import {
  collectFileEdits,
  fileActivityManySummaryKey,
  fileActivitySummaryKey,
  fileActivityVerb,
  messageHasOnlyFileActivity,
  shortFileName,
  summarizeFileEdits,
} from "@/components/thread/activity/file-edits";
import {
  collectMcpRuns,
  mcpActivityManySummaryDefault,
  mcpActivityManySummaryKey,
  mcpActivitySummaryDefault,
  mcpActivitySummaryKey,
} from "@/components/thread/activity/mcp-runs";
import { countActivity } from "@/components/thread/activity/trace-format";
import {
  activityDurationMs,
  formatActivityDuration,
  isReasoningOnlyAssistant,
} from "@/components/thread/activity/types";

export { isAgentActivityMember } from "@/components/thread/activity/types";

/** Scrollport height for the Cursor-style “live trace” strip (tailwind spacing). */
const CLUSTER_SCROLL_MAX_CLASS = "max-h-52";
const ACTIVITY_SCROLL_NEAR_BOTTOM_PX = 24;

interface AgentActivityClusterProps {
  messages: UIMessage[];
  /** True while the session turn is still running (drives “Working…” copy + header sheen). */
  isTurnStreaming: boolean;
  hasBodyBelow: boolean;
  /** Persisted end-to-end turn latency from the assistant answer, used for history replay. */
  turnLatencyMs?: number;
}

/**
 * Outer fold wrapping interleaved reasoning-only assistant rows and tool-trace rows.
 * Fixed max height with inner scroll; each block keeps its own small collapsible (reasoning / tools).
 */
export function AgentActivityCluster({
  messages,
  isTurnStreaming,
  hasBodyBelow,
  turnLatencyMs,
}: AgentActivityClusterProps) {
  const { t } = useTranslation();
  const fileEdits = useMemo(
    () => summarizeFileEdits(collectFileEdits(messages), isTurnStreaming),
    [messages, isTurnStreaming],
  );
  const cliRuns = useMemo(() => collectCliRuns(messages), [messages]);
  const mcpRuns = useMemo(() => collectMcpRuns(messages), [messages]);
  const {
    reasoningSteps,
    toolCalls,
    cliCount,
    mcpCount,
    fileCount,
    added,
    deleted,
    hasDiffStats,
    hasEditingFiles,
    hasFailedFiles,
    hasDeletedFiles,
    primaryFilePath,
    primaryFileTooltipPath,
    primaryCliName,
    primaryCliStatus,
    primaryMcpDisplayName,
    primaryMcpStatus,
  } = countActivity(messages, fileEdits, cliRuns, mcpRuns);
  const hasPendingFileEdit = fileEdits.some((edit) => edit.pending);

  const [userToggledOuter, setUserToggledOuter] = useState(false);
  const [outerOpenLocal, setOuterOpenLocal] = useState(false);
  const [completionHoldOpen, setCompletionHoldOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const activityScrollRef = useRef<HTMLDivElement>(null);
  const activityContentRef = useRef<HTMLDivElement>(null);
  const autoFollowActivityRef = useRef(true);
  const scrollFrameRef = useRef<number | null>(null);
  const wasTurnStreamingRef = useRef(isTurnStreaming);
  const wasTurnStreaming = wasTurnStreamingRef.current;
  /** Live work stays open; completed work briefly shows the done state, then tucks away. */
  const outerExpanded = userToggledOuter
    ? outerOpenLocal
    : isTurnStreaming || completionHoldOpen || (wasTurnStreaming && !isTurnStreaming);

  const hasLiveEditingFiles = isTurnStreaming && hasEditingFiles;
  const singleFilePath = fileCount === 1 ? primaryFilePath : undefined;
  const singleFileTooltipPath = fileCount === 1 ? primaryFileTooltipPath : undefined;
  const hasVisibleActivity = reasoningSteps > 0 || toolCalls > 0 || cliCount > 0 || mcpCount > 0 || fileCount > 0;
  const hasOnlyFileActivity = fileCount > 0 && messages.every(messageHasOnlyFileActivity);
  const durationMs = activityDurationMs(messages, isTurnStreaming, now, turnLatencyMs);
  const activityDuration = formatActivityDuration(durationMs);
  const thoughtLabel = isTurnStreaming
    ? t("message.activityThinkingFor", {
        duration: activityDuration,
        defaultValue: "Thinking for {{duration}}",
      })
    : durationMs <= 0
      ? t("message.activityThought", { defaultValue: "Thought" })
    : t("message.activityThoughtFor", {
        duration: activityDuration,
        defaultValue: "Thought for {{duration}}",
      });

  const fileActivitySummary = fileCount > 0
    ? hasPendingFileEdit && !singleFilePath
      ? t("message.fileActivityPreparing", { defaultValue: "Preparing edit…" })
      : singleFilePath
      ? t(fileActivitySummaryKey(hasLiveEditingFiles, hasFailedFiles, hasDeletedFiles), {
          file: shortFileName(singleFilePath),
          defaultValue: `${fileActivityVerb(hasLiveEditingFiles, hasFailedFiles, hasDeletedFiles)} {{file}}`,
        })
      : t(fileActivityManySummaryKey(hasLiveEditingFiles, hasFailedFiles, hasDeletedFiles), {
          count: fileCount,
          defaultValue: `${fileActivityVerb(hasLiveEditingFiles, hasFailedFiles, hasDeletedFiles)} {{count}} files`,
        })
    : "";

  const cliActivitySummary = cliCount > 0
    ? cliCount === 1 && primaryCliName
      ? t(cliActivitySummaryKey(primaryCliStatus, isTurnStreaming), {
          name: primaryCliName,
          defaultValue: cliActivitySummaryDefault(primaryCliStatus, isTurnStreaming),
        })
      : t(cliActivityManySummaryKey(cliRuns, isTurnStreaming), {
          count: cliCount,
          defaultValue: cliActivityManySummaryDefault(cliRuns, isTurnStreaming),
        })
    : "";

  const mcpActivitySummary = mcpCount > 0
    ? mcpCount === 1 && primaryMcpDisplayName
      ? t(mcpActivitySummaryKey(primaryMcpStatus, isTurnStreaming), {
          name: primaryMcpDisplayName,
          defaultValue: mcpActivitySummaryDefault(primaryMcpStatus, isTurnStreaming),
        })
      : t(mcpActivityManySummaryKey(mcpRuns, isTurnStreaming), {
          count: mcpCount,
          defaultValue: mcpActivityManySummaryDefault(mcpRuns, isTurnStreaming),
        })
    : "";

  const summary = fileCount > 0
    ? fileActivitySummary
    : cliCount > 0
      ? cliActivitySummary
    : mcpCount > 0
      ? mcpActivitySummary
    : isTurnStreaming
      ? reasoningSteps > 0
        ? t("message.agentActivityLiveSummary", {
            reasoning: reasoningSteps,
            tools: toolCalls,
            defaultValue: "Working… · {{reasoning}} steps · {{tools}} tool calls",
          })
        : toolCalls === 0 && fileCount > 0
          ? t("message.agentActivityLiveFilesOnly", { defaultValue: "Working…" })
        : t("message.agentActivityLiveToolsOnly", {
            tools: toolCalls,
            defaultValue: "Working… · {{tools}} tool calls",
          })
      : reasoningSteps > 0
        ? t("message.agentActivitySummary", {
            reasoning: reasoningSteps,
            tools: toolCalls,
            defaultValue: "{{reasoning}} steps · {{tools}} tool calls",
          })
        : toolCalls === 0 && fileCount > 0
          ? t("message.agentActivityFilesOnly", { defaultValue: "File changes" })
        : t("message.agentActivityToolsOnly", {
            tools: toolCalls,
            defaultValue: "{{tools}} tool calls",
          });

  const cancelActivityScrollFrame = useCallback(() => {
    if (scrollFrameRef.current !== null) {
      window.cancelAnimationFrame(scrollFrameRef.current);
      scrollFrameRef.current = null;
    }
  }, []);

  const scrollActivityToBottom = useCallback(() => {
    const el = activityScrollRef.current;
    if (!el) return;
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
  }, []);

  const scheduleActivityScrollToBottom = useCallback(() => {
    cancelActivityScrollFrame();
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      scrollActivityToBottom();
    });
  }, [cancelActivityScrollFrame, scrollActivityToBottom]);

  const toggleOuter = () => {
    const nextOpen = userToggledOuter ? !outerOpenLocal : !outerExpanded;
    if (nextOpen) {
      autoFollowActivityRef.current = true;
    }
    setUserToggledOuter(true);
    setOuterOpenLocal(nextOpen);
  };

  useLayoutEffect(() => {
    if (!outerExpanded || !autoFollowActivityRef.current) return;
    scheduleActivityScrollToBottom();
  }, [outerExpanded, messages, isTurnStreaming, scheduleActivityScrollToBottom]);

  useEffect(() => {
    if (!outerExpanded) {
      autoFollowActivityRef.current = true;
      return;
    }
    const target = activityContentRef.current;
    if (!target || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (autoFollowActivityRef.current) {
        scheduleActivityScrollToBottom();
      }
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [outerExpanded, scheduleActivityScrollToBottom]);

  useEffect(() => cancelActivityScrollFrame, [cancelActivityScrollFrame]);

  useEffect(() => {
    if (!isTurnStreaming) return undefined;
    const interval = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(interval);
  }, [isTurnStreaming]);

  useEffect(() => {
    const wasStreaming = wasTurnStreamingRef.current;
    wasTurnStreamingRef.current = isTurnStreaming;
    if (isTurnStreaming) {
      setCompletionHoldOpen(false);
      return undefined;
    }
    if (!wasStreaming || userToggledOuter) return undefined;
    setCompletionHoldOpen(true);
    const timeout = window.setTimeout(() => setCompletionHoldOpen(false), 900);
    return () => window.clearTimeout(timeout);
  }, [isTurnStreaming, userToggledOuter]);

  const onActivityScroll = useCallback(() => {
    const el = activityScrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    autoFollowActivityRef.current = distance < ACTIVITY_SCROLL_NEAR_BOTTOM_PX;
  }, []);

  if (!hasVisibleActivity) return null;

  if (hasOnlyFileActivity) {
    return (
      <FileEditFlatActivity
        edits={fileEdits}
        active={isTurnStreaming}
        hasBodyBelow={hasBodyBelow}
        summary={summary}
        singleFilePath={singleFilePath}
        singleFileTooltipPath={singleFileTooltipPath}
        hasLiveEditingFiles={hasLiveEditingFiles}
        hasFailedFiles={hasFailedFiles}
        hasDeletedFiles={hasDeletedFiles}
        added={added}
        deleted={deleted}
        hasDiffStats={hasDiffStats}
      />
    );
  }

  return (
    <div className={cn("w-full", hasBodyBelow && "mb-2")}>
      <button
        type="button"
        onClick={toggleOuter}
        className={cn(
          "group flex max-w-full items-center gap-1.5 rounded-md px-1 py-1",
          "text-[12.5px] text-muted-foreground/72 transition-colors hover:text-muted-foreground",
        )}
        aria-expanded={outerExpanded}
        aria-label={summary}
      >
        <StreamingLabelSheen
          active={isTurnStreaming}
          className="min-w-0"
        >
          {singleFilePath ? fileActivityVerb(hasLiveEditingFiles, hasFailedFiles, hasDeletedFiles) : thoughtLabel}
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
        <span className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-left">
          {fileCount > 0 && hasDiffStats && (
            <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground/85">
              <DiffPair added={added} deleted={deleted} />
            </span>
          )}
        </span>
        <ChevronRight
          aria-hidden
          className={cn(
            "h-3.5 w-3.5 shrink-0 transition-transform duration-200",
            outerExpanded && "rotate-90",
          )}
        />
      </button>

      {outerExpanded && (
        <div
          className={cn(
            "ml-2 mt-1 overflow-hidden border-l border-muted-foreground/14 pl-4",
          )}
        >
          <div
            ref={activityScrollRef}
            data-testid="agent-activity-scroll"
            onScroll={onActivityScroll}
            className={cn(
              CLUSTER_SCROLL_MAX_CLASS,
              "overflow-y-auto py-1 pr-1 scrollbar-thin scrollbar-track-transparent",
            )}
          >
            <div ref={activityContentRef} className="flex flex-col gap-1.5">
              {messages.map((m) => {
                if (isReasoningOnlyAssistant(m)) {
                  return (
                    <ActivityReasoningRow
                      key={m.id}
                      text={m.reasoning ?? ""}
                      streaming={isTurnStreaming && !!m.reasoningStreaming}
                    />
                  );
                }
                if (m.kind === "trace") {
                  return (
                    <ActivityTraceTimeline
                      key={m.id}
                      message={m}
                      active={isTurnStreaming}
                    />
                  );
                }
                return null;
              })}
              {fileEdits.length ? <FileEditGroup edits={fileEdits} /> : null}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ActivityReasoningRow({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  const { t } = useTranslation();
  useEffect(() => {
    if (text.length > 0) preloadMarkdownText();
  }, [text.length]);
  return (
    <div className="min-w-0 py-0.5">
      <div className="flex min-w-0 items-center gap-2 text-[13px] leading-5 text-muted-foreground/78">
        <ReasoningMarker streaming={streaming} />
        <StreamingLabelSheen active={streaming} className="min-w-0 font-medium">
          {streaming
            ? t("message.reasoningStreaming", { defaultValue: "Thinking…" })
            : t("message.reasoning", { defaultValue: "Thinking" })}
        </StreamingLabelSheen>
      </div>
      {text.trim() ? (
        <MarkdownText
          streaming={streaming}
          className={cn(
            "mt-1 min-w-0 pl-5 text-[12.5px] italic text-muted-foreground/78",
            "prose-p:my-1 prose-li:my-0.5",
            "prose-headings:mt-2 prose-headings:mb-1 prose-headings:font-medium",
            "prose-headings:text-muted-foreground/88 prose-strong:text-muted-foreground",
            "prose-h1:text-[15px] prose-h2:text-[13.5px] prose-h3:text-[12.5px] prose-h4:text-[12px]",
            "prose-a:text-muted-foreground/95 prose-a:underline hover:prose-a:opacity-90",
            "prose-code:text-[0.92em]",
          )}
        >
          {text}
        </MarkdownText>
      ) : null}
    </div>
  );
}

function ReasoningMarker({ streaming }: { streaming: boolean }) {
  const wasStreamingRef = useRef(streaming);
  const [justCompleted, setJustCompleted] = useState(false);

  useEffect(() => {
    if (wasStreamingRef.current && !streaming) {
      setJustCompleted(true);
      const timeout = window.setTimeout(() => setJustCompleted(false), 650);
      wasStreamingRef.current = streaming;
      return () => window.clearTimeout(timeout);
    }
    wasStreamingRef.current = streaming;
    return undefined;
  }, [streaming]);

  if (streaming) {
    return (
      <CircleDashed
        data-testid="activity-reasoning-marker"
        data-state="thinking"
        className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground"
        strokeWidth={1.8}
        aria-hidden
      />
    );
  }
  return (
    <span
      data-testid="activity-reasoning-marker"
      data-state="done"
      className={cn(
        "grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border border-emerald-500/28 text-emerald-500/78",
        "bg-emerald-500/[0.035] transition-[border-color,background-color,box-shadow,transform] duration-300 ease-out",
        justCompleted
          && "animate-in fade-in-0 zoom-in-75 shadow-[0_0_0_3px_rgba(16,185,129,0.10)] motion-reduce:animate-none",
      )}
      aria-hidden
    >
      <Check
        className={cn(
          "h-2.5 w-2.5 stroke-[2.4]",
          justCompleted && "animate-in fade-in-0 zoom-in-50 duration-300 motion-reduce:animate-none",
        )}
      />
    </span>
  );
}
