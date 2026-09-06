"""Explicit Planner result contracts and non-lossy task extraction."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.planner import (
    Planner,
    PlannerStatus,
    _normalize_evidence_level,
    effective_evidence_level,
)
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.providers.base import LLMProvider, LLMResponse


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

    _planner, plan, task_text, _tools_summary = await runner.init_planner(spec)

    assert plan is not None
    assert task_text == long_task
    call = provider.chat_with_retry.await_args.kwargs
    assert long_task in call["messages"][1]["content"]


# --- W0-A4: evidence_level protocol ------------------------------------------


@pytest.mark.asyncio
async def test_parses_tool_evidence_level() -> None:
    provider = _provider_with_content(
        json.dumps(
            {
                "goal": "ship",
                "steps": [{"id": 1, "action": "write it", "evidence_level": "tool"}],
            }
        )
    )

    result = await Planner(provider, "test-model").create_plan("ship", "tools")

    assert result.plan.steps[0].evidence_level == "tool"
    assert effective_evidence_level(result.plan.steps[0]) == "tool"


@pytest.mark.asyncio
async def test_missing_evidence_level_defaults_to_text() -> None:
    provider = _provider_with_content(
        json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "research it"}]})
    )

    result = await Planner(provider, "test-model").create_plan("ship", "tools")

    assert result.plan.steps[0].evidence_level == "text"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("none", "text"),
        ("TOOL", "tool"),
        ("tool ", "tool"),
        ("Text", "text"),
        ("", "text"),
    ],
)
def test_evidence_level_normalization(raw: str, expected: str) -> None:
    assert _normalize_evidence_level(raw) == expected


@pytest.mark.parametrize("raw", [True, 1, None, ["tool"], {"level": "tool"}])
def test_non_string_evidence_level_falls_back_to_text(raw: object) -> None:
    assert _normalize_evidence_level(raw) == "text"


def test_planner_template_declares_evidence_level() -> None:
    template = (
        Path(__file__).resolve().parents[2]
        / "erza"
        / "templates"
        / "agent"
        / "planner_system.md"
    )

    assert "evidence_level" in template.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_end_to_end_tool_level_step_reaches_acceptance() -> None:
    """A Planner-declared tool step keeps tool level through to the judgement."""
    provider = _provider_with_content(
        json.dumps(
            {
                "goal": "ship",
                "steps": [
                    {
                        "id": 1,
                        "action": "write the report",
                        "tool_hint": None,
                        "evidence_level": "tool",
                    }
                ],
            }
        )
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "ship"}],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=1000,
        use_planner=True,
    )

    _planner, plan, _task_text, _summary = await AgentRunner(provider).init_planner(spec)

    assert plan is not None
    # No tool_hint, so the level comes from the Planner's declaration alone.
    assert effective_evidence_level(plan.steps[0]) == "tool"
