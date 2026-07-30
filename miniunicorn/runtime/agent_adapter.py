"""Agent execution callback bridging the Worker to the existing Agent Core.

Design §8.3, §17.1, §29.3: the Worker Adapter calls a pluggable
:class:`~miniunicorn.runtime.worker.ExecutionCallback` with a decoded task
payload and the current session base revision. The callback runs the
existing Agent Core (TurnExecutor / AgentLoop) and returns a structured
:class:`~miniunicorn.runtime.worker.WorkerExecutionResult`.

This module provides:

- :class:`AgentExecutionCallback` — the production callback that wraps an
  :class:`~miniunicorn.agent.loop.AgentLoop` (or any host satisfying the
  :class:`TurnDispatchHost` protocol).

Durable ingress (submit/wait/result) lives in
:mod:`miniunicorn.runtime.application` via ``RuntimeApplication`` and
``build_inbound_envelope`` (design §29.1, Task 4). The legacy
``submit_durable`` / ``dispatch_durable`` envelope builders were removed
once the deterministic ingress helpers centralized that construction.

WP3 scope: the callback wraps the existing Agent loop without per-attempt
Provider journaling or Tool Gateway routing (those are WP4). The Agent loop
runs unchanged; the callback just captures the result for the Worker to
commit durably.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent.ports import SafeError
from miniunicorn.agent.turn_coordinator import DurableIdentifiers
from miniunicorn.bus.events import InboundMessage
from miniunicorn.runtime.worker import (
    WorkerExecutionResult,
    WorkerTaskPayload,
)

if TYPE_CHECKING:
    from miniunicorn.agent.turn_coordinator import TurnCoordinator
    from miniunicorn.agent.turn_dispatcher import TurnDispatcher


class AgentExecutionCallback:
    """Production execution callback wrapping the existing Agent Core.

    Implements the :class:`~miniunicorn.runtime.worker.ExecutionCallback`
    protocol. Constructed by the LightweightHost (or Supervised Host) with
    the host's :class:`TurnDispatcher` and :class:`TurnCoordinator`.

    The callback is stateless between calls — all task state lives in the
    Runtime Store and the Session Manager.
    """

    def __init__(
        self,
        dispatcher: "TurnDispatcher",
        coordinator: "TurnCoordinator",
        *,
        tool_execution_port: Any | None = None,
        turn_journal: Any | None = None,
        outbound_port_factory: Any | None = None,
        progress_port_factory: Any | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._coordinator = coordinator
        self._tool_execution_port = tool_execution_port
        self._turn_journal = turn_journal
        self._outbound_port_factory = outbound_port_factory
        self._progress_port_factory = progress_port_factory

    async def __call__(
        self,
        payload: WorkerTaskPayload,
        session_base_revision: int,
    ) -> WorkerExecutionResult:
        """Execute the Agent turn for one claimed durable task.

        Returns a :class:`WorkerExecutionResult` carrying the final content
        and the assistant/tool messages produced after the inbound commit.
        The Worker uses these to build the FINAL session commit and the
        Outbox completion.

        Transient streaming (Token deltas, reasoning deltas) is published
        to the Message Bus for realtime UX (design §23.1, WP5 task 6).
        The final reply is NOT published to the bus — it goes through the
        Outbox via the Worker's ``_complete_task`` (WP5 task 7).
        """
        import time as _time

        from miniunicorn.bus.events import OutboundMessage

        # Build the InboundMessage from the decoded payload.
        msg = InboundMessage(
            channel=payload.channel or "cli",
            sender_id="user",
            chat_id=payload.session_key.split(":", 1)[-1]
            if ":" in payload.session_key
            else payload.session_key,
            content=payload.content,
            media=list(payload.media or []),
            metadata=dict(payload.metadata or {}),
        )

        # Bind a TurnRuntime with durable identifiers (design §29.5).
        durable_ids = DurableIdentifiers(
            task_id=payload.task_id,
            session_sequence=0,  # Not carried in WorkerTaskPayload for WP3
            lease_epoch=0,
            run_segment=0,
            trace_id=None,
        )

        try:
            async with self._coordinator.scope(
                payload.session_key,
                turn_id=payload.turn_id,
                durable_identifiers=durable_ids,
            ) as turn_runtime:
                # Bind the durable runtime ports so the Agent Core routes
                # tools through ToolExecutionPort and journals Provider
                # attempts through TurnJournalPort (design §11.1, §19, §20).
                turn_runtime.tool_execution_port = self._tool_execution_port
                turn_runtime.turn_journal = self._turn_journal
                # Per-task ports (design Task 5 Step 5): the Worker binds
                # the active claim/delivery ledger and containment scope via
                # ContextVars before calling this callback; the callback
                # creates the per-task OutboundPort and ProgressPort from
                # factories and binds the Worker's containment scope to the
                # TurnRuntime so Agent-owned tools (Shell, Message) reach
                # them through Agent-owned ports.
                if self._outbound_port_factory is not None:
                    turn_runtime.outbound_port = self._outbound_port_factory(
                        payload.task_id
                    )
                if self._progress_port_factory is not None:
                    turn_runtime.progress_port = self._progress_port_factory(
                        payload.task_id
                    )
                from miniunicorn.runtime.containment import (
                    current_containment_scope,
                )

                _containment = current_containment_scope()
                if _containment is not None:
                    turn_runtime.containment_port = _containment
                host = self._dispatcher.host

                # Build transient streaming callbacks that publish Token
                # deltas to the Message Bus (design §23.1, WP5 task 6) and
                # to the Realtime hub via the ProgressPort (Task 6 Step 5).
                # The final reply is delivered via the Outbox, not the bus.
                #
                # The realtime hub path is always active when a progress_port
                # is bound: CLI/API subscribers consume deltas from
                # ``RuntimeApplication.subscribe(task_id)``. The MessageBus
                # path is retained for legacy consumers (gateway) until Task 9.
                on_stream = on_stream_end = None
                progress_port = turn_runtime.progress_port
                wants_bus_stream = msg.metadata.get("_wants_stream")
                if wants_bus_stream or progress_port is not None:
                    from miniunicorn.bus.agent_events import (
                        DeltaEvent,
                        StreamEndEvent,
                    )

                    stream_base_id = f"{payload.session_key}:{_time.time_ns()}"
                    stream_segment = 0

                    def _current_stream_id() -> str:
                        return f"{stream_base_id}:{stream_segment}"

                    async def on_stream(delta: str) -> None:
                        if progress_port is not None:
                            await progress_port.emit(
                                DeltaEvent(
                                    chat_id=msg.chat_id,
                                    text=delta,
                                    stream_id=_current_stream_id(),
                                )
                            )
                        if wants_bus_stream:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await host.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=delta,
                                    metadata=meta,
                                )
                            )

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        nonlocal stream_segment
                        if progress_port is not None:
                            await progress_port.emit(
                                StreamEndEvent(
                                    chat_id=msg.chat_id,
                                    stream_id=_current_stream_id(),
                                )
                            )
                        if wants_bus_stream:
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await host.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content="",
                                    metadata=meta,
                                )
                            )
                        stream_segment += 1

                result = await host._execute_message(
                    msg,
                    session_key=payload.session_key,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                    runtime_mode=True,
                )
                # Copy cumulative metrics into the bound runtime.
                from miniunicorn.agent.turn_runtime import complete_turn_runtime

                complete_turn_runtime(turn_runtime, result.context)

                outbound = result.outbound
                if outbound is None:
                    return WorkerExecutionResult(
                        final_content=None,
                        messages=[],
                        suppress_final=True,
                    )

                # Extract the assistant messages produced after the inbound
                # commit. For WP3, we use the context's all_messages if
                # available; otherwise, build a single assistant message
                # from the outbound content.
                messages: list[dict[str, Any]] = []
                if result.context is not None and result.context.all_messages:
                    # all_messages includes the history + new messages.
                    # We want only the new assistant/tool messages.
                    skip = result.context.save_skip or 0
                    messages = [
                        m for m in result.context.all_messages[skip:]
                        if m.get("role") in ("assistant", "tool")
                    ]
                if not messages and outbound.content:
                    messages = [{"role": "assistant", "content": outbound.content}]

                return WorkerExecutionResult(
                    final_content=outbound.content,
                    messages=messages,
                    metadata_updates={},
                    suppress_final=False,
                )
        except Exception as exc:
            logger.exception("AgentExecutionCallback failed for task {}", payload.task_id)
            return WorkerExecutionResult(
                final_content=None,
                messages=[],
                error=SafeError(
                    error_code="AGENT_EXECUTION_FAILURE",
                    error_summary=str(exc)[:500],
                ),
            )


__all__ = ["AgentExecutionCallback"]
