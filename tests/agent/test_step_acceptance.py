"""P1-T3: StepAcceptancePolicy — deterministic step evidence acceptance.

Covers the policy's accept/reject matrix, StepEvidence field population and
serialization, the ``Plan.step_evidence`` ledger, and integration of evidence
evaluation into ``PlanningReflectionService.complete_plan_step``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from miniunicorn.agent.execution.planning import PlanningReflectionService
from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.planner import Plan, PlanStep, StepStatus
from miniunicorn.agent.step_acceptance import StepAcceptancePolicy, StepEvidence


def _step(**overrides: Any) -> PlanStep:
    defaults: dict[str, Any] = {"id": 1, "action": "do the thing"}
    defaults.update(overrides)
    return PlanStep(**defaults)


def _service() -> PlanningReflectionService:
    async def _emit_checkpoint(spec: Any, payload: dict[str, Any]) -> None:
        return None

    return PlanningReflectionService(SimpleNamespace(_emit_checkpoint=_emit_checkpoint))


def _hook() -> AgentHook:
    hook = AgentHook()
    hook.after_iteration = AsyncMock()
    return hook


# 1. Accept with non-empty content, no done_criteria.


def test_accepts_non_empty_content_without_done_criteria() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(),
        tool_calls=[],
        tool_results=[],
        final_content="The task is finished.",
        iterations_used=1,
    )

    assert evidence.accepted is True
    assert evidence.rejection_reason is None


# 2. Accept with non-empty content and matched done_criteria.


@pytest.mark.parametrize(
    ("content", "criteria"),
    [
        ("All green: tests pass", "tests pass"),
        ("ALL GREEN: TESTS PASS", "tests pass"),
    ],
)
def test_accepts_when_done_criteria_matched(content: str, criteria: str) -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria=criteria),
        tool_calls=[],
        tool_results=[],
        final_content=content,
        iterations_used=2,
    )

    assert evidence.accepted is True


# 3. Reject empty content without tools.


@pytest.mark.parametrize("empty_content", [None, "", "   "])
def test_rejects_empty_content_without_tools(empty_content: str | None) -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(),
        tool_calls=[],
        tool_results=[],
        final_content=empty_content,
        iterations_used=0,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "empty_content_no_tools"


# 4. Reject empty content even when tools were used.


def test_rejects_empty_content_with_tools() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(),
        tool_calls=[{"name": "web_search"}],
        tool_results=[{"summary": "3 hits"}],
        final_content=None,
        iterations_used=3,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "empty_content_with_tools"


# 5. Reject content that misses done_criteria when no tools were used.


def test_rejects_unmet_done_criteria_without_tools() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria="proof of work"),
        tool_calls=[],
        tool_results=[],
        final_content="I did something else entirely",
        iterations_used=1,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "done_criteria_not_met"


# 6. Reject unmet done_criteria even when tools were used (F-002: tool usage is not completion).


def test_rejects_unmet_done_criteria_with_tools() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria="proof of work"),
        tool_calls=[{"name": "run_tests"}],
        tool_results=[{"summary": "ok"}],
        final_content="Ran the suite, looks fine",
        iterations_used=2,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "done_criteria_not_met"


# 6b. Regression (F-002): non-empty tool_calls must not satisfy done criteria.


def test_rejects_unmet_done_criteria_with_tool_calls_regression() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria="report.md created"),
        tool_calls=[{"name": "shell"}],
        tool_results=[],
        final_content="I ran some commands",
        iterations_used=1,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "done_criteria_not_met"


# 6c. Regression guard: no done_criteria with non-empty content stays accepted.


def test_accepts_non_empty_content_when_done_criteria_is_none() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria=None),
        tool_calls=[{"name": "shell"}],
        tool_results=[],
        final_content="Work completed.",
        iterations_used=1,
    )

    assert evidence.accepted is True
    assert evidence.rejection_reason is None


# 6d. Regression guard: matched done_criteria stays accepted regardless of tools.


def test_accepts_matched_done_criteria_with_tools() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria="report.md created"),
        tool_calls=[{"name": "shell"}],
        tool_results=[{"summary": "file written"}],
        final_content="Done: report.md created",
        iterations_used=2,
    )

    assert evidence.accepted is True


# 7. StepEvidence fields are populated from evaluate() inputs.


def test_evidence_fields_populated_correctly() -> None:
    step = _step(id=7, action="fetch data")
    tool_calls = [{"name": "http_get", "args": {"url": "https://x"}}]
    tool_results = [{"summary": "200 OK"}]

    evidence = StepAcceptancePolicy().evaluate(
        step=step,
        tool_calls=tool_calls,
        tool_results=tool_results,
        final_content="data fetched",
        iterations_used=4,
    )

    assert isinstance(evidence, StepEvidence)
    assert evidence.step_id == 7
    assert evidence.tool_calls == tool_calls
    assert evidence.tool_results == tool_results
    assert evidence.final_content == "data fetched"
    assert evidence.iterations_used == 4
    assert evidence.accepted is True
    assert evidence.rejection_reason is None


def test_rejected_evidence_carries_reason_and_inputs() -> None:
    evidence = StepAcceptancePolicy().evaluate(
        step=_step(done_criteria="signed off"),
        tool_calls=[],
        tool_results=[],
        final_content="half done",
        iterations_used=1,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "done_criteria_not_met"
    assert evidence.final_content == "half done"


# 8. to_dict round-trip exposes all 7 keys with matching values.


def test_to_dict_round_trip_has_all_keys() -> None:
    evidence = StepEvidence(
        step_id=3,
        tool_calls=[{"name": "t"}],
        tool_results=[{"summary": "s"}],
        final_content="done",
        iterations_used=2,
        accepted=True,
    )

    data = evidence.to_dict()

    assert set(data) == {
        "step_id",
        "tool_calls",
        "tool_results",
        "final_content",
        "iterations_used",
        "accepted",
        "rejection_reason",
    }
    assert data == {
        "step_id": 3,
        "tool_calls": [{"name": "t"}],
        "tool_results": [{"summary": "s"}],
        "final_content": "done",
        "iterations_used": 2,
        "accepted": True,
        "rejection_reason": None,
    }


# 9. Plan.step_evidence defaults empty, accepts appends, to_dict conditional.


def test_plan_step_evidence_defaults_empty_and_appends() -> None:
    plan = Plan(goal="ship")

    assert plan.step_evidence == []

    evidence = StepEvidence(
        step_id=1,
        tool_calls=[],
        tool_results=[],
        final_content=None,
        iterations_used=0,
        accepted=False,
        rejection_reason="empty_content_no_tools",
    )
    plan.step_evidence.append(evidence)

    assert plan.step_evidence == [evidence]

    data = plan.to_dict()
    assert "step_evidence" not in data

    populated = Plan(goal="ship", steps=[_step()], step_evidence=[evidence])
    assert populated.to_dict()["step_evidence"] == [evidence.to_dict()]


# 10. Integration: complete_plan_step evaluates evidence against the plan.


@pytest.mark.asyncio
async def test_complete_plan_step_appends_accepted_evidence_and_completes() -> None:
    step = _step(action="search docs", iterations_used=2)
    plan = Plan(goal="answer", steps=[step])
    service = _service()
    hook = _hook()
    context = AgentHookContext(iteration=1, messages=[])

    more_steps = await service.complete_plan_step(
        plan,
        context,
        hook,
        "found the answer",
        "stop",
        tool_calls=[{"name": "web_search", "args": {"q": "docs"}}],
        tool_results=[{"summary": "3 hits"}],
    )

    assert more_steps is False
    assert step.status is StepStatus.COMPLETED
    assert len(plan.step_evidence) == 1
    evidence = plan.step_evidence[0]
    assert evidence.step_id == 1
    assert evidence.accepted is True
    assert evidence.tool_calls == [{"name": "web_search", "args": {"q": "docs"}}]
    assert evidence.tool_results == [{"summary": "3 hits"}]
    assert evidence.final_content == "found the answer"
    assert evidence.iterations_used == 2
    hook.after_iteration.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_plan_step_rejection_keeps_step_in_progress() -> None:
    step = _step(
        action="prove it",
        done_criteria="QED",
        status=StepStatus.IN_PROGRESS,
        iterations_used=1,
    )
    plan = Plan(goal="answer", steps=[step])
    service = _service()
    hook = _hook()
    context = AgentHookContext(iteration=0, messages=[])

    more_steps = await service.complete_plan_step(plan, context, hook, "not yet", "stop")

    assert more_steps is True
    assert step.status is StepStatus.IN_PROGRESS
    assert len(plan.step_evidence) == 1
    assert plan.step_evidence[0].accepted is False
    assert plan.step_evidence[0].rejection_reason == "done_criteria_not_met"
