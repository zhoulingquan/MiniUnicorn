"""P2-T2: Prompt-component telemetry.

Tests that ``TurnTelemetry`` gains per-component token estimates populated
after context governance, that component estimates sum to total_estimated,
and that telemetry stays None when no context budget exists.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent import turn_telemetry
from miniunicorn.agent.context_governor import PressureLevel
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.turn_telemetry import PromptComponentTokens, TurnTelemetry
from miniunicorn.providers.base import LLMProvider


def _spec(context_window_tokens: int | None = 1000) -> AgentRunSpec:
    tools = MagicMock()
    tools.get_definitions = MagicMock(return_value=[])
    return AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=2000,
        context_window_tokens=context_window_tokens,
    )


async def _govern(spec: AgentRunSpec, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)
    return await runner._context_governance.govern_messages(spec, messages, 0)


@pytest.fixture
def bound_telemetry() -> Any:
    telemetry = TurnTelemetry()
    token = turn_telemetry.bind(telemetry)
    yield telemetry
    turn_telemetry.reset(token)


def test_prompt_component_tokens_defaults_zero() -> None:
    pc = PromptComponentTokens()
    assert pc.system_prompt == 0
    assert pc.tool_definitions == 0
    assert pc.conversation_history == 0
    assert pc.step_guidance == 0
    assert pc.reflection_context == 0
    assert pc.compacted_context == 0
    assert pc.total_estimated == 0


async def test_telemetry_populated_after_governance(bound_telemetry) -> None:
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
    ]
    await _govern(_spec(), messages)
    assert bound_telemetry.prompt_components is not None
    assert bound_telemetry.governance_pressure is not None
    assert bound_telemetry.governance_pressure.level is PressureLevel.GREEN
    assert bound_telemetry.prompt_components.system_prompt > 0
    assert bound_telemetry.prompt_components.conversation_history > 0


async def test_component_estimates_sum_to_total(bound_telemetry) -> None:
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "read_file",
            "content": "[read_file result omitted from context]",
        },
    ]
    await _govern(_spec(), messages)
    pc = bound_telemetry.prompt_components
    assert pc is not None
    total = (
        pc.system_prompt
        + pc.tool_definitions
        + pc.conversation_history
        + pc.step_guidance
        + pc.reflection_context
        + pc.compacted_context
    )
    assert total == pc.total_estimated


async def test_telemetry_stays_none_without_budget(bound_telemetry) -> None:
    messages = [{"role": "user", "content": "hi"}]
    await _govern(_spec(context_window_tokens=None), messages)
    assert bound_telemetry.prompt_components is None
    assert bound_telemetry.governance_pressure is None


async def test_compacted_context_tracked_separately(bound_telemetry) -> None:
    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "read_file",
            "content": "[read_file result omitted from context]",
        },
    ]
    await _govern(_spec(), messages)
    pc = bound_telemetry.prompt_components
    assert pc is not None
    assert pc.compacted_context > 0
    # Compacted tool results are carved out of conversation_history.
    assert pc.conversation_history < pc.total_estimated


async def test_unbound_telemetry_is_safe() -> None:
    """Governance must not fail when no telemetry is bound to the context."""
    messages = [{"role": "user", "content": "hi"}]
    result = await _govern(_spec(), messages)
    assert isinstance(result, list)
