"""Deterministic step evidence acceptance policy (P1-T3).

Evaluates whether a plan step has produced sufficient evidence to be marked
COMPLETED. No LLM calls — acceptance is rule-based.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from miniunicorn.agent.planner import PlanStep


@dataclass(frozen=True, slots=True)
class StepEvidence:
    """Structured evidence collected during a plan step's execution."""

    step_id: int
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    final_content: str | None
    iterations_used: int
    accepted: bool
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "final_content": self.final_content,
            "iterations_used": self.iterations_used,
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
        }


class StepAcceptancePolicy:
    """Deterministic step acceptance — no LLM calls."""

    def evaluate(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        final_content: str | None,
        iterations_used: int,
    ) -> StepEvidence:
        accepted = self._is_accepted(step, tool_calls, final_content)
        return StepEvidence(
            step_id=step.id,
            tool_calls=list(tool_calls),
            tool_results=list(tool_results),
            final_content=final_content,
            iterations_used=iterations_used,
            accepted=accepted,
            rejection_reason=None
            if accepted
            else self._rejection_reason(step, tool_calls, final_content),
        )

    def _is_accepted(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        final_content: str | None,
    ) -> bool:
        if final_content and final_content.strip():
            if step.done_criteria:
                if step.done_criteria.lower() in final_content.lower():
                    return True
                if tool_calls:
                    return True
                return False
            return True
        return False

    def _rejection_reason(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        final_content: str | None,
    ) -> str:
        if not final_content or not final_content.strip():
            if not tool_calls:
                return "empty_content_no_tools"
            return "empty_content_with_tools"
        if step.done_criteria and step.done_criteria.lower() not in final_content.lower():
            return "done_criteria_not_met"
        return "unknown"
