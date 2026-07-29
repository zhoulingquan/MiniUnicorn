"""Tests for TurnCoordinator: session serialization, cross-session overlap, and turn-runtime isolation."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.hook import AgentHook
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.turn_coordinator import TurnCoordinator
from miniunicorn.agent.turn_runtime import (
    ProcessedTurn,
    current_turn_runtime,
)
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus


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


# ---------------------------------------------------------------------------
# Entry-point behavioral tests: process_direct and _dispatch route through
# the same coordinator so same-session calls serialize across entry points.
# ---------------------------------------------------------------------------


def _make_loop(tmp_path: Path, *, max_concurrent: int = 2) -> AgentLoop:
    """Build an AgentLoop whose runner/tools are stubbed out.

    Tests patch ``_execute_message`` to record concurrency; the loop's real
    state machine is never invoked. ``process_direct`` and ``_dispatch``
    still go through ``TurnCoordinator.scope`` for serialization.
    """
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    # Force the coordinator's concurrency gate to the test value.
    import os

    os.environ["MINIUNICORN_MAX_CONCURRENT_REQUESTS"] = str(max_concurrent)
    try:
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    finally:
        os.environ.pop("MINIUNICORN_MAX_CONCURRENT_REQUESTS", None)
    # Override the gate after construction so test isolation is exact.
    from miniunicorn.agent.turn_coordinator import TurnCoordinator

    loop._turn_coordinator = TurnCoordinator(max_concurrent_requests=max_concurrent)
    loop._session_locks = loop._turn_coordinator.session_locks
    loop._connect_mcp = AsyncMock()  # type: ignore[method-assign]
    return loop


def _patch_execute_message(loop: AgentLoop, *, tracker: dict) -> AsyncMock:
    """Replace _execute_message with a tracker that records peak concurrency.

    Each invocation sleeps one event-loop tick so concurrent calls overlap
    visibly. ``tracker['peak']`` tracks the high-water mark of simultaneous
    in-flight executions; ``tracker['entered']`` / ``tracker['exited']``
    record ordering.
    """

    active = 0

    async def _fake_execute_message(*args, **kwargs):
        nonlocal active
        active += 1
        tracker["peak"] = max(tracker["peak"], active)
        tracker["entered"].append(kwargs.get("session_key"))
        try:
            await asyncio.sleep(0)
            return ProcessedTurn(outbound=None, context=None)
        finally:
            active -= 1
            tracker["exited"].append(kwargs.get("session_key"))

    return AsyncMock(side_effect=_fake_execute_message)


@pytest.mark.asyncio
async def test_process_direct_same_session_peaks_at_one(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, max_concurrent=2)
    tracker = {"peak": 0, "entered": [], "exited": []}
    loop._execute_message = _patch_execute_message(loop, tracker=tracker)  # type: ignore[method-assign]

    await asyncio.gather(
        loop.process_direct("a", session_key="sdk:a"),
        loop.process_direct("b", session_key="sdk:a"),
    )

    assert tracker["peak"] == 1


@pytest.mark.asyncio
async def test_process_direct_different_sessions_peak_at_two(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, max_concurrent=2)
    tracker = {"peak": 0, "entered": [], "exited": []}
    loop._execute_message = _patch_execute_message(loop, tracker=tracker)  # type: ignore[method-assign]

    await asyncio.gather(
        loop.process_direct("a", session_key="sdk:a"),
        loop.process_direct("b", session_key="sdk:b"),
    )

    assert tracker["peak"] == 2


@pytest.mark.asyncio
async def test_dispatch_and_direct_same_session_peak_at_one(tmp_path: Path) -> None:
    """A bus _dispatch and a direct call using the same effective session key
    must serialize against the same per-session lock."""
    loop = _make_loop(tmp_path, max_concurrent=2)
    tracker = {"peak": 0, "entered": [], "exited": []}
    loop._execute_message = _patch_execute_message(loop, tracker=tracker)  # type: ignore[method-assign]
    # _dispatch reads metadata/_wants_stream and publishes run-status/idle
    # via _webui_turns; stub the WebUI coordinator to avoid bus chatter.
    loop._webui_turns.publish_run_status = AsyncMock()  # type: ignore[method-assign]
    loop._webui_turns.discard = MagicMock()  # type: ignore[method-assign]

    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="x")
    # Force the effective session key to match the direct call below.
    msg = InboundMessage(
        channel="cli",
        sender_id="u1",
        chat_id="c1",
        content="x",
        session_key_override="cli:c1",
    )

    await asyncio.gather(
        loop._dispatch(msg),
        loop.process_direct("y", session_key="cli:c1", channel="cli"),
    )

    assert tracker["peak"] == 1


@pytest.mark.asyncio
async def test_hooks_passed_to_concurrent_direct_calls_stay_isolated(
    tmp_path: Path,
) -> None:
    """Per-call hooks must remain attached to their own ``process_direct`` call
    even when two direct calls run concurrently in different sessions."""

    captured: list[object] = []

    class _RecordingHook(AgentHook):
        def __init__(self, label: str) -> None:
            self._label = label

        async def before_iteration(self, context) -> None:  # type: ignore[override]
            captured.append(self._label)

    async def _fake_execute_message(*args, **kwargs):
        # Simulate the runner invoking per-turn hooks: each call should see
        # only its own hook. We pull turn_hooks out of kwargs.
        hooks = kwargs.get("turn_hooks") or []
        for h in hooks:
            await h.before_iteration(None)
        await asyncio.sleep(0)
        return ProcessedTurn(outbound=None, context=None)

    loop = _make_loop(tmp_path, max_concurrent=2)
    loop._execute_message = AsyncMock(side_effect=_fake_execute_message)  # type: ignore[method-assign]

    await asyncio.gather(
        loop.process_direct("a", session_key="sdk:a", hooks=[_RecordingHook("a")]),
        loop.process_direct("b", session_key="sdk:b", hooks=[_RecordingHook("b")]),
    )

    assert sorted(captured) == ["a", "b"]


@pytest.mark.asyncio
async def test_cancelled_turn_releases_session_lock_and_permit(tmp_path: Path) -> None:
    """A cancelled turn must release both the session lock and the global
    concurrency permit so a follow-up call for the same session can proceed."""
    loop = _make_loop(tmp_path, max_concurrent=1)
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

    first = asyncio.create_task(
        loop.process_direct("slow", session_key="sdk:a", channel="websocket")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    # First call now holds the only global permit and the sdk:a session lock.

    # Second call for the same session must block on the lock; ensure it does
    # not complete before the first is cancelled.
    second = asyncio.create_task(
        loop.process_direct("queued", session_key="sdk:a", channel="websocket")
    )
    await asyncio.sleep(0)
    assert not second.done()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    # After cancellation, the second call should acquire the lock+permit and
    # complete. Release the slow path so _slow_execute returns.
    cancel_released.set()
    await asyncio.wait_for(second, timeout=2)
    assert second.done()
    assert not second.cancelled()


# ---------------------------------------------------------------------------
# Self-inspection regression: concurrent turns must read their own
# iteration/usage from the bound TurnRuntime, not a shared loop field.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_turns_read_own_iteration_and_usage(tmp_path: Path) -> None:
    """Two concurrent direct calls must each see their own iteration and usage
    when reading ``_current_iteration`` / ``_last_usage`` via the loop's
    compatibility properties."""

    loop = _make_loop(tmp_path, max_concurrent=2)

    seen: list[tuple[str, int, dict]] = []

    async def _tracking_execute(msg, **kwargs):
        runtime = current_turn_runtime()
        if runtime is not None:
            if msg.content == "a":
                runtime.iteration = 3
                runtime.usage = {"prompt_tokens": 101}
                runtime.last_call_usage = {"prompt_tokens": 101}
                seen.append(("a", runtime.iteration, dict(runtime.usage)))
            else:
                runtime.iteration = 7
                runtime.usage = {"prompt_tokens": 202}
                runtime.last_call_usage = {"prompt_tokens": 202}
                seen.append(("b", runtime.iteration, dict(runtime.usage)))
        await asyncio.sleep(0)
        return ProcessedTurn(outbound=None, context=None)

    loop._execute_message = AsyncMock(side_effect=_tracking_execute)  # type: ignore[method-assign]

    await asyncio.gather(
        loop.process_direct("a", session_key="sdk:a"),
        loop.process_direct("b", session_key="sdk:b"),
    )

    assert len(seen) == 2
    by_label = {label: (it, usage) for label, it, usage in seen}
    assert by_label["a"] == (3, {"prompt_tokens": 101})
    assert by_label["b"] == (7, {"prompt_tokens": 202})
