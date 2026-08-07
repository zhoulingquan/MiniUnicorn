"""Tests for the AgentRunner adapter extracted from AgentLoop."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.turn_runtime import (
    AgentLoopRunResult,
    TurnRuntime,
    bind_turn_runtime,
    reset_turn_runtime,
)
from miniunicorn.providers.base import LLMResponse


def _make_loop(tmp_path: Path):
    """Create a minimal AgentLoop with mocked dependencies."""
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)

    with (
        patch("miniunicorn.agent.loop.ContextBuilder"),
        patch("miniunicorn.agent.loop.SessionManager"),
        patch("miniunicorn.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


class TestAdapterSignature:
    """Step 1: Characterize the full adapter signature before extraction."""

    def test_run_agent_loop_parameter_names_match_contract(self) -> None:
        from miniunicorn.agent.loop import AgentLoop

        expected = [
            "self",
            "initial_messages",
            "on_progress",
            "on_stream",
            "on_stream_end",
            "on_retry_wait",
            "session",
            "channel",
            "chat_id",
            "message_id",
            "metadata",
            "session_key",
            "pending_queue",
            "agent_override",
            "turn_hooks",
            "turn_query",
        ]
        assert list(inspect.signature(AgentLoop._run_agent_loop).parameters) == expected

    def test_run_agent_loop_return_annotation_is_agent_loop_run_result(self) -> None:
        from miniunicorn.agent.loop import AgentLoop

        annotation = inspect.signature(AgentLoop._run_agent_loop).return_annotation
        # When ``from __future__ import annotations`` is active the annotation
        # may be a string, so accept either the type or its qualified name.
        if annotation is inspect.Signature.empty:
            pytest.fail("return annotation must be AgentLoopRunResult, got empty")
        if annotation is AgentLoopRunResult:
            return
        assert "AgentLoopRunResult" in str(annotation)


class TestAdapterResultForwarding:
    """Step 2: Result forwarding and isolation tests."""

    @pytest.mark.asyncio
    async def test_adapter_returns_same_final_content_messages_and_usage(
        self, tmp_path: Path
    ) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(
                content="hello world",
                tool_calls=[],
                usage={"prompt_tokens": 5, "completion_tokens": 7},
            )
        )
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(return_value="ok")

        runtime = TurnRuntime(turn_id="t-fwd", session_key="cli:c")
        token = bind_turn_runtime(runtime)
        try:
            result = await loop._run_agent_loop([])
        finally:
            reset_turn_runtime(token)

        assert isinstance(result, AgentLoopRunResult)
        assert result.final_content == "hello world"
        assert result.stop_reason == "completed"
        assert result.usage == {"prompt_tokens": 5, "completion_tokens": 7}
        assert result.last_call_usage == {"prompt_tokens": 5, "completion_tokens": 7}

    @pytest.mark.asyncio
    async def test_adapter_does_not_write_loop_level_turn_fields(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="ok", tool_calls=[], usage={})
        )
        loop.tools.get_definitions = MagicMock(return_value=[])

        # The loop must not expose mutable per-turn attributes on itself.
        forbidden = (
            "_last_usage",
            "_last_call_usage",
            "_current_iteration",
            "_pending_turn_latency_ms",
        )
        for name in forbidden:
            assert not hasattr(loop, name) or isinstance(
                getattr(type(loop), name, None), property
            ), f"loop exposes mutable attribute {name!r}"

        runtime = TurnRuntime(turn_id="t-no-shared", session_key="cli:c")
        token = bind_turn_runtime(runtime)
        try:
            await loop._run_agent_loop([])
        finally:
            reset_turn_runtime(token)

        # Loop instance must still not gain those attributes as instance state.
        for name in forbidden:
            assert name not in loop.__dict__, f"adapter wrote loop-level field {name!r}"

    @pytest.mark.asyncio
    async def test_concurrent_calls_isolate_runtime_per_turn(self, tmp_path: Path) -> None:
        """Two concurrent calls must keep distinct TurnRuntime state.

        The adapter is fed distinct ``pending_queue``/``session``/hooks
        and the runner fake surfaces the spec it received. Each runtime
        retains its own usage after completion.
        """
        from miniunicorn.agent.hook import AgentHook
        from miniunicorn.agent.runner import AgentRunResult
        from miniunicorn.session.manager import Session

        loop = _make_loop(tmp_path)
        loop.tools.get_definitions = MagicMock(return_value=[])

        captured_specs: list = []

        async def _fake_run(spec):
            captured_specs.append(spec)
            # Simulate per-call usage so we can verify each runtime ends up
            # with its own values after the adapter copies them.
            key_len = len(spec.session_key or "")
            return AgentRunResult(
                final_content=f"done-{spec.session_key}",
                messages=[],
                tools_used=[],
                usage={"prompt_tokens": key_len, "completion_tokens": 1},
                stop_reason="completed",
                had_injections=False,
                last_call_usage={"prompt_tokens": key_len, "completion_tokens": 1},
            )

        loop.runner.run = _fake_run  # type: ignore[method-assign]

        queue_a: asyncio.Queue = asyncio.Queue()
        queue_b: asyncio.Queue = asyncio.Queue()

        class _HookA(AgentHook):
            pass

        class _HookB(AgentHook):
            pass

        hook_a = _HookA()
        hook_b = _HookB()

        async def _call(session: Session, queue, hook, runtime: TurnRuntime) -> AgentLoopRunResult:
            token = bind_turn_runtime(runtime)
            try:
                return await loop._run_agent_loop(
                    [],
                    session=session,
                    pending_queue=queue,
                    turn_hooks=[hook],
                )
            finally:
                reset_turn_runtime(token)

        runtime_a = TurnRuntime(turn_id="t-a", session_key="cli:a")
        runtime_b = TurnRuntime(turn_id="t-b", session_key="cli:b")

        result_a, result_b = await asyncio.gather(
            _call(Session(key="cli:a"), queue_a, hook_a, runtime_a),
            _call(Session(key="cli:b"), queue_b, hook_b, runtime_b),
        )

        assert len(captured_specs) == 2
        spec_keys = {spec.session_key for spec in captured_specs}
        assert spec_keys == {"cli:a", "cli:b"}

        assert result_a.final_content == "done-cli:a"
        assert result_b.final_content == "done-cli:b"
        # Each runtime should carry its own usage, not the other's.
        assert runtime_a.usage == {"prompt_tokens": len("cli:a"), "completion_tokens": 1}
        assert runtime_b.usage == {"prompt_tokens": len("cli:b"), "completion_tokens": 1}


class TestAdapterHost:
    """Verify the host protocol is constructed on AgentLoop."""

    def test_agent_loop_constructs_agent_run_adapter(self, tmp_path: Path) -> None:
        from miniunicorn.agent.agent_run_adapter import AgentRunAdapter

        loop = _make_loop(tmp_path)
        assert isinstance(loop._agent_run_adapter, AgentRunAdapter)
        assert loop._agent_run_adapter.host is loop
