"""Tests for TurnDispatcher: turn dispatch and entry-point coordination.

Verifies the extraction of ``run()``, ``_dispatch()``, ``_process_message``,
``process_direct()``, and ``_cancel_active_tasks()`` from :class:`AgentLoop`
into :class:`TurnDispatcher` preserves ownership invariants, monkeypatch
seams, and dispatch lifecycle behavior.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.turn_runtime import ProcessedTurn
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus


def _make_loop(tmp_path: Path, **kwargs: Any):
    """Build an AgentLoop with heavy deps patched for dispatch-level tests."""
    from miniunicorn.agent.loop import AgentLoop

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with (
        patch("miniunicorn.agent.loop.ContextBuilder"),
        patch("miniunicorn.agent.loop.SessionManager"),
        patch("miniunicorn.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# TestOwnershipAndAliases — registries live on the dispatcher
# ---------------------------------------------------------------------------


class TestOwnershipAndAliases:
    """``_active_tasks`` / ``_pending_queues`` are owned by TurnDispatcher."""

    def test_active_tasks_is_shared(self, loop_factory):
        loop = loop_factory()
        assert loop._active_tasks is loop._turn_dispatcher.active_tasks

    def test_pending_queues_is_shared(self, loop_factory):
        loop = loop_factory()
        assert loop._pending_queues is loop._turn_dispatcher.pending_queues

    def test_host_is_loop(self, loop_factory):
        loop = loop_factory()
        assert loop._turn_dispatcher.host is loop

    def test_active_tasks_assignment_raises(self, loop_factory):
        loop = loop_factory()
        with pytest.raises(AttributeError):
            loop._active_tasks = {}

    def test_pending_queues_assignment_raises(self, loop_factory):
        loop = loop_factory()
        with pytest.raises(AttributeError):
            loop._pending_queues = {}


# ---------------------------------------------------------------------------
# TestMonkeypatchSeams — existing seams stay patchable on the loop instance
# ---------------------------------------------------------------------------


class TestMonkeypatchSeams:
    """Patching loop._dispatch / loop._process_message / loop._execute_message
    must still intercept calls routed through TurnDispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_awaits_patched_process_message(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._process_message = AsyncMock(return_value=None)  # type: ignore[method-assign]
        loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]
        loop._webui_turns.discard = MagicMock()  # type: ignore[method-assign]
        loop._emit_telemetry = AsyncMock()  # type: ignore[method-assign]

        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        await loop._dispatch(msg)

        loop._process_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_awaits_patched_execute_message_through_chain(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._execute_message = AsyncMock(  # type: ignore[method-assign]
            return_value=ProcessedTurn(outbound=None, context=None)
        )
        loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]
        loop._webui_turns.discard = MagicMock()  # type: ignore[method-assign]
        loop._emit_telemetry = AsyncMock()  # type: ignore[method-assign]

        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        await loop._dispatch(msg)

        loop._execute_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_schedules_patched_dispatch(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._dispatch = AsyncMock()  # type: ignore[method-assign]
        loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]

        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        run_task = asyncio.create_task(loop.run())
        await loop.bus.publish_inbound(msg)

        deadline = asyncio.get_event_loop().time() + 2.0
        while loop._dispatch.await_count == 0 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

        loop.stop()
        await asyncio.wait_for(run_task, timeout=2.0)

        assert loop._dispatch.await_count == 1
        dispatched_msg = loop._dispatch.await_args.args[0]
        assert dispatched_msg.content == "hi"


# ---------------------------------------------------------------------------
# TestDispatchLifecycle — end-to-end dispatch behavior
# ---------------------------------------------------------------------------


class TestDispatchLifecycle:
    """Queues, cleanup, cancellation, task tracking, and MCP setup."""

    @pytest.mark.asyncio
    async def test_pending_queue_published_only_while_scope_owned(self, tmp_path: Path):
        """The pending queue exists only while the coordinator scope is held."""
        loop = _make_loop(tmp_path)

        seen_with_queue: list[str] = []

        async def _fake_execute(msg, **kwargs):
            if msg.session_key in loop._pending_queues:
                seen_with_queue.append(msg.session_key)
            return ProcessedTurn(outbound=None, context=None)

        loop._execute_message = AsyncMock(side_effect=_fake_execute)  # type: ignore[method-assign]
        loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]
        loop._webui_turns.discard = MagicMock()  # type: ignore[method-assign]
        loop._emit_telemetry = AsyncMock()  # type: ignore[method-assign]

        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        assert msg.session_key not in loop._pending_queues

        await loop._dispatch(msg)

        assert seen_with_queue == [msg.session_key]
        assert msg.session_key not in loop._pending_queues

    @pytest.mark.asyncio
    async def test_pending_queue_cleanup_is_identity_checked(self, tmp_path: Path):
        """A waiting dispatch must not steal cleanup of the active queue."""
        loop = _make_loop(tmp_path)

        session_key = "cli:c"
        lock = loop._session_locks.setdefault(session_key, asyncio.Lock())
        await lock.acquire()
        active_pending = asyncio.Queue(maxsize=1)
        loop._pending_queues[session_key] = active_pending

        waiting_at_lock = asyncio.Event()
        original_acquire = asyncio.Lock.acquire

        async def _patched_acquire(self, *args, **kwargs):
            if self is lock:
                waiting_at_lock.set()
            return await original_acquire(self, *args, **kwargs)

        with patch.object(asyncio.Lock, "acquire", _patched_acquire):
            waiting = asyncio.create_task(
                loop._dispatch(
                    InboundMessage(channel="cli", sender_id="u", chat_id="c", content="queued")
                )
            )
            await asyncio.wait_for(waiting_at_lock.wait(), timeout=2.0)

        assert loop._pending_queues[session_key] is active_pending

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        lock.release()

        assert loop._pending_queues[session_key] is active_pending

    @pytest.mark.asyncio
    async def test_same_session_followup_injected_not_duplicated(self, tmp_path: Path):
        """A follow-up for an active session is injected, not dispatched twice."""
        loop = _make_loop(tmp_path)
        loop._unified_session = True
        loop._dispatch = AsyncMock()  # type: ignore[method-assign]
        loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]

        from miniunicorn.agent.loop import UNIFIED_SESSION_KEY

        pending = asyncio.Queue(maxsize=20)
        loop._pending_queues[UNIFIED_SESSION_KEY] = pending

        run_task = asyncio.create_task(loop.run())
        msg = InboundMessage(channel="discord", sender_id="u", chat_id="c", content="follow-up")
        await loop.bus.publish_inbound(msg)

        deadline = asyncio.get_event_loop().time() + 2.0
        while pending.empty() and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

        loop.stop()
        await asyncio.wait_for(run_task, timeout=2.0)

        assert loop._dispatch.await_count == 0
        assert not pending.empty()
        queued_msg = pending.get_nowait()
        assert queued_msg.content == "follow-up"
        assert queued_msg.session_key == UNIFIED_SESSION_KEY

    @pytest.mark.asyncio
    async def test_cancellation_releases_coordinator_lock_and_permit(self, tmp_path: Path):
        """A cancelled dispatch must release the session lock and global permit."""
        max_concurrent = 1
        os.environ["MINIUNICORN_MAX_CONCURRENT_REQUESTS"] = str(max_concurrent)
        try:
            loop = _make_loop(tmp_path)
        finally:
            os.environ.pop("MINIUNICORN_MAX_CONCURRENT_REQUESTS", None)

        from miniunicorn.agent.turn_coordinator import TurnCoordinator

        loop._turn_coordinator = TurnCoordinator(max_concurrent_requests=max_concurrent)
        loop._session_locks = loop._turn_coordinator.session_locks

        started = asyncio.Event()
        cancel_released = asyncio.Event()

        async def _slow_execute(*args, **kwargs):
            started.set()
            try:
                await asyncio.wait_for(cancel_released.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            return ProcessedTurn(outbound=None, context=None)

        loop._execute_message = AsyncMock(side_effect=_slow_execute)  # type: ignore[method-assign]
        loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]
        loop._webui_turns.discard = MagicMock()  # type: ignore[method-assign]
        loop._emit_telemetry = AsyncMock()  # type: ignore[method-assign]

        first = asyncio.create_task(
            loop._dispatch(
                InboundMessage(
                    channel="websocket",
                    sender_id="u",
                    chat_id="c",
                    content="slow",
                    session_key_override="ws:c",
                )
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        second = asyncio.create_task(
            loop._dispatch(
                InboundMessage(
                    channel="websocket",
                    sender_id="u",
                    chat_id="c",
                    content="queued",
                    session_key_override="ws:c",
                )
            )
        )
        await asyncio.sleep(0)
        assert not second.done()

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        cancel_released.set()
        await asyncio.wait_for(second, timeout=2.0)
        assert second.done()
        assert not second.cancelled()

    @pytest.mark.asyncio
    async def test_active_tasks_removes_completed_without_removing_siblings(self, tmp_path: Path):
        """A completed task is removed from active_tasks; siblings remain."""
        loop = _make_loop(tmp_path)
        loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]
        loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]
        loop._webui_turns.discard = MagicMock()  # type: ignore[method-assign]
        loop._emit_telemetry = AsyncMock()  # type: ignore[method-assign]

        release_slow = asyncio.Event()
        started_slow = asyncio.Event()

        async def _execute(msg, **kwargs):
            if msg.content == "slow":
                started_slow.set()
                await release_slow.wait()
            return ProcessedTurn(outbound=None, context=None)

        loop._execute_message = AsyncMock(side_effect=_execute)  # type: ignore[method-assign]

        msg_fast = InboundMessage(channel="cli", sender_id="u", chat_id="fast", content="fast")
        msg_slow = InboundMessage(channel="cli", sender_id="u", chat_id="slow", content="slow")

        await loop.bus.publish_inbound(msg_fast)
        await loop.bus.publish_inbound(msg_slow)

        run_task = asyncio.create_task(loop.run())

        await asyncio.wait_for(started_slow.wait(), timeout=2.0)
        await asyncio.sleep(0.1)  # Let done-callbacks fire.

        # The fast task completed and was removed; the slow task remains.
        assert "cli:slow" in loop._active_tasks
        assert len(loop._active_tasks["cli:slow"]) == 1
        assert "cli:fast" not in loop._active_tasks or not loop._active_tasks["cli:fast"]

        release_slow.set()
        loop.stop()
        await asyncio.wait_for(run_task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_process_direct_always_runs_mcp_connection_setup(self, tmp_path: Path):
        """process_direct must call _connect_mcp before any turn work."""
        loop = _make_loop(tmp_path)
        loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]
        loop._execute_message = AsyncMock(  # type: ignore[method-assign]
            return_value=ProcessedTurn(outbound=None, context=None)
        )
        loop._emit_telemetry = AsyncMock()  # type: ignore[method-assign]

        await loop.process_direct("hi", session_key="cli:direct")

        loop._connect_mcp.assert_awaited_once()
