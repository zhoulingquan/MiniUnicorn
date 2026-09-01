"""W2-2: main-loop phase split — pure-move regression lock.

Covers ``PlanSnapshot.with_origin``, the ``_maybe_escalate_to_managed`` phase
method (escalate / already-escalated / failure), the god-method size guard on
``_run_with_ledger``, and a minimal managed-turn smoke with receipt acceptance.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger as loguru_logger

from miniunicorn.agent.plan_snapshot import PlanSnapshot
from miniunicorn.agent.planner import Plan, PlanStep, StepStatus
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec, _TurnState
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.tools.filesystem import WriteFileTool
from miniunicorn.tools.registry import ToolRegistry


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


def _fast_spec() -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "test task"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.FAST),
    )


# --- 1: with_origin keeps every other field identical ------------------------


def test_with_origin_changes_only_origin() -> None:
    snapshot = PlanSnapshot.from_plan(_make_plan(), "turn-1")

    escalated = snapshot.with_origin("escalated")

    assert escalated.origin == "escalated"
    assert escalated.to_dict() == {**snapshot.to_dict(), "origin": "escalated"}
    assert escalated.digest == snapshot.digest
    # The original snapshot is untouched and immutable.
    assert snapshot.origin == "planner"
    with pytest.raises(FrozenInstanceError):
        snapshot.origin = "escalated"  # type: ignore[misc]


# --- 2: _maybe_escalate_to_managed escalates on FAST stall --------------------


@pytest.mark.asyncio
async def test_maybe_escalate_escalates_on_fast_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(MagicMock(spec=LLMProvider))
    spec = _fast_spec()
    state = _TurnState()
    state.turn_id = "turn-1"
    state.consecutive_nontool_iterations = 2
    plan = _make_plan()
    planner_sentinel = object()
    calls = {"init_planner": 0}

    async def fake_init_planner(spec: AgentRunSpec) -> tuple[Any, Any, str | None, str | None]:
        calls["init_planner"] += 1
        return planner_sentinel, plan, "task text", "tools summary"

    async def fake_emit(
        spec: AgentRunSpec,
        plan: Any,
        turn_id: str,
        stop_reason: str | None = None,
        origin: str = "planner",
    ) -> PlanSnapshot:
        return PlanSnapshot.from_plan(plan, turn_id, stop_reason, origin=origin)

    monkeypatch.setattr(runner, "_init_planner", fake_init_planner)
    monkeypatch.setattr(runner, "_emit_plan_snapshot", fake_emit)

    out_planner, out_plan, out_task, out_summary = await runner._maybe_escalate_to_managed(
        spec, state, None, None, None, None
    )

    assert out_plan is plan
    assert out_planner is planner_sentinel
    assert out_task == "task text"
    assert out_summary == "tools summary"
    assert calls["init_planner"] == 1
    assert state.escalated_this_turn is True
    assert state.consecutive_nontool_iterations == 0
    assert state.progress_tracker is not None
    assert state.plan_snapshot is not None
    assert state.plan_snapshot.origin == "escalated"


@pytest.mark.asyncio
async def test_maybe_escalate_skips_when_already_escalated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(MagicMock(spec=LLMProvider))
    spec = _fast_spec()
    state = _TurnState()
    state.turn_id = "turn-1"
    state.escalated_this_turn = True
    state.consecutive_nontool_iterations = 5
    original_plan = _make_plan()
    original_planner = object()

    async def fail_init(spec: AgentRunSpec) -> tuple[Any, Any, str | None, str | None]:
        raise AssertionError("_init_planner must not be called")

    monkeypatch.setattr(runner, "_init_planner", fail_init)

    result = await runner._maybe_escalate_to_managed(
        spec, state, original_planner, original_plan, "task", "summary"
    )

    assert result == (original_planner, original_plan, "task", "summary")


# --- 3: escalation failure falls back to FAST with a warning ------------------


@pytest.mark.asyncio
async def test_maybe_escalate_failure_returns_original_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(MagicMock(spec=LLMProvider))
    spec = _fast_spec()
    state = _TurnState()
    state.turn_id = "turn-1"
    state.consecutive_nontool_iterations = 2

    async def raising_init(spec: AgentRunSpec) -> tuple[Any, Any, str | None, str | None]:
        raise RuntimeError("planner down")

    monkeypatch.setattr(runner, "_init_planner", raising_init)

    warnings: list[str] = []
    handler_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        out_planner, out_plan, out_task, out_summary = await runner._maybe_escalate_to_managed(
            spec, state, "planner-x", None, None, None
        )
    finally:
        loguru_logger.remove(handler_id)

    assert out_planner == "planner-x"
    assert out_plan is None
    assert out_task is None
    assert out_summary is None
    assert state.escalated_this_turn is False
    assert any("Escalation FAST->MANAGED failed" in w for w in warnings)


# --- 4: structural guard — the main loop stays an orchestrator ---------------


def test_run_with_ledger_is_not_a_god_method() -> None:
    source = inspect.getsource(AgentRunner._run_with_ledger)

    assert len(source.splitlines()) < 260


# --- 5: managed-turn smoke with receipt acceptance ---------------------------


@pytest.mark.asyncio
async def test_managed_turn_smoke_with_receipt_acceptance(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(WriteFileTool(workspace=tmp_path))
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=json.dumps(
                    {
                        "goal": "ship",
                        "steps": [
                            {"id": 1, "action": "write a.txt", "done_criteria": "written"},
                            {"id": 2, "action": "report", "done_criteria": "reported"},
                        ],
                    }
                ),
                usage={},
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "hello"},
                    )
                ],
                usage={},
            ),
            LLMResponse(content="written", usage={}),
            LLMResponse(content="reported", usage={}),
        ]
    )

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=tools,
            model="test-model",
            max_iterations=8,
            max_tool_result_chars=1000,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
        )
    )

    assert result.stop_reason == "completed"
    assert result.final_content == "reported"
    assert result.plan is not None
    assert all(step.status is StepStatus.COMPLETED for step in result.plan.steps)
    step1_evidence = result.plan.step_evidence[0]
    assert step1_evidence.accepted is True
    assert step1_evidence.observations[0]["tool_name"] == "write_file"
    assert step1_evidence.observations[0]["receipt"]["committed"] is True
    assert [o["tool_name"] for o in result.tool_observations] == ["write_file"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"
