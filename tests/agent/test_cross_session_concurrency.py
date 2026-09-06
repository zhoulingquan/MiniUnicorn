"""Cross-session concurrency and same-session serialization behavior tests.

Locks the PR-2a dispatch contract before the ``MessageDispatcher`` extraction:

- messages from *different* sessions may execute concurrently;
- messages from the *same* session are strictly serialized
  (``start-a -> end-a -> start-b -> end-b``).

Overlap assertions use event/flag-based detection instead of wall-clock timing
so the tests stay robust against Windows timer jitter.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from erza.bus.events import InboundMessage, OutboundMessage
from tests.agent.conftest import make_loop


def _make_loop(tmp_path):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return make_loop(tmp_path, provider=provider)


async def _mock_process_overlapping(entered: asyncio.Event, release: asyncio.Event, overlap: dict):
    """Return a _process_message mock that detects when two turns overlap."""

    async def mock_process(msg, **kwargs):
        if overlap["entries"] >= 1:
            overlap["detected"] = True
        overlap["entries"] += 1
        if overlap["entries"] == 2:
            entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=msg.content,
        )

    return mock_process


@pytest.mark.asyncio
async def test_cross_session_messages_execute_concurrently(tmp_path):
    loop = _make_loop(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    overlap = {"entries": 0, "detected": False}
    loop._process_message = await _mock_process_overlapping(entered, release, overlap)

    msg_a = InboundMessage(channel="test", sender_id="u1", chat_id="c-a", content="a")
    msg_b = InboundMessage(channel="test", sender_id="u1", chat_id="c-b", content="b")

    task_a = asyncio.create_task(loop._dispatch(msg_a))
    task_b = asyncio.create_task(loop._dispatch(msg_b))

    # Wait until BOTH turns have entered _process_message (both still blocked on
    # ``release``), then let them finish together.
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert overlap["entries"] == 2
    release.set()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)

    assert overlap["detected"] is True, "Two different sessions must be allowed to run concurrently"


@pytest.mark.asyncio
async def test_same_session_messages_serialize(tmp_path):
    loop = _make_loop(tmp_path)
    order: list[str] = []

    async def mock_process(msg, **kwargs):
        order.append(f"start-{msg.content}")
        await asyncio.sleep(0.05)
        order.append(f"end-{msg.content}")
        return OutboundMessage(channel="test", chat_id=msg.chat_id, content=msg.content)

    loop._process_message = mock_process

    msg1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="a")
    msg2 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="b")

    await asyncio.gather(loop._dispatch(msg1), loop._dispatch(msg2))

    assert order == ["start-a", "end-a", "start-b", "end-b"]
