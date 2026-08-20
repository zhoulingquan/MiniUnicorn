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

if TYPE_CHECKING:
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec


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

    async def init_planner(
        self, spec: AgentRunSpec
    ) -> tuple[Any, Any, str | None, str | None]:
        """Plan-and-Execute 初始化, 返回 (planner, plan, task_text, tools_summary)。

        创建计划失败时回退 ReAct-only (planner/plan 均为 None)。Typed as Any
        to avoid importing planner at module load time (keeps runner.py
        import-light).
        """
        if not getattr(spec, "use_planner", False):
            return None, None, None, None
        from miniunicorn.agent.planner import Planner as _Planner

        planner_model = getattr(spec, "planner_model", None) or spec.model
        planner = _Planner(self._runner.provider, planner_model)
        task_text = self._runner._extract_task_from_messages(spec.initial_messages)
        tools_summary = self._runner._build_tools_summary(spec.tools)
        try:
            plan = await planner.create_plan(
                task=task_text,
                tools_summary=tools_summary,
            )
            plan.max_replans = getattr(spec, "planner_max_replans", 3)
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

    def apply_plan_step_guidance(
        self, messages_for_model: list[dict[str, Any]], plan: Any
    ) -> list[dict[str, Any]]:
        """Mark the current plan step IN_PROGRESS and append step guidance."""
        from miniunicorn.agent.planner import StepStatus as _StepStatus

        step = plan.current_step
        step.status = _StepStatus.IN_PROGRESS
        step.iterations_used += 1
        guidance = (
            f"\n\n[Current Plan Step {step.id}/{len(plan.steps)}: {step.action}]\n"
            f"Done when: {step.done_criteria or 'step goal achieved'}\n"
            f"Focus on this step. Use tool_hint={step.tool_hint} if applicable."
        )
        return self._runner._inject_step_guidance(messages_for_model, guidance)

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
        if reflection is None or (iteration + 1) % getattr(
            spec, "reflection_interval", 5
        ) != 0:
            return
        # 跟踪 reflection 任务避免被 GC 回收，完成后从集合移除
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

    async def complete_plan_step(
        self,
        plan: Any,
        context: AgentHookContext,
        hook: AgentHook,
        clean: str | None,
        stop_reason: str,
    ) -> bool:
        """Mark the current plan step COMPLETED.

        Returns True when pending steps remain (continue the loop); False
        when all steps are done (caller finalizes the turn).
        """
        from miniunicorn.agent.planner import StepStatus as _StepStatus

        completed_step = plan.current_step
        completed_step.status = _StepStatus.COMPLETED
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
