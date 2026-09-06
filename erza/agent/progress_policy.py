"""ProgressPolicy — no-progress detection for managed plans (P1-T4).

Detects when a plan step is stalling and recommends an action: continue,
replan, or abort. Three signals are tracked per turn:

1. ``iterations_used`` exceeding ``max_step_iterations`` → ``REPLAN``
   (reason ``step_iteration_limit``).
2. Consecutive step evaluations with truly empty evidence (no tool calls
   and no content) reaching ``max_empty_steps`` → ``ABORT`` (reason
   ``no_progress``).
3. Accumulated FAILED steps reaching ``max_repeated_failures`` →
   ``ABORT`` (reason ``repeated_failures``).

The policy is a frozen tuning-knob record; the tracker holds the per-turn
counters. No LLM calls — detection is rule-based.

Design ref: docs/superpowers/specs/2026-08-24-lean-react-kernel-p1-p2-design.md
§ P1 Architecture §4.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from erza.agent.planner import Plan, PlanStep, StepStatus
from erza.agent.step_acceptance import StepEvidence


class ProgressAction(str, Enum):
    """Recommended reaction to the observed progress signal."""

    CONTINUE = "continue"
    REPLAN = "replan"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class ProgressVerdict:
    """Outcome of a progress check: recommended action plus reason code."""

    action: ProgressAction
    reason: str


@dataclass(frozen=True, slots=True)
class ProgressPolicy:
    """Tuning knobs for stall detection."""

    max_step_iterations: int = 5  # max ReAct iterations per step
    max_empty_steps: int = 3  # max consecutive steps with empty evidence
    max_repeated_failures: int = 2  # max consecutive step failures before giving up


class ProgressTracker:
    """Stateful per-turn detector for stalled managed-plan execution."""

    def __init__(self, policy: ProgressPolicy) -> None:
        self._policy = policy
        self._consecutive_empty: int = 0
        self._consecutive_failures: int = 0
        self._verifier_failures: int = 0

    def record_verifier_failure(self) -> None:
        self._verifier_failures += 1

    def record_verifier_success(self) -> None:
        self._verifier_failures = 0

    def check_step_progress(
        self,
        step: PlanStep,
        evidence: StepEvidence | None,
    ) -> ProgressVerdict:
        """Evaluate progress after a step evaluation."""
        # Check iteration limit.
        if step.iterations_used > self._policy.max_step_iterations:
            return ProgressVerdict(ProgressAction.REPLAN, "step_iteration_limit")

        # The verifier is the rescue channel for tool-level evidence. When it
        # keeps failing, step verdicts are untrustworthy — change approach
        # instead of burning more verifier calls.
        if self._verifier_failures >= 2:
            return ProgressVerdict(ProgressAction.REPLAN, "verifier_unavailable")

        # Check for empty evidence (no tools, no content).
        if evidence is not None and not evidence.accepted:
            if not evidence.tool_calls and not (
                evidence.final_content and evidence.final_content.strip()
            ):
                self._consecutive_empty += 1
                if self._consecutive_empty >= self._policy.max_empty_steps:
                    return ProgressVerdict(ProgressAction.ABORT, "no_progress")
            else:
                self._consecutive_empty = 0
        else:
            self._consecutive_empty = 0

        return ProgressVerdict(ProgressAction.CONTINUE, "ok")

    def check_failure_progress(
        self,
        plan: Plan,
    ) -> ProgressVerdict:
        """Called after a step fails (FAILED status)."""
        failed = [s for s in plan.steps if s.status == StepStatus.FAILED]
        self._consecutive_failures = len(failed)
        if self._consecutive_failures >= self._policy.max_repeated_failures:
            return ProgressVerdict(ProgressAction.ABORT, "repeated_failures")
        return ProgressVerdict(ProgressAction.REPLAN, "step_failed")
