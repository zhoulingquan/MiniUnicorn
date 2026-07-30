"""Tests for TurnCoordinator: session serialization, cross-session overlap, and turn-runtime isolation."""

import asyncio

import pytest

from miniunicorn.agent.turn_coordinator import TurnCoordinator
from miniunicorn.agent.turn_runtime import current_turn_runtime


@pytest.mark.asyncio
async def test_same_session_is_serialized():
    coordinator = TurnCoordinator(max_concurrent_requests=2)
    active = 0
    peak = 0

    async def run_one():
        nonlocal active, peak
        async with coordinator.scope("ws:same"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(run_one(), run_one())
    assert peak == 1


@pytest.mark.asyncio
async def test_different_sessions_overlap():
    coordinator = TurnCoordinator(max_concurrent_requests=2)
    entered = asyncio.Event()
    count = 0

    async def run_one(key):
        nonlocal count
        async with coordinator.scope(key):
            count += 1
            if count == 2:
                entered.set()
            await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.gather(run_one("ws:a"), run_one("ws:b"))
    assert count == 2


@pytest.mark.asyncio
async def test_waiting_same_session_does_not_consume_global_permit():
    coordinator = TurnCoordinator(max_concurrent_requests=2)
    release_a = asyncio.Event()
    b_entered = asyncio.Event()

    async def first_a():
        async with coordinator.scope("ws:a"):
            await release_a.wait()

    async def second_a():
        async with coordinator.scope("ws:a"):
            return

    async def session_b():
        async with coordinator.scope("ws:b"):
            b_entered.set()

    task_a1 = asyncio.create_task(first_a())
    await asyncio.sleep(0)
    task_a2 = asyncio.create_task(second_a())
    await asyncio.sleep(0)
    task_b = asyncio.create_task(session_b())
    await asyncio.wait_for(b_entered.wait(), timeout=1)
    release_a.set()
    await asyncio.gather(task_a1, task_a2, task_b)


@pytest.mark.asyncio
async def test_runtime_is_reset_after_exception():
    coordinator = TurnCoordinator(max_concurrent_requests=1)
    with pytest.raises(RuntimeError, match="boom"):
        async with coordinator.scope("ws:a"):
            assert current_turn_runtime() is not None
            raise RuntimeError("boom")
    assert current_turn_runtime() is None


# NOTE: The entry-point concurrency tests that exercised the legacy
# ``AgentLoop.process_direct`` / ``_dispatch`` in-process locking model were
# removed in design Task 10. Those methods were deleted alongside the
# bus-consume loop; the durable runtime now handles concurrency through
# Worker / Store leases, not in-process locks, so the legacy behavior those
# tests asserted no longer exists in this form.
