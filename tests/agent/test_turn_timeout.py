"""P1-T7: Bounded sustained-goal time slices.

Tests that ``AgentRunner`` enforces a per-turn wall-clock deadline.
When ``max_turn_wall_time_s`` is set, the runner stops with
``stop_reason="turn_timeout"`` once the deadline is reached.  When
``None`` (default), the runner is unlimited (P0 behavior).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _slow_provider(delay: float = 0.15) -> MagicMock:
    provider = MagicMock(spec=LLMProvider)

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        await asyncio.sleep(delay)
        return LLMResponse(
            content="thinking",
            tool_calls=[ToolCallRequest(id="call_1", name="noop", arguments={})],
            usage={},
            finish_reason="tool_calls",
        )

    provider.chat_with_retry = chat_with_retry
    return provider


def _tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")
    return tools


# 1. Deadline stops the loop with turn_timeout.


@pytest.mark.asyncio
async def test_turn_timeout_stops_loop() -> None:
    provider = _slow_provider(delay=0.15)
    runner = AgentRunner(provider)

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "long task"}],
            tools=_tools(),
            model="test-model",
            max_iterations=100,
            max_tool_result_chars=1000,
            max_turn_wall_time_s=0.1,
        )
    )

    assert result.stop_reason == "turn_timeout"


# 2. None deadline is unlimited (P0 behavior).


@pytest.mark.asyncio
async def test_none_deadline_is_unlimited() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[], usage={})
    )
    runner = AgentRunner(provider)

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_tools(),
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=1000,
        )
    )

    assert result.stop_reason == "completed"


# 3. Sufficient deadline allows completion.


@pytest.mark.asyncio
async def test_sufficient_deadline_allows_completion() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[], usage={})
    )
    runner = AgentRunner(provider)

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_tools(),
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=1000,
            max_turn_wall_time_s=30.0,
        )
    )

    assert result.stop_reason == "completed"
