import { useCallback, useEffect, useRef, useState } from "react";

import { reduceStream, type StreamAction } from "@/hooks/stream-reducer";
import { useClient } from "@/providers/ClientProvider";
import {
  createInitialStreamState,
  type StreamState,
} from "@/hooks/stream-state";
import { toMediaAttachment } from "@/lib/media";
import type { StreamError } from "@/lib/miniunicorn-client";
import type {
  ContextUsagePayload,
  GoalStateWsPayload,
  InboundEvent,
  OutboundCliAppMention,
  OutboundMcpPresetMention,
  OutboundMedia,
  UIImage,
  UIMessage,
  WorkspaceScopePayload,
} from "@/lib/types";

/**
 * Subscribe to a chat by ID. Returns the in-memory message list for the chat,
 * a streaming flag, and a ``send`` function. Initial history must be seeded
 * separately (e.g. via ``fetchWebuiThread``) since the server only replays
 * live events.
 */
/** Payload passed to ``send`` when the user attaches one or more images.
 *
 * ``media`` is handed to the wire client verbatim; ``preview`` powers the
 * optimistic user bubble (blob URLs so the preview appears before the server
 * acks the frame). Keeping the two separate lets the bubble re-use the local
 * blob URL even after the server persists the file under a different name. */
export interface SendImage {
  media: OutboundMedia;
  preview: UIImage;
}

export interface SendOptions {
  cliApps?: OutboundCliAppMention[];
  mcpPresets?: OutboundMcpPresetMention[];
  workspaceScope?: WorkspaceScopePayload | null;
  /** When set, the backend routes this user turn to the matching subagent. */
  agentId?: string;
}

export function useMiniunicornStream(
  chatId: string | null,
  initialMessages: UIMessage[] = [],
  hasPendingToolCalls = false,
  onTurnEnd?: () => void,
): {
  messages: UIMessage[];
  isStreaming: boolean;
  /** Unix epoch seconds when the current user turn started (WebSocket ``goal_status``). */
  runStartedAt: number | null;
  /** Latest sustained goal for this ``chatId`` (``goal_state`` WS events). */
  goalState: GoalStateWsPayload | undefined;
  /** Token usage from the last LLM call of the most recent turn (``turn_end``). */
  contextUsage: ContextUsagePayload | null;
  send: (content: string, images?: SendImage[], options?: SendOptions) => void;
  stop: () => void;
  setMessages: React.Dispatch<React.SetStateAction<UIMessage[]>>;
  /** Latest transport-level fault raised since the last ``dismissStreamError``.
   * ``null`` when there is nothing to show. */
  streamError: StreamError | null;
  /** Clear the current ``streamError`` (e.g. after the user dismisses the
   * notification or starts a fresh action). */
  dismissStreamError: () => void;
} {
  const { client } = useClient();
  const [state, setState] = useState<StreamState>(() =>
    createInitialStreamState(chatId ?? "", initialMessages),
  );
  const [streamError, setStreamError] = useState<StreamError | null>(null);
  /** Buffered ``delta`` / ``reasoning_delta`` actions waiting for the next
   * animation frame. Batching keeps rapid token streams from triggering a
   * React re-render per chunk. */
  const pendingActionsRef = useRef<StreamAction[]>([]);
  const streamFrameRef = useRef<number | null>(null);
  /** Timer that defers ``isStreaming = false`` after ``stream_end``.
   *
   * When the model finishes a text segment and calls a tool, the server
   * sends ``stream_end`` but the agent is still "thinking" while the tool
   * executes.  By deferring the flag reset by a short window (1 s) we keep
   * the loading spinner alive across tool-call boundaries without needing
   * backend changes. */
  const streamEndTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return client.onError((err) => setStreamError(err));
  }, [client]);

  const dismissStreamError = useCallback(() => setStreamError(null), []);

  const clearPendingStreamWork = useCallback(() => {
    if (streamFrameRef.current !== null) {
      window.cancelAnimationFrame(streamFrameRef.current);
      streamFrameRef.current = null;
    }
    pendingActionsRef.current = [];
  }, []);

  const schedulePendingFlush = useCallback(() => {
    if (streamFrameRef.current !== null) return;
    streamFrameRef.current = window.requestAnimationFrame(() => {
      streamFrameRef.current = null;
      const actions = pendingActionsRef.current;
      if (actions.length === 0) return;
      pendingActionsRef.current = [];
      setState((prev) => actions.reduce(reduceStream, prev));
    });
  }, []);

  const flushPending = useCallback(() => {
    if (streamFrameRef.current !== null) {
      window.cancelAnimationFrame(streamFrameRef.current);
      streamFrameRef.current = null;
    }
    const actions = pendingActionsRef.current;
    if (actions.length === 0) return;
    pendingActionsRef.current = [];
    setState((prev) => actions.reduce(reduceStream, prev));
  }, []);

  const dispatch = useCallback((action: StreamAction) => {
    setState((prev) => reduceStream(prev, action));
  }, []);

  const setMessages = useCallback<React.Dispatch<React.SetStateAction<UIMessage[]>>>(
    (updater) => {
      setState((prev) => ({
        ...prev,
        messages:
          typeof updater === "function"
            ? (updater as (m: UIMessage[]) => UIMessage[])(prev.messages)
            : updater,
      }));
    },
    [],
  );

  // Reset local state when switching chats. Do not reset on every
  // ``initialMessages`` update: a brand-new chat can receive an empty/404
  // history response after the optimistic first message has already rendered.
  useEffect(() => {
    setState((prev) =>
      reduceStream(prev, {
        type: "session_switch",
        chatId: chatId ?? "",
        messages: initialMessages,
        runStartedAt: chatId ? client.getRunStartedAt(chatId) : null,
        goalState: chatId ? client.getGoalState(chatId) : undefined,
        hasPendingToolCalls,
      }),
    );
    setStreamError(null);
    clearPendingStreamWork();
    if (streamEndTimerRef.current !== null) {
      clearTimeout(streamEndTimerRef.current);
      streamEndTimerRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, client, clearPendingStreamWork]);

  useEffect(() => {
    if (hasPendingToolCalls) {
      setState((prev) => ({ ...prev, streaming: true }));
    }
  }, [hasPendingToolCalls]);

  useEffect(() => {
    if (!chatId) return;

    const handle = (ev: InboundEvent) => {
      // Any incoming event while the debounce timer is alive means the model
      // is still working (e.g. tool result arrived, more text to stream).
      // Cancel the pending "stream ended" timer so we don't hide the spinner.
      if (streamEndTimerRef.current !== null) {
        clearTimeout(streamEndTimerRef.current);
        streamEndTimerRef.current = null;
      }

      if (ev.event === "delta") {
        const chunk = typeof ev.text === "string" ? ev.text : "";
        if (!chunk) return;
        pendingActionsRef.current.push({
          type: "delta",
          text: chunk,
          chatId: ev.chat_id,
          receivedAt: Date.now(),
        });
        schedulePendingFlush();
        return;
      }

      if (ev.event === "reasoning_delta") {
        const chunk = ev.text;
        if (!chunk) return;
        pendingActionsRef.current.push({
          type: "reasoning_delta",
          text: chunk,
          chatId: ev.chat_id,
          receivedAt: Date.now(),
        });
        schedulePendingFlush();
        return;
      }

      // All other event types must flush any pending deltas first so the
      // ordering (delta → stream_end → turn_end) is preserved.
      flushPending();

      if (ev.event === "stream_end") {
        dispatch({
          type: "stream_end",
          chatId: ev.chat_id,
          ...(typeof ev.text === "string" ? { finalAnswerText: ev.text } : {}),
          receivedAt: Date.now(),
        });
        // stream_end only means the text segment finished — the model may
        // still be executing tools.  Do NOT reset isStreaming here; the
        // definitive "turn is complete" signal is ``turn_end``.
        return;
      }

      if (ev.event === "reasoning_end") {
        dispatch({ type: "reasoning_end", chatId: ev.chat_id, receivedAt: Date.now() });
        return;
      }

      if (ev.event === "goal_state") {
        dispatch({
          type: "goal_state",
          chatId: ev.chat_id,
          goalState: ev.goal_state,
          receivedAt: Date.now(),
        });
        return;
      }

      if (ev.event === "goal_status") {
        dispatch({
          type: "goal_status",
          chatId: ev.chat_id,
          status: ev.status,
          startedAt: ev.started_at,
          receivedAt: Date.now(),
        });
        return;
      }

      if (ev.event === "turn_end") {
        const goalState =
          "goal_state" in ev &&
          ev.goal_state != null &&
          typeof ev.goal_state === "object"
            ? (ev.goal_state as GoalStateWsPayload)
            : undefined;
        dispatch({
          type: "turn_end",
          chatId: ev.chat_id,
          latencyMs: ev.latency_ms ?? undefined,
          contextUsage: ev.context_usage ?? null,
          ...(goalState ? { goalState } : {}),
          receivedAt: Date.now(),
        });
        // Definitive signal that the turn is fully complete.  Cancel any
        // pending debounce timer and stop the loading indicator immediately.
        if (streamEndTimerRef.current !== null) {
          clearTimeout(streamEndTimerRef.current);
          streamEndTimerRef.current = null;
        }
        onTurnEnd?.();
        return;
      }

      if (ev.event === "message") {
        // Back-compat: a legacy ``kind: "reasoning"`` message (no streaming
        // partner) is treated as one complete delta + immediate end so the
        // bubble renders identically to the streaming path.
        if (ev.kind === "reasoning") {
          if (!ev.text) return;
          dispatch({
            type: "reasoning_message",
            text: ev.text,
            chatId: ev.chat_id,
            receivedAt: Date.now(),
          });
          return;
        }
        // Intermediate agent breadcrumbs (tool-call hints, raw progress).
        if (ev.kind === "tool_hint" || ev.kind === "progress") {
          dispatch({
            type: "tool_progress",
            text: ev.text,
            kind: ev.kind,
            toolEvents: ev.tool_events,
            chatId: ev.chat_id,
            receivedAt: Date.now(),
          });
          return;
        }

        const media = ev.media_urls?.length
          ? ev.media_urls.map((m) => toMediaAttachment(m))
          : ev.media?.map((url) => toMediaAttachment({ url }));
        const hasMedia = !!media && media.length > 0;

        // A complete (non-streamed) assistant message. If a stream was in
        // flight, the reducer drops the placeholder so we don't render the
        // text twice.  Do NOT reset isStreaming here — only ``turn_end``
        // signals that the full turn (all tool calls + final text) is
        // complete.
        dispatch({
          type: "assistant_message",
          text: ev.text,
          ...(hasMedia ? { media } : {}),
          latencyMs: ev.latency_ms ?? undefined,
          chatId: ev.chat_id,
          receivedAt: Date.now(),
        });
        return;
      }

      if (ev.event === "file_edit") {
        dispatch({
          type: "file_edit",
          edits: Array.isArray(ev.edits) ? ev.edits : [],
          chatId: ev.chat_id,
          receivedAt: Date.now(),
        });
        return;
      }
      // ``attached`` / ``error`` frames aren't actionable here; the client
      // shell handles them separately.
    };

    const unsub = client.onChat(chatId, handle);
    return () => {
      unsub();
      clearPendingStreamWork();
      if (streamEndTimerRef.current !== null) {
        clearTimeout(streamEndTimerRef.current);
        streamEndTimerRef.current = null;
      }
    };
  }, [chatId, client, clearPendingStreamWork, dispatch, flushPending, onTurnEnd, schedulePendingFlush]);

  const send = useCallback(
    (content: string, images?: SendImage[], options?: SendOptions) => {
      if (!chatId) return;
      const hasImages = !!images && images.length > 0;
      // Text is optional when images are attached — the agent will still see
      // the image blocks via ``media`` paths.
      if (!hasImages && !content.trim()) return;

      flushPending();
      const previews = hasImages ? images!.map((i) => i.preview) : undefined;
      setState((prev) =>
        reduceStream(prev, {
          type: "send_message",
          content,
          ...(previews ? { images: previews } : {}),
          ...(options?.cliApps?.length ? { cliApps: options.cliApps } : {}),
          ...(options?.mcpPresets?.length ? { mcpPresets: options.mcpPresets } : {}),
          receivedAt: Date.now(),
        }),
      );
      // Mark streaming immediately so the UI shows the loading indicator
      // right away, before the first delta arrives from the server.
      const wireMedia = hasImages ? images!.map((i) => i.media) : undefined;
      if (options) {
        client.sendMessage(chatId, content, wireMedia, options);
      } else {
        client.sendMessage(chatId, content, wireMedia);
      }
    },
    [chatId, client, flushPending],
  );

  const stop = useCallback(() => {
    if (!chatId) return;
    flushPending();
    setState((prev) => reduceStream(prev, { type: "stop", receivedAt: Date.now() }));
    client.sendMessage(chatId, "/stop");
  }, [chatId, client, flushPending]);

  return {
    messages: state.messages,
    isStreaming: state.streaming,
    runStartedAt: state.runStartedAt,
    goalState: state.goalState,
    contextUsage: state.contextUsage,
    send,
    stop,
    setMessages,
    streamError,
    dismissStreamError,
  };
}
