"""Boundary and fallback semantics for managed replanning."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.hook import AgentHook, AgentHookContext
from erza.agent.planner import (
    Plan,
    Planner,
    PlannerStatus,
    PlanStep,
    StepStatus,
)
from erza.agent.runner import AgentRunner, AgentRunSpec, _TurnState
from erza.providers.base import LLMProvider, LLMResponse


def _valid_provider() -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "new approach"}]}),
            tool_calls=[],
            usage={},
        )
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("max_replans", [0, 1, 2])
async def test_replan_limit_is_exact_provider_attempt_count(max_replans: int) -> None:
    provider = _valid_provider()
    planner = Planner(provider, "test-model")
    plan = Plan(
        goal="ship",
        steps=[PlanStep(id=1, action="old approach", status=StepStatus.FAILED)],
        max_replans=max_replans,
    )

    for _ in range(max_replans + 1):
        result = await planner.replan(
            plan,
            plan.steps[-1],
            "failed",
            "ship",
            "tools",
        )
        plan = result.plan

    assert provider.chat_with_retry.await_count == max_replans
    assert plan.replan_count == max_replans
    assert plan.max_replans == max_replans


@pytest.mark.asyncio
async def test_valid_replan_inherits_counters_and_prepends_completed_history() -> None:
    provider = _valid_provider()
    planner = Planner(provider, "test-model")
    completed = PlanStep(id=7, action="completed work", status=StepStatus.COMPLETED)
    failed = PlanStep(id=8, action="failed work", status=StepStatus.FAILED)
    old_plan = Plan(
        goal="ship",
        steps=[completed, failed],
        replan_count=0,
        max_replans=2,
    )

    result = await planner.replan(old_plan, failed, "boom", "ship", "tools")

    assert result.status is PlannerStatus.VALID
    assert result.plan.replan_count == 1
    assert result.plan.max_replans == 2
    assert [(step.action, step.status) for step in result.plan.steps] == [
        ("completed work", StepStatus.COMPLETED),
        ("new approach", StepStatus.PENDING),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_outcome", ["malformed", "raises"])
async def test_invalid_replan_degrades_recovery_to_fast(provider_outcome: str) -> None:
    provider = MagicMock(spec=LLMProvider)
    if provider_outcome == "malformed":
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="not json", tool_calls=[], usage={})
        )
    else:
        provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("offline"))
    runner = AgentRunner(provider)
    planner = Planner(provider, "test-model")
    failed = PlanStep(id=1, action="old approach")
    plan = Plan(goal="ship", steps=[failed], max_replans=1)
    hook = AgentHook()
    hook.after_iteration = AsyncMock()
    state = _TurnState()
    context = AgentHookContext(iteration=0, messages=[])
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
    )

    action, replacement = await runner.handle_fatal_tool_error(
        spec,
        state,
        context,
        hook,
        [],
        plan=plan,
        planner=planner,
        planner_task_text="ship",
        planner_tools_summary="tools",
        fatal_error=RuntimeError("boom"),
        iteration=0,
        reflection=None,
    )

    assert action == "continue"
    assert replacement is None
    hook.after_iteration.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_response_replan_returns_provider_error() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="provider unavailable",
            tool_calls=[],
            usage={},
            finish_reason="error",
        )
    )
    planner = Planner(provider, "test-model")
    failed = PlanStep(id=1, action="old", status=StepStatus.FAILED)
    plan = Plan(goal="ship", steps=[failed], max_replans=1)

    result = await planner.replan(plan, failed, "boom", "ship", "tools")

    assert result.status is PlannerStatus.FALLBACK
    assert result.error_code == "provider_error"
    assert result.plan is plan


@pytest.mark.asyncio
async def test_exhausted_plan_returns_plan_failed() -> None:
    provider = _valid_provider()
    runner = AgentRunner(provider)
    planner = Planner(provider, "test-model")
    step = PlanStep(id=1, action="old approach")
    plan = Plan(goal="ship", steps=[step], max_replans=0)
    state = _TurnState()
    context = AgentHookContext(iteration=0, messages=[])
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
    )

    action, replacement = await runner.handle_fatal_tool_error(
        spec,
        state,
        context,
        AgentHook(),
        [],
        plan=plan,
        planner=planner,
        planner_task_text="ship",
        planner_tools_summary="tools",
        fatal_error=RuntimeError("boom"),
        iteration=0,
        reflection=None,
    )

    assert action == "break"
    assert replacement is plan
    assert state.stop_reason == "plan_failed"
    assert provider.chat_with_retry.await_count == 0
