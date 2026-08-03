"""ReAct control flow collaborator for AgentRunner (Task 12).

``RunController`` owns the main agent iteration loop previously inlined in
``AgentRunner.run``.  Per-run mutable data flows through ``RunLoopState``;
the controller calls the façade's delegate methods so existing monkeypatch
seams remain effective.  No method exceeds 200 physical lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from loguru import logger

from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.ports import (
    _provider_attempt_observer,
    current_provider_attempt_observer,
)
from miniunicorn.agent.runner_types import (
    _DEFAULT_ERROR_MESSAGE,
    AgentRunResult,
    AgentRunSpec,
)
from miniunicorn.agent.turn_runtime import current_turn_runtime
from miniunicorn.providers.base import LLMProvider, LLMResponse
from miniunicorn.utils.helpers import (
    build_assistant_message,
    extract_reasoning,
)
from miniunicorn.utils.prompt_templates import render_template
from miniunicorn.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_length_recovery_message,
    is_blank_text,
)

_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3


class IterationAction(Enum):
    """Action the run loop should take after an iteration phase."""

    CONTINUE = auto()
    BREAK = auto()


@dataclass(slots=True)
class RunLoopState:
    """Per-run mutable data for the ReAct loop.

    Carries exactly the mutable accumulators that flow across iterations.
    Must not own a Provider, registry, callback, session object, or second
    task authority — those stay on the façade and are accessed through it.
    """

    messages: list[dict[str, Any]]
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    last_call_usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    stop_reason: str = "completed"
    tool_events: list[dict[str, str]] = field(default_factory=list)
    external_lookup_counts: dict[str, int] = field(default_factory=dict)
    workspace_violation_counts: dict[str, int] = field(default_factory=dict)
    empty_content_retries: int = 0
    length_recovery_count: int = 0
    had_injections: bool = False
    injection_cycles: int = 0
    plan: Any | None = None
    planner: Any | None = None
    planner_task_text: str | None = None
    planner_tools_summary: str | None = None
    reflection: Any | None = None
    # Setup-only per-run data (not part of the public contract but needed
    # across phase methods).
    restored_models: dict[str, Any] = field(default_factory=dict)
    observer_token: Any = None


class RunController:
    """Owns the ReAct iteration loop (Task 12).

    Constructed by ``AgentRunner`` with a reference to the façade so the
    controller can call delegate methods (``_execute_tools``,
    ``_request_model``, etc.) and existing monkeypatch seams remain
    effective.
    """

    def __init__(self, owner: Any, reflection_supervisor: Any) -> None:
        self._owner = owner
        self._reflection_supervisor = reflection_supervisor

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        state = await self._setup(spec, hook)
        try:
            for iteration in range(spec.max_iterations):
                action = await self._run_iteration(spec, state, hook, iteration)
                if action is IterationAction.BREAK:
                    break
            else:
                await self._finish_exhausted(spec, state, hook)
        finally:
            _provider_attempt_observer.reset(state.observer_token)
        return self._build_result(state)

    # ------------------------------------------------------------------
    # Phase: setup
    # ------------------------------------------------------------------

    async def _setup(self, spec: AgentRunSpec, hook: AgentHook) -> RunLoopState:
        """Initialize per-run state, observer, restore point, planner, reflection."""
        state = RunLoopState(messages=list(spec.initial_messages))
        state.observer_token = _provider_attempt_observer.set(spec.provider_attempt_observer)
        # Load durable restore point before the loop so completed model
        # decisions can be reused without another network request.
        if spec.turn_journal is not None:
            runtime = current_turn_runtime()
            if runtime is not None and runtime.task_id:
                from miniunicorn.agent.ports import TaskIdentity

                task_identity = TaskIdentity(
                    task_id=runtime.task_id,
                    turn_id=runtime.turn_id,
                    session_key=runtime.session_key,
                    session_sequence=runtime.session_sequence,
                    run_segment=runtime.run_segment,
                    lease_epoch=runtime.lease_epoch,
                )
                try:
                    restore_point = await spec.turn_journal.load_restore_point(task_identity)
                except Exception:
                    logger.exception("load_restore_point failed; starting fresh")
                    restore_point = None
                if restore_point is not None:
                    for m in restore_point.completed_models:
                        state.restored_models[m.logical_call_id] = m
                    logger.info(
                        "Loaded restore point: {} completed model decisions",
                        len(state.restored_models),
                    )
        # Plan-and-Execute mode.
        if getattr(spec, "use_planner", False):
            from miniunicorn.agent.planner import Planner as _Planner

            planner_model = getattr(spec, "planner_model", None) or spec.model
            state.planner = _Planner(self._owner.provider, planner_model)
            state.planner_task_text = self._owner._extract_task_from_messages(spec.initial_messages)
            state.planner_tools_summary = self._owner._build_tools_summary(spec.tools)
            try:
                state.plan = await state.planner.create_plan(
                    task=state.planner_task_text,
                    tools_summary=state.planner_tools_summary,
                )
                state.plan.max_replans = getattr(spec, "planner_max_replans", 3)
                logger.info(
                    "Planner produced {} steps for: {}",
                    len(state.plan.steps),
                    state.plan.goal,
                )
            except Exception:
                logger.exception("Planner.create_plan failed; falling back to ReAct-only")
                state.plan = None
                state.planner = None
        # Optional reflection.
        if getattr(spec, "enable_reflection", False):
            from miniunicorn.agent.reflection import Reflection

            state.reflection = Reflection(self._owner.provider, spec.model, spec.workspace)
        return state

    # ------------------------------------------------------------------
    # Phase: single iteration (dispatches to sub-phases)
    # ------------------------------------------------------------------

    async def _run_iteration(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        hook: AgentHook,
        iteration: int,
    ) -> IterationAction:
        """Execute one iteration of the ReAct loop."""
        messages_for_model = self._govern_messages(spec, state, iteration)
        context = AgentHookContext(iteration=iteration, messages=state.messages)
        await hook.before_iteration(context)
        observer = current_provider_attempt_observer()
        logical_call_id: str | None = None
        if observer is not None:
            logical_call_id = observer.begin_logical_call()

        response = await self._restore_or_request_model(
            spec, state, messages_for_model, hook, context, iteration, logical_call_id
        )
        # Usage + budget check.
        budget_result = self._handle_usage_and_budget(
            spec, state, response, context, hook, iteration
        )
        if budget_result is not None:
            return IterationAction.BREAK

        # Extract reasoning.
        reasoning_text, cleaned_content = extract_reasoning(
            response.reasoning_content,
            response.thinking_blocks,
            response.content,
        )
        response.content = cleaned_content
        if reasoning_text and not context.streamed_reasoning:
            await hook.emit_reasoning(reasoning_text)
            await hook.emit_reasoning_end()
            context.streamed_reasoning = True

        if response.should_execute_tools:
            return await self._execute_tools_phase(spec, state, response, context, hook, iteration)
        return await self._handle_final_response(
            spec, state, response, context, hook, iteration, messages_for_model
        )

    # ------------------------------------------------------------------
    # Phase: context governance + plan guidance
    # ------------------------------------------------------------------

    def _govern_messages(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Run context governance and inject plan step guidance."""
        try:
            from miniunicorn.agent.context_governor import GovernanceContext

            governor = self._owner._get_governor(spec)
            ctx_gov = GovernanceContext(
                spec=spec,
                tools=spec.tools,
                provider=self._owner.provider,
                iteration=iteration,
                runner=self._owner,
            )
            messages_for_model = governor.govern(state.messages, ctx_gov)
        except Exception:
            logger.exception(
                "Context governance failed on turn {} for {}; using raw messages",
                iteration,
                spec.session_key or "default",
            )
            messages_for_model = state.messages
        # Plan-and-Execute: inject current step as guidance.
        if state.plan is not None and state.plan.current_step is not None:
            from miniunicorn.agent.planner import StepStatus as _StepStatus

            step = state.plan.current_step
            step.status = _StepStatus.IN_PROGRESS
            step.iterations_used += 1
            guidance = (
                f"\n\n[Current Plan Step {step.id}/{len(state.plan.steps)}: {step.action}]\n"
                f"Done when: {step.done_criteria or 'step goal achieved'}\n"
                f"Focus on this step. Use tool_hint={step.tool_hint} if applicable."
            )
            messages_for_model = self._owner._inject_step_guidance(messages_for_model, guidance)
        return messages_for_model

    # ------------------------------------------------------------------
    # Phase: model request (with restore-point check)
    # ------------------------------------------------------------------

    async def _restore_or_request_model(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        messages_for_model: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        iteration: int,
        logical_call_id: str | None,
    ) -> LLMResponse:
        """Check restore point for a cached decision, or make a new request."""
        import hashlib
        import json

        response: LLMResponse | None = None
        if (
            logical_call_id is not None
            and logical_call_id in state.restored_models
            and spec.turn_journal is not None
        ):
            restored = state.restored_models[logical_call_id]
            current_hash = hashlib.sha256(
                json.dumps(messages_for_model, default=str, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if current_hash != restored.request_hash:
                raise RuntimeError(
                    f"MODEL_RESTORE_HASH_MISMATCH: logical_call_id={logical_call_id}"
                )
            try:
                decision = await spec.turn_journal.read_model_response(restored.response_blob_id)
                from miniunicorn.providers.base import ToolCallRequest

                tool_calls = [
                    ToolCallRequest(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        arguments=tc.get("arguments", {}),
                    )
                    for tc in decision.tool_calls
                ]
                response = LLMResponse(
                    content=decision.content,
                    tool_calls=tool_calls,
                    finish_reason=decision.finish_reason,
                    usage=decision.usage,
                    reasoning_content=decision.reasoning_content,
                )
                logger.info(
                    "Reusing restored model decision for logical_call_id={}",
                    logical_call_id,
                )
            except RuntimeError:
                raise
            except Exception:
                logger.exception(
                    "Failed to read restored model decision for {}; making new request",
                    logical_call_id,
                )
                response = None
        if response is None:
            response = await self._owner._request_model(spec, messages_for_model, hook, context)
        return response

    # ------------------------------------------------------------------
    # Phase: usage accumulation + budget check
    # ------------------------------------------------------------------

    def _handle_usage_and_budget(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        response: LLMResponse,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
    ) -> str | None:
        """Accumulate usage, check budget. Returns stop_reason if exceeded."""
        raw_usage = self._owner._usage_dict(response.usage)
        context.response = response
        context.usage = dict(raw_usage)
        context.tool_calls = list(response.tool_calls)
        self._owner._accumulate_usage(state.usage, raw_usage)
        if raw_usage:
            state.last_call_usage = dict(raw_usage)
        runtime = current_turn_runtime()
        if runtime is not None:
            runtime.usage = dict(state.usage)
            if raw_usage:
                runtime.last_call_usage = dict(raw_usage)
        budget = getattr(spec, "turn_budget", None)
        fc, sr, err = self._owner._handle_budget_exceeded(
            budget,
            raw_usage,
            spec.model,
            spec,
            state.messages,
            iteration,
            context,
            hook,
        )
        if fc is not None:
            state.final_content = fc
            state.stop_reason = sr or "budget_exceeded"
            state.error = err
            return "budget_exceeded"
        return None

    # ------------------------------------------------------------------
    # Phase: tool execution
    # ------------------------------------------------------------------

    async def _execute_tools_phase(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        response: LLMResponse,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
    ) -> IterationAction:
        """Execute tool calls, append results, drain injections."""
        context.tool_calls = list(response.tool_calls)
        if hook.wants_streaming():
            await hook.on_stream_end(context, resuming=True)

        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        state.messages.append(assistant_message)
        state.tools_used.extend(tc.name for tc in response.tool_calls)
        await self._owner._emit_checkpoint(
            spec,
            {
                "phase": "awaiting_tools",
                "iteration": iteration,
                "model": spec.model,
                "assistant_message": assistant_message,
                "completed_tool_results": [],
                "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
            },
        )
        await hook.before_execute_tools(context)
        results, new_events, fatal_error = await self._owner._execute_tools(
            spec,
            response.tool_calls,
            state.external_lookup_counts,
            state.workspace_violation_counts,
        )
        state.tool_events.extend(new_events)
        context.tool_results = list(results)
        context.tool_events = list(new_events)
        completed_tool_results: list[dict[str, Any]] = []
        for tool_call, result in zip(response.tool_calls, results):
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "content": self._owner._normalize_tool_result(
                    spec, tool_call.id, tool_call.name, result
                ),
            }
            state.messages.append(tool_message)
            completed_tool_results.append(tool_message)
        if fatal_error is not None:
            return await self._handle_tool_error(spec, state, context, hook, iteration, fatal_error)
        await self._owner._emit_checkpoint(
            spec,
            {
                "phase": "tools_completed",
                "iteration": iteration,
                "model": spec.model,
                "assistant_message": assistant_message,
                "completed_tool_results": completed_tool_results,
                "pending_tool_calls": [],
            },
        )
        state.empty_content_retries = 0
        state.length_recovery_count = 0
        drained, state.injection_cycles = await self._owner._try_drain_injections(
            spec,
            state.messages,
            None,
            state.injection_cycles,
            phase="after tool execution",
        )
        if drained:
            state.had_injections = True
        await hook.after_iteration(context)
        # Periodic reflection (fire-and-forget).
        if (
            state.reflection is not None
            and (iteration + 1) % getattr(spec, "reflection_interval", 5) == 0
        ):
            self._reflection_supervisor.create(
                state.reflection.reflect(
                    trigger="periodic",
                    iteration=iteration,
                    context_summary=f"Periodic reflection at iteration {iteration}",
                    messages=state.messages,
                    session_key=spec.session_key,
                ),
                name=f"reflection:{spec.session_key or 'default'}:{iteration}",
            )
        return IterationAction.CONTINUE

    # ------------------------------------------------------------------
    # Phase: tool error handling (split from _execute_tools_phase)
    # ------------------------------------------------------------------

    async def _handle_tool_error(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
        fatal_error: BaseException,
    ) -> IterationAction:
        """Handle fatal tool error: plan replan, reflection, injection drain."""
        if (
            state.plan is not None
            and state.planner is not None
            and state.plan.current_step is not None
        ):
            from miniunicorn.agent.planner import StepStatus as _StepStatus

            failed_step = state.plan.current_step
            failed_step.status = _StepStatus.FAILED
            failed_step.failure_reason = str(fatal_error)
            if state.plan.can_replan:
                logger.info(
                    "Step {} failed ({}); triggering replan {}/{}",
                    failed_step.id,
                    failed_step.action,
                    state.plan.replan_count + 1,
                    state.plan.max_replans,
                )
                state.plan = await state.planner.replan(
                    state.plan,
                    failed_step,
                    str(fatal_error),
                    state.planner_task_text or "",
                    state.planner_tools_summary or "",
                )
                await hook.after_iteration(context)
                return IterationAction.CONTINUE
            logger.warning(
                "Step {} failed and max_replans reached; failing turn",
                failed_step.id,
            )
            state.stop_reason = "plan_failed"
        state.error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
        state.final_content = state.error
        if state.stop_reason != "plan_failed":
            state.stop_reason = "tool_error"
        self._owner._append_final_message(state.messages, state.final_content)
        context.final_content = state.final_content
        context.error = state.error
        context.stop_reason = state.stop_reason
        if state.reflection is not None:
            await state.reflection.reflect(
                trigger=("plan_failed" if state.stop_reason == "plan_failed" else "tool_error"),
                iteration=iteration,
                context_summary=state.error,
                messages=state.messages,
                session_key=spec.session_key,
            )
        await hook.after_iteration(context)
        should_continue, state.injection_cycles = await self._owner._try_drain_injections(
            spec,
            state.messages,
            None,
            state.injection_cycles,
            phase="after tool error",
        )
        if should_continue:
            state.had_injections = True
            return IterationAction.CONTINUE
        return IterationAction.BREAK

    # ------------------------------------------------------------------
    # Phase: final response (empty retry, length recovery, final answer)
    # ------------------------------------------------------------------

    async def _handle_final_response(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        response: LLMResponse,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
        messages_for_model: list[dict[str, Any]],
    ) -> IterationAction:
        """Handle non-tool response: empty retry, length recovery, final answer."""
        if response.has_tool_calls:
            logger.warning(
                "Ignoring tool calls under finish_reason='{}' for {}",
                response.finish_reason,
                spec.session_key or "default",
            )
        clean = hook.finalize_content(context, response.content)
        # Empty content retry path.
        if response.finish_reason != "error" and is_blank_text(clean):
            action = await self._handle_empty_retry(
                spec, state, response, context, hook, iteration, messages_for_model
            )
            if action is not None:
                return action
            clean = hook.finalize_content(context, response.content)
        # Length recovery path.
        if response.finish_reason == "length" and not is_blank_text(clean):
            state.length_recovery_count += 1
            if state.length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                logger.info(
                    "Output truncated on turn {} for {} ({}/{}); continuing",
                    iteration,
                    spec.session_key or "default",
                    state.length_recovery_count,
                    _MAX_LENGTH_RECOVERIES,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)
                state.messages.append(
                    build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    )
                )
                state.messages.append(build_length_recovery_message())
                await hook.after_iteration(context)
                return IterationAction.CONTINUE
        return await self._handle_final_answer(
            spec, state, response, context, hook, iteration, clean
        )

    # ------------------------------------------------------------------
    # Phase: empty content retry (split from _handle_final_response)
    # ------------------------------------------------------------------

    async def _handle_empty_retry(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        response: LLMResponse,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
        messages_for_model: list[dict[str, Any]],
    ) -> IterationAction | None:
        """Handle empty response: retry or finalization. Returns action or None."""
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
            return IterationAction.CONTINUE
        logger.warning(
            "Empty response on turn {} for {} after {} retries; attempting finalization",
            iteration,
            spec.session_key or "default",
            state.empty_content_retries,
        )
        if hook.wants_streaming():
            await hook.on_stream_end(context, resuming=False)
        new_response = await self._owner._request_finalization_retry(spec, messages_for_model)
        retry_usage = self._owner._usage_dict(new_response.usage)
        self._owner._accumulate_usage(state.usage, retry_usage)
        budget = getattr(spec, "turn_budget", None)
        fc, sr, err = self._owner._handle_budget_exceeded(
            budget,
            retry_usage,
            spec.model,
            spec,
            state.messages,
            iteration,
            context,
            hook,
        )
        if fc is not None:
            state.final_content = fc
            state.stop_reason = sr or "budget_exceeded"
            state.error = err
            self._owner._append_final_message(state.messages, fc)
            await hook.after_iteration(context)
            return IterationAction.BREAK
        # Merge original response usage with retry usage for context.
        merged = self._owner._merge_usage(self._owner._usage_dict(response.usage), retry_usage)
        # Mutate the original response object so the caller (_handle_final_response)
        # sees the finalization retry's content, tool_calls, finish_reason, etc.
        response.content = new_response.content
        response.tool_calls = new_response.tool_calls
        response.finish_reason = new_response.finish_reason
        response.reasoning_content = new_response.reasoning_content
        response.thinking_blocks = new_response.thinking_blocks
        response.usage = merged
        context.response = response
        context.usage = dict(merged)
        context.tool_calls = list(response.tool_calls)
        if retry_usage:
            state.last_call_usage = dict(retry_usage)
        runtime = current_turn_runtime()
        if runtime is not None:
            runtime.usage = dict(state.usage)
            if retry_usage:
                runtime.last_call_usage = dict(retry_usage)
        # Finalize content for the caller.
        response.content = hook.finalize_content(context, response.content)
        return None

    # ------------------------------------------------------------------
    # Phase: final answer (split from _handle_final_response)
    # ------------------------------------------------------------------

    async def _handle_final_answer(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        response: LLMResponse,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
        clean: str,
    ) -> IterationAction:
        """Handle the final answer: injection, error, or completion."""
        assistant_message: dict[str, Any] | None = None
        if response.finish_reason != "error" and not is_blank_text(clean):
            assistant_message = build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
        should_continue, state.injection_cycles = await self._owner._try_drain_injections(
            spec,
            state.messages,
            assistant_message,
            state.injection_cycles,
            phase="after final response",
            iteration=iteration,
            allow_goal_continue=True,
        )
        if should_continue:
            state.had_injections = True
        if hook.wants_streaming():
            await hook.on_stream_end(context, resuming=should_continue)
        if should_continue:
            await hook.after_iteration(context)
            return IterationAction.CONTINUE
        if response.finish_reason == "error":
            return await self._handle_llm_error(
                spec, state, response, context, hook, iteration, clean
            )
        if is_blank_text(clean):
            state.final_content = EMPTY_FINAL_RESPONSE_MESSAGE
            state.stop_reason = "empty_final_response"
            state.error = state.final_content
            self._owner._append_final_message(state.messages, state.final_content)
            context.final_content = state.final_content
            context.error = state.error
            context.stop_reason = state.stop_reason
            await hook.after_iteration(context)
            should_continue, state.injection_cycles = await self._owner._try_drain_injections(
                spec,
                state.messages,
                None,
                state.injection_cycles,
                phase="after empty response",
            )
            if should_continue:
                state.had_injections = True
                return IterationAction.CONTINUE
            return IterationAction.BREAK
        state.messages.append(
            assistant_message
            or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
        )
        await self._owner._emit_checkpoint(
            spec,
            {
                "phase": "final_response",
                "iteration": iteration,
                "model": spec.model,
                "assistant_message": state.messages[-1],
                "completed_tool_results": [],
                "pending_tool_calls": [],
            },
        )
        # Plan-and-Execute: mark step completed.
        if state.plan is not None and state.plan.current_step is not None:
            from miniunicorn.agent.planner import StepStatus as _StepStatus

            completed_step = state.plan.current_step
            completed_step.status = _StepStatus.COMPLETED
            if state.plan.current_step is not None:
                logger.info(
                    "Step {} completed ({}); {} steps remaining",
                    completed_step.id,
                    completed_step.action,
                    len(state.plan.pending_steps),
                )
                context.final_content = clean
                context.stop_reason = state.stop_reason
                await hook.after_iteration(context)
                return IterationAction.CONTINUE
            logger.info(
                "All plan steps completed (last: {})",
                completed_step.action,
            )
        state.final_content = clean
        context.final_content = state.final_content
        context.stop_reason = state.stop_reason
        await hook.after_iteration(context)
        return IterationAction.BREAK

    # ------------------------------------------------------------------
    # Phase: LLM error (split from _handle_final_answer)
    # ------------------------------------------------------------------

    async def _handle_llm_error(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        response: LLMResponse,
        context: AgentHookContext,
        hook: AgentHook,
        iteration: int,
        clean: str,
    ) -> IterationAction:
        """Handle LLM error response: arrearage, placeholder, injection drain."""
        if LLMProvider.is_arrearage_response(response):
            state.final_content = _ARREARAGE_ERROR_MESSAGE
        else:
            state.final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
        state.stop_reason = "error"
        state.error = state.final_content
        self._owner._append_model_error_placeholder(state.messages)
        context.final_content = state.final_content
        context.error = state.error
        context.stop_reason = state.stop_reason
        if state.reflection is not None:
            await state.reflection.reflect(
                trigger="llm_error",
                iteration=iteration,
                context_summary=state.final_content or "LLM error",
                messages=state.messages,
                session_key=spec.session_key,
            )
        await hook.after_iteration(context)
        should_continue, state.injection_cycles = await self._owner._try_drain_injections(
            spec,
            state.messages,
            None,
            state.injection_cycles,
            phase="after LLM error",
        )
        if should_continue:
            state.had_injections = True
            return IterationAction.CONTINUE
        return IterationAction.BREAK

    # ------------------------------------------------------------------
    # Phase: max iterations exhausted
    # ------------------------------------------------------------------

    async def _finish_exhausted(
        self,
        spec: AgentRunSpec,
        state: RunLoopState,
        hook: AgentHook,
    ) -> None:
        """Handle max_iterations exhaustion."""
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
        self._owner._append_final_message(state.messages, state.final_content)
        if state.reflection is not None:
            await state.reflection.reflect(
                trigger="max_iterations",
                iteration=spec.max_iterations - 1,
                context_summary=f"Hit max_iterations ({spec.max_iterations})",
                messages=state.messages,
                session_key=spec.session_key,
            )
        drained, state.injection_cycles = await self._owner._try_drain_injections(
            spec,
            state.messages,
            None,
            state.injection_cycles,
            phase="after max_iterations",
        )
        if drained:
            state.had_injections = True

    # ------------------------------------------------------------------
    # Phase: build result
    # ------------------------------------------------------------------

    def _build_result(self, state: RunLoopState) -> AgentRunResult:
        """Construct the final AgentRunResult from loop state."""
        return AgentRunResult(
            final_content=state.final_content,
            messages=state.messages,
            tools_used=state.tools_used,
            usage=state.usage,
            stop_reason=state.stop_reason,
            error=state.error,
            tool_events=state.tool_events,
            had_injections=state.had_injections,
            budget_exceeded=(state.stop_reason == "budget_exceeded"),
            plan=state.plan,
            last_call_usage=state.last_call_usage,
        )
