"""TurnDispatcher: turn dispatch and entry-point coordination.

Owns the bus-consume loop (:meth:`run`), per-session dispatch
(:meth:`dispatch`), the ``_process_message`` compatibility bridge
(:meth:`process_message`), the direct entry point (:meth:`process_direct`),
and task cancellation (:meth:`cancel_active_tasks`). Task registries
(``active_tasks``, ``pending_queues``) live here; :class:`AgentLoop` exposes
them as read-only compatibility properties.

The dispatcher sees the host through the :class:`TurnDispatchHost` protocol
and never imports ``AgentLoop`` at runtime. Monkeypatch seams
(``_dispatch``, ``_process_message``, ``_execute_message``) remain on the
host, so existing tests and extensions that patch those methods on the loop
instance continue to intercept calls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loguru import logger

from miniunicorn.agent import context as agent_context
from miniunicorn.agent.turn_coordinator import TurnCoordinator
from miniunicorn.agent.turn_runtime import (
    ProcessedTurn,
    complete_turn_runtime,
    current_turn_runtime,
)
from miniunicorn.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from miniunicorn.agent.autocompact import AutoCompact
    from miniunicorn.agent.dream_trigger import DreamIdleTrigger
    from miniunicorn.agent.hook import AgentHook
    from miniunicorn.agent.subagent import SubagentManager
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.command.router import CommandRouter
    from miniunicorn.session.manager import SessionManager
    from miniunicorn.session.webui_turns import WebuiTurnCoordinator


@runtime_checkable
class TurnDispatchHost(Protocol):
    """Host capabilities required by :class:`TurnDispatcher`."""

    bus: "MessageBus"
    commands: "CommandRouter"
    auto_compact: "AutoCompact"
    dream_idle_trigger: "DreamIdleTrigger"
    sessions: "SessionManager"
    subagents: "SubagentManager"
    _turn_coordinator: TurnCoordinator
    _webui_turns: "WebuiTurnCoordinator"
    _running: bool

    def _schedule_background(self, coro: Any, *, name: str = ...) -> None: ...

    async def _emit_telemetry(self, turn_runtime: Any) -> None: ...

    def _restore_runtime_checkpoint(self, session: Any) -> bool: ...

    def _clear_pending_user_turn(self, session: Any) -> None: ...

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Any,
    ) -> None: ...

    async def _connect_mcp(self) -> None: ...

    async def _dispatch(self, msg: InboundMessage) -> None: ...

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = ...,
        on_progress: Any = ...,
        on_stream: Any = ...,
        on_stream_end: Any = ...,
        pending_queue: asyncio.Queue | None = ...,
        turn_hooks: list["AgentHook"] | None = ...,
    ) -> OutboundMessage | None: ...

    async def _execute_message(
        self,
        msg: InboundMessage,
        session_key: str | None = ...,
        on_progress: Any = ...,
        on_stream: Any = ...,
        on_stream_end: Any = ...,
        pending_queue: asyncio.Queue | None = ...,
        turn_hooks: list["AgentHook"] | None = ...,
    ) -> ProcessedTurn: ...

    def _effective_session_key(self, msg: InboundMessage) -> str: ...


class TurnDispatcher:
    """Single owner of bus-consume loop, dispatch, and entry-point coordination."""

    def __init__(self, host: TurnDispatchHost, coordinator: TurnCoordinator) -> None:
        self._host = host
        self._coordinator = coordinator
        self.active_tasks: dict[str, list[asyncio.Task[Any]]] = {}
        self.pending_queues: dict[str, asyncio.Queue[InboundMessage]] = {}

    @property
    def host(self) -> TurnDispatchHost:
        """Read-only diagnostic accessor for the bound host."""
        return self._host

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._host._running = True
        await self._host._connect_mcp()
        logger.info("Agent loop started")

        while self._host._running:
            try:
                msg = await asyncio.wait_for(self._host.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self._host.auto_compact.check_expired(
                    self._host._schedule_background,
                    active_session_keys=self.pending_queues.keys(),
                )
                # Dream 空闲触发：无活跃会话且有积压数据时后台触发 Dream
                await self._host.dream_idle_trigger.maybe_trigger(
                    active_session_keys=self.pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # 兼容写法：低版本 Python 没有 Task.cancelling()
                _task = asyncio.current_task()
                _cancelling = getattr(_task, "cancelling", lambda: 0)() if _task else 0
                if not self._host._running or _cancelling:
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            # 标记用户有新活动，重置 Dream 空闲触发器的空闲计时器
            self._host.dream_idle_trigger.notify_user_activity()
            effective_key = self._host._effective_session_key(msg)
            if await agent_context.handle_runtime_control(self._host, msg, self._host.tools):
                continue
            if self._host.commands.is_priority(raw):
                await self._host._dispatch_command_inline(
                    msg,
                    effective_key,
                    raw,
                    self._host.commands.dispatch_priority,
                )
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self.pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self._host.commands.is_dispatchable_command(raw):
                    await self._host._dispatch_command_inline(
                        msg,
                        effective_key,
                        raw,
                        self._host.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self.pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._host._dispatch(msg))
            self.active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: (
                    self.active_tasks.get(k, []) and self.active_tasks[k].remove(t)
                    if t in self.active_tasks.get(k, [])
                    else None
                )
            )

    async def dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._host._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        # TurnCoordinator owns the per-session lock (weakly held so idle
        # sessions are GC'd) and the global concurrency semaphore. Lock
        # acquisition precedes semaphore acquisition inside ``scope`` so a
        # task waiting on its session lock cannot consume a global permit.
        pending: asyncio.Queue | None = None
        try:
            async with self._coordinator.scope(session_key) as turn_runtime:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self.pending_queues[session_key] = pending
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self._host.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=delta,
                                    metadata=meta,
                                )
                            )

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self._host.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content="",
                                    metadata=meta,
                                )
                            )
                            stream_segment += 1

                    response = await self._host._process_message(
                        msg,
                        on_stream=on_stream,
                        on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    # Legacy direct final Channel send (design §23.1, WP5 task 7).
                    # Runtime tasks bypass this path entirely: they are submitted
                    # via ``submit_durable`` and processed by the Worker through
                    # ``AgentExecutionCallback``, which calls ``_execute_message``
                    # directly and enqueues the final reply to the Outbox.
                    if response is not None:
                        await self._host.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self._host.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=msg.metadata or {},
                            )
                        )
                    if msg.channel == "websocket":
                        await self._host._webui_turns.handle_turn_end(
                            msg,
                            session_key=session_key,
                            latency_ms=turn_runtime.latency_ms,
                            context_usage=turn_runtime.last_call_usage,
                        )
                    await self._host._emit_telemetry(turn_runtime)
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.
                    try:
                        key = self._host._effective_session_key(msg)
                        session = self._host.sessions.get_or_create(key)
                        if self._host._restore_runtime_checkpoint(session):
                            self._host._clear_pending_user_turn(session)
                            self._host.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    turn_runtime.stop_reason = "cancelled"
                    await self._host._emit_telemetry(turn_runtime)
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self._host.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                        )
                    )
                    if not turn_runtime.stop_reason:
                        turn_runtime.stop_reason = "error"
                    await self._host._emit_telemetry(turn_runtime)
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self.pending_queues.get(session_key) is pending:
                        queue = self.pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self._host.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover,
                                session_key,
                            )
                    await self._host._webui_turns.publish_run_status(msg, "idle")
                    self._host._webui_turns.discard(session_key)
        finally:
            if pending is None:
                await self._host._webui_turns.publish_run_status(msg, "idle")
                self._host._webui_turns.discard(session_key)

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
        bound (e.g. by :meth:`dispatch` or :meth:`process_direct`), this method
        simply invokes the host's ``_execute_message`` and copies cumulative
        metrics into the runtime before returning the outbound payload.

        When no runtime is bound (e.g. legacy external callers), this method
        opens a coordinator scope so a fresh runtime is bound for the turn.
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

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: "Callable[..., Awaitable[None]] | None" = None,
        on_stream: "Callable[[str], Awaitable[None]] | None" = None,
        on_stream_end: "Callable[..., Awaitable[None]] | None" = None,
        hooks: list["AgentHook"] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload.

        *hooks*: per-call lifecycle hooks bound to this single turn only.

        Direct calls share the same :class:`TurnCoordinator` as bus
        dispatches: same-session ``process_direct`` calls serialize against
        each other and against any concurrent ``_dispatch`` for the same
        effective session key.
        """
        await self._host._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
        )
        effective_key = session_key
        async with self._coordinator.scope(effective_key) as turn_runtime:
            try:
                response = await self._host._process_message(
                    msg,
                    session_key=effective_key,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                    turn_hooks=hooks,
                )
                await self._host._emit_telemetry(turn_runtime)
                return response
            except asyncio.CancelledError:
                turn_runtime.stop_reason = "cancelled"
                await self._host._emit_telemetry(turn_runtime)
                raise
            except Exception:
                if not turn_runtime.stop_reason:
                    turn_runtime.stop_reason = "error"
                await self._host._emit_telemetry(turn_runtime)
                raise
            finally:
                # WebUI run-status cleanup mirrors dispatch: publish the
                # terminal "idle" status and drop cached title context so a
                # later direct call for the same session starts fresh.
                if channel == "websocket":
                    await self._host._webui_turns.publish_run_status(msg, "idle")
                    self._host._webui_turns.discard(effective_key)

    async def cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self.active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        # 为每个任务等待设置超时，避免 /stop 因单个任务卡死而长时间阻塞
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=10.0)
        # subagents 取消也加超时保护，超时则返回已取消的部分
        try:
            sub_cancelled = await asyncio.wait_for(
                self._host.subagents.cancel_by_session(key), timeout=10.0
            )
        except asyncio.TimeoutError:
            sub_cancelled = 0
        return cancelled + sub_cancelled

