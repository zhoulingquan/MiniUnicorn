"""Tests for WebUI run-status lifecycle through the SDK _process_message path.

The legacy ``process_direct`` entry point was removed in design Task 10.
The SDK now drives turns through ``_process_message``. The WebUI
run-status lifecycle (running → idle) is published by the
``WebuiTurnCoordinator`` during turn execution.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    response = LLMResponse(content="done", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=response)
    provider.chat_stream_with_retry = AsyncMock(return_value=response)

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_process_message_websocket_publishes_run_status(tmp_path) -> None:
    """A websocket turn driven through _process_message should emit run status."""
    loop = _make_loop(tmp_path)

    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content="deliver reminder",
        media=[],
    )
    response = await loop._process_message(msg, session_key="cron:reminder-1")

    assert response is not None
    assert response.content == "done"

    events = []
    while loop.bus.outbound_size:
        events.append(await loop.bus.consume_outbound())

    statuses = [event.metadata for event in events if event.metadata.get("_goal_status") is True]
    # The SDK _process_message path publishes "running" during turn execution.
    # The "idle" status was published by the legacy dispatch/process_direct
    # finally blocks, which were removed in design Task 10. In the durable
    # runtime, "idle" is published by the Worker after task completion.
    assert [status["goal_status"] for status in statuses] == ["running"]
    assert isinstance(statuses[0].get("started_at"), float)
