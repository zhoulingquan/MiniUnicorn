import type {
  ContextUsagePayload,
  GoalStateWsPayload,
  ToolProgressEvent,
  UIFileEdit,
  UIMessage,
} from "@/lib/types";
import {
  mergeToolProgressEvents,
  mergeUniqueToolTraceLines,
  toolTraceLinesFromEvents,
} from "@/lib/tool-traces";

/** ID of the assistant message currently receiving deltas (cleared on ``stream_end``). */
export interface StreamBuffer {
  messageId: string;
}

export interface ActiveAssistantCursor {
  id: string;
  index: number;
}

export type PendingStreamEvent =
  | { kind: "delta"; text: string }
  | { kind: "reasoning"; text: string };

export const FILE_EDIT_TOOL_NAMES = new Set([
  "write_file",
  "edit_file",
  "apply_patch",
]);

export interface StreamState {
  chatId: string;
  messages: UIMessage[];
  streaming: boolean;
  runStartedAt: number | null;
  goalState: GoalStateWsPayload | undefined;
  contextUsage: ContextUsagePayload | null;
  cursor: ActiveAssistantCursor | null;
  buffer: StreamBuffer | null;
  closedStreamIds: ReadonlySet<string>;
  activitySegment: string | null;
  fileEditSegment: string | null;
  activitySegmentCounter: number;
  /** When true, the reducer ignores ``delta`` / ``reasoning_delta`` /
   *  ``reasoning_end`` / ``stream_end`` / ``tool_progress`` events until
   *  ``turn_end`` clears the flag. Set by ``assistant_message`` actions
   *  that carry media (the backend always emits a redundant text stream
   *  after media-bearing replies; suppression keeps the canonical message
   *  authoritative). */
  suppressStreamUntilTurnEnd: boolean;
}

export function createInitialStreamState(
  chatId: string,
  messages: UIMessage[] = [],
): StreamState {
  return {
    chatId,
    messages,
    streaming: false,
    runStartedAt: null,
    goalState: undefined,
    contextUsage: null,
    cursor: null,
    buffer: null,
    closedStreamIds: new Set<string>(),
    activitySegment: null,
    fileEditSegment: null,
    activitySegmentCounter: 0,
    suppressStreamUntilTurnEnd: false,
  };
}

/** Find a still-open streamed assistant turn. Closed stream segments stay
 * visible as streaming until ``turn_end`` for visual continuity, but they
 * must not receive later delta segments. */
export function findStreamingAssistantIndex(
  prev: UIMessage[],
  closedStreamIds: ReadonlySet<string>,
): number | null {
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const m = prev[i];
    if (m.kind === "trace") continue;
    if (m.role === "assistant" && m.isStreaming && !closedStreamIds.has(m.id))
      return i;
    if (m.role === "user") break;
  }
  return null;
}

/**
 * Append a reasoning chunk to the last open reasoning stream in ``prev``.
 *
 * Lookup rule: prefer the most recent assistant turn in the active UI tail.
 * Most providers emit reasoning before answer text, but some only expose
 * ``reasoning_content`` after the answer stream completes. In that post-hoc
 * case the reasoning still belongs to the same assistant turn and must
 * render above the answer, not as a new row below it.
 */
export function attachReasoningChunk(
  prev: UIMessage[],
  chunk: string,
  activitySegmentId?: string,
): UIMessage[] {
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const candidate = prev[i];
    if (candidate.role === "user") break;
    if (candidate.kind === "trace") break;
    if (candidate.role !== "assistant") continue;
    const segId = candidate.activitySegmentId ?? activitySegmentId;
    const hasAnswer = candidate.content.length > 0;
    if (
      candidate.reasoningStreaming
      || candidate.reasoning !== undefined
      || hasAnswer
      || candidate.isStreaming
    ) {
      const merged: UIMessage = {
        ...candidate,
        reasoning: (candidate.reasoning ?? "") + chunk,
        reasoningStreaming: true,
        ...(segId ? { activitySegmentId: segId } : {}),
      };
      return [...prev.slice(0, i), merged, ...prev.slice(i + 1)];
    }
    if (!hasAnswer && candidate.isStreaming) {
      const merged: UIMessage = {
        ...candidate,
        reasoning: chunk,
        reasoningStreaming: true,
        ...(segId ? { activitySegmentId: segId } : {}),
      };
      return [...prev.slice(0, i), merged, ...prev.slice(i + 1)];
    }
    break;
  }
  return [
    ...prev,
    {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      isStreaming: true,
      reasoning: chunk,
      reasoningStreaming: true,
      ...(activitySegmentId ? { activitySegmentId } : {}),
      createdAt: Date.now(),
    },
  ];
}

/**
 * Find the most recent assistant placeholder that an incoming answer
 * delta should adopt instead of spawning a parallel row.
 */
export function findActiveAssistantPlaceholderIndex(
  prev: UIMessage[],
): number | null {
  const last = prev[prev.length - 1];
  if (!last) return null;
  if (last.role !== "assistant" || last.kind === "trace") return null;
  if (last.content.length > 0) return null;
  if (!last.isStreaming) return null;
  return prev.length - 1;
}

export function replaceMessageAt(
  prev: UIMessage[],
  index: number,
  message: UIMessage,
): UIMessage[] {
  const next = prev.slice();
  next[index] = message;
  return next;
}

/** Close the active reasoning stream segment, if any. Idempotent. */
export function closeReasoningStream(prev: UIMessage[]): UIMessage[] {
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const candidate = prev[i];
    if (!candidate.reasoningStreaming) continue;
    const merged: UIMessage = { ...candidate, reasoningStreaming: false };
    return [...prev.slice(0, i), merged, ...prev.slice(i + 1)];
  }
  return prev;
}

export function isReasoningOnlyPlaceholder(message: UIMessage): boolean {
  return (
    message.role === "assistant"
    && message.kind !== "trace"
    && message.content.trim().length === 0
    && !!message.reasoning
    && !message.reasoningStreaming
    && !message.media?.length
  );
}

export function isToolTrace(message: UIMessage | undefined): boolean {
  return message?.kind === "trace";
}

export function pruneReasoningOnlyPlaceholders(prev: UIMessage[]): UIMessage[] {
  return prev.filter((message, index) => {
    if (!isReasoningOnlyPlaceholder(message)) return true;
    return isToolTrace(prev[index + 1]);
  });
}

export function stampLastAssistantLatency(
  prev: UIMessage[],
  latencyMs: number,
): UIMessage[] {
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const m = prev[i];
    if (m.role === "assistant" && m.kind !== "trace") {
      const merged: UIMessage = { ...m, latencyMs, isStreaming: false };
      return [...prev.slice(0, i), merged, ...prev.slice(i + 1)];
    }
  }
  return prev;
}

export function absorbCompleteAssistantMessage(
  prev: UIMessage[],
  message: Omit<UIMessage, "id" | "role" | "createdAt">,
): UIMessage[] {
  const last = prev[prev.length - 1];
  if (!last || !isReasoningOnlyPlaceholder(last)) {
    return [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: "assistant",
        createdAt: Date.now(),
        ...message,
      },
    ];
  }
  return [
    ...prev.slice(0, -1),
    {
      ...last,
      ...message,
      isStreaming: false,
      reasoningStreaming: false,
    },
  ];
}

export function fileEditKey(
  edit: Pick<UIFileEdit, "call_id" | "tool" | "path">,
): string {
  if (edit.call_id) return `${edit.call_id}|${edit.tool}`;
  return `${edit.tool}|${edit.path}`;
}

export function toolEventFileEditKey(event: ToolProgressEvent): string | null {
  const fn = (event as { function?: { name?: unknown } }).function;
  const name =
    typeof event.name === "string"
      ? event.name
      : typeof fn?.name === "string"
        ? fn.name
        : "";
  const callId = typeof event.call_id === "string" ? event.call_id : "";
  if (!name || !callId || !FILE_EDIT_TOOL_NAMES.has(name)) return null;
  return `${callId}|${name}`;
}

export function hasFileEditForToolEvent(
  messages: UIMessage[],
  event: ToolProgressEvent,
): boolean {
  const key = toolEventFileEditKey(event);
  if (!key) return false;
  return messages.some((message) =>
    message.fileEdits?.some((edit) => fileEditKey(edit) === key),
  );
}

export function filterCoveredFileEditToolEvents(
  messages: UIMessage[],
  events: ToolProgressEvent[],
): ToolProgressEvent[] {
  if (events.length === 0) return events;
  return events.filter((event) => !hasFileEditForToolEvent(messages, event));
}

export function stripCoveredFileEditToolHints(
  message: UIMessage,
  edits: UIFileEdit[],
): UIMessage {
  const incomingKeys = new Set(edits.map(fileEditKey));
  const events = message.toolEvents ?? [];
  if (!events.length || incomingKeys.size === 0) return message;

  const removedTraceLines = new Set<string>();
  const keptEvents: ToolProgressEvent[] = [];
  let changed = false;
  for (const event of events) {
    const key = toolEventFileEditKey(event);
    if (key && incomingKeys.has(key)) {
      changed = true;
      for (const line of toolTraceLinesFromEvents([event])) {
        removedTraceLines.add(line);
      }
      continue;
    }
    keptEvents.push(event);
  }
  if (!changed) return message;

  const previousTraces = message.traces?.length
    ? message.traces
    : message.content
      ? [message.content]
      : [];
  const nextTraces = previousTraces.filter(
    (line) => !removedTraceLines.has(line),
  );
  return {
    ...message,
    traces: nextTraces,
    content: nextTraces[nextTraces.length - 1] ?? "",
    toolEvents: keptEvents.length ? keptEvents : undefined,
  };
}

export function demoteInterruptedAssistantToActivity(
  prev: UIMessage[],
  segmentId: string,
): UIMessage[] {
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const message = prev[i];
    if (message.role === "user") break;
    if (
      message.role !== "assistant"
      || message.kind === "trace"
      || !message.isStreaming
      || !message.content.trim()
      || message.media?.length
    ) {
      continue;
    }
    const reasoning = [message.reasoning, message.content]
      .filter(
        (part): part is string => typeof part === "string" && part.trim().length > 0,
      )
      .join("\n\n");
    const demoted: UIMessage = {
      ...message,
      content: "",
      reasoning,
      reasoningStreaming: false,
      isStreaming: false,
      activitySegmentId: message.activitySegmentId ?? segmentId,
    };
    return replaceMessageAt(prev, i, demoted);
  }
  return prev;
}

export function normalizeFileEdit(edit: UIFileEdit): UIFileEdit | null {
  if (!edit || !edit.tool || (!edit.path && !edit.pending)) return null;
  const inferredStatus =
    edit.phase === "error"
      ? "error"
      : edit.phase === "end"
        ? "done"
        : "editing";
  const normalized: UIFileEdit = {
    ...edit,
    call_id: edit.call_id || `${edit.tool}:${edit.path}`,
    added: Number.isFinite(edit.added ?? 0)
      ? Math.max(0, Math.round(edit.added ?? 0))
      : 0,
    deleted: Number.isFinite(edit.deleted ?? 0)
      ? Math.max(0, Math.round(edit.deleted ?? 0))
      : 0,
    status:
      edit.status === "error" || edit.status === "done" || edit.status === "editing"
        ? edit.status
        : inferredStatus,
  };
  if (edit.pending && !edit.path) normalized.pending = true;
  return normalized;
}

export function mergeFileEdits(
  existing: UIFileEdit[] | undefined,
  incoming: UIFileEdit[],
): UIFileEdit[] {
  const next = [...(existing ?? [])];
  const indexByKey = new Map(
    next.map((edit, index) => [fileEditKey(edit), index]),
  );
  for (const raw of incoming) {
    const edit = normalizeFileEdit(raw);
    if (!edit) continue;
    const key = fileEditKey(edit);
    const existingIndex = indexByKey.get(key);
    if (existingIndex === undefined) {
      indexByKey.set(key, next.length);
      next.push(edit);
      continue;
    }
    const merged = { ...next[existingIndex], ...edit };
    if (edit.path && !edit.pending) delete merged.pending;
    next[existingIndex] = merged;
  }
  return next;
}

export function findFileEditTraceIndex(
  prev: UIMessage[],
  segmentId: string | null,
  incoming: UIFileEdit[],
): number | null {
  const incomingKeys = new Set(incoming.map(fileEditKey));
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const candidate = prev[i];
    if (candidate.role === "user") break;
    if (candidate.kind !== "trace") continue;
    if (segmentId && candidate.activitySegmentId === segmentId) return i;
    for (const existing of candidate.fileEdits ?? []) {
      if (incomingKeys.has(fileEditKey(existing))) return i;
    }
    for (const event of candidate.toolEvents ?? []) {
      const key = toolEventFileEditKey(event);
      if (key && incomingKeys.has(key)) return i;
    }
  }
  return null;
}

// Re-export trace helpers so the reducer can import everything from one place.
export {
  mergeToolProgressEvents,
  mergeUniqueToolTraceLines,
  toolTraceLinesFromEvents,
};
