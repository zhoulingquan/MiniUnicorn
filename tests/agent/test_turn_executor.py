"""Unit tests for the extracted :class:`TurnExecutor`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent._state_machine import TurnContext, TurnState
from miniunicorn.agent.turn_executor import TURN_TRANSITIONS, TurnExecutor
from miniunicorn.agent.turn_runtime import (
    ProcessedTurn,
    TurnRuntime,
    bind_turn_runtime,
    reset_turn_runtime,
)
from miniunicorn.bus.events import InboundMessage, OutboundMessage


class _FakeHost:
    """Minimal host satisfying ``TurnExecutionHost`` for state-machine tests.

    Each state handler appends the active ``TurnState`` to ``visited`` and
    returns the production event for that state. Handlers can be overridden
    per-test by replacing attributes on the instance.
    """

    def __init__(self, *, command_event: str = "dispatch") -> None:
        self.visited: list[TurnState] = []
        self.command_event = command_event
        self.refresh_calls = 0
        self.system_outbound: OutboundMessage | None = None
        self.system_calls = 0
        self.agent_override: Any = None

    def _refresh_provider_snapshot(self) -> None:
        self.refresh_calls += 1

    def _resolve_agent_override(self, msg: InboundMessage) -> Any:
        return self.agent_override

    async def _process_system_message(self, *args: Any, **kwargs: Any) -> OutboundMessage | None:
        self.system_calls += 1
        return self.system_outbound

    async def _state_restore(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        return self.command_event

    async def _state_build(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        self.visited.append(ctx.state)
        ctx.outbound = OutboundMessage(channel="cli", chat_id="direct", content="done")
        return "ok"


def _bind_runtime(*, turn_id: str = "turn:test-sequence", session_key: str = "cli:test"):
    runtime = TurnRuntime(turn_id=turn_id, session_key=session_key)
    token = bind_turn_runtime(runtime)
    return runtime, token


def _make_message(*, channel: str = "cli", content: str = "hello") -> InboundMessage:
    return InboundMessage(channel=channel, sender_id="u1", chat_id="direct", content=content)


# ---------------------------------------------------------------------------
# Step 1: state-sequence unit test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_runs_full_state_sequence_and_returns_processed_turn() -> None:
    host = _FakeHost()
    executor = TurnExecutor(host)
    message = _make_message()

    runtime, token = _bind_runtime()
    try:
        result = await executor.execute(message)
    finally:
        reset_turn_runtime(token)

    assert result.context is not None
    assert result.context.turn_id == "turn:test-sequence"
    assert [entry.state for entry in result.context.trace] == [
        TurnState.RESTORE,
        TurnState.COMPACT,
        TurnState.COMMAND,
        TurnState.BUILD,
        TurnState.RUN,
        TurnState.SAVE,
        TurnState.RESPOND,
    ]
    assert result.context.state is TurnState.DONE
    assert result.outbound is result.context.outbound
    assert host.refresh_calls == 1


# ---------------------------------------------------------------------------
# Step 2: failure and shortcut tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_shortcut_skips_build_run_save_respond() -> None:
    host = _FakeHost(command_event="shortcut")

    async def _command_with_outbound(ctx: TurnContext) -> str:
        host.visited.append(ctx.state)
        ctx.outbound = OutboundMessage(channel="cli", chat_id="direct", content="shortcut!")
        return "shortcut"

    host._state_command = _command_with_outbound  # type: ignore[method-assign]
    executor = TurnExecutor(host)

    runtime, token = _bind_runtime()
    try:
        result = await executor.execute(_make_message())
    finally:
        reset_turn_runtime(token)

    assert [entry.state for entry in result.context.trace] == [
        TurnState.RESTORE,
        TurnState.COMPACT,
        TurnState.COMMAND,
    ]
    assert result.context.state is TurnState.DONE
    assert result.outbound is not None
    assert result.outbound.content == "shortcut!"
    assert TurnState.BUILD not in host.visited
    assert TurnState.RUN not in host.visited
    assert TurnState.SAVE not in host.visited
    assert TurnState.RESPOND not in host.visited


@pytest.mark.asyncio
async def test_missing_handler_raises_runtime_error() -> None:
    host = _FakeHost()
    # Shadow the class method with ``None`` so ``getattr(..., None)`` returns
    # ``None`` and the driver raises ``Missing state handler for ...``.
    host._state_compact = None  # type: ignore[method-assign]
    executor = TurnExecutor(host)

    runtime, token = _bind_runtime()
    try:
        with pytest.raises(RuntimeError, match="Missing state handler for"):
            await executor.execute(_make_message())
    finally:
        reset_turn_runtime(token)


@pytest.mark.asyncio
async def test_unknown_event_raises_no_transition_error() -> None:
    host = _FakeHost()

    async def _bad_compact(ctx: TurnContext) -> str:
        host.visited.append(ctx.state)
        return "bogus-event"

    host._state_compact = _bad_compact  # type: ignore[method-assign]
    executor = TurnExecutor(host)

    runtime, token = _bind_runtime()
    try:
        with pytest.raises(RuntimeError, match="No transition from"):
            await executor.execute(_make_message())
    finally:
        reset_turn_runtime(token)


@pytest.mark.asyncio
async def test_handler_exception_appends_trace_with_error_and_reraises() -> None:
    host = _FakeHost()
    sentinel = RuntimeError("handler-failed")
    captured_ctx: list[TurnContext] = []

    async def _raising_build(ctx: TurnContext) -> str:
        captured_ctx.append(ctx)
        raise sentinel

    host._state_build = _raising_build  # type: ignore[method-assign]
    executor = TurnExecutor(host)

    runtime, token = _bind_runtime()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            await executor.execute(_make_message())
    finally:
        reset_turn_runtime(token)

    assert exc_info.value is sentinel
    assert len(captured_ctx) == 1
    build_entries = [e for e in captured_ctx[0].trace if e.state is TurnState.BUILD]
    assert len(build_entries) == 1
    assert build_entries[0].error == "exception"


@pytest.mark.asyncio
async def test_system_message_returns_processed_turn_without_context() -> None:
    host = _FakeHost()
    host.system_outbound = OutboundMessage(channel="cli", chat_id="direct", content="system-ok")
    executor = TurnExecutor(host)

    runtime, token = _bind_runtime()
    try:
        result = await executor.execute(_make_message(channel="system", content="announce"))
    finally:
        reset_turn_runtime(token)

    assert result.outbound is host.system_outbound
    assert result.context is None
    assert host.system_calls == 1
    assert host.visited == []  # no normal state handler called


def test_turn_transitions_table_matches_expected_states() -> None:
    assert TURN_TRANSITIONS == {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }


# ---------------------------------------------------------------------------
# Step 3: compatibility-chain tests
# ---------------------------------------------------------------------------


def _make_minimal_loop_with_executor(processed_turn: ProcessedTurn) -> Any:
    """Build an AgentLoop shell whose executor is a mock returning ``processed_turn``."""
    from miniunicorn.agent.loop import AgentLoop

    loop = object.__new__(AgentLoop)
    loop._turn_executor = AsyncMock()
    loop._turn_executor.execute = AsyncMock(return_value=processed_turn)
    return loop


@pytest.mark.asyncio
async def test_execute_message_delegates_to_turn_executor() -> None:
    processed = ProcessedTurn(
        outbound=OutboundMessage(channel="cli", chat_id="direct", content="hi"),
        context=MagicMock(spec=TurnContext),
    )
    loop = _make_minimal_loop_with_executor(processed)
    message = _make_message()

    result = await loop._execute_message(message)

    assert result is processed
    loop._turn_executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_message_calls_execute_message_and_returns_outbound() -> None:
    processed = ProcessedTurn(
        outbound=OutboundMessage(channel="cli", chat_id="direct", content="hi"),
        context=None,
    )
    loop = _make_minimal_loop_with_executor(processed)
    message = _make_message()

    # Patch _execute_message to verify the wrapper routes through it.
    loop._execute_message = AsyncMock(return_value=processed)  # type: ignore[method-assign]

    outbound = await loop._process_message(message)

    loop._execute_message.assert_awaited_once()
    assert outbound is processed.outbound
