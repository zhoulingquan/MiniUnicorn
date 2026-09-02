"""Planning and reflection service split out of ``AgentRunner`` (PR-5c).

``PlanningReflectionService`` owns the plan-and-execute and reflection
paths that used to live on ``AgentRunner``: planner initialization,
per-step plan guidance, plan-step completion, reflection initialization,
and periodic reflection firing.  It also owns the ``_reflection_tasks``
task-tracking set that used to live on the runner.

The provider is read through ``runner.provider`` at call time (never cached
at construction) so ``ProviderRegistry`` hot-switching keeps applying to
in-flight turns.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.ledger import allow_call_ledger_child_tasks

if TYPE_CHECKING:
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
    from miniunicorn.agent.step_acceptance import ToolObservation


class PlanningReflectionService:
    """Plan-and-Execute and reflection for a single agent turn.

    Constructed with the host ``AgentRunner``; helper collaborators (task
    extraction, tools summary, step guidance injection) are reached through
    the runner reference this service is constructed with, following the
    PR-5a host pattern.
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner
        # 跟踪 reflection 后台任务，避免被 GC 回收
        self._reflection_tasks: set[asyncio.Task] = set()

    async def emit_plan_snapshot(
        self,
        spec: AgentRunSpec,
        plan: Any,
        turn_id: str,
        stop_reason: str | None = None,
        origin: str = "planner",
    ) -> Any:
        """Serialize *plan* into a PlanSnapshot and emit it as a checkpoint.

        Returns the created snapshot so callers can keep the latest one on
        the turn state. The ``plan_snapshot`` checkpoint payload is additive
        and never replaces existing checkpoint emissions.
        """
        from miniunicorn.agent.plan_snapshot import PlanSnapshot

        snapshot = PlanSnapshot.from_plan(plan, turn_id, stop_reason, origin=origin)
        await self._runner.emit_checkpoint(
            spec,
            {
                "phase": "plan_snapshot",
                "plan_snapshot": snapshot.to_dict(),
            },
        )
        return snapshot

    async def init_planner(self, spec: AgentRunSpec) -> tuple[Any, Any, str | None, str | None]:
        """Plan-and-Execute 初始化, 返回 (planner, plan, task_text, tools_summary)。

        创建计划失败时回退 ReAct-only (planner/plan 均为 None)。Typed as Any
        to avoid importing planner at module load time (keeps runner.py
        import-light).

        Mode resolution (P1): an explicit ``spec.planning_policy`` takes
        priority; otherwise fall back to P0's legacy ``use_planner`` flag.
        ``PlanningMode.FAST`` skips the planner entirely.
        """
        from miniunicorn.agent.planning_policy import PlanningMode

        if getattr(spec, "planning_policy", None) is not None:
            policy_mode = spec.planning_policy.mode
            planner_model = spec.planning_policy.planner_model or spec.model
            max_replans = spec.planning_policy.planner_max_replans
            if policy_mode != PlanningMode.MANAGED:
                return None, None, None, None
        elif not getattr(spec, "use_planner", False):
            return None, None, None, None
        else:
            planner_model = getattr(spec, "planner_model", None) or spec.model
            max_replans = getattr(spec, "planner_max_replans", 3)

        from miniunicorn.agent.planner import Planner as _Planner
        from miniunicorn.agent.planner import PlannerStatus as _PlannerStatus

        planner = _Planner(self._runner.provider, planner_model)
        task_text = self._runner.extract_task_from_messages(spec.initial_messages)
        tools_summary = self._runner.build_tools_summary(spec.tools)
        try:
            result = await planner.create_plan(
                task=task_text,
                tools_summary=tools_summary,
            )
            if result.status is not _PlannerStatus.VALID:
                logger.warning(
                    "Planner returned fallback {}; using ReAct-only",
                    result.error_code,
                )
                return None, None, task_text, tools_summary
            plan = result.plan
            plan.max_replans = max_replans
            logger.info(
                "Planner produced {} steps for: {}",
                len(plan.steps),
                plan.goal,
            )
        except Exception:
            logger.exception("Planner.create_plan failed; falling back to ReAct-only")
            return None, None, None, None
        return planner, plan, task_text, tools_summary

    def init_reflection(self, spec: AgentRunSpec) -> Any | None:
        """Optional reflection: produces "lesson learned" entries on failure or
        every reflection_interval iterations. Default False keeps the legacy
        behavior with zero reflection overhead."""
        if not getattr(spec, "enable_reflection", False):
            return None
        from miniunicorn.agent.reflection import Reflection

        return Reflection(
            self._runner.provider,
            spec.model,
            spec.workspace,
        )

    async def apply_plan_step_guidance(
        self,
        messages_for_model: list[dict[str, Any]],
        plan: Any,
        *,
        spec: AgentRunSpec | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mark the current plan step IN_PROGRESS and append step guidance.

        When *spec* and *turn_id* are provided, a plan snapshot is emitted
        after the status transition.
        """
        from miniunicorn.agent.planner import StepStatus as _StepStatus

        step = plan.current_step
        step.status = _StepStatus.IN_PROGRESS
        step.iterations_used += 1
        if spec is not None and turn_id is not None:
            await self.emit_plan_snapshot(spec, plan, turn_id)
        guidance = (
            f"\n\n[Current Plan Step {step.id}/{len(plan.steps)}: {step.action}]\n"
            f"Done when: {step.done_criteria or 'step goal achieved'}\n"
            f"Focus on this step. Use tool_hint={step.tool_hint} if applicable."
        )
        return self._runner.inject_step_guidance(messages_for_model, guidance)

    def fire_periodic_reflection(
        self,
        reflection: Any | None,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> None:
        """Periodic reflection (every reflection_interval iterations).

        Non-blocking: fire-and-forget so the main loop isn't slowed.
        """
        if reflection is None or (iteration + 1) % getattr(spec, "reflection_interval", 5) != 0:
            return
        # 跟踪 reflection 任务避免被 GC 回收，完成后从集合移除
        with allow_call_ledger_child_tasks():
            task = asyncio.create_task(
                reflection.reflect(
                    trigger="periodic",
                    iteration=iteration,
                    context_summary=f"Periodic reflection at iteration {iteration}",
                    messages=messages,
                    session_key=spec.session_key,
                    user_key=spec.user_key,
                )
            )
        self._reflection_tasks.add(task)
        task.add_done_callback(self._reflection_tasks.discard)

    # Stop reasons that already have inline reflection in the loop or
    # recovery path — terminal reflection skips them to avoid duplication.
    _INLINE_REFLECTED_REASONS = frozenset(
        {"tool_error", "plan_failed", "no_progress", "error", "max_iterations"}
    )

    _TERMINAL_TRIGGER_MAP = {
        "completed": "turn_completed",
        "budget_exceeded": "budget_exceeded",
        "turn_timeout": "turn_timeout",
    }

    async def fire_terminal_reflection(
        self,
        reflection: Any | None,
        spec: AgentRunSpec,
        state: Any,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> None:
        """Fire reflection once after the runner loop exits (P1-T6).

        Only fires for stop reasons that lack inline reflection (completed,
        budget_exceeded, turn_timeout). Wrapped in try/except so a
        reflection failure never blocks ``AgentRunResult`` return.
        """
        if reflection is None:
            return
        reason = state.stop_reason
        if reason in self._INLINE_REFLECTED_REASONS:
            return
        trigger = self._TERMINAL_TRIGGER_MAP.get(reason, reason)
        try:
            await reflection.reflect(
                trigger=trigger,
                iteration=iteration,
                context_summary=state.final_content or f"Turn ended: {reason}",
                messages=messages,
                session_key=spec.session_key,
                user_key=spec.user_key,
            )
        except Exception:
            logger.warning(
                "Terminal reflection failed for {}; result still returned",
                reason,
            )

    async def complete_plan_step(
        self,
        plan: Any,
        context: AgentHookContext,
        hook: AgentHook,
        clean: str | None,
        stop_reason: str,
        *,
        spec: AgentRunSpec | None = None,
        turn_id: str | None = None,
        tool_observations: list[ToolObservation] | None = None,
    ) -> bool:
        """Evaluate step evidence and mark COMPLETED if accepted.

        Returns True when pending steps remain (continue the loop); False
        when all steps are done (caller finalizes the turn). When *spec*
        and *turn_id* are provided, a plan snapshot is emitted after the
        transition — terminal with ``stop_reason="plan_completed"`` when
        the plan is fully done.

        When evidence is rejected, the step remains IN_PROGRESS and True
        is returned (more steps remain — the current step still needs work).
        """
        from miniunicorn.agent.planner import StepStatus as _StepStatus
        from miniunicorn.agent.step_acceptance import StepAcceptancePolicy

        completed_step = plan.current_step
        if completed_step is None:
            return False

        policy = StepAcceptancePolicy()

        # Initialize verifier cache on plan if not present
        if not hasattr(plan, "_verifier_cache"):
            plan._verifier_cache = {}

        # Use verifier-enabled evaluation when spec provides the config
        if spec is not None and getattr(spec, "enable_step_verifier", False):
            evidence = await policy.evaluate_with_verifier(
                step=completed_step,
                observations=tool_observations,
                final_content=clean,
                iterations_used=completed_step.iterations_used,
                provider=self._runner.provider,
                model=spec.model,
                enable_verifier=True,
                step_evidence_cache=plan._verifier_cache,
            )
        else:
            evidence = policy.evaluate(
                step=completed_step,
                observations=tool_observations,
                final_content=clean,
                iterations_used=completed_step.iterations_used,
            )
        plan.step_evidence.append(evidence)

        if not evidence.accepted:
            logger.info(
                "Step {} evidence rejected ({}); keeping IN_PROGRESS",
                completed_step.id,
                evidence.rejection_reason,
            )
            return True

        completed_step.status = _StepStatus.COMPLETED
        if spec is not None and turn_id is not None:
            await self.emit_plan_snapshot(
                spec,
                plan,
                turn_id,
                stop_reason="plan_completed" if plan.all_done else None,
            )
        if plan.current_step is not None:
            logger.info(
                "Step {} completed ({}); {} steps remaining",
                completed_step.id,
                completed_step.action,
                len(plan.pending_steps),
            )
            context.final_content = clean
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            return True
        logger.info(
            "All plan steps completed (last: {})",
            completed_step.action,
        )
        return False

    def evaluate_step_progress(self, plan: Any, tracker: Any | None) -> Any | None:
        """Check managed-plan progress after a step evaluation (P1-T4).

        Feeds the latest ``Plan.step_evidence`` entry (if any) plus the
        current step into *tracker*. Returns the tracker's
        ``ProgressVerdict``, or ``None`` when no tracker is attached
        (FAST mode / legacy ReAct-only turns).
        """
        if tracker is None or plan is None:
            return None
        evidence = plan.step_evidence[-1] if plan.step_evidence else None
        # Feed the verifier circuit breaker before the verdict is computed: a
        # verdict that was reached without the verifier says nothing about it.
        verdict_dict = evidence.verifier_verdict if evidence is not None else None
        if verdict_dict is not None:
            if verdict_dict.get("error") == "verifier_failed":
                tracker.record_verifier_failure()
            else:
                tracker.record_verifier_success()
        return tracker.check_step_progress(plan.current_step, evidence)
