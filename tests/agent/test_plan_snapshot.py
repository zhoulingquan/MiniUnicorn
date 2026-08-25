"""P1-T2: PlanSnapshot — durable plan state serialization.

Covers PlanSnapshot.from_plan/to_dict semantics, frozen-dataclass
immutability, and checkpoint emission of ``plan_snapshot`` payloads from
a managed (MANAGED-mode) runner turn.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.plan_snapshot import PlanSnapshot
from miniunicorn.agent.planner import Plan, PlanStep, StepStatus
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.providers.base import LLMProvider, LLMResponse


def _make_plan() -> Plan:
    return Plan(
        goal="ship it",
        steps=[
            PlanStep(id=1, action="first", tool_hint="tool_a", done_criteria="a done"),
            PlanStep(id=2, action="second"),
            PlanStep(id=3, action="third"),
        ],
        replan_count=0,
        max_replans=3,
    )


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


# --- 1: from_plan basic ------------------------------------------------------


def test_from_plan_basic() -> None:
    snapshot = PlanSnapshot.from_plan(_make_plan(), "test123")

    assert snapshot.goal == "ship it"
    assert len(snapshot.steps) == 3
    assert all(isinstance(step, dict) for step in snapshot.steps)
    assert snapshot.replan_count == 0
    assert snapshot.max_replans == 3
    assert snapshot.current_step_id == 1
    assert snapshot.turn_id == "test123"
    assert snapshot.created_at
    assert snapshot.stop_reason is None


# --- 2: current_step_id skips completed steps --------------------------------


def test_from_plan_current_step_advances_past_completed() -> None:
    plan = _make_plan()
    plan.steps[0].status = StepStatus.COMPLETED

    snapshot = PlanSnapshot.from_plan(plan, "t1")

    assert snapshot.current_step_id == 2


# --- 3: all done → no current step -------------------------------------------


def test_from_plan_all_done_has_no_current_step() -> None:
    plan = _make_plan()
    for step in plan.steps:
        step.status = StepStatus.COMPLETED

    snapshot = PlanSnapshot.from_plan(plan, "t1")

    assert snapshot.current_step_id is None


# --- 4: terminal stop_reason -------------------------------------------------


def test_from_plan_carries_terminal_stop_reason() -> None:
    snapshot = PlanSnapshot.from_plan(_make_plan(), "t1", stop_reason="plan_failed")

    assert snapshot.stop_reason == "plan_failed"


# --- 5: to_dict round-trip ---------------------------------------------------


def test_to_dict_round_trip() -> None:
    plan = _make_plan()
    plan.steps[0].status = StepStatus.IN_PROGRESS
    snapshot = PlanSnapshot.from_plan(plan, "tid", stop_reason=None)

    data = snapshot.to_dict()

    assert set(data) == {
        "goal",
        "steps",
        "replan_count",
        "max_replans",
        "current_step_id",
        "turn_id",
        "created_at",
        "stop_reason",
        "origin",
    }
    assert data["goal"] == "ship it"
    assert data["steps"] == [step.to_dict() for step in plan.steps]
    assert data["replan_count"] == 0
    assert data["max_replans"] == 3
    assert data["current_step_id"] == 1
    assert data["turn_id"] == "tid"
    # created_at must parse as ISO 8601.
    datetime.fromisoformat(data["created_at"])
    assert data["stop_reason"] is None


# --- 6: steps serialized with full PlanStep schema ----------------------------


def test_steps_serialized_with_full_schema() -> None:
    snapshot = PlanSnapshot.from_plan(_make_plan(), "t1")

    expected_keys = {
        "id",
        "action",
        "tool_hint",
        "done_criteria",
        "status",
        "failure_reason",
        "iterations_used",
    }
    for step in snapshot.steps:
        assert set(step) == expected_keys
    assert snapshot.steps[0]["id"] == 1
    assert snapshot.steps[0]["action"] == "first"
    assert snapshot.steps[0]["tool_hint"] == "tool_a"
    assert snapshot.steps[0]["done_criteria"] == "a done"
    assert snapshot.steps[0]["status"] == "pending"
    assert snapshot.steps[0]["failure_reason"] is None
    assert snapshot.steps[0]["iterations_used"] == 0


# --- 7: frozen dataclass ------------------------------------------------------


def test_snapshot_is_frozen() -> None:
    snapshot = PlanSnapshot.from_plan(_make_plan(), "t1")

    with pytest.raises(FrozenInstanceError):
        snapshot.goal = "mutated"  # type: ignore[misc]


# --- 8: integration — runner emits snapshots via checkpoint callback ----------


@pytest.mark.asyncio
async def test_runner_emits_plan_snapshots_via_checkpoint_callback() -> None:
    responses = [
        LLMResponse(
            content=json.dumps(
                {
                    "goal": "ship",
                    "steps": [{"id": 1, "action": "do a"}, {"id": 2, "action": "do b"}],
                }
            ),
            tool_calls=[],
            usage={},
        ),
        LLMResponse(content="step one done", tool_calls=[], usage={}),
        LLMResponse(content="step two done", tool_calls=[], usage={}),
    ]
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    checkpoints: list[dict[str, Any]] = []

    async def capture_checkpoint(payload: dict[str, Any]) -> None:
        checkpoints.append(payload)

    runner = AgentRunner(provider)
    result = await runner.run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=_make_tools(),
            model="test-model",
            max_iterations=6,
            max_tool_result_chars=1000,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
            checkpoint_callback=capture_checkpoint,
        )
    )

    snapshots = [
        payload for payload in checkpoints if payload.get("phase") == "plan_snapshot"
    ]
    assert len(snapshots) >= 2
    first = snapshots[0]["plan_snapshot"]
    last = snapshots[-1]["plan_snapshot"]
    # All snapshots share one per-turn id.
    turn_ids = {payload["plan_snapshot"]["turn_id"] for payload in snapshots}
    assert len(turn_ids) == 1
    assert len(next(iter(turn_ids))) == 12
    # Initial snapshot reflects the fresh two-step plan...
    assert first["current_step_id"] == 1
    assert len(first["steps"]) == 2
    # ...and the terminal snapshot marks the plan completed.
    assert last["stop_reason"] == "plan_completed"
    assert last["current_step_id"] is None
    assert result.stop_reason == "completed"
