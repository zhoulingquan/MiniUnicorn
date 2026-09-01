"""``activate_plan`` tool: mount a plan for the outer main loop to drive.

This is an *activator*, not an executor. The model submits a JSON plan which is
parsed, structurally validated (no LLM involved), and hung onto a module-private
contextvar. Control returns to the main loop immediately — no steps run, no
subagents spawn, no nested loop. The main loop's existing plan-driving machinery
(``apply_plan_step_guidance`` per-iteration step guidance + ``complete_plan_step``
acceptance) advances the plan on the next iteration.

The pending plan is turn-scoped: it is consumed once by the main loop via
``take_pending_plan``; if a turn ends without consuming it, the plan is dropped.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from loguru import logger

from miniunicorn.agent.planner import Plan, PlanStep, _normalize_evidence_level
from miniunicorn.agent.safety_policy import RiskLevel
from miniunicorn.tools.base import Tool, tool_parameters
from miniunicorn.tools.schema import StringSchema, tool_parameters_schema

_MAX_ACTIVATED_STEPS = 8

_pending_plan: ContextVar[Plan | None] = ContextVar("pending_plan_activation", default=None)


def take_pending_plan() -> Plan | None:
    """Consume and reset the pending activated plan (turn-scoped, read-once)."""
    plan = _pending_plan.get()
    _pending_plan.set(None)
    return plan


@tool_parameters(
    tool_parameters_schema(
        plan=StringSchema(
            'A JSON plan: {"goal": "...", "steps": [{"id": 1, "action": "...", '
            '"tool_hint": "...", "done_criteria": "...", "evidence_level": "text"|"tool"}]}'
        ),
        required=["plan"],
    )
)
class ActivatePlanTool(Tool):
    """Mount a plan for the main loop to drive step by step (activator only)."""

    _scopes = {"core"}  # Not available to subagents: no nested mounting ambiguity.

    @property
    def name(self) -> str:
        return "activate_plan"

    @property
    def description(self) -> str:
        return (
            "Mount a plan for the main loop to drive step by step. Provide a JSON "
            "object with a 'goal' string and a 'steps' array (max 8 steps), each "
            "step having 'id' and 'action', plus optional 'tool_hint', "
            "'done_criteria', and 'evidence_level' ('text' or 'tool'). The system "
            "guides you through each step in order; complete steps are accepted by "
            "the verifier. Do NOT claim steps are done — only call this to activate "
            "a plan, never to mark steps complete."
        )

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    async def execute(self, plan: str | None = None, **kwargs: Any) -> str:
        if not plan:
            return "Error: plan is required"
        try:
            parsed = json.loads(plan) if isinstance(plan, str) else plan
        except json.JSONDecodeError as e:
            return f"Error: invalid plan JSON: {e}"

        if not isinstance(parsed, dict):
            return "Error: plan must be a JSON object"
        goal = parsed.get("goal", "untitled goal")
        steps = parsed.get("steps")
        if not isinstance(steps, list) or not steps:
            return "Error: plan has no steps"
        if len(steps) > _MAX_ACTIVATED_STEPS:
            return f"Error: plan exceeds maximum of {_MAX_ACTIVATED_STEPS} steps"

        built: list[PlanStep] = []
        next_id = 1
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            action = raw.get("action") or raw.get("description") or ""
            if not action:
                continue
            built.append(
                PlanStep(
                    id=raw.get("id", next_id),
                    action=action,
                    tool_hint=raw.get("tool_hint"),
                    done_criteria=raw.get("done_criteria"),
                    evidence_level=_normalize_evidence_level(raw.get("evidence_level")),
                )
            )
            next_id += 1
        if not built:
            return "Error: plan has no valid steps"

        if _pending_plan.get() is not None:
            logger.warning(
                "activate_plan: an un-consumed plan was already pending; "
                "the newer activation overwrites it"
            )
        _pending_plan.set(Plan(goal=goal, steps=built))

        return (
            f"Plan activated: {len(built)} steps for goal: {goal}\n"
            f"Step 1: {built[0].action}\n"
            "The system will guide you through each step; complete them in order."
        )
