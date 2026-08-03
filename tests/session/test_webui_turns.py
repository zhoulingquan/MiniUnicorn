"""Cross-session turn-end regression tests.

Verifies that concurrent WebSocket turns emit their own ``context_usage``
in ``turn_end`` metadata rather than reading a shared loop-global field.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent._state_machine import TurnContext, TurnState
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.turn_runtime import ProcessedTurn
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with (
        patch("miniunicorn.agent.loop.ContextBuilder"),
        patch("miniunicorn.agent.loop.SessionManager"),
        patch("miniunicorn.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]
    return loop


def _make_ctx(
    msg: InboundMessage, *, usage: dict[str, int], last_call_usage: dict[str, int]
) -> TurnContext:
    return TurnContext(
        msg=msg,
        session_key=msg.session_key,
        state=TurnState.DONE,
        turn_id="test",
        usage=dict(usage),
        last_call_usage=dict(last_call_usage),
        turn_latency_ms=42,
    )


@pytest.mark.asyncio
async def test_concurrent_turns_emit_own_context_usage(tmp_path: Path) -> None:
    """Two concurrent websocket turns must each emit their own context_usage."""
    loop = _make_loop(tmp_path)

    usage_a = {"prompt_tokens": 101, "completion_tokens": 11}
    usage_b = {"prompt_tokens": 202, "completion_tokens": 22}

    msg_a = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="c1",
        content="a",
        session_key_override="ws:c1",
    )
    msg_b = InboundMessage(
        channel="websocket",
        sender_id="u2",
        chat_id="c2",
        content="b",
        session_key_override="ws:c2",
    )

    ctx_a = _make_ctx(msg_a, usage=usage_a, last_call_usage=usage_a)
    ctx_b = _make_ctx(msg_b, usage=usage_b, last_call_usage=usage_b)

    barrier = asyncio.Event()

    captured_contexts: dict[str, TurnContext] = {}

    async def _fake_execute_a(*args, **kwargs):
        await barrier.wait()
        return ProcessedTurn(outbound=None, context=ctx_a)

    async def _fake_execute_b(*args, **kwargs):
        await barrier.wait()
        return ProcessedTurn(outbound=None, context=ctx_b)

    # Patch _execute_message to dispatch based on session key
    async def _execute_for_message(msg, **kwargs):
        if msg.session_key == "ws:c1":
            captured_contexts[msg.session_key] = ctx_a
            return await _fake_execute_a(msg, **kwargs)
        captured_contexts[msg.session_key] = ctx_b
        return await _fake_execute_b(msg, **kwargs)

    loop._execute_message = AsyncMock(side_effect=_execute_for_message)  # type: ignore[method-assign]
    loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]

    # Patch _state_save path: we need sessions.get_or_create to return a mock
    # session for the turn_end handler.
    mock_session = MagicMock()
    mock_session.metadata = {}
    loop.sessions.get_or_create = MagicMock(return_value=mock_session)  # type: ignore[method-assign]

    async def _dispatch_via_seam(msg):
        """Simulate the old dispatch flow: process_message + publish + turn_end."""
        response = await loop.core_dispatcher.process_message(msg)
        if response is not None:
            await loop.bus.publish_outbound(response)
        if msg.channel == "websocket":
            ctx = captured_contexts[msg.session_key]
            await loop._webui_turns.handle_turn_end(
                msg,
                session_key=msg.session_key,
                latency_ms=ctx.turn_latency_ms,
                context_usage=ctx.last_call_usage,
            )

    tasks = [
        asyncio.create_task(_dispatch_via_seam(msg_a)),
        asyncio.create_task(_dispatch_via_seam(msg_b)),
    ]
    # Release both turns simultaneously so they overlap.
    barrier.set()
    await asyncio.gather(*tasks)

    # Collect all outbound messages (turn_end messages have _turn_end metadata)
    turn_end_msgs = []
    for _ in range(4):  # 2 turns * 2 msgs each (response + turn_end)
        try:
            out = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)
            if out.metadata.get("_turn_end"):
                turn_end_msgs.append(out)
        except asyncio.TimeoutError:
            break

    assert len(turn_end_msgs) == 2, f"Expected 2 turn_end messages, got {len(turn_end_msgs)}"

    # Each turn_end should carry its own context_usage
    usage_values = sorted(m.metadata["context_usage"]["prompt_tokens"] for m in turn_end_msgs)
    assert usage_values == [101, 202], (
        f"Expected context_usage prompt_tokens [101, 202], got {usage_values}"
    )
