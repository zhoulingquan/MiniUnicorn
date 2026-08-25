"""Tests for turn-wide CallLedger binding and accounting."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.call_ledger import (
    CallLedger,
    CallPurpose,
    bind_call_ledger,
    call_purpose,
    current_call_ledger,
)
from miniunicorn.agent.execution.model_request import ModelRequestExecutor
from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.reflection import Reflection
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.turn_budget import TurnBudget
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import GenerationSettings, LLMProvider, LLMResponse


def _usage(prompt: int, completion: int, cost: float | None = None) -> dict[str, int | float]:
    d: dict[str, int | float] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        d["cost_usd"] = cost
    return d


class _ScriptedProvider(LLMProvider):
    """Minimal provider that returns scripted responses and records to ledger via chat_with_retry."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self._call_count = 0
        self.generation = GenerationSettings()

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ) -> LLMResponse:
        self._call_count += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


class _ScriptedProviderWithCounter(LLMProvider):
    """Minimal provider with call counter for planner budget test."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.call_count = 0
        self.generation = GenerationSettings()

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ) -> LLMResponse:
        self.call_count += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


class TestTurnCallLedger:
    """Test ledger binding across the whole turn and direct runner runs."""

    @pytest.mark.asyncio
    async def test_compact_planner_executor_usage_summed(self, tmp_path: Path) -> None:
        """Compact + planner + executor usage should be summed in the turn ledger."""
        bus = MessageBus()

        responses = [
            LLMResponse(
                content=(
                    '{"goal":"do task","steps":['
                    '{"id":1,"action":"do task","tool_hint":null}]}'
                ),
                tool_calls=[],
                usage=_usage(100, 20, 0.002),
            ),
            LLMResponse(content="done", tool_calls=[], usage=_usage(10, 5, 0.0005)),
        ]
        provider = _ScriptedProvider(responses)

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            use_planner=True,
            planner_model="test-model",
        )
        loop.tools.get_definitions = MagicMock(return_value=[
            {"type": "function", "function": {"name": "list_dir", "description": "List dir"}}
        ])
        loop.tools.execute = AsyncMock(return_value="tool result")

        auto_compact = loop._turn_orchestrator._deps.resources.auto_compact
        original_prepare = auto_compact.prepare_session

        def _prepare_with_compact_usage(session, session_key):
            ledger = current_call_ledger()
            assert ledger is not None
            ledger.record(
                model="test-model",
                usage=_usage(50, 10, 0.001),
                finish_reason="stop",
                purpose=CallPurpose.COMPACT,
            )
            return original_prepare(session, session_key)

        auto_compact.prepare_session = _prepare_with_compact_usage

        await loop._dispatch(
            InboundMessage(channel="websocket", sender_id="u1", chat_id="chat1", content="do task")
        )
        await asyncio.sleep(0.01)  # let background tasks settle

        outbound = []
        while bus.outbound_size > 0:
            outbound.append(await bus.consume_outbound())

        turn_end = [m for m in outbound if m.metadata.get("_turn_end")]
        assert len(turn_end) == 1
        event = turn_end[0]
        # turn_end context_usage intentionally exposes only the last call;
        # the trailing runner snapshot contains the complete turn ledger.
        usage = loop._response._last_usage
        assert usage["prompt_tokens"] == 160  # 50 + 100 + 10
        assert usage["completion_tokens"] == 35  # 10 + 20 + 5
        assert abs(usage.get("cost_usd", 0) - 0.0035) < 0.0001  # 0.001 + 0.002 + 0.0005
        assert event.metadata["context_usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 0,
            "cached_tokens": 0,
        }
        assert provider._call_count == 2

    @pytest.mark.asyncio
    async def test_planner_budget_exhaustion_prevents_executor(self, tmp_path: Path) -> None:
        """Planner-only budget exhaustion should prevent the first executor call."""
        bus = MessageBus()

        responses = [
            LLMResponse(
                content=(
                    '{"goal":"do task","steps":['
                    '{"id":1,"action":"do task","tool_hint":null}]}'
                ),
                tool_calls=[],
                usage=_usage(100, 20, 0.05),
            ),
        ]
        provider = _ScriptedProviderWithCounter(responses)

        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            use_planner=True,
            planner_model="test-model",
            max_cost_per_turn_usd=0.03,
        )
        loop.tools.get_definitions = MagicMock(return_value=[
            {"type": "function", "function": {"name": "list_dir", "description": "List dir"}}
        ])
        loop.tools.execute = AsyncMock(return_value="tool result")

        auto_compact = loop._turn_orchestrator._deps.resources.auto_compact
        original_prepare = auto_compact.prepare_session

        def _prepare_with_compact_usage(session, session_key):
            ledger = current_call_ledger()
            assert ledger is not None
            ledger.record(
                model="test-model",
                usage=_usage(50, 10, 0.001),
                finish_reason="stop",
                purpose=CallPurpose.COMPACT,
            )
            return original_prepare(session, session_key)

        auto_compact.prepare_session = _prepare_with_compact_usage

        await loop._dispatch(
            InboundMessage(channel="websocket", sender_id="u1", chat_id="chat1", content="do task")
        )

        assert provider.call_count == 1, f"Executor should not run; got {provider.call_count} calls"

    @pytest.mark.asyncio
    async def test_direct_runner_run_creates_local_ledger(self, tmp_path: Path) -> None:
        """Direct AgentRunner.run() should create and bind a local ledger when none exists."""
        provider = _ScriptedProvider([
            LLMResponse(content="done", tool_calls=[], usage=_usage(10, 5, 0.001))
        ])

        tools = MagicMock()
        tools.get_definitions.return_value = []
        tools.execute = AsyncMock(return_value="tool result")

        runner = AgentRunner(provider)
        result = await runner.run(
            AgentRunSpec(
                initial_messages=[{"role": "user", "content": "hello"}],
                tools=tools,
                model="test-model",
                max_iterations=3,
                max_tool_result_chars=10000,
                turn_budget=TurnBudget(max_cost_usd=1.0),
            )
        )

        # Result usage should come from the ledger
        assert result.usage == _usage(10, 5, 0.001)
        assert result.last_call_usage == _usage(10, 5, 0.001)

    @pytest.mark.asyncio
    async def test_empty_final_usage_does_not_reuse_previous_call_usage(
        self,
        tmp_path: Path,
    ) -> None:
        provider = _ScriptedProvider([
            LLMResponse(content="", tool_calls=[], usage=_usage(10, 1)),
            LLMResponse(content="done", tool_calls=[], usage={}),
        ])
        tools = MagicMock()
        tools.get_definitions.return_value = []
        runner = AgentRunner(provider)

        result = await runner.run(
            AgentRunSpec(
                initial_messages=[{"role": "user", "content": "hello"}],
                tools=tools,
                model="test-model",
                max_iterations=3,
                max_tool_result_chars=1000,
            )
        )

        assert result.usage == {"prompt_tokens": 10, "completion_tokens": 1}
        assert result.last_call_usage == {}

    @pytest.mark.asyncio
    async def test_concurrent_runner_runs_do_not_share_ledger(self, tmp_path: Path) -> None:
        """Concurrent AgentRunner.run() invocations should not share ledger totals."""

        async def run_task(task_id: int, usage: dict):
            prov = _ScriptedProvider([
                LLMResponse(content=f"response-{task_id}", tool_calls=[], usage=usage)
            ])
            tools = MagicMock()
            tools.get_definitions.return_value = []
            tools.execute = AsyncMock(return_value="tool result")
            runner = AgentRunner(prov)
            result = await runner.run(
                AgentRunSpec(
                    initial_messages=[{"role": "user", "content": f"task-{task_id}"}],
                    tools=tools,
                    model="test-model",
                    max_iterations=3,
                    max_tool_result_chars=10000,
                    turn_budget=TurnBudget(max_cost_usd=1.0),
                )
            )
            return result

        results = await asyncio.gather(
            run_task(1, _usage(10, 20, 0.01)),
            run_task(2, _usage(5, 15, 0.02)),
        )

        assert results[0].usage == _usage(10, 20, 0.01)
        assert results[1].usage == _usage(5, 15, 0.02)
        assert results[0].last_call_usage == _usage(10, 20, 0.01)
        assert results[1].last_call_usage == _usage(5, 15, 0.02)

    @pytest.mark.asyncio
    async def test_ledger_context_resets_after_exception(self, tmp_path: Path) -> None:
        """Ledger context binding should reset after exceptions."""
        ledger = CallLedger()
        async with bind_call_ledger(ledger):
            async with call_purpose(CallPurpose.EXECUTOR):
                ledger.record(model="test", usage=_usage(10, 20), finish_reason="stop")
            # Simulate exception
            try:
                raise ValueError("test error")
            except ValueError:
                pass
        # After exiting the bind context, ledger should be reset
        assert current_call_ledger() is None

    @pytest.mark.asyncio
    async def test_ledger_context_resets_after_cancellation(self, tmp_path: Path) -> None:
        """Ledger context binding should reset after cancellation."""
        ledger = CallLedger()
        try:
            async with bind_call_ledger(ledger):
                async with call_purpose(CallPurpose.EXECUTOR):
                    ledger.record(model="test", usage=_usage(10, 20), finish_reason="stop")
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            pass
        # After cancellation, ledger should be reset
        assert current_call_ledger() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [RuntimeError("boom"), asyncio.CancelledError()])
    async def test_orchestrator_resets_ledger_after_failure(
        self,
        tmp_path: Path,
        monkeypatch,
        failure: BaseException,
    ) -> None:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=_ScriptedProvider([]),
            workspace=tmp_path,
            model="test-model",
        )

        async def fail_inside_state(_ctx):
            assert current_call_ledger() is not None
            raise failure

        monkeypatch.setattr(loop._turn_orchestrator, "_state_restore", fail_inside_state)
        message = InboundMessage(
            channel="websocket",
            sender_id="u1",
            chat_id="chat1",
            content="test",
        )

        with pytest.raises(type(failure)):
            await loop._process_message(message)

        assert current_call_ledger() is None

    @pytest.mark.asyncio
    async def test_system_turn_binds_ledger_before_early_branch(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=_ScriptedProvider([]),
            workspace=tmp_path,
            model="test-model",
        )

        async def process_system(_msg, **_kwargs):
            assert current_call_ledger() is not None
            return None

        monkeypatch.setattr(
            loop._turn_orchestrator._deps,
            "process_system_message",
            process_system,
        )
        message = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="websocket:chat1",
            content="done",
        )

        await loop._process_message(message)

        assert current_call_ledger() is None

    @pytest.mark.asyncio
    async def test_executor_call_purpose_records_usage_not_unclassified(self, tmp_path: Path) -> None:
        """ModelRequestExecutor.request_model should record usage under EXECUTOR purpose, not UNCLASSIFIED."""
        ledger = CallLedger()

        class TestProvider(LLMProvider):
            def __init__(self):
                super().__init__()
                self._default_model = "test-model"

            async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, reasoning_effort=None, tool_choice=None):
                return LLMResponse(content="done", tool_calls=[], usage=_usage(10, 5, 0.001))

            def get_default_model(self) -> str:
                return self._default_model

        provider = TestProvider()

        runner = AgentRunner(provider)
        executor = ModelRequestExecutor(runner)

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=MagicMock(),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=10000,
            turn_budget=TurnBudget(max_cost_usd=1.0),
        )
        spec.tools.get_definitions.return_value = []

        hook = MagicMock(spec=AgentHook)
        hook.wants_streaming.return_value = False
        context = AgentHookContext(iteration=0, messages=[{"role": "user", "content": "hello"}])

        async with bind_call_ledger(ledger):
            response = await executor.request_model(spec, [{"role": "user", "content": "hello"}], hook, context)

        assert response.content == "done"
        # Usage should be recorded under EXECUTOR purpose
        assert "executor" in ledger.purpose_usage
        assert ledger.purpose_usage["executor"]["prompt_tokens"] == 10
        assert ledger.purpose_usage["executor"]["completion_tokens"] == 5
        # UNCLASSIFIED should not have the executor usage
        assert "unclassified" not in ledger.purpose_usage or ledger.purpose_usage["unclassified"].get("prompt_tokens", 0) == 0

    @pytest.mark.asyncio
    async def test_executor_accounting_survives_python311_wait_for_task_hop(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        ledger = CallLedger()
        provider = _ScriptedProvider([
            LLMResponse(content="done", tool_calls=[], usage=_usage(10, 5))
        ])
        runner = AgentRunner(provider)
        tools = MagicMock()
        tools.get_definitions.return_value = []
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=1000,
            llm_timeout_s=1,
        )
        hook = MagicMock(spec=AgentHook)
        hook.wants_streaming.return_value = False
        context = AgentHookContext(iteration=0, messages=spec.initial_messages)

        async def task_hopping_wait_for(coro, *, timeout):
            return await asyncio.create_task(coro)

        monkeypatch.setattr(
            "miniunicorn.agent.execution.model_request.asyncio.wait_for",
            task_hopping_wait_for,
        )

        async with bind_call_ledger(ledger):
            await runner._model_request.request_model(
                spec,
                spec.initial_messages,
                hook,
                context,
            )

        assert ledger.purpose_usage["executor"] == _usage(10, 5)

    @pytest.mark.asyncio
    async def test_periodic_reflection_counts_if_it_finishes_before_turn_end(
        self,
        tmp_path: Path,
    ) -> None:
        ledger = CallLedger()
        provider = _ScriptedProvider([
            LLMResponse(
                content='{"lesson":"learned"}',
                tool_calls=[],
                usage=_usage(4, 2),
            )
        ])
        runner = AgentRunner(provider)
        tools = MagicMock()
        tools.get_definitions.return_value = []
        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hello"}],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=1000,
            workspace=tmp_path,
            enable_reflection=True,
            reflection_interval=1,
        )
        reflection = Reflection(provider, "test-model", tmp_path)

        async with bind_call_ledger(ledger):
            runner._planning.fire_periodic_reflection(
                reflection,
                spec,
                spec.initial_messages,
                0,
            )
            await asyncio.gather(*runner._planning._reflection_tasks)

        assert ledger.purpose_usage["reflection"] == _usage(4, 2)
