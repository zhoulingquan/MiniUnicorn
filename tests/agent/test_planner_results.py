"""Explicit Planner result contracts and non-lossy task extraction."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.planner import Planner, PlannerStatus
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.providers.base import LLMProvider, LLMResponse


def _provider_with_content(content: str) -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content=content, tool_calls=[], usage={})
    )
    return provider


@pytest.mark.asyncio
async def test_valid_plan_returns_explicit_valid_result() -> None:
    provider = _provider_with_content(
        json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "test"}]})
    )

    result = await Planner(provider, "test-model").create_plan("ship", "tools")

    assert result.status is PlannerStatus.VALID
    assert result.error_code is None
    assert result.plan.goal == "ship"
    assert [step.action for step in result.plan.steps] == ["test"]


@pytest.mark.asyncio
async def test_provider_error_returns_stable_fallback_code() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("offline"))

    result = await Planner(provider, "test-model").create_plan("ship", "tools")

    assert result.status is PlannerStatus.FALLBACK
    assert result.error_code == "provider_error"
    assert [step.action for step in result.plan.steps] == ["ship"]


@pytest.mark.asyncio
async def test_error_response_returns_stable_provider_error_code() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="provider unavailable",
            tool_calls=[],
            usage={},
            finish_reason="error",
        )
    )

    result = await Planner(provider, "test-model").create_plan("ship", "tools")

    assert result.status is PlannerStatus.FALLBACK
    assert result.error_code == "provider_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        ("plain prose", "missing_json"),
        ("```json\n{not json}\n```", "invalid_json"),
        (json.dumps({"goal": "ship"}), "missing_steps"),
        (
            json.dumps({"goal": "ship", "steps": [None, {}, {"description": ""}]}),
            "all_invalid_steps",
        ),
    ],
)
async def test_invalid_plan_shapes_return_stable_fallback_codes(
    content: str,
    error_code: str,
) -> None:
    result = await Planner(_provider_with_content(content), "test-model").create_plan(
        "ship", "tools"
    )

    assert result.status is PlannerStatus.FALLBACK
    assert result.error_code == error_code
    assert [step.action for step in result.plan.steps] == ["ship"]


@pytest.mark.asyncio
async def test_planner_receives_full_latest_user_message() -> None:
    long_task = "x" * 2000
    provider = _provider_with_content(
        json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "test"}]})
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": long_task}],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=1000,
        use_planner=True,
    )

    _planner, plan, task_text, _tools_summary = await runner._init_planner(spec)

    assert plan is not None
    assert task_text == long_task
    call = provider.chat_with_retry.await_args.kwargs
    assert long_task in call["messages"][1]["content"]
