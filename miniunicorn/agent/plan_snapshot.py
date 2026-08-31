"""Immutable point-in-time serialization of a managed Plan (P1-T2).

A :class:`PlanSnapshot` captures the plan's goal, serialized steps,
replan counters, and current step id at a transition point (plan created,
step started/completed, replanned, or terminal). Snapshots are emitted
through the runner's checkpoint callback under ``phase="plan_snapshot"``
so plan progress is observable and recoverable without holding live
Plan/PlanStep object references.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from miniunicorn.agent.planner import Plan


def _plan_digest(goal: str, steps: list[dict[str, Any]], replan_count: int) -> str:
    """Canonical digest of the plan body.

    Deliberately excludes turn_id / created_at / stop_reason / origin: those
    change on every emission, so including them would make the digest useless
    for telling "the plan actually changed" apart from "a transition happened".
    """
    payload = json.dumps(
        {"goal": goal, "steps": steps, "replan_count": replan_count},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """Serialized view of a :class:`~miniunicorn.agent.planner.Plan`."""

    goal: str
    steps: list[dict[str, Any]]  # serialized PlanStep.to_dict()
    replan_count: int
    max_replans: int
    current_step_id: int | None  # None when plan is complete or has no current step
    turn_id: str
    created_at: str  # ISO 8601 timestamp
    stop_reason: str | None = None  # set on terminal snapshots
    origin: str = "planner"  # "planner" or "escalated"
    digest: str = ""  # canonical digest of the plan body (audit comparison)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": list(self.steps),
            "replan_count": self.replan_count,
            "max_replans": self.max_replans,
            "current_step_id": self.current_step_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "stop_reason": self.stop_reason,
            "origin": self.origin,
            "digest": self.digest,
        }

    def with_origin(self, origin: str) -> PlanSnapshot:
        """Copy of this snapshot with a different origin (e.g. 'escalated')."""
        return PlanSnapshot(
            goal=self.goal,
            steps=self.steps,
            replan_count=self.replan_count,
            max_replans=self.max_replans,
            current_step_id=self.current_step_id,
            turn_id=self.turn_id,
            created_at=self.created_at,
            stop_reason=self.stop_reason,
            origin=origin,
            digest=self.digest,
        )

    @classmethod
    def from_plan(
        cls,
        plan: Plan,
        turn_id: str,
        stop_reason: str | None = None,
        origin: str = "planner",
    ) -> PlanSnapshot:
        current = plan.current_step
        steps = [s.to_dict() for s in plan.steps]
        return cls(
            goal=plan.goal,
            steps=steps,
            replan_count=plan.replan_count,
            max_replans=plan.max_replans,
            current_step_id=current.id if current is not None else None,
            turn_id=turn_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            stop_reason=stop_reason,
            origin=origin,
            digest=_plan_digest(plan.goal, steps, plan.replan_count),
        )
