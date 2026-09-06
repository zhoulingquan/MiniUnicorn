"""W3-2: ``AgentLoop._run_agent_loop`` split into seven extraction methods.

The split is a pure-move refactor. These tests pin the structural and
behavioral invariants:

1. ``_drain_pending_messages`` unit semantics (None-queue early return,
   ``limit`` cap, empty queue, subagent block-wait timeout).
2. The injection callback contract: ``inspect.signature`` on the
   ``functools.partial`` binding still exposes ``limit`` — this is what
   runner.py ``drain_injections`` dispatches on to pass
   ``limit=_MAX_INJECTIONS_PER_TURN`` (batched pulls).
3. ``_run_agent_loop`` stays a thin orchestrator (line budget).
4. Hook merge order: per-run ``turn_hooks`` after loop-level ``_extra_hooks``.
5. Minimal smoke: a full ``_run_agent_loop`` turn with a pending follow-up.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger

from erza.agent.hook import AgentHook, CompositeHook
from erza.agent.loop import AgentLoop
from erza.agent.progress_hook import AgentProgressHook
from erza.agent.runner import _MAX_INJECTIONS_PER_TURN
from erza.bus.events import InboundMessage
from erza.bus.queue import MessageBus
from erza.providers.base import LLMResponse
from tests.agent.conftest import make_loop, make_provider


def _drain_session() -> SimpleNamespace:
    """Minimal session stand-in (only ``key``/``metadata`` are consumed)."""
    return SimpleNamespace(key="cli:c1", metadata={})


def _queued_queue(*contents: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    for content in contents:
        queue.put_nowait(
            InboundMessage(channel="cli", sender_id="u", chat_id="c1", content=content)
        )
    return queue


# ---------------------------------------------------------------------------
# 1. _drain_pending_messages unit semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_pending_messages_none_queue_returns_empty(tmp_path) -> None:
    loop = make_loop(tmp_path, provider=make_provider(spec=False))

    items = await loop._drain_pending_messages(None, _drain_session())

    assert items == []


@pytest.mark.asyncio
async def test_drain_pending_messages_returns_user_messages(tmp_path) -> None:
    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    queue = _queued_queue("first", "second")

    items = await loop._drain_pending_messages(queue, _drain_session(), limit=2)

    assert [(m["role"], m["content"]) for m in items] == [
        ("user", "first"),
        ("user", "second"),
    ]


@pytest.mark.asyncio
async def test_drain_pending_messages_limit_caps_batch(tmp_path) -> None:
    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    queue = _queued_queue("first", "second")

    items = await loop._drain_pending_messages(queue, _drain_session(), limit=1)

    assert [m["content"] for m in items] == ["first"]
    # The overflow stays queued for the next injection cycle.
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_drain_pending_messages_empty_queue_no_subagent(tmp_path) -> None:
    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    queue: asyncio.Queue = asyncio.Queue()

    items = await loop._drain_pending_messages(queue, _drain_session())

    assert items == []


@pytest.mark.asyncio
async def test_drain_pending_messages_block_wait_timeout(tmp_path, monkeypatch) -> None:
    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    queue: asyncio.Queue = asyncio.Queue()
    session = _drain_session()
    monkeypatch.setattr(loop.subagents, "get_running_count_by_session", lambda key: 1)

    async def _timeout_wait_for(fut, timeout=None):
        fut.close()  # close the unawaited Queue.get() coroutine
        raise asyncio.TimeoutError()

    monkeypatch.setattr("erza.agent.loop.asyncio.wait_for", _timeout_wait_for)

    records: list[str] = []
    handler_id = logger.add(records.append, level="WARNING", format="{level.name}: {message}")
    try:
        items = await loop._drain_pending_messages(queue, session)
    finally:
        logger.remove(handler_id)

    assert items == []
    timeout_warnings = [r for r in records if r.startswith("WARNING:")]
    assert timeout_warnings, "expected a TimeoutError warning"
    assert "cli:c1" in timeout_warnings[0]


# ---------------------------------------------------------------------------
# 2. Injection callback contract (runner.py drain_injections dispatch)
# ---------------------------------------------------------------------------


def test_injection_callback_partial_signature_keeps_limit(tmp_path) -> None:
    """``inspect.signature(partial(...))`` must still expose ``limit``.

    runner.py drains via ``injection_callback(limit=_MAX_INJECTIONS_PER_TURN)``
    only when the resolved signature has a ``limit`` parameter; losing it would
    silently degrade injection draining from batched to single-message pulls.
    """
    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    callback = functools.partial(loop._drain_pending_messages, asyncio.Queue(), _drain_session())

    parameters = inspect.signature(callback).parameters
    assert "limit" in parameters
    assert parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["limit"].default == _MAX_INJECTIONS_PER_TURN


# ---------------------------------------------------------------------------
# 3. Structural line budget
# ---------------------------------------------------------------------------


def test_run_agent_loop_stays_below_130_lines() -> None:
    """``_run_agent_loop`` must remain a thin orchestrator after the split."""
    source = inspect.getsource(AgentLoop._run_agent_loop)
    line_count = len(source.strip().splitlines())
    assert line_count < 130, f"_run_agent_loop regressed to {line_count} lines"


# ---------------------------------------------------------------------------
# 4. Hook merge order: turn_hooks after _extra_hooks
# ---------------------------------------------------------------------------


class _MarkerHook(AgentHook):
    """Identifiable hook for merge-order assertions."""


def test_turn_hooks_merge_after_extra_hooks(tmp_path) -> None:
    extra_hook = _MarkerHook()
    turn_hook = _MarkerHook()
    loop = make_loop(tmp_path, provider=make_provider(spec=False), hooks=[extra_hook])

    hook = loop._build_turn_hook(
        None,
        None,
        None,
        None,
        channel="cli",
        chat_id="c1",
        message_id=None,
        metadata=None,
        session_key=None,
        turn_hooks=[turn_hook],
    )

    assert isinstance(hook, CompositeHook)
    assert isinstance(hook._hooks[0], AgentProgressHook)
    assert hook._hooks.index(extra_hook) < hook._hooks.index(turn_hook)


def test_turn_hook_without_extras_is_not_composite(tmp_path) -> None:
    loop = make_loop(tmp_path, provider=make_provider(spec=False))

    hook = loop._build_turn_hook(
        None,
        None,
        None,
        None,
        channel="cli",
        chat_id="c1",
        message_id=None,
        metadata=None,
        session_key=None,
        turn_hooks=None,
    )

    assert isinstance(hook, AgentProgressHook)


# ---------------------------------------------------------------------------
# 5. Minimal smoke: a full turn with a pending follow-up injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_loop_smoke_with_pending_queue(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(content="first answer", tool_calls=[], usage={})
        return LLMResponse(content="second answer", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])

    pending_queue: asyncio.Queue = asyncio.Queue()
    pending_queue.put_nowait(
        InboundMessage(channel="cli", sender_id="u", chat_id="c", content="follow-up")
    )

    final_content, tools_used, messages, stop_reason, had_injections = await loop._run_agent_loop(
        [{"role": "user", "content": "hello"}],
        channel="cli",
        chat_id="c",
        pending_queue=pending_queue,
    )

    assert final_content == "second answer"
    assert had_injections is True
    assert call_count["n"] == 2
    assert pending_queue.empty()
