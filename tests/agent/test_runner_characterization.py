"""Characterization tests for AgentRunner's public façade (Task 9).

These tests pin the current behavior of ``AgentRunner.run`` for the
scenarios that Tasks 10-12 must preserve during extraction:

- a no-tool final answer
- one direct tool call
- one gateway tool call
- Provider error placeholder
- budget stop
- injected message
- finalization retry

Each case reuses the existing ``FakeToolExecutionPort`` and ``make_provider``
helpers so the assertions are exact, not approximate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from tests.agent.conftest import FakeToolExecutionPort

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _spec(
    messages: list[dict[str, Any]],
    *,
    tools: Any = None,
    tool_execution_port: Any = None,
    max_iterations: int = 3,
    **kwargs: Any,
) -> AgentRunSpec:
    if tools is None:
        tools = MagicMock()
        tools.get_definitions.return_value = []
    return AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        tool_execution_port=tool_execution_port,
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_no_tool_final_answer() -> None:
    """A single LLM response with no tool calls completes immediately."""
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="hello",
            tool_calls=[],
            usage={"prompt_tokens": 5, "completion_tokens": 2},
        )
    )
    runner = AgentRunner(provider)
    result = await runner.run(_spec([{"role": "user", "content": "hi"}]))
    assert result.final_content == "hello"
    assert result.tools_used == []
    assert result.stop_reason == "completed"
    assert provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_one_direct_tool_call() -> None:
    """One tool call followed by a final answer."""
    provider = MagicMock(spec=LLMProvider)
    call_count = {"n": 0}

    async def chat_with_retry(*, messages: Any, **kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})
                ],
                usage={"prompt_tokens": 5, "completion_tokens": 3},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="entries")
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "list"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
        )
    )
    assert result.final_content == "done"
    assert result.tools_used == ["list_dir"]
    assert result.tool_events == [
        {"name": "list_dir", "status": "ok", "detail": "entries"}
    ]


@pytest.mark.asyncio
async def test_one_gateway_tool_call() -> None:
    """A tool call routed through the ToolExecutionPort gateway."""
    provider = MagicMock(spec=LLMProvider)
    call_count = {"n": 0}

    async def chat_with_retry(*, messages: Any, **kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="read_file", arguments={"path": "/x"})
                ],
                usage={"prompt_tokens": 5, "completion_tokens": 3},
            )
        return LLMResponse(content="ok", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    port = FakeToolExecutionPort(tools)
    tools.execute = AsyncMock(return_value="file contents")
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec([{"role": "user", "content": "read"}], tools=tools, tool_execution_port=port)
    )
    assert result.final_content == "ok"
    assert result.tools_used == ["read_file"]


@pytest.mark.asyncio
async def test_provider_error_placeholder() -> None:
    """A Provider finish_reason='error' produces an error placeholder message."""
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="429 rate limit exceeded",
            finish_reason="error",
            tool_calls=[],
            usage={},
        )
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
        )
    )
    assert result.final_content is not None
    assert result.stop_reason == "error"
    assert result.error is not None


@pytest.mark.asyncio
async def test_budget_stop() -> None:
    """Exceeding the turn budget stops the run with budget_exceeded=True."""
    from miniunicorn.agent.turn_budget import TurnBudget

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="ans",
            tool_calls=[],
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )
    )
    runner = AgentRunner(provider)
    budget = TurnBudget(max_input_tokens=1, max_output_tokens=None, max_cost_usd=None)
    result = await runner.run(
        _spec([{"role": "user", "content": "hi"}], turn_budget=budget)
    )
    assert result.budget_exceeded is True
    assert result.stop_reason == "budget_exceeded"


@pytest.mark.asyncio
async def test_injected_message() -> None:
    """An injection callback injects a message mid-run."""
    provider = MagicMock(spec=LLMProvider)
    call_count = {"n": 0}

    async def chat_with_retry(*, messages: Any, **kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call_1", name="wait", arguments={})
                ],
                usage={"prompt_tokens": 5, "completion_tokens": 3},
            )
        return LLMResponse(content="injected-done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")

    injected = {"done": False}

    async def injection_callback() -> list[dict[str, Any]]:
        if injected["done"]:
            return []
        injected["done"] = True
        return [{"role": "user", "content": "injected"}]

    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "start"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
            injection_callback=injection_callback,
            max_iterations=5,
        )
    )
    assert result.had_injections is True
    assert result.final_content == "injected-done"


@pytest.mark.asyncio
async def test_finalization_retry() -> None:
    """A final response with tool_calls triggers a finalization retry."""
    provider = MagicMock(spec=LLMProvider)
    call_count = {"n": 0}

    async def chat_with_retry(*, messages: Any, **kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Return a "final" response that still has tool calls — the
            # runner should retry once to get a clean final answer.
            return LLMResponse(
                content="partial",
                tool_calls=[
                    ToolCallRequest(id="call_1", name="noop", arguments={})
                ],
                usage={"prompt_tokens": 5, "completion_tokens": 3},
            )
        return LLMResponse(content="final", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
        )
    )
    assert result.final_content == "final"
    assert call_count["n"] >= 2
