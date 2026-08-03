"""WebSocket outbound emission service (Task 15).

``WebSocketOutboundEmitter`` owns every code path that serialises an
``AgentEvent`` and fans it out to WebSocket subscribers.  It is constructed
with explicit callable dependencies so that no channel lifecycle, HTTP
routing, authentication, or inbound-envelope logic leaks into this module.

The owning ``WebSocketChannel`` wires its own ``_safe_send_to`` (which handles
``ConnectionClosed`` cleanup), ``_subs`` lookup, markdown-rewrite helper,
transcript-append helper, and media-signing helper into the emitter and then
delegates each public ``send_*`` method here with an unchanged signature.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from miniunicorn.bus.agent_events import (
    AGENT_EVENT_ADAPTER,
    AgentEvent,
    AttachedEvent,
    DeltaEvent,
    ErrorEvent,
    FileEditEvent,
    GoalStateEvent,
    GoalStatusEvent,
    MessageEvent,
    ReasoningDeltaEvent,
    ReasoningEndEvent,
    RuntimeModelUpdatedEvent,
    SessionUpdatedEvent,
    StreamEndEvent,
    SubagentActivityEvent,
    TurnEndEvent,
    serialize_agent_event,
)
from miniunicorn.bus.events import (
    OUTBOUND_META_AGENT_EVENT,
    OUTBOUND_META_AGENT_UI,
    OutboundMessage,
)

# Wildcard ``chat_id`` used by ``send_runtime_model_updated`` to request every
# open connection from the ``connections_for_chat`` callback.
_ALL_CONNECTIONS = "*"

# Stream-text-buffer TTL (seconds).  Mirrors the channel constant so the
# emitter can self-clean without depending on channel state.
_STREAM_TEXT_BUF_TTL = 1800


class WebSocketOutboundEmitter:
    """Serialize ``AgentEvent``\\ s and fan them out to WebSocket subscribers.

    Dependencies are injected so the service has no knowledge of channel
    lifecycle, HTTP routing, or connection bookkeeping:

    - ``connections_for_chat``: resolve subscribers for a ``chat_id`` (or
      ``"*"`` for every open connection).
    - ``safe_send``: deliver one raw JSON frame to one connection.  The
      caller is responsible for catching ``ConnectionClosed`` and performing
      cleanup; the emitter's fan-out loop assumes ``safe_send`` does not
      raise for broken connections so delivery to remaining subscribers
      continues uninterrupted.
    - ``rewrite_markdown_images``: rewrite local image paths to signed URLs.
    - ``append_transcript``: persist a wire payload to the WebUI transcript.
    - ``sign_media``: sign media paths for wire delivery (returns signed URL
      dicts or ``None``).
    """

    def __init__(
        self,
        *,
        connections_for_chat: Callable[[str], Iterable[Any]],
        safe_send: Callable[[Any, str], Awaitable[None]],
        rewrite_markdown_images: Callable[[str], str],
        append_transcript: Callable[[str, dict[str, Any]], None],
        sign_media: Callable[[list[str] | None], list[dict[str, str]] | None] | None = None,
    ) -> None:
        self._connections_for_chat = connections_for_chat
        self._safe_send = safe_send
        self._rewrite_markdown_images = rewrite_markdown_images
        self._append_transcript = append_transcript
        self._sign_media = sign_media or (lambda media: None)
        # Streaming text buffers keyed by (chat_id, stream_id) so that
        # ``stream_end`` can rewrite the full accumulated text.  These were
        # previously owned by ``WebSocketChannel``; they move here because
        # they are pure outbound-emission state.
        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}
        self._stream_text_buffer_times: dict[tuple[str, str], float] = {}

    # -- core fan-out -------------------------------------------------------

    async def send_agent_event(
        self,
        event: AgentEvent,
        *,
        connections: list[Any] | None = None,
        label: str = "",
        persist: bool = False,
    ) -> None:
        """Serialize *event* and fan it out to *connections*.

        When *connections* is ``None`` the subscribers for the event's
        ``chat_id`` are resolved via the ``connections_for_chat`` callback.
        When *persist* is True and the event carries a ``chat_id`` the
        serialized payload is appended to the WebUI transcript.
        """
        payload = serialize_agent_event(event)
        chat_id = payload.get("chat_id")
        if persist and isinstance(chat_id, str):
            self._append_transcript(chat_id, payload)
        raw = json.dumps(payload, ensure_ascii=False)
        targets = connections
        if targets is None and isinstance(chat_id, str):
            targets = list(self._connections_for_chat(chat_id))
        for connection in targets or []:
            await self._safe_send(connection, raw)

    # -- outbound message dispatch ------------------------------------------

    async def dispatch_outbound(self, msg: OutboundMessage) -> None:
        """Route an ``OutboundMessage`` from the bus to the appropriate emission.

        This is the body of ``WebSocketChannel.send``; it inspects message
        metadata to choose the right event type, signs media URLs for regular
        messages, and delegates to the specific ``send_*`` method.
        """
        # Typed envelope: when present, validate and forward as-is.
        typed_payload = msg.metadata.get(OUTBOUND_META_AGENT_EVENT)
        if isinstance(typed_payload, dict):
            event = AGENT_EVENT_ADAPTER.validate_python(typed_payload)
            await self.send_agent_event(event, persist=True)
            return

        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return

        conns = list(self._connections_for_chat(msg.chat_id))
        if not conns:
            return

        if msg.metadata.get("_subagent_activity"):
            event = SubagentActivityEvent(
                chat_id=msg.chat_id,
                label=msg.metadata.get("_subagent_label"),
                task_id=msg.metadata.get("_subagent_task_id"),
                content=msg.content,
            )
            await self.send_agent_event(
                event, connections=conns, persist=True, label=" subagent_activity "
            )
            return
        if msg.metadata.get("_goal_state_sync"):
            blob = msg.metadata.get("goal_state")
            await self.send_goal_state(
                msg.chat_id, blob if isinstance(blob, dict) else {"active": False}
            )
            return
        if msg.metadata.get("_goal_status"):
            status = msg.metadata.get("goal_status")
            if status in ("running", "idle"):
                started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                await self.send_goal_status(
                    msg.chat_id,
                    status,
                    started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                )
            return
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            cu = msg.metadata.get("context_usage")
            await self.send_turn_end(
                msg.chat_id,
                latency_ms=lat_i,
                goal_state=gs if isinstance(gs, dict) else None,
                context_usage=cu if isinstance(cu, dict) else None,
            )
            return
        if msg.metadata.get("_session_updated"):
            scope = msg.metadata.get("_session_update_scope")
            await self.send_session_updated(
                msg.chat_id,
                scope=scope if isinstance(scope, str) else None,
            )
            return
        if msg.metadata.get("_file_edit_events"):
            event = FileEditEvent(
                chat_id=msg.chat_id,
                edits=msg.metadata["_file_edit_events"],
            )
            await self.send_agent_event(event, connections=conns, persist=True, label=" ")
            return
        # Regular message: sign media, then delegate to send_message.
        media_urls = self._sign_media(msg.media)
        kind: str | None = None
        if msg.metadata.get("_tool_hint"):
            kind = "tool_hint"
        elif msg.metadata.get("_progress"):
            kind = "progress"
        lat = msg.metadata.get("latency_ms")
        lat_i = int(lat) if isinstance(lat, (int, float)) else None
        await self.send_message(
            msg.chat_id,
            msg.content,
            reply_to=msg.reply_to or None,
            media=msg.media or None,
            media_urls=media_urls,
            tool_events=msg.metadata.get("_tool_events"),
            kind=kind,
            latency_ms=lat_i,
            agent_ui=msg.metadata.get(OUTBOUND_META_AGENT_UI),
            connections=conns,
        )

    # -- control event (single-connection) ----------------------------------

    async def send_control_event(
        self,
        connection: Any,
        event: str,
        fields: dict[str, Any],
    ) -> None:
        """Construct and send a control event to a single connection."""
        typed = self._build_control_event(event, fields)
        await self.send_agent_event(typed, connections=[connection])

    @staticmethod
    def _build_control_event(event: str, fields: dict[str, Any]) -> AgentEvent:
        """Map a legacy control-event name to its typed ``AgentEvent`` model."""
        if event == "attached":
            return AttachedEvent(
                chat_id=fields["chat_id"],
                request_id=fields.get("request_id"),
            )
        if event == "session_updated":
            return SessionUpdatedEvent(
                chat_id=fields["chat_id"],
                scope=fields.get("scope"),
                workspace_scope=fields.get("workspace_scope"),
            )
        if event == "error":
            return ErrorEvent(
                chat_id=fields.get("chat_id"),
                detail=fields.get("detail"),
                reason=fields.get("reason"),
            )
        return ErrorEvent(detail=f"unknown control event: {event!r}")

    # -- regular message ----------------------------------------------------

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        media: list[str] | None = None,
        media_urls: list[dict[str, str]] | None = None,
        tool_events: list[Any] | None = None,
        kind: str | None = None,
        latency_ms: int | None = None,
        agent_ui: dict[str, Any] | None = None,
        connections: list[Any] | None = None,
    ) -> None:
        """Construct a ``MessageEvent`` and deliver it to subscribers.

        Markdown-image rewriting happens on the wire text; the transcript
        persists the original (pre-rewrite) text so replay matches what the
        agent actually produced.
        """
        wire_text = self._rewrite_markdown_images(text)
        event = MessageEvent(
            chat_id=chat_id,
            text=wire_text,
            reply_to=reply_to,
            media=media,
            media_urls=media_urls,
            tool_events=tool_events,
            kind=kind,  # type: ignore[arg-type]
            latency_ms=latency_ms,
            agent_ui=agent_ui,
        )
        transcript_event = event.model_copy(update={"text": text})
        self._append_transcript(chat_id, serialize_agent_event(transcript_event))
        await self.send_agent_event(event, connections=connections, label=" ")

    # -- reasoning stream ---------------------------------------------------

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push one chunk of model reasoning."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns or not delta:
            return
        meta = metadata or {}
        event = ReasoningDeltaEvent(
            chat_id=chat_id,
            text=delta,
            stream_id=meta.get("_stream_id"),
        )
        await self.send_agent_event(event, connections=conns, persist=True, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close the current reasoning stream segment."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns:
            return
        meta = metadata or {}
        event = ReasoningEndEvent(
            chat_id=chat_id,
            stream_id=meta.get("_stream_id"),
        )
        await self.send_agent_event(event, connections=conns, persist=True, label=" reasoning_end ")

    # -- text stream --------------------------------------------------------

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push one streaming text chunk (or ``stream_end`` to flush)."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns:
            return
        meta = metadata or {}
        stream_key = (chat_id, str(meta.get("_stream_id") or ""))
        stream_id = meta.get("_stream_id")
        if meta.get("_stream_end"):
            buffered = self._stream_text_buffers.pop(stream_key, [])
            self._stream_text_buffer_times.pop(stream_key, None)
            if delta:
                buffered.append(delta)
            full_text = "".join(buffered)
            rewritten = self._rewrite_markdown_images(full_text)
            event: AgentEvent = StreamEndEvent(
                chat_id=chat_id,
                stream_id=stream_id if stream_id is not None else None,
                text=rewritten if rewritten != full_text else None,
            )
        else:
            event = DeltaEvent(
                chat_id=chat_id,
                text=delta,
                stream_id=stream_id if stream_id is not None else None,
            )
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
            self._stream_text_buffer_times[stream_key] = time.monotonic()
        await self.send_agent_event(event, connections=conns, persist=True, label=" stream ")

    # -- turn end -----------------------------------------------------------

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
        context_usage: dict[str, Any] | None = None,
    ) -> None:
        """Signal that the agent finished processing the current turn."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns:
            return
        event = TurnEndEvent(
            chat_id=chat_id,
            latency_ms=int(latency_ms) if latency_ms is not None else None,
            goal_state=goal_state,  # type: ignore[arg-type]
            context_usage=context_usage,  # type: ignore[arg-type]
        )
        await self.send_agent_event(event, connections=conns, persist=True, label=" turn_end ")

    # -- goal state / status ------------------------------------------------

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id*."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns:
            return
        event = GoalStateEvent(chat_id=chat_id, goal_state=blob)  # type: ignore[arg-type]
        await self.send_agent_event(event, connections=conns, persist=True, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """Notify subscribers that a turn started or finished (wall-clock hint)."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns:
            return
        event = GoalStatusEvent(
            chat_id=chat_id,
            status=status,  # type: ignore[arg-type]
            started_at=started_at if status == "running" else None,
        )
        await self.send_agent_event(event, connections=conns, persist=True, label=" goal_status ")

    # -- session update -----------------------------------------------------

    async def send_session_updated(
        self,
        chat_id: str,
        *,
        scope: str | None = None,
    ) -> None:
        """Notify clients that session metadata changed outside the main turn."""
        conns = list(self._connections_for_chat(chat_id))
        if not conns:
            return
        event = SessionUpdatedEvent(chat_id=chat_id, scope=scope)
        await self.send_agent_event(
            event, connections=conns, persist=True, label=" session_updated "
        )

    # -- runtime model update ----------------------------------------------

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._connections_for_chat(_ALL_CONNECTIONS))
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        event = RuntimeModelUpdatedEvent(
            model_name=model_name.strip(),
            model_preset=model_preset.strip() if isinstance(model_preset, str) else None,
        )
        await self.send_agent_event(event, connections=conns, label=" runtime_model_updated ")

    # -- buffer maintenance (called by the channel) -------------------------

    def cleanup_stale_buffers(self, ttl: float | None = None) -> int:
        """Remove stream-text buffers older than *ttl* seconds.

        Returns the number of buffers evicted so the caller can log it.
        """
        cutoff = time.monotonic() - (ttl if ttl is not None else _STREAM_TEXT_BUF_TTL)
        stale = [k for k, t in self._stream_text_buffer_times.items() if t < cutoff]
        for k in stale:
            self._stream_text_buffers.pop(k, None)
            self._stream_text_buffer_times.pop(k, None)
        return len(stale)

    def clear_buffers(self) -> None:
        """Drop all stream-text buffers (called on channel ``stop()``)."""
        self._stream_text_buffers.clear()
        self._stream_text_buffer_times.clear()
