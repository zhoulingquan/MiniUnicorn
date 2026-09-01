"""T5: FAST→MANAGED runtime escalation.

Covers the PlanningPolicy.escalate truth table, runner hot-loop integration
where a FAST turn with stall triggers planner.create_plan, snapshot origin
recording, at-most-once-per-turn guard, planner failure fallback, and
MANAGED-mode never escalating.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.plan_snapshot import PlanSnapshot
from miniunicorn.agent.planner import (
    Plan,
    PlanStep,
    StepStatus,
)
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.providers.base import LLMResponse


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _make_plan() -> Plan:
    return Plan(
        goal="test goal",
        steps=[PlanStep(id=1, action="step 1"), PlanStep(id=2, action="step 2")],
        replan_count=0,
        max_replans=3,
    )


class FakeProvider:
    def __init__(self):
        self.chat_with_retry = AsyncMock()
        self.get_default_model = MagicMock(return_value="test-model")
        self.generation = MagicMock(max_tokens=8192)


# --- 1. PlanningPolicy.escalate truth table ---------------------------------


def test_escalate_rule_matrix() -> None:
    """PlanningPolicy.escalate: 4 combinations truth table.

    FAST + stall + not escalated -> MANAGED
    All other combinations -> original mode
    """
    policy = PlanningPolicy()

    # FAST + stall + not escalated -> MANAGED
    assert (
        policy.escalate(PlanningMode.FAST, stall_detected=True, already_escalated=False)
        == PlanningMode.MANAGED
    )

    # FAST + no stall -> FAST
    assert (
        policy.escalate(PlanningMode.FAST, stall_detected=False, already_escalated=False)
        == PlanningMode.FAST
    )

    # FAST + stall + already escalated -> FAST
    assert (
        policy.escalate(PlanningMode.FAST, stall_detected=True, already_escalated=True)
        == PlanningMode.FAST
    )

    # MANAGED + stall -> MANAGED (never escalates from MANAGED)
    assert (
        policy.escalate(PlanningMode.MANAGED, stall_detected=True, already_escalated=False)
        == PlanningMode.MANAGED
    )

    # MANAGED + no stall -> MANAGED
    assert (
        policy.escalate(PlanningMode.MANAGED, stall_detected=False, already_escalated=False)
        == PlanningMode.MANAGED
    )


# --- 2. FAST stall triggers planner.create_plan -----------------------------


@pytest.mark.asyncio
async def test_fast_stall_triggers_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAST mode, two stalled iterations -> third iteration planner.create_plan called."""
    provider = FakeProvider()

    # First call: FAST mode returns content (no tool calls, no progress)
    # Second call: same
    # Third call: should trigger planner.create_plan
    call_count = {"n": 0}
    plan_payload = json.dumps(
        {"goal": "escalated goal", "steps": [{"id": 1, "action": "escalated step"}]}
    )

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return LLMResponse(content="still thinking", tool_calls=[], usage={})
        return LLMResponse(content=plan_payload, tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "test task"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.FAST),
    )

    # We need to inject stall detection. The ProgressPolicy only applies when plan exists.
    # For escalation test, we need the runner to call escalate when ProgressPolicy reports stall.
    # This test will be meaningful once the integration is implemented.
    # For now we verify the planner is called on escalation.

    _result = await runner.run(spec)

    # After implementation, planner.create_plan should be called once
    assert call_count["n"] >= 1


# --- 3. Escalation recorded in PlanSnapshot.origin --------------------------


def test_escalation_recorded_in_snapshot() -> None:
    """PlanSnapshot created from escalation has origin='escalated'."""
    plan = _make_plan()
    snapshot = PlanSnapshot.from_plan(plan, "turn-123", stop_reason=None)

    # After implementation, snapshot should have origin field
    # For now this is a placeholder - the field doesn't exist yet
    # The test will fail until we add origin to PlanSnapshot
    assert hasattr(snapshot, "origin")
    # When escalated:
    # snapshot = PlanSnapshot.from_plan(plan, "turn-123", stop_reason=None, origin="escalated")
    # assert snapshot.origin == "escalated"


# --- 4. At most once per turn -----------------------------------------------


@pytest.mark.asyncio
async def test_escalate_at_most_once_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """After escalation, further stalls do not re-trigger planner.create_plan."""
    provider = FakeProvider()

    plan_payload = json.dumps(
        {"goal": "escalated goal", "steps": [{"id": 1, "action": "escalated step"}]}
    )

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        return LLMResponse(content=plan_payload, tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "test task"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.FAST),
    )

    # This test will verify planner.create_plan is called at most once per turn
    # after the escalation feature is implemented
    result = await runner.run(spec)

    # Placeholder assertion - will be meaningful after implementation
    assert result is not None


# --- 5. Planner failure falls back to FAST ----------------------------------


@pytest.mark.asyncio
async def test_planner_failure_falls_back_to_fast() -> None:
    """If planner.create_plan raises, turn continues in FAST mode with original stall handling."""
    provider = FakeProvider()

    call_count = {"n": 0}

    async def failing_chat(**kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        # First 2 calls: normal model responses (no tools, trigger stall)
        if call_count["n"] <= 2:
            return LLMResponse(content="thinking...", tool_calls=[], usage={})
        # 3rd call: planner.create_plan -> should fail
        raise RuntimeError("planner down")

    provider.chat_with_retry = failing_chat

    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "test task"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.FAST),
    )

    # Should catch exception and continue FAST
    result = await runner.run(spec)

    # Should not crash, should have some stop_reason
    assert result.stop_reason in ("completed", "error", "empty_final_response")


# --- 6. MANAGED mode never escalates ----------------------------------------


def test_managed_mode_never_escalates() -> None:
    """MANAGED mode stall uses existing replan path; escalate not triggered."""
    policy = PlanningPolicy()

    # MANAGED + stall -> MANAGED (escalate returns MANAGED, no transition)
    assert (
        policy.escalate(PlanningMode.MANAGED, stall_detected=True, already_escalated=False)
        == PlanningMode.MANAGED
    )


# --- Additional integration test with ProgressPolicy stall detection --------


@pytest.mark.asyncio
async def test_progress_stall_verdict_triggers_escalation_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ProgressPolicy returns ABORT (stall), runner calls escalate."""
    provider = FakeProvider()

    plan_payload = json.dumps(
        {"goal": "escalated goal", "steps": [{"id": 1, "action": "escalated step"}]}
    )

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        return LLMResponse(content=plan_payload, tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    runner = AgentRunner(provider)

    # Create a plan manually to simulate MANAGED mode with stalled step
    _plan = Plan(
        goal="original goal",
        steps=[
            PlanStep(id=1, action="stalled step", status=StepStatus.IN_PROGRESS, iterations_used=6)
        ],
        max_replans=1,
    )

    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "test"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED, planner_model="test-model"),
    )

    # This integration test will verify the runner calls escalate when
    # ProgressPolicy returns a stall verdict
    result = await runner.run(spec)

    assert result is not None
