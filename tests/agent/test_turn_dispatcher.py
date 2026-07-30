"""Tests for TurnDispatcher: Agent Core execution helper.

Verifies the ``process_message`` normalization bridge and the ``host``
accessor. Legacy bus-consume loop, dispatch, process_direct,
pending_queues, and active_tasks were removed in design Task 10.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.turn_runtime import ProcessedTurn
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus


def _make_loop(tmp_path: Path, **kwargs: Any):
    """Build an AgentLoop with heavy deps patched for dispatcher tests."""
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
# TestOwnershipAndHost — the dispatcher exposes the host read-only
# ---------------------------------------------------------------------------


class TestOwnershipAndHost:
    """The dispatcher exposes its host read-only for AgentExecutionCallback."""

    def test_host_is_loop(self, loop_factory):
        loop = loop_factory()
        assert loop._turn_dispatcher.host is loop


# ---------------------------------------------------------------------------
# TestProcessMessageBridge — the surviving normalization helper
# ---------------------------------------------------------------------------


class TestProcessMessageBridge:
    """``process_message`` opens a coordinator scope and calls _execute_message."""

    @pytest.mark.asyncio
    async def test_process_message_awaits_patched_execute_message(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._execute_message = AsyncMock(  # type: ignore[method-assign]
            return_value=ProcessedTurn(outbound=None, context=None)
        )

        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        await loop._turn_dispatcher.process_message(msg)

        loop._execute_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_process_message_returns_outbound_payload(self, tmp_path: Path):
        from miniunicorn.bus.events import OutboundMessage

        loop = _make_loop(tmp_path)
        outbound = OutboundMessage(channel="cli", chat_id="c", content="hello back")
        loop._execute_message = AsyncMock(  # type: ignore[method-assign]
            return_value=ProcessedTurn(outbound=outbound, context=None)
        )

        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        result = await loop._turn_dispatcher.process_message(msg)

        assert result is outbound

    @pytest.mark.asyncio
    async def test_process_message_forwards_turn_hooks(self, tmp_path: Path):
        from miniunicorn.agent.hook import AgentHook

        loop = _make_loop(tmp_path)
        loop._execute_message = AsyncMock(  # type: ignore[method-assign]
            return_value=ProcessedTurn(outbound=None, context=None)
        )

        hook = AgentHook()
        msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi")
        await loop._turn_dispatcher.process_message(msg, turn_hooks=[hook])

        passed_hooks = loop._execute_message.await_args.kwargs["turn_hooks"]
        assert passed_hooks == [hook]


# ---------------------------------------------------------------------------
# TestLegacySurfaceRemoved — inventory guards against regression
# ---------------------------------------------------------------------------


class TestLegacySurfaceRemoved:
    """The dispatcher must not reintroduce legacy process-local authority."""

    def test_dispatcher_has_no_run_method(self) -> None:
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        assert not hasattr(TurnDispatcher, "run"), (
            "TurnDispatcher.run was removed in design Task 10."
        )

    def test_dispatcher_has_no_dispatch_method(self) -> None:
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        assert not hasattr(TurnDispatcher, "dispatch"), (
            "TurnDispatcher.dispatch was removed in design Task 10."
        )

    def test_dispatcher_has_no_process_direct_method(self) -> None:
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        assert not hasattr(TurnDispatcher, "process_direct"), (
            "TurnDispatcher.process_direct was removed in design Task 10."
        )

    def test_dispatcher_has_no_cancel_active_tasks_method(self) -> None:
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        assert not hasattr(TurnDispatcher, "cancel_active_tasks"), (
            "TurnDispatcher.cancel_active_tasks was removed in design Task 10."
        )
