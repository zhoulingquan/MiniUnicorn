"""P1-T4: ProgressPolicy — no-progress detection.

Covers the ``ProgressTracker`` verdict matrix (iteration limit, consecutive
empty evidence, repeated step failures), the frozen value types, and the
runner / recovery integration points that translate verdicts into turn
outcomes (``stop_reason="no_progress"``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import miniunicorn.agent.progress_policy as progress_policy_module
from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.planner import Plan, Planner, PlanStep, StepStatus
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.progress_policy import (
    ProgressAction,
    ProgressPolicy,
    ProgressTracker,
    ProgressVerdict,
)
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec, _TurnState
from miniunicorn.agent.step_acceptance import StepEvidence
from miniunicorn.providers.base import LLMProvider, LLMResponse


def _tracker() -> ProgressTracker:
    return ProgressTracker(ProgressPolicy())


def _evidence(**overrides: Any) -> StepEvidence:
    defaults: dict[str, Any] = {
        "step_id": 1,
        "tool_calls": [],
        "tool_results": [],
        "final_content": None,
        "iterations_used": 1,
        "accepted": False,
        "rejection_reason": "empty_content_no_tools",
    }
    defaults.update(overrides)
    return StepEvidence(**defaults)


# 1. CONTINUE on normal progress.


def test_continue_on_normal_progress() -> None:
    tracker = _tracker()
    step = PlanStep(id=1, action="do the thing", iterations_used=1)

    verdict = tracker.check_step_progress(step, _evidence(accepted=True))

    assert verdict.action is ProgressAction.CONTINUE
    assert verdict.reason == "ok"


# 2. REPLAN on iteration limit (> max_step_iterations; boundary stays CONTINUE).


def test_replan_on_step_iteration_limit() -> None:
    tracker = _tracker()
    step = PlanStep(id=1, action="do the thing", iterations_used=6)

    boundary = tracker.check_step_progress(
        replace(step, iterations_used=5), _evidence(accepted=True)
    )
    over_limit = tracker.check_step_progress(step, _evidence(accepted=True))

    assert boundary.action is ProgressAction.CONTINUE
    assert over_limit.action is ProgressAction.REPLAN
    assert over_limit.reason == "step_iteration_limit"


# 3. ABORT on consecutive empty steps (no tools, no content).


def test_abort_after_consecutive_empty_steps() -> None:
    tracker = _tracker()
    step = PlanStep(id=1, action="stuck")

    first = tracker.check_step_progress(step, _evidence())
    second = tracker.check_step_progress(step, _evidence())
    third = tracker.check_step_progress(step, _evidence())

    assert first.action is ProgressAction.CONTINUE
    assert second.action is ProgressAction.CONTINUE
    assert third.action is ProgressAction.ABORT
    assert third.reason == "no_progress"


# 4. Reset empty counter on progress (evidence with tool calls counts as motion).


def test_empty_counter_resets_on_tool_evidence() -> None:
    tracker = _tracker()
    step = PlanStep(id=1, action="recovering")
    stalled = _evidence()
    with_tools = _evidence(
        tool_calls=[{"name": "web_fetch"}],
        tool_results=[{"summary": "3 hits"}],
        iterations_used=2,
        rejection_reason="empty_content_with_tools",
    )

    tracker.check_step_progress(step, stalled)
    tracker.check_step_progress(step, stalled)
    recovered = tracker.check_step_progress(step, with_tools)

    assert recovered.action is ProgressAction.CONTINUE
    # Counter was reset: two further empty evaluations must not abort yet.
    assert tracker.check_step_progress(step, stalled).action is ProgressAction.CONTINUE
    assert tracker.check_step_progress(step, stalled).action is ProgressAction.CONTINUE


# 5. ABORT on repeated failures.


def test_abort_on_repeated_failures() -> None:
    tracker = _tracker()
    plan = Plan(
        goal="ship",
        steps=[
            PlanStep(id=1, action="first try", status=StepStatus.FAILED),
            PlanStep(id=2, action="second try", status=StepStatus.FAILED),
        ],
    )

    verdict = tracker.check_failure_progress(plan)

    assert verdict.action is ProgressAction.ABORT
    assert verdict.reason == "repeated_failures"


# 6. REPLAN on first failure.


def test_replan_on_first_failure() -> None:
    tracker = _tracker()
    plan = Plan(
        goal="ship",
        steps=[
            PlanStep(id=1, action="first try", status=StepStatus.FAILED),
            PlanStep(id=2, action="second try"),
        ],
    )

    verdict = tracker.check_failure_progress(plan)

    assert verdict.action is ProgressAction.REPLAN
    assert verdict.reason == "step_failed"


# 7. ProgressPolicy defaults.


def test_progress_policy_defaults() -> None:
    policy = ProgressPolicy()

    assert policy.max_step_iterations == 5
    assert policy.max_empty_steps == 3
    assert policy.max_repeated_failures == 2


# 8. ProgressVerdict frozen.


def test_progress_verdict_is_frozen() -> None:
    verdict = ProgressVerdict(ProgressAction.CONTINUE, "ok")

    assert verdict.reason == "ok"
    with pytest.raises(Exception):
        verdict.reason = "changed"  # type: ignore[misc]


# 9. Integration: repeated fatal errors abort the turn with no_progress.


@pytest.mark.asyncio
async def test_fatal_error_after_repeated_failures_aborts_with_no_progress() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock()
    runner = AgentRunner(provider)
    plan = Plan(
        goal="ship",
        steps=[
            PlanStep(id=1, action="first try", status=StepStatus.FAILED),
            PlanStep(id=2, action="second try"),
        ],
        max_replans=3,
    )
    state = _TurnState(progress_tracker=_tracker())
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
        planner=Planner(provider, "test-model"),
        planner_task_text="ship",
        planner_tools_summary="tools",
        fatal_error=RuntimeError("boom"),
        iteration=0,
        reflection=None,
    )

    assert action == "break"
    assert replacement is plan
    assert state.stop_reason == "no_progress"
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_run_replans_stalling_step_and_tracks_progress(monkeypatch) -> None:
    """A stalling managed step hits the iteration limit, triggers REPLAN via
    the planner, and the turn attaches exactly one ProgressTracker."""
    created: list[ProgressTracker] = []

    class SpyTracker(ProgressTracker):
        def __init__(self, policy: ProgressPolicy) -> None:
            super().__init__(policy)
            created.append(self)

    monkeypatch.setattr(progress_policy_module, "ProgressTracker", SpyTracker)

    plan_payload = json.dumps(
        {
            "goal": "prove theorem",
            "steps": [{"id": 1, "action": "attempt proof", "done_criteria": "QED-XYZZY"}],
        }
    )
    provider = MagicMock(spec=LLMProvider)
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        # First call and replan calls return the plan JSON; iteration calls
        # return content that does NOT contain the done_criteria token, so
        # evidence is rejected and iterations_used keeps climbing.
        if call_count["n"] == 1 or call_count["n"] == 8:
            return LLMResponse(content=plan_payload, tool_calls=[], usage={})
        return LLMResponse(content="still working on the proof", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "prove theorem"}],
            tools=tools,
            model="test-model",
            max_iterations=20,
            max_tool_result_chars=1000,
            planning_policy=PlanningPolicy(
                mode=PlanningMode.MANAGED,
                planner_model="test-model",
                planner_max_replans=1,
            ),
        )
    )

    assert len(created) == 1
    assert result.plan is not None
    assert result.plan.replan_count == 1
    # create_plan + 6 stalling iterations + replan + 6 more until exhausted.
    assert call_count["n"] == 14
    assert result.stop_reason == "plan_failed"


# 10. No tracker when FAST mode (no plan).


@pytest.mark.asyncio
async def test_fast_mode_attaches_no_progress_tracker(monkeypatch) -> None:
    created: list[ProgressTracker] = []

    class SpyTracker(ProgressTracker):
        def __init__(self, policy: ProgressPolicy) -> None:
            super().__init__(policy)
            created.append(self)

    monkeypatch.setattr(progress_policy_module, "ProgressTracker", SpyTracker)

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="all done", tool_calls=[], usage={})
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)

    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=1000,
        )
    )

    assert result.stop_reason == "completed"
    assert created == []
