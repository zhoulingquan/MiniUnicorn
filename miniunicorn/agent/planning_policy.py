"""PlanningPolicy — deterministic FAST/MANAGED planning-mode selection.

Replaces the static ``use_planner: bool`` flag (P0) with a frozen dataclass
so FAST/MANAGED selection stays config-driven and extensible without adding
a classifier LLM call. ``PlanningMode.FAST`` skips the planner entirely;
``PlanningMode.MANAGED`` invokes ``Planner.create_plan()`` as before.

Design ref: docs/superpowers/specs/2026-08-24-lean-react-kernel-p1-p2-design.md
§ P1 Architecture §1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlanningMode(str, Enum):
    """Deterministic planning modes. Config-driven, never runtime-classified."""

    FAST = "fast"
    MANAGED = "managed"


@dataclass(frozen=True, slots=True)
class PlanningPolicy:
    """Which planning mode a turn uses, plus planner tuning knobs."""

    mode: PlanningMode = PlanningMode.FAST
    planner_model: str | None = None
    planner_max_replans: int = 3

    @classmethod
    def from_use_planner(
        cls,
        use_planner: bool,
        planner_model: str | None = None,
        planner_max_replans: int = 3,
    ) -> "PlanningPolicy":
        """Backward-compatible constructor from P0's use_planner flag."""
        mode = PlanningMode.MANAGED if use_planner else PlanningMode.FAST
        return cls(mode=mode, planner_model=planner_model, planner_max_replans=planner_max_replans)

    def escalate(
        self,
        current: PlanningMode,
        stall_detected: bool,
        *,
        already_escalated: bool,
    ) -> PlanningMode:
        """FAST + stall + not already escalated -> MANAGED; else unchanged."""
        if current is PlanningMode.FAST and stall_detected and not already_escalated:
            return PlanningMode.MANAGED
        return current
