"""Independent owner of the single-turn state-machine driver.

:class:`TurnExecutor` was extracted from :class:`miniunicorn.agent.loop.AgentLoop`
so the loop can stay a thin facade. The executor drives one normal or system
turn through the RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND →
DONE state machine and returns a :class:`ProcessedTurn`.

The executor only sees the host capabilities it needs through the
:class:`TurnExecutionHost` protocol; it never imports ``AgentLoop`` at runtime.
Monkeypatch seams (``_run_agent_loop``, ``_save_turn``,
``_persist_subagent_followup``) remain on the host, so existing tests and
extensions that patch those methods on the loop instance continue to
intercept calls.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

from loguru import logger

from miniunicorn.agent._state_machine import (
    StateTraceEntry,
    TurnContext,
    TurnState,
)
from miniunicorn.agent.turn_runtime import (
    ProcessedTurn,
    current_turn_runtime,
)
from miniunicorn.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from miniunicorn.agent.hook import AgentHook
    from miniunicorn.agent.subagent_registry import SubagentDefinition


TURN_TRANSITIONS: Final[Mapping[tuple[TurnState, str], TurnState]] = {
    (TurnState.RESTORE, "ok"): TurnState.COMPACT,
    (TurnState.COMPACT, "ok"): TurnState.COMMAND,
    (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
    (TurnState.COMMAND, "shortcut"): TurnState.DONE,
    (TurnState.BUILD, "ok"): TurnState.RUN,
    (TurnState.RUN, "ok"): TurnState.SAVE,
    (TurnState.SAVE, "ok"): TurnState.RESPOND,
    (TurnState.RESPOND, "ok"): TurnState.DONE,
}

# Runtime transition table (design §18.1, §29.3, WP3 task 8).
# Removes ``COMMAND -> DONE`` shortcut so all user-visible commands pass
# through SAVE and RESPOND (durable session commit + Outbox enqueue).
# Shortcut commands now flow ``COMMAND -> SAVE -> RESPOND -> DONE``.
RUNTIME_TURN_TRANSITIONS: Final[Mapping[tuple[TurnState, str], TurnState]] = {
    **TURN_TRANSITIONS,
    (TurnState.COMMAND, "shortcut"): TurnState.SAVE,
}


class TurnExecutionHost(Protocol):
    """Host capabilities required by :class:`TurnExecutor`."""

    def _refresh_provider_snapshot(self) -> None: ...

    def _resolve_agent_override(self, msg: InboundMessage) -> "SubagentDefinition | None": ...

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None: ...


class TurnExecutor:
    """Drive a single inbound message through the turn state machine."""

    def __init__(self, host: TurnExecutionHost) -> None:
        self._host = host

    @property
    def host(self) -> TurnExecutionHost:
        """Read-only diagnostic accessor for the bound host."""
        return self._host

    async def execute(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list["AgentHook"] | None = None,
        *,
        runtime_mode: bool = False,
    ) -> ProcessedTurn:
        """Execute a single inbound message and return the outbound + context.

        Returns a :class:`ProcessedTurn` carrying the optional outbound
        payload and the completed :class:`TurnContext` (when one was built).
        System-message shortcuts return ``ProcessedTurn(outbound, None)``
        because they bypass the state machine and have no TurnContext to
        copy cumulative metrics from.

        When ``runtime_mode=True`` (design §18.1, §29.3, WP3 task 8):

        - uses :data:`RUNTIME_TURN_TRANSITIONS` which removes the
          ``COMMAND -> DONE`` shortcut so all user-visible commands flow
          through SAVE and RESPOND (durable commit + Outbox);
        - when COMMAND returns ``"shortcut"``, the command's outbound
          content is copied into ``ctx.final_content`` so SAVE/RESPOND
          handle it like a normal turn output.
        """
        self._host._refresh_provider_snapshot()

        if msg.channel == "system":
            system_outbound = await self._host._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )
            return ProcessedTurn(outbound=system_outbound, context=None)

        key = session_key or msg.session_key
        # Pull the turn ID from the bound TurnRuntime so the state trace,
        # telemetry, and coordinator all share one identifier. The runtime
        # is bound by ``TurnCoordinator.scope`` before this method runs.
        # Fall back to a generated ID for legacy callers (e.g. tests) that
        # invoke _process_message without entering a coordinator scope.
        runtime = current_turn_runtime()
        turn_id = runtime.turn_id if runtime is not None else f"{key}:{time.time_ns()}"
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=turn_id,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            agent_override=self._host._resolve_agent_override(msg),
            turn_hooks=list(turn_hooks or []),
        )

        transitions = RUNTIME_TURN_TRANSITIONS if runtime_mode else TURN_TRANSITIONS

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self._host, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            # Runtime mode: COMMAND shortcut flows to SAVE (design §18.1).
            # Copy the command's outbound content into final_content so
            # SAVE/RESPOND handle it as the turn output.
            if (
                runtime_mode
                and ctx.state is TurnState.COMMAND
                and event == "shortcut"
                and ctx.outbound is not None
                and ctx.final_content is None
            ):
                ctx.final_content = ctx.outbound.content

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = transitions.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ProcessedTurn(outbound=ctx.outbound, context=ctx)

    async def process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        host = self._host
        channel, chat_id = msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = host.sessions.get_or_create(key)

        session, pending = host.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        await host.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=host._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and host._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            host.sessions.save(session)
        host._set_tool_context(
            channel,
            chat_id,
            msg.metadata.get("message_id"),
            msg.metadata,
            session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": host._max_messages,
            "max_tokens": host._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"
        workspace_scope = host.workspace_scopes.for_message(msg, session.metadata)
        query_embedding = await host._compute_query_embedding(
            "" if is_subagent else msg.content,
        )

        messages = host.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
            workspace=workspace_scope.project_path,
            runtime_state=host,
            inbound_message=msg,
            skip_runtime_lines=is_subagent,
            query_embedding=query_embedding,
            vector_recall=host._vector_recall,
        )
        t_wall = time.time()
        result = await host._run_agent_loop(
            messages,
            session=session,
            channel=channel,
            chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        host._save_turn(session, result.messages, 1 + len(history), turn_latency_ms=latency_ms)
        session.enforce_file_cap(on_archive=host.context.memory.raw_archive)
        host.sessions.save(session)
        host._schedule_background(
            host.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=host._max_messages,
            ),
            name=f"consolidate:{session.key}",
        )
        content = result.final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        # 从 thread-scoped session key 中提取 slack thread_ts,确保 subagent
        # followup 回到原线程而非频道主页(session key 格式: slack:C123:1700.42)
        if channel == "slack":
            parts = key.split(":", 2)
            if len(parts) == 3:
                outbound_metadata["slack"] = {"thread_ts": parts[2]}
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )
