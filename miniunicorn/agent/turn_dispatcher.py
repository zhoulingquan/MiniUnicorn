"""TurnDispatcher: Agent Core execution helper (design Task 10).

Provides the ``process_message`` normalization bridge used by the SDK
path (``Miniunicorn.run()`` → ``AgentLoop._process_message``). The
durable runtime calls ``_execute_message`` directly through
:class:`~miniunicorn.runtime.agent_adapter.AgentExecutionCallback`.

All legacy authority has been removed (design Task 10): the bus-consume
loop, per-session dispatch, the direct SDK entry point, the process-local
pending-queue and active-task registries, the final-bus-publish calls,
and checkpoint restore/clear calls. Inbound work is submitted through
:class:`~miniunicorn.runtime.application.RuntimeApplication`; final
replies are delivered through the Outbox.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from miniunicorn.agent.turn_coordinator import TurnCoordinator
from miniunicorn.agent.turn_runtime import (
    ProcessedTurn,
    complete_turn_runtime,
    current_turn_runtime,
)
from miniunicorn.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from miniunicorn.agent.hook import AgentHook
    from miniunicorn.bus.queue import MessageBus


@runtime_checkable
class TurnDispatchHost(Protocol):
    """Host capabilities required by :class:`TurnDispatcher`."""

    bus: "MessageBus"

    async def _execute_message(
        self,
        msg: InboundMessage,
        session_key: str | None = ...,
        on_progress: Any = ...,
        on_stream: Any = ...,
        on_stream_end: Any = ...,
        pending_queue: asyncio.Queue | None = ...,
        turn_hooks: list["AgentHook"] | None = ...,
        *,
        runtime_mode: bool = ...,
    ) -> ProcessedTurn: ...


class TurnDispatcher:
    """Agent Core execution helper for the SDK and Worker callback paths.

    Owns the ``process_message`` normalization bridge that opens a
    :class:`TurnCoordinator` scope (when no runtime is bound) and calls
    the host's ``_execute_message``. The durable Worker callback
    bypasses this bridge and calls ``_execute_message`` directly through
    its own coordinator scope.

    Legacy process-local authority (bus-consume loop, per-session
    dispatch, the direct SDK entry point, the pending-queue and
    active-task registries, the final-bus-publish calls, and
    checkpoint restore/clear) was removed in design Task 10.
    """

    def __init__(self, host: TurnDispatchHost, coordinator: TurnCoordinator) -> None:
        self._host = host
        self._coordinator = coordinator

    @property
    def host(self) -> TurnDispatchHost:
        """Read-only diagnostic accessor for the bound host."""
        return self._host

    async def process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: "Callable[..., Awaitable[None]] | None" = None,
        on_stream: "Callable[[str], Awaitable[None]] | None" = None,
        on_stream_end: "Callable[..., Awaitable[None]] | None" = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list["AgentHook"] | None = None,
    ) -> OutboundMessage | None:
        """Compatibility bridge: call ``_execute_message`` and complete the TurnRuntime.

        When a :class:`~miniunicorn.agent.turn_runtime.TurnRuntime` is already
        bound (e.g. by :meth:`AgentExecutionCallback.__call__`), this method
        simply invokes the host's ``_execute_message`` and copies cumulative
        metrics into the runtime before returning the outbound payload.

        When no runtime is bound (e.g. SDK callers via
        :class:`Miniunicorn`), this method opens a coordinator scope so a
        fresh runtime is bound for the turn.
        """
        runtime = current_turn_runtime()
        if runtime is not None:
            result = await self._host._execute_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
                turn_hooks=turn_hooks,
            )
            complete_turn_runtime(runtime, result.context)
            return result.outbound

        effective_key = session_key or msg.session_key
        async with self._coordinator.scope(effective_key) as turn_runtime:
            # Wire DirectOutboundPort fallback for legacy/test turns where
            # no durable Outbox is bound (design §20.6, WP5 hard cutover).
            # The MessageTool requires a non-None outbound_port; this
            # fallback mirrors the former send_callback path.
            if turn_runtime.outbound_port is None:
                from miniunicorn.agent.ports import DirectOutboundPort

                turn_runtime.outbound_port = DirectOutboundPort(self._host.bus)
            result = await self._host._execute_message(
                msg,
                session_key=effective_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
                turn_hooks=turn_hooks,
            )
            complete_turn_runtime(turn_runtime, result.context)
            return result.outbound
