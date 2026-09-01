"""W0-B: plan_snapshot audit isolation and snapshot digest persistence.

Plan snapshots are emitted on every step transition. Before this batch they
were written into the single-slot runtime checkpoint, so the last thing
persisted before a crash was usually a snapshot instead of the real execution
breakpoint — recovery then materialised a payload with nothing to restore.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.plan_snapshot import PlanSnapshot
from miniunicorn.agent.planner import Plan, PlanStep, StepStatus
from miniunicorn.agent.session_turn import SessionTurnService


def _service() -> SessionTurnService:
    return SessionTurnService(MagicMock())


def _session(messages: list[dict[str, Any]] | None = None) -> Any:
    return SimpleNamespace(metadata={}, messages=messages if messages is not None else [])


def _plan() -> Plan:
    return Plan(
        goal="ship",
        steps=[PlanStep(id=1, action="first"), PlanStep(id=2, action="second")],
        replan_count=0,
        max_replans=3,
    )


# --- 1: a plan snapshot must not evict a real recovery checkpoint ------------


def test_plan_snapshot_does_not_overwrite_recovery_checkpoint() -> None:
    service = _service()
    session = _session()
    awaiting: dict[str, Any] = {
        "phase": "awaiting_tools",
        "assistant_message": {"role": "assistant", "content": "calling tools"},
        "completed_tool_results": [],
        "pending_tool_calls": [],
    }

    service._set_runtime_checkpoint(session, awaiting)
    service._set_runtime_checkpoint(session, {"phase": "plan_snapshot", "plan_snapshot": {}})

    assert session.metadata["runtime_checkpoint"] == awaiting


# --- 2: audit-only phases are filtered, recovery phases still persist --------


@pytest.mark.parametrize(
    "phase", ["tool_started", "tool_completed", "tool_blocked", "plan_snapshot"]
)
def test_audit_only_phases_skip_persistence(phase: str) -> None:
    service = _service()
    session = _session()

    service._set_runtime_checkpoint(session, {"phase": phase})

    assert "runtime_checkpoint" not in session.metadata


def test_recovery_phases_still_persist() -> None:
    service = _service()
    session = _session()

    for phase in ("awaiting_tools", "tools_completed", "final_response"):
        service._set_runtime_checkpoint(session, {"phase": phase})

    assert session.metadata["runtime_checkpoint"]["phase"] == "final_response"


# --- 3: restore still materialises the breakpoint, not the snapshot ----------


def test_restore_after_snapshot_burst_materialises_breakpoint() -> None:
    service = _service()
    session = _session()
    awaiting: dict[str, Any] = {
        "phase": "awaiting_tools",
        "assistant_message": {"role": "assistant", "content": "calling tools"},
        "completed_tool_results": [],
        "pending_tool_calls": [{"id": "c1", "function": {"name": "write_file"}}],
    }

    service._set_runtime_checkpoint(session, awaiting)
    # A managed step emits a snapshot on start and on completion.
    service._set_runtime_checkpoint(session, {"phase": "plan_snapshot"})
    service._set_runtime_checkpoint(session, {"phase": "plan_snapshot"})

    assert service._restore_runtime_checkpoint(session) is True
    assert [m.get("role") for m in session.messages] == ["assistant", "tool"]
    assert session.messages[-1]["tool_call_id"] == "c1"
    assert "runtime_checkpoint" not in session.metadata


# --- 4: digest is deterministic across emissions -----------------------------


def test_digest_is_stable_across_emissions() -> None:
    first = PlanSnapshot.from_plan(_plan(), "turn-1")
    second = PlanSnapshot.from_plan(_plan(), "turn-2")

    assert first.digest == second.digest
    assert first.digest


# --- 5: digest tracks the plan body, not the emission context ----------------


def test_digest_changes_with_step_status() -> None:
    plan = _plan()
    before = PlanSnapshot.from_plan(plan, "t").digest

    plan.steps[0].status = StepStatus.COMPLETED
    after = PlanSnapshot.from_plan(plan, "t").digest

    assert before != after


def test_digest_ignores_emission_metadata() -> None:
    plan = _plan()
    base = PlanSnapshot.from_plan(plan, "t1")
    terminal = PlanSnapshot.from_plan(plan, "t2", stop_reason="plan_completed", origin="escalated")

    assert base.digest == terminal.digest


# --- 6: digest is serialized -------------------------------------------------


def test_to_dict_carries_digest() -> None:
    data = PlanSnapshot.from_plan(_plan(), "t1").to_dict()

    assert "digest" in data
    assert data["digest"]
