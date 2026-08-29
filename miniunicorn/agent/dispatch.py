"""Message dispatch engine extracted from the agent loop.

The ``MessageDispatcher`` owns the inbound-message routing machinery that was
formerly part of the agent loop: the run loop, per-session serialization,
cross-session concurrency, command shortcuts, cancellation, background task
tracking, and system-message turns.  The agent loop delegates to it, keeping
the loop focused on turn execution while the dispatcher handles how messages
flow through the process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol
from weakref import WeakValueDictionary

from loguru import logger

from miniunicorn.agent import context as agent_context
from miniunicorn.agent import turn_telemetry
from miniunicorn.bus.events import InboundMessage, OutboundMessage, make_session_key
from miniunicorn.bus.queue import MessageBus
from miniunicorn.command import CommandApplicationService, CommandContext, CommandRouter
from miniunicorn.utils.callback_types import ProgressCallback

if TYPE_CHECKING:
    from miniunicorn.agent.autocompact import AutoCompact
    from miniunicorn.agent.response import ResponseAssembler
    from miniunicorn.agent.runtime_resources import RuntimeResourceRegistry
    from miniunicorn.agent.session_turn import SessionTurnService
    from miniunicorn.agent.tools.registry import ToolRegistry
    from miniunicorn.session.webui_turns import WebuiTurnCoordinator

UNIFIED_SESSION_KEY = "unified:default"

# 主循环/任务管理相关超时 (集中定义, 避免魔法数字散落):
_TASK_CANCEL_WAIT_S = 10.0  # /stop 等待单个被取消任务收尾的上限
_SUBAGENT_CANCEL_WAIT_S = 10.0  # /stop 等待全部子代理取消完成的上限


class DispatchHost(Protocol):
    """Interface the ``MessageDispatcher`` requires from its host agent loop.

    Only the members the dispatcher touches are declared; everything else on
    the host is opaque to this module.
    """

    commands: CommandRouter
    auto_compact: AutoCompact
    dream_idle_trigger: Any
    tools: ToolRegistry
    subagents: Any
    sessions: Any
    workspace_scopes: Any
    context: Any
    _webui_turns: WebuiTurnCoordinator
    _session_turn: SessionTurnService
    _resources: RuntimeResourceRegistry
    _response: ResponseAssembler
    _unified_session: bool
    _max_messages: int

    async def _connect_mcp(self) -> None: ...

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: ProgressCallback | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        turn_hooks: list[Any] | None = None,
    ) -> OutboundMessage | None: ...

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None: ...

    def _replay_token_budget(self) -> int: ...

    async def _run_agent_loop(
        self, initial_messages: list[dict], *args: Any, **kwargs: Any
    ) -> tuple[str | None, list[str], list[dict], str, bool]: ...


class MessageDispatcher:
    """Own the inbound-message routing machinery for the agent loop.

    All mutable routing state lives here: the run flag, per-session active
    tasks and locks, mid-turn pending queues, background tasks, and the global
    concurrency gate.  The host agent loop exposes this state through
    read/write properties so existing callers (commands, tools, tests) keep
    working unchanged.
    """

    def __init__(
        self,
        agent: DispatchHost,
        bus: MessageBus,
        commands: CommandApplicationService | None = None,
    ) -> None:
        self._agent = agent
        self.bus = bus
        self._commands = commands or CommandApplicationService(bus=bus, router=agent.commands)
        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: set[asyncio.Task] = set()
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # MINIUNICORN_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("MINIUNICORN_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._agent._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self._agent.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                # Dream 空闲触发：无活跃会话且有积压数据时后台触发 Dream
                await self._agent.dream_idle_trigger.maybe_trigger(
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                # 兼容写法：低版本 Python 没有 Task.cancelling()
                _task = asyncio.current_task()
                _cancelling = getattr(_task, "cancelling", lambda: 0)() if _task else 0
                if not self._running or _cancelling:
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            # 标记用户有新活动，重置 Dream 空闲触发器的空闲计时器
            self._agent.dream_idle_trigger.notify_user_activity()
            effective_key = self._effective_session_key(msg)
            if await agent_context.handle_runtime_control(self._agent, msg, self._agent.tools):
                continue
            if self._commands.is_priority_command(raw):
                await self._commands.dispatch_priority_inline(
                    self._agent,
                    msg,
                    effective_key,
                    raw,
                )
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self._commands.is_dispatchable_command(raw):
                    await self._commands.dispatch_inline(
                        self._agent,
                        msg,
                        effective_key,
                        raw,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
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
            task = asyncio.create_task(self._agent._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: (
                    self._active_tasks.get(k, []) and self._active_tasks[k].remove(t)
                    if t in self._active_tasks.get(k, [])
                    else None
                )
            )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Compatibility delegate for the agent loop.

        The command execution side lives in ``CommandApplicationService``
        (``miniunicorn/command/service.py``); this thin shim forwards to it so
        the loop's historical surface keeps working unchanged.
        """
        await self._commands._dispatch_inline(self._agent, msg, key, raw, dispatch_fn)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        # 为每个任务等待设置超时，避免 /stop 因单个任务卡死而长时间阻塞。
        # 只吞掉取消信号与等待超时; 取消过程中暴露的真实异常记日志,
        # 不再静默 suppress(此前会掩盖所有错误)。
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=_TASK_CANCEL_WAIT_S)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.exception("Error surfaced while cancelling task in session {}", key)
        # subagents 取消也加超时保护，超时则返回已取消的部分
        try:
            sub_cancelled = await asyncio.wait_for(
                self._agent.subagents.cancel_by_session(key), timeout=_SUBAGENT_CANCEL_WAIT_S
            )
        except asyncio.TimeoutError:
            sub_cancelled = 0
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if msg.session_key_override:
            return msg.session_key_override
        # When a subagent is manually selected via @, isolate its session
        # history under a namespaced key so it does not collide with the
        # parent (main agent) session.
        agent_id = msg.metadata.get("agent_id") if msg.metadata else None
        if agent_id:
            return make_session_key(msg.channel, msg.chat_id, agent_id=agent_id)
        if self._agent._unified_session:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        # WeakValueDictionary: when a session goes idle and all in-flight
        # tasks drop their strong refs to the lock, the entry is GC'd
        # automatically — prevents unbounded growth of session locks for
        # short-lived chats.
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        gate = self._concurrency_gate or nullcontext()

        pending: asyncio.Queue | None = None
        try:
            async with lock, gate:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self._pending_queues[session_key] = pending
                # Bind a fresh per-turn telemetry so usage / iteration survive
                # through turn_end emission (Phase 6 convergence).  The command
                # path keeps the empty telemetry and falls back to the trailing
                # response snapshot, preserving turn_end field behavior.
                telemetry_token = None
                telemetry = turn_telemetry.current()
                if telemetry is None:
                    telemetry = turn_telemetry.TurnTelemetry()
                    telemetry_token = turn_telemetry.bind(telemetry)
                try:
                    on_stream, on_stream_end = self._agent._response.build_stream_closures(msg)

                    response = await self._agent._process_message(
                        msg,
                        on_stream=on_stream,
                        on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata=msg.metadata or {},
                            )
                        )
                    if msg.channel == "websocket":
                        turn_lat = self._agent._response.pop_pending_turn_latency(session_key)
                        await self._agent._webui_turns.handle_turn_end(
                            msg,
                            session_key=session_key,
                            latency_ms=turn_lat,
                            context_usage=telemetry.last_call_usage
                            or self._agent._response._last_call_usage,
                        )
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self._agent.sessions.get_or_create(key)
                        if self._agent._session_turn._restore_runtime_checkpoint(session):
                            self._agent._session_turn._clear_pending_user_turn(session)
                            self._agent.sessions.save(session)
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
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                        )
                    )
                finally:
                    if telemetry_token is not None:
                        turn_telemetry.reset(telemetry_token)
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover,
                                session_key,
                            )
                    await self._agent._webui_turns.publish_run_status(msg, "idle")
                    self._agent._response.pop_pending_turn_latency(session_key)
                    self._agent._webui_turns.discard(session_key)
        finally:
            if pending is None:
                await self._agent._webui_turns.publish_run_status(msg, "idle")
                self._agent._response.pop_pending_turn_latency(session_key)
                self._agent._webui_turns.discard(session_key)

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        # 使用 set 而非 list，回调用 discard 避免 remove 时 KeyError
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: ProgressCallback | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self._agent.sessions.get_or_create(key)
        if self._agent._session_turn._restore_runtime_checkpoint(session):
            self._agent.sessions.save(session)
        if self._agent._session_turn._restore_pending_user_turn(session):
            self._agent.sessions.save(session)

        session, pending = self._agent.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        workspace_scope = self._agent.workspace_scopes.for_message(msg, session.metadata)
        await self._agent._resources._consolidator_for(
            workspace_scope.project_path
        ).maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._agent._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._agent._session_turn._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self._agent.sessions.save(session)
        self._agent._set_tool_context(
            channel,
            chat_id,
            msg.metadata.get("message_id"),
            msg.metadata,
            session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._agent._max_messages,
            "max_tokens": self._agent._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"
        memory_user_key = None
        if is_subagent:
            parent_sender = next(
                (
                    str(message["sender_id"])
                    for message in reversed(session.messages)
                    if message.get("role") == "user"
                    and message.get("sender_id")
                    and message.get("sender_id") != "subagent"
                ),
                None,
            )
            memory_user_key = f"user:{parent_sender}" if parent_sender else "user:default"
        elif msg.sender_id and msg.sender_id != "subagent":
            memory_user_key = f"user:{msg.sender_id}"

        messages = self._agent.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_key=key,
            memory_user_key=memory_user_key,
            session_summary=pending,
            session_metadata=session.metadata,
            workspace=workspace_scope.project_path,
            runtime_state=self._agent,
            inbound_message=msg,
            skip_runtime_lines=is_subagent,
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _ = await self._agent._run_agent_loop(
            messages,
            session=session,
            channel=channel,
            chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            user_key=memory_user_key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._agent._session_turn._save_turn(
            session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms
        )
        if channel == "websocket":
            self._agent._response.record_pending_turn_latency(key, latency_ms)
        session.enforce_file_cap(
            on_archive=self._agent._resources.memory_for(workspace_scope.project_path).raw_archive
        )
        self._agent._session_turn._clear_runtime_checkpoint(session)
        self._agent.sessions.save(session)
        self._schedule_background(
            self._agent._resources._consolidator_for(
                workspace_scope.project_path
            ).maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._agent._max_messages,
            )
        )
        content = final_content or "Background task completed."
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
