"""Turn recovery policy split out of ``AgentRunner`` (PR-5b).

``TurnRecoveryPolicy`` owns the turn-ending / recovery paths that used to
live on ``AgentRunner``: empty-response retries, truncated-output length
recovery, max-iterations finalization, end-turn-with-drain, and fatal
tool-error handling.

Collaborators (the model request path, budget checks, injection drain,
message append helpers, planner integration) are reached through the host
``AgentRunner`` reference this policy is constructed with, following the
PR-5a host pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.utils.helpers import build_assistant_message
from miniunicorn.utils.prompt_templates import render_template
from miniunicorn.utils.runtime import (
    build_length_recovery_message,
    is_blank_text,
)

if TYPE_CHECKING:
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec, _TurnState
    from miniunicorn.providers.base import LLMResponse

_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_SNIP_SAFETY_BUFFER = 1024


class TurnRecoveryPolicy:
    """Recovery and turn-ending decisions for a single agent turn.

    Constructed with the host ``AgentRunner``; collaborators are reached
    through the runner reference so the runner keeps only thin delegation
    methods of the same name.
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    async def handle_fatal_tool_error(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        context: AgentHookContext,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        *,
        plan: Any,
        planner: Any,
        planner_task_text: str | None,
        planner_tools_summary: str | None,
        fatal_error: BaseException,
        iteration: int,
        reflection: Any | None,
    ) -> tuple[str, Any]:
        """Fatal tool error path. Returns (action, plan).

        Plan-and-Execute: mark current step failed and trigger replan with
        the failure reason. Successful steps are preserved by the planner;
        the failed step is excluded from the new plan's approach. When
        replans are exhausted we fall through to the normal failure exit.
        """
        if plan is not None and planner is not None and plan.current_step is not None:
            from miniunicorn.agent import progress_policy as _progress_policy
            from miniunicorn.agent.planner import StepStatus as _StepStatus

            failed_step = plan.current_step
            failed_step.status = _StepStatus.FAILED
            failed_step.failure_reason = str(fatal_error)
            # P1-T4: no-progress detection — repeated failures abort the turn.
            tracker = state.progress_tracker
            if tracker is not None:
                verdict = tracker.check_failure_progress(plan)
                if verdict.action is _progress_policy.ProgressAction.ABORT:
                    logger.warning(
                        "ProgressPolicy: {} after {} failed step(s); aborting turn",
                        verdict.reason,
                        len(plan.failed_steps),
                    )
                    state.stop_reason = "no_progress"
            if state.stop_reason == "no_progress":
                logger.warning(
                    "Step {} failed; progress policy aborted the turn",
                    failed_step.id,
                )
            elif plan.can_replan:
                from miniunicorn.agent.planner import PlannerStatus as _PlannerStatus

                logger.info(
                    "Step {} failed ({}); triggering replan {}/{}",
                    failed_step.id,
                    failed_step.action,
                    plan.replan_count + 1,
                    plan.max_replans,
                )
                result = await planner.replan(
                    plan,
                    failed_step,
                    str(fatal_error),
                    planner_task_text or "",
                    planner_tools_summary or "",
                )
                await hook.after_iteration(context)
                if result.status is _PlannerStatus.VALID:
                    # Drain tool-result messages are already appended; the loop
                    # picks up the replacement plan's first pending step.
                    return "continue", result.plan
                logger.warning(
                    "Replan returned fallback {}; continuing in FAST mode",
                    result.error_code,
                )
                return "continue", None
            else:
                logger.warning(
                    "Step {} failed and max_replans reached; failing turn",
                    failed_step.id,
                )
                state.stop_reason = "plan_failed"
        if state.stop_reason == "no_progress":
            state.error = "Stopped: repeated plan-step failures with no progress."
            state.final_content = state.error
        else:
            state.error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
            state.final_content = state.error
            if state.stop_reason != "plan_failed":
                state.stop_reason = "tool_error"
        self._runner.append_final_message(messages, state.final_content)
        context.final_content = state.final_content
        context.error = state.error
        context.stop_reason = state.stop_reason
        # Reflection: capture lesson learned on fatal tool/plan/no-progress exit.
        if reflection is not None:
            trigger = "tool_error"
            if state.stop_reason == "plan_failed":
                trigger = "plan_failed"
            elif state.stop_reason == "no_progress":
                trigger = "no_progress"
            await reflection.reflect(
                trigger=trigger,
                iteration=iteration,
                context_summary=state.error,
                messages=messages,
                session_key=spec.session_key,
                user_key=spec.user_key,
            )
        action = await self.end_turn_with_drain(
            spec, state, context, hook, messages, phase="after tool error"
        )
        return action, plan

    async def end_turn_with_drain(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        context: AgentHookContext,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        *,
        phase: str,
    ) -> str:
        """after_iteration + injection drain. Returns "continue" or "break"."""
        await hook.after_iteration(context)
        should_continue, state.injection_cycles = await self._runner.try_drain_injections(
            spec,
            messages,
            None,
            state.injection_cycles,
            phase=phase,
        )
        if should_continue:
            state.had_injections = True
            return "continue"
        return "break"

    async def retry_empty_response(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        context: AgentHookContext,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        messages_for_model: list[dict[str, Any]],
        budget: Any,
        response: LLMResponse,
        raw_usage: dict[str, int],
        clean: str | None,
        iteration: int,
    ) -> tuple[str, LLMResponse, dict[str, int], str | None]:
        """Empty-response retry path.

        Returns (action, response, raw_usage, clean); action is "proceed"
        (continue termination checks), "continue" (retry iteration) or
        "break" (budget exceeded during finalization retry).
        """
        if response.finish_reason == "error" or not is_blank_text(clean):
            return "proceed", response, raw_usage, clean
        state.empty_content_retries += 1
        if state.empty_content_retries < _MAX_EMPTY_RETRIES:
            logger.warning(
                "Empty response on turn {} for {} ({}/{}); retrying",
                iteration,
                spec.session_key or "default",
                state.empty_content_retries,
                _MAX_EMPTY_RETRIES,
            )
            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=False)
            await hook.after_iteration(context)
            return "continue", response, raw_usage, clean
        logger.warning(
            "Empty response on turn {} for {} after {} retries; attempting finalization",
            iteration,
            spec.session_key or "default",
            state.empty_content_retries,
        )
        if hook.wants_streaming():
            await hook.on_stream_end(context, resuming=False)
        response = await self._runner.request_finalization_retry(spec, messages_for_model)
        retry_usage = self._runner.usage_dict(response.usage)
        self._runner.accumulate_usage(state.usage, retry_usage)
        # Budget check: stop early if cumulative usage exceeds limits.
        _fc, _sr, _err = self._runner.handle_budget_exceeded(
            budget,
            retry_usage,
            spec.model,
            spec,
            messages,
            iteration,
            context,
            hook,
        )
        if _fc is not None:
            state.final_content, state.stop_reason, state.error = _fc, _sr, _err
            self._runner.append_final_message(messages, state.final_content)
            await hook.after_iteration(context)
            return "break", response, raw_usage, clean
        raw_usage = self._runner.merge_usage(raw_usage, retry_usage)
        context.response = response
        context.usage = dict(raw_usage)
        context.tool_calls = list(response.tool_calls)
        if retry_usage:
            state.last_call_usage = dict(retry_usage)
        clean = hook.finalize_content(context, response.content)
        return "proceed", response, raw_usage, clean

    async def handle_length_recovery(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        context: AgentHookContext,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        response: LLMResponse,
        clean: str | None,
        iteration: int,
    ) -> bool:
        """Truncated-output recovery. Returns True to retry the iteration."""
        state.length_recovery_count += 1
        if state.length_recovery_count > _MAX_LENGTH_RECOVERIES:
            return False
        logger.info(
            "Output truncated on turn {} for {} ({}/{}); continuing",
            iteration,
            spec.session_key or "default",
            state.length_recovery_count,
            _MAX_LENGTH_RECOVERIES,
        )
        if hook.wants_streaming():
            await hook.on_stream_end(context, resuming=True)
        messages.append(
            build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
        )
        messages.append(build_length_recovery_message())
        await hook.after_iteration(context)
        return True

    async def finalize_max_iterations(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        messages: list[dict[str, Any]],
        reflection: Any | None,
    ) -> None:
        """for-else branch: max_iterations exhausted."""
        state.stop_reason = "max_iterations"
        if spec.max_iterations_message:
            state.final_content = spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        else:
            state.final_content = render_template(
                "agent/max_iterations_message.md",
                strip=True,
                max_iterations=spec.max_iterations,
            )
        self._runner.append_final_message(messages, state.final_content)
        # Reflection: capture lesson learned on max_iterations exhaustion.
        if reflection is not None:
            await reflection.reflect(
                trigger="max_iterations",
                iteration=spec.max_iterations - 1,
                context_summary=f"Hit max_iterations ({spec.max_iterations})",
                messages=messages,
                session_key=spec.session_key,
                user_key=spec.user_key,
            )
        # Drain any remaining injections so they are appended to the
        # conversation history instead of being re-published as
        # independent inbound messages by _dispatch's finally block.
        # We ignore should_continue here because the for-loop has already
        # exhausted all iterations.
        drained, state.injection_cycles = await self._runner.try_drain_injections(
            spec,
            messages,
            None,
            state.injection_cycles,
            phase="after max_iterations",
        )
        if drained:
            state.had_injections = True
