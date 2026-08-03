import {
  mergeToolProgressEvents,
  mergeUniqueToolTraceLines,
  normalizeToolProgressEvents,
  toolTraceLinesFromEvents,
} from "@/lib/tool-traces";
import type {
  ContextUsagePayload,
  GoalStateWsPayload,
  OutboundCliAppMention,
  OutboundMcpPresetMention,
  UIImage,
  UIFileEdit,
  UIMediaAttachment,
  UIMessage,
} from "@/lib/types";
import {
  attachReasoningChunk,
  closeReasoningStream,
  findActiveAssistantPlaceholderIndex,
  findFileEditTraceIndex,
  findStreamingAssistantIndex,
  replaceMessageAt,
  pruneReasoningOnlyPlaceholders,
  stampLastAssistantLatency,
  absorbCompleteAssistantMessage,
  demoteInterruptedAssistantToActivity,
  filterCoveredFileEditToolEvents,
  stripCoveredFileEditToolHints,
  mergeFileEdits,
  type StreamState,
} from "@/hooks/stream-state";

export interface StreamActionBase {
  chatId?: string;
  receivedAt?: number;
}

export type StreamAction =
  | (StreamActionBase & { type: "delta"; text: string })
  | (StreamActionBase & { type: "reasoning_delta"; text: string })
  | (StreamActionBase & { type: "reasoning_end" })
  | (StreamActionBase & { type: "stream_end"; finalAnswerText?: string })
  | (StreamActionBase & {
      type: "turn_end";
      goalState?: GoalStateWsPayload;
      contextUsage?: ContextUsagePayload | null;
      latencyMs?: number;
    })
  | (StreamActionBase & { type: "goal_state"; goalState: GoalStateWsPayload })
  | (StreamActionBase & {
      type: "goal_status";
      status: string;
      startedAt?: number | null;
    })
  | (StreamActionBase & {
      type: "tool_progress";
      text: string;
      kind: "tool_hint" | "progress";
      toolEvents?: unknown;
    })
  | (StreamActionBase & { type: "reasoning_message"; text: string })
  | (StreamActionBase & {
      type: "assistant_message";
      text: string;
      media?: UIMediaAttachment[];
      latencyMs?: number;
    })
  | (StreamActionBase & { type: "file_edit"; edits: UIFileEdit[] })
  | {
      type: "session_switch";
      chatId: string;
      messages: UIMessage[];
      runStartedAt: number | null;
      goalState: GoalStateWsPayload | undefined;
      hasPendingToolCalls: boolean;
    }
  | (StreamActionBase & {
      type: "send_message";
      content: string;
      images?: UIImage[];
      cliApps?: OutboundCliAppMention[];
      mcpPresets?: OutboundMcpPresetMention[];
    })
  | (StreamActionBase & { type: "stop" });

// ---------------------------------------------------------------------------
// Internal state-level helpers (operate on StreamState, not just UIMessage[])
// ---------------------------------------------------------------------------

function ensureActivitySegment(state: StreamState): {
  state: StreamState;
  segmentId: string;
} {
  if (state.activitySegment) {
    return { state, segmentId: state.activitySegment };
  }
  const counter = state.activitySegmentCounter + 1;
  const segmentId = `activity-${counter}`;
  return {
    state: {
      ...state,
      activitySegment: segmentId,
      activitySegmentCounter: counter,
    },
    segmentId,
  };
}

function createDetachedSegment(state: StreamState): {
  state: StreamState;
  segmentId: string;
} {
  const counter = state.activitySegmentCounter + 1;
  const segmentId = `activity-${counter}`;
  return {
    state: { ...state, activitySegmentCounter: counter },
    segmentId,
  };
}

function clearActivitySegment(state: StreamState): StreamState {
  return { ...state, activitySegment: null, fileEditSegment: null };
}

function closeActiveAssistantStream(state: StreamState): StreamState {
  const closedId = state.buffer?.messageId ?? state.cursor?.id;
  if (!closedId) {
    return { ...state, buffer: null, cursor: null };
  }
  const closedStreamIds = new Set(state.closedStreamIds);
  closedStreamIds.add(closedId);
  return { ...state, buffer: null, cursor: null, closedStreamIds };
}

function appendAnswerDelta(state: StreamState, chunk: string): StreamState {
  let { messages, cursor, buffer } = state;
  const closedSet = new Set(state.closedStreamIds);

  let targetIndex: number | null = null;

  if (cursor) {
    const cursorId = cursor.id;
    const indexed = messages[cursor.index];
    if (
      indexed?.id === cursorId
      && indexed.role === "assistant"
      && indexed.kind !== "trace"
      && indexed.isStreaming
    ) {
      targetIndex = cursor.index;
    } else {
      const idx = messages.findIndex((m) => m.id === cursorId);
      if (
        idx !== -1
        && messages[idx].role === "assistant"
        && messages[idx].kind !== "trace"
        && messages[idx].isStreaming
      ) {
        targetIndex = idx;
        cursor = { id: cursorId, index: idx };
      } else {
        cursor = null;
      }
    }
  }

  if (targetIndex === null) {
    targetIndex = findActiveAssistantPlaceholderIndex(messages);
  }
  if (targetIndex === null) {
    targetIndex = findStreamingAssistantIndex(messages, closedSet);
  }
  if (targetIndex === null) {
    const id = crypto.randomUUID();
    messages = [
      ...messages,
      {
        id,
        role: "assistant",
        content: "",
        isStreaming: true,
        createdAt: Date.now(),
      },
    ];
    targetIndex = messages.length - 1;
  }

  const target = messages[targetIndex];
  const merged: UIMessage = {
    ...target,
    content: target.content + chunk,
    isStreaming: true,
  };
  closedSet.delete(merged.id);
  cursor = { id: merged.id, index: targetIndex };
  buffer = { messageId: merged.id };
  messages = replaceMessageAt(messages, targetIndex, merged);

  return { ...state, messages, cursor, buffer, closedStreamIds: closedSet };
}

function applyFinalAnswerText(
  state: StreamState,
  text: string,
): StreamState {
  let { messages, cursor } = state;
  const closedSet = new Set(state.closedStreamIds);

  let targetIndex: number | null = null;

  if (cursor) {
    const cursorId = cursor.id;
    const indexed = messages[cursor.index];
    if (
      indexed?.id === cursorId
      && indexed.role === "assistant"
      && indexed.kind !== "trace"
      && indexed.isStreaming
    ) {
      targetIndex = cursor.index;
    } else {
      const idx = messages.findIndex((m) => m.id === cursorId);
      if (
        idx !== -1
        && messages[idx].role === "assistant"
        && messages[idx].kind !== "trace"
        && messages[idx].isStreaming
      ) {
        targetIndex = idx;
        cursor = { id: cursorId, index: idx };
      } else {
        cursor = null;
      }
    }
  }

  if (targetIndex === null) {
    targetIndex = findStreamingAssistantIndex(messages, closedSet);
  }

  if (targetIndex !== null) {
    const target = messages[targetIndex];
    messages = replaceMessageAt(messages, targetIndex, {
      ...target,
      content: text,
      isStreaming: true,
    });
  } else {
    const id = crypto.randomUUID();
    closedSet.add(id);
    messages = [
      ...messages,
      {
        id,
        role: "assistant",
        content: text,
        isStreaming: true,
        createdAt: Date.now(),
      },
    ];
  }

  return { ...state, messages, cursor, closedStreamIds: closedSet };
}

function applyToolProgress(
  state: StreamState,
  action: {
    text: string;
    kind: "tool_hint" | "progress";
    toolEvents?: unknown;
  },
): StreamState {
  let next = state;
  const structuredEvents = normalizeToolProgressEvents(action.toolEvents);
  const { state: ensured, segmentId } = ensureActivitySegment(next);
  next = ensured;
  let messages = demoteInterruptedAssistantToActivity(next.messages, segmentId);
  const visibleStructuredEvents = filterCoveredFileEditToolEvents(
    messages,
    structuredEvents,
  );
  const structuredLines = toolTraceLinesFromEvents(visibleStructuredEvents);
  const lines =
    structuredLines.length > 0
      ? structuredLines
      : structuredEvents.length > 0
        ? []
        : action.text
          ? [action.text]
          : [];
  if (lines.length === 0) {
    return { ...next, messages };
  }
  const last = messages[messages.length - 1];
  if (
    last
    && last.kind === "trace"
    && !last.isStreaming
    && (!last.activitySegmentId || last.activitySegmentId === segmentId)
  ) {
    const previousTraces = last.traces?.length
      ? last.traces
      : last.content
        ? [last.content]
        : [];
    const mergedLines =
      visibleStructuredEvents.length > 0
        ? mergeUniqueToolTraceLines(previousTraces, structuredLines)
        : null;
    const merged: UIMessage = {
      ...last,
      traces: mergedLines ? mergedLines.traces : [...previousTraces, ...lines],
      content: mergedLines
        ? mergedLines.traces[mergedLines.traces.length - 1]
        : lines[lines.length - 1],
      toolEvents: visibleStructuredEvents.length
        ? mergeToolProgressEvents(last.toolEvents, visibleStructuredEvents)
        : last.toolEvents,
      activitySegmentId: last.activitySegmentId ?? segmentId,
    };
    messages = [...messages.slice(0, -1), merged];
  } else {
    messages = [
      ...messages,
      {
        id: crypto.randomUUID(),
        role: "tool" as const,
        kind: "trace" as const,
        content: lines[lines.length - 1],
        traces: lines,
        ...(visibleStructuredEvents.length
          ? { toolEvents: visibleStructuredEvents }
          : {}),
        activitySegmentId: segmentId,
        createdAt: Date.now(),
      },
    ];
  }
  return { ...next, messages };
}

function applyFileEdit(
  state: StreamState,
  action: { edits: UIFileEdit[] },
): StreamState {
  let next = state;
  const edits = Array.isArray(action.edits) ? action.edits : [];
  if (edits.length === 0) return next;
  const normalized = mergeFileEdits(undefined, edits);
  if (normalized.length === 0) return next;
  const opensFileEditPhase = normalized.some(
    (edit) => edit.status === "editing" || edit.phase === "start",
  );

  let eventSegmentId = next.fileEditSegment;
  if (!eventSegmentId && opensFileEditPhase) {
    const { state: detached, segmentId } = createDetachedSegment(next);
    next = { ...detached, fileEditSegment: segmentId };
    eventSegmentId = segmentId;
  }

  let segmentId = eventSegmentId;
  let messages = segmentId
    ? demoteInterruptedAssistantToActivity(next.messages, segmentId)
    : next.messages;
  const targetIndex = findFileEditTraceIndex(messages, segmentId, normalized);

  if (targetIndex !== null) {
    const target = messages[targetIndex];
    if (target.activitySegmentId) {
      segmentId = target.activitySegmentId;
    } else if (!segmentId) {
      const { state: detached, segmentId: newSeg } = createDetachedSegment(next);
      next = detached;
      segmentId = newSeg;
    }
    if (opensFileEditPhase) {
      next = { ...next, fileEditSegment: segmentId };
    }
    const cleanedTarget = stripCoveredFileEditToolHints(target, normalized);
    const merged: UIMessage = {
      ...cleanedTarget,
      fileEdits: mergeFileEdits(cleanedTarget.fileEdits, normalized),
      activitySegmentId: segmentId,
    };
    messages = replaceMessageAt(messages, targetIndex, merged);
  } else {
    if (!segmentId) {
      const { state: detached, segmentId: newSeg } = createDetachedSegment(next);
      next = detached;
      segmentId = newSeg;
    }
    if (opensFileEditPhase) {
      next = { ...next, fileEditSegment: segmentId };
    }
    messages = [
      ...messages,
      {
        id: crypto.randomUUID(),
        role: "tool" as const,
        kind: "trace" as const,
        content: "",
        traces: [],
        fileEdits: normalized,
        activitySegmentId: segmentId,
        createdAt: Date.now(),
      },
    ];
  }

  return { ...next, messages };
}

// ---------------------------------------------------------------------------
// Main reducer
// ---------------------------------------------------------------------------

export function reduceStream(
  state: StreamState,
  action: StreamAction,
): StreamState {
  switch (action.type) {
    case "delta": {
      if (state.suppressStreamUntilTurnEnd) return state;
      const chunk = action.text;
      if (!chunk) return state;
      let next = clearActivitySegment(state);
      next = { ...next, streaming: true };
      return appendAnswerDelta(next, chunk);
    }

    case "reasoning_delta": {
      if (state.suppressStreamUntilTurnEnd) return state;
      const chunk = action.text;
      if (!chunk) return state;
      let next = state;
      if (next.fileEditSegment) {
        next = clearActivitySegment(next);
      }
      const { state: ensured, segmentId } = ensureActivitySegment(next);
      next = {
        ...ensured,
        streaming: true,
        messages: attachReasoningChunk(ensured.messages, chunk, segmentId),
      };
      return next;
    }

    case "reasoning_end": {
      if (state.suppressStreamUntilTurnEnd) return state;
      return { ...state, messages: closeReasoningStream(state.messages) };
    }

    case "stream_end": {
      if (state.suppressStreamUntilTurnEnd) return state;
      let next = state;
      if (action.finalAnswerText !== undefined) {
        next = applyFinalAnswerText(next, action.finalAnswerText);
      }
      return closeActiveAssistantStream(next);
    }

    case "turn_end": {
      let next = state;
      if (action.goalState != null && typeof action.goalState === "object") {
        next = { ...next, goalState: action.goalState };
      }
      if (action.contextUsage) {
        next = { ...next, contextUsage: action.contextUsage };
      }
      next = { ...next, runStartedAt: null, streaming: false };
      let messages = next.messages.map((m) =>
        m.isStreaming ? { ...m, isStreaming: false } : m,
      );
      messages = pruneReasoningOnlyPlaceholders(messages);
      if (typeof action.latencyMs === "number" && action.latencyMs >= 0) {
        messages = stampLastAssistantLatency(messages, Math.round(action.latencyMs));
      }
      return {
        ...next,
        messages,
        buffer: null,
        cursor: null,
        activitySegment: null,
        fileEditSegment: null,
        closedStreamIds: new Set<string>(),
        suppressStreamUntilTurnEnd: false,
      };
    }

    case "goal_state": {
      return { ...state, goalState: action.goalState };
    }

    case "goal_status": {
      if (
        action.status === "running"
        && typeof action.startedAt === "number"
      ) {
        return { ...state, runStartedAt: action.startedAt };
      }
      return { ...state, runStartedAt: null };
    }

    case "tool_progress": {
      if (state.suppressStreamUntilTurnEnd) return state;
      return applyToolProgress(state, action);
    }

    case "reasoning_message": {
      if (state.suppressStreamUntilTurnEnd) return state;
      if (!action.text) return state;
      let next = state;
      if (next.fileEditSegment) {
        next = clearActivitySegment(next);
      }
      const { state: ensured, segmentId } = ensureActivitySegment(next);
      next = {
        ...ensured,
        messages: attachReasoningChunk(ensured.messages, action.text, segmentId),
      };
      return { ...next, messages: closeReasoningStream(next.messages) };
    }

    case "assistant_message": {
      const next = clearActivitySegment(state);
      const activeId = next.buffer?.messageId;
      const filtered = activeId
        ? next.messages.filter((m) => m.id !== activeId)
        : next.messages;
      const lat =
        typeof action.latencyMs === "number" && action.latencyMs >= 0
          ? Math.round(action.latencyMs)
          : undefined;
      const messages = absorbCompleteAssistantMessage(filtered, {
        content: action.text,
        ...(action.media && action.media.length > 0
          ? { media: action.media }
          : {}),
        ...(lat !== undefined ? { latencyMs: lat } : {}),
      });
      const hasMedia = !!action.media && action.media.length > 0;
      return {
        ...next,
        messages,
        buffer: null,
        cursor: null,
        suppressStreamUntilTurnEnd: hasMedia ? true : next.suppressStreamUntilTurnEnd,
      };
    }

    case "file_edit": {
      return applyFileEdit(state, action);
    }

    case "session_switch": {
      const initialStreaming =
        (action.messages.length > 0
          ? action.messages[action.messages.length - 1].kind === "trace"
          : false) || action.hasPendingToolCalls;
      return {
        chatId: action.chatId,
        messages: action.messages,
        streaming: initialStreaming,
        runStartedAt: action.runStartedAt,
        goalState: action.goalState,
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

    case "send_message": {
      const next = clearActivitySegment(state);
      const messages = [
        ...pruneReasoningOnlyPlaceholders(next.messages),
        {
          id: crypto.randomUUID(),
          role: "user" as const,
          content: action.content,
          createdAt: action.receivedAt ?? Date.now(),
          ...(action.images ? { images: action.images } : {}),
          ...(action.cliApps?.length ? { cliApps: action.cliApps } : {}),
          ...(action.mcpPresets?.length
            ? { mcpPresets: action.mcpPresets }
            : {}),
        },
      ];
      return {
        ...next,
        messages,
        streaming: true,
        buffer: null,
        cursor: null,
        closedStreamIds: new Set<string>(),
      };
    }

    case "stop": {
      const next = clearActivitySegment(state);
      const messages = next.messages.map((m) =>
        m.isStreaming ? { ...m, isStreaming: false } : m,
      );
      return {
        ...next,
        messages,
        streaming: false,
        buffer: null,
        cursor: null,
        closedStreamIds: new Set<string>(),
        suppressStreamUntilTurnEnd: false,
      };
    }

    default: {
      return state;
    }
  }
}
