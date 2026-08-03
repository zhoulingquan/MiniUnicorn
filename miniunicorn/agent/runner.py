"""Shared execution loop for tool-using agents.

Public symbols ``AgentRunSpec`` and ``AgentRunResult`` are defined in
``runner_types`` and re-exported here so existing imports from
``miniunicorn.agent.runner`` continue to work. ``__all__`` declares the
stable public surface (Task 10).
"""

from __future__ import annotations

import inspect
from typing import Any

from loguru import logger

from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.ports import (
    _provider_attempt_observer,
    current_provider_attempt_observer,
)
from miniunicorn.agent.runner_model import ModelRequester
from miniunicorn.agent.runner_tools import ToolExecutor
from miniunicorn.agent.runner_types import (
    _DEFAULT_ERROR_MESSAGE,
    AgentRunResult,
    AgentRunSpec,
)
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.agent.turn_runtime import current_turn_runtime
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from miniunicorn.utils.helpers import (
    build_assistant_message,
    extract_reasoning,
    merge_message_content,
)
from miniunicorn.utils.prompt_templates import render_template
from miniunicorn.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_goal_continue_message,
    build_length_recovery_message,
    is_blank_text,
)
from miniunicorn.utils.task_supervisor import TaskSupervisor

_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_SNIP_SAFETY_BUFFER = 1024


# Backward-compatible module attribute for tests/extensions that monkeypatch
# the former single-file tracker hook. Runtime uses prepare_file_edit_trackers.
prepare_file_edit_tracker = _prepare_file_edit_tracker


__all__ = ["AgentRunner", "AgentRunResult", "AgentRunSpec"]


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider
        # Model request collaborator (Task 10). Resolves the Provider through
        # a getter on every call so swapping ``self.provider`` between runs
        # is observed by the next request.
        self._model_requester: ModelRequester = ModelRequester(lambda: self.provider)
        # Tool execution collaborator (Task 11). Owns batching, gateway
        # routing, violation classification, normalization, result budget,
        # and history snipping. The Provider getter is shared with
        # ``ModelRequester`` so token estimation sees the live Provider.
        self._tool_executor: ToolExecutor = ToolExecutor(
            lambda: self.provider, self._emit_checkpoint
        )
        # Lazily-constructed default ContextGovernor; built on first use so
        # that entry-point plugins are loaded at most once per runner.
        self._default_governor: Any | None = None
        # Supervised fire-and-forget reflection tasks. The supervisor owns
        # strong references and surfaces unhandled exceptions via a
        # done-callback so a failed reflection never disappears silently.
        self._reflection_supervisor: TaskSupervisor = TaskSupervisor()

    def _get_governor(self, spec: AgentRunSpec) -> Any:
        """Resolve the context governor: spec-provided override or default.

        Returns the spec-level ``context_governor`` when set, otherwise a
        lazily-built default ``ContextGovernor`` whose pipeline reproduces
        the legacy hardcoded governance steps.
        """
        governor = getattr(spec, "context_governor", None)
        if governor is not None:
            return governor
        if self._default_governor is None:
            from miniunicorn.agent.context_governor import ContextGovernor

            self._default_governor = ContextGovernor()
        return self._default_governor

    async def aclose(self) -> None:
        """Drain supervised reflection tasks with a bounded timeout.

        Called by :meth:`AgentLoop.close_mcp` during shutdown so pending
        reflections get a chance to flush to ``reflections.jsonl`` before
        the process exits. Stuck reflections are force-cancelled after
        ``timeout_s=10``.
        """

        await self._reflection_supervisor.close(cancel=False, timeout_s=10)

    def _build_tools_summary(self, tools: ToolRegistry) -> str:
        """Build a compact summary of available tools for the planner."""
        lines: list[str] = []
        for schema in tools.get_definitions():
            fn = schema.get("function", schema)
            if not isinstance(fn, dict):
                fn = schema
            name = fn.get("name", "")
            desc = fn.get("description", "")
            if not isinstance(desc, str):
                desc = str(desc)
            desc = desc.split("\n")[0][:100] if desc else ""
            lines.append(f"- {name}: {desc}".rstrip())
        return "\n".join(lines) if lines else "(no tools)"

    @staticmethod
    def _extract_task_from_messages(messages: list[dict[str, Any]]) -> str:
        """Extract the user's task from the initial messages (last user msg)."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content[:500]
                if isinstance(content, list):
                    for block in reversed(content):
                        if isinstance(block, dict) and block.get("type") == "text":
                            return str(block.get("text", ""))[:500]
        return "(task)"

    @staticmethod
    def _inject_step_guidance(
        messages: list[dict[str, Any]],
        guidance: str,
    ) -> list[dict[str, Any]]:
        """Append step guidance to the last user message (non-destructive copy).

        Returns a new list; the input list and its dicts are not mutated. The
        guidance is appended to the last user message's content so the model
        sees it as additional context without polluting the persisted history
        (the caller passes the returned list only to the LLM, not to messages).
        """
        if not messages:
            return messages
        updated = [dict(m) for m in messages]
        for i in range(len(updated) - 1, -1, -1):
            if updated[i].get("role") == "user":
                content = updated[i].get("content")
                if isinstance(content, str):
                    updated[i] = {**updated[i], "content": content + guidance}
                elif isinstance(content, list):
                    new_content = list(content) + [{"type": "text", "text": guidance}]
                    updated[i] = {**updated[i], "content": new_content}
                break
        return updated

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        return merge_message_content(left, right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if messages and injection.get("role") == "user" and messages[-1].get("role") == "user":
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_goal_continue and assistant_message is not None:
            predicate = spec.goal_active_predicate
            if predicate is not None and predicate():
                injections = [build_goal_continue_message(spec.goal_continue_message)]
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected {} follow-up message(s) {} ({}/{})",
                len(injections),
                phase,
                injection_cycles,
                _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected sustained-goal continuation {}", phase)
        return True, injection_cycles

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = "limit" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                injected_messages.append(item)
                continue
            text = getattr(item, "content", str(item))
            if text.strip():
                injected_messages.append({"role": "user", "content": text})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages),
                _MAX_INJECTIONS_PER_TURN,
                dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        last_call_usage: dict[str, int] = {}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        # Provider attempt observer is bound via ContextVar for the duration
        # of this run (design §19). The runtime supplies the observer through
        # spec.provider_attempt_observer; the Provider reads it via
        # current_provider_attempt_observer() so concurrent turns never share
        # mutable Provider state.
        _observer_token = _provider_attempt_observer.set(spec.provider_attempt_observer)
        # Task 5 Step 5: load the durable restore point before the Agent
        # loop so completed model decisions can be reused without another
        # network request after a crash/reclaim (design §17.4, §19).
        restored_models: dict[str, Any] = {}
        if spec.turn_journal is not None:
            _runtime_for_restore = current_turn_runtime()
            if _runtime_for_restore is not None and _runtime_for_restore.task_id:
                from miniunicorn.agent.ports import TaskIdentity

                _task_identity = TaskIdentity(
                    task_id=_runtime_for_restore.task_id,
                    turn_id=_runtime_for_restore.turn_id,
                    session_key=_runtime_for_restore.session_key,
                    session_sequence=_runtime_for_restore.session_sequence,
                    run_segment=_runtime_for_restore.run_segment,
                    lease_epoch=_runtime_for_restore.lease_epoch,
                )
                try:
                    _restore_point = await spec.turn_journal.load_restore_point(_task_identity)
                except Exception:
                    logger.exception("load_restore_point failed; starting fresh")
                    _restore_point = None
                if _restore_point is not None:
                    for _m in _restore_point.completed_models:
                        restored_models[_m.logical_call_id] = _m
                    logger.info(
                        "Loaded restore point: {} completed model decisions",
                        len(restored_models),
                    )
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0
        # Optional per-turn budget tracking. Only enforced when the caller
        # explicitly passes a TurnBudget via spec.turn_budget; when None,
        # behavior is identical to the legacy unbounded loop.
        budget = getattr(spec, "turn_budget", None)
        # Plan-and-Execute mode: when spec.use_planner is True, the runner
        # first decomposes the task into ordered steps via a Planner LLM call,
        # then drives each step through the existing ReAct loop below. When
        # False (default) the plan stays None and the legacy ReAct-only loop
        # runs unchanged. Typed as Any to avoid importing planner at module
        # load time (keeps runner.py import-light).
        use_planner = getattr(spec, "use_planner", False)
        plan: Any | None = None
        planner: Any | None = None
        planner_task_text: str | None = None
        planner_tools_summary: str | None = None

        if use_planner:
            from miniunicorn.agent.planner import Planner as _Planner

            planner_model = getattr(spec, "planner_model", None) or spec.model
            planner = _Planner(self.provider, planner_model)
            planner_task_text = self._extract_task_from_messages(spec.initial_messages)
            planner_tools_summary = self._build_tools_summary(spec.tools)
            try:
                plan = await planner.create_plan(
                    task=planner_task_text,
                    tools_summary=planner_tools_summary,
                )
                plan.max_replans = getattr(spec, "planner_max_replans", 3)
                logger.info(
                    "Planner produced {} steps for: {}",
                    len(plan.steps),
                    plan.goal,
                )
            except Exception:
                logger.exception("Planner.create_plan failed; falling back to ReAct-only")
                plan = None
                planner = None

        # Optional reflection: produces "lesson learned" entries on failure or
        # every reflection_interval iterations. Default False keeps the legacy
        # behavior with zero reflection overhead.
        enable_reflection = getattr(spec, "enable_reflection", False)
        reflection: Any | None = None
        if enable_reflection:
            from miniunicorn.agent.reflection import Reflection

            reflection = Reflection(self.provider, spec.model, spec.workspace)

        for iteration in range(spec.max_iterations):
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                # The governor runs an ordered list of ContextStrategy; the
                # default pipeline reproduces the legacy hardcoded steps
                # (drop_orphan -> backfill -> microcompact -> budget -> snip
                # -> drop_orphan -> backfill) and falls back to minimal repair
                # on failure. Spec-provided governors override the default.
                from miniunicorn.agent.context_governor import GovernanceContext

                governor = self._get_governor(spec)
                ctx_gov = GovernanceContext(
                    spec=spec,
                    tools=spec.tools,
                    provider=self.provider,
                    iteration=iteration,
                    runner=self,
                )
                messages_for_model = governor.govern(messages, ctx_gov)
            except Exception:
                logger.exception(
                    "Context governance failed on turn {} for {}; using raw messages",
                    iteration,
                    spec.session_key or "default",
                )
                messages_for_model = messages
            # Plan-and-Execute: inject current step as guidance before LLM call.
            # We do this AFTER governance so the governor's prior edits to
            # historical messages are preserved; only the last user message is
            # appended with step context (non-destructively) to focus the model
            # on the current step. Each step still flows through the full ReAct
            # loop (_request_model + _execute_tools), so context governor and
            # turn budget remain in effect per step.
            if plan is not None and plan.current_step is not None:
                from miniunicorn.agent.planner import StepStatus as _StepStatus

                step = plan.current_step
                step.status = _StepStatus.IN_PROGRESS
                step.iterations_used += 1
                guidance = (
                    f"\n\n[Current Plan Step {step.id}/{len(plan.steps)}: {step.action}]\n"
                    f"Done when: {step.done_criteria or 'step goal achieved'}\n"
                    f"Focus on this step. Use tool_hint={step.tool_hint} if applicable."
                )
                messages_for_model = self._inject_step_guidance(messages_for_model, guidance)
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            _observer = current_provider_attempt_observer()
            _logical_call_id: str | None = None
            if _observer is not None:
                _logical_call_id = _observer.begin_logical_call()
            # Task 5 Step 6: check the restore point for a previously
            # completed model decision. If the logical_call_id matches a
            # completed attempt whose request_hash also matches, decode the
            # response blob into an LLMResponse and continue without network
            # I/O. If request_hash differs, fail closed (design §19).
            response: LLMResponse | None = None
            if (
                _logical_call_id is not None
                and _logical_call_id in restored_models
                and spec.turn_journal is not None
            ):
                _restored = restored_models[_logical_call_id]
                # Compute request hash from current messages using the same
                # formula as Provider._invoke_with_observer so the
                # comparison is exact.
                import hashlib as _hashlib
                import json as _json

                _current_hash = _hashlib.sha256(
                    _json.dumps(messages_for_model, default=str, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                if _current_hash != _restored.request_hash:
                    raise RuntimeError(
                        f"MODEL_RESTORE_HASH_MISMATCH: logical_call_id={_logical_call_id}"
                    )
                try:
                    _decision = await spec.turn_journal.read_model_response(
                        _restored.response_blob_id
                    )
                    # Reconstruct the LLMResponse from the stored decision.
                    from miniunicorn.providers.base import ToolCallRequest

                    _tool_calls = [
                        ToolCallRequest(
                            id=tc.get("id", ""),
                            name=tc.get("name", ""),
                            arguments=tc.get("arguments", {}),
                        )
                        for tc in _decision.tool_calls
                    ]
                    response = LLMResponse(
                        content=_decision.content,
                        tool_calls=_tool_calls,
                        finish_reason=_decision.finish_reason,
                        usage=_decision.usage,
                        reasoning_content=_decision.reasoning_content,
                    )
                    logger.info(
                        "Reusing restored model decision for logical_call_id={}",
                        _logical_call_id,
                    )
                except RuntimeError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to read restored model decision for {}; making new request",
                        _logical_call_id,
                    )
                    response = None
            if response is None:
                response = await self._request_model(spec, messages_for_model, hook, context)
            raw_usage = self._usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self._accumulate_usage(usage, raw_usage)
            if raw_usage:
                last_call_usage = dict(raw_usage)
            # Mirror cumulative usage into the bound TurnRuntime so
            # self-inspection and turn-end reads during a running turn
            # see this turn's own values, not a shared loop-global field.
            _runtime = current_turn_runtime()
            if _runtime is not None:
                _runtime.usage = dict(usage)
                if raw_usage:
                    _runtime.last_call_usage = dict(raw_usage)
            # Budget check: stop early if cumulative usage exceeds limits.
            _fc, _sr, _err = self._handle_budget_exceeded(
                budget,
                raw_usage,
                spec.model,
                spec,
                messages,
                iteration,
                context,
                hook,
            )
            if _fc is not None:
                final_content, stop_reason, error = _fc, _sr, _err
                await hook.after_iteration(context)
                break

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
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                tools_used.extend(tc.name for tc in response.tool_calls)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [
                            tc.to_openai_tool_call() for tc in response.tool_calls
                        ],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                )
                tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:
                    # Plan-and-Execute: mark current step failed and trigger
                    # replan with the failure reason. Successful steps are
                    # preserved by the planner; the failed step is excluded
                    # from the new plan's approach. When replans are exhausted
                    # we fall through to the normal break below.
                    if plan is not None and planner is not None and plan.current_step is not None:
                        from miniunicorn.agent.planner import StepStatus as _StepStatus

                        failed_step = plan.current_step
                        failed_step.status = _StepStatus.FAILED
                        failed_step.failure_reason = str(fatal_error)
                        if plan.can_replan:
                            logger.info(
                                "Step {} failed ({}); triggering replan {}/{}",
                                failed_step.id,
                                failed_step.action,
                                plan.replan_count + 1,
                                plan.max_replans,
                            )
                            plan = await planner.replan(
                                plan,
                                failed_step,
                                str(fatal_error),
                                planner_task_text or "",
                                planner_tools_summary or "",
                            )
                            # Drain tool-result messages are already appended;
                            # fall through to continue the loop, which picks up
                            # the new plan's first pending step.
                            await hook.after_iteration(context)
                            continue
                        logger.warning(
                            "Step {} failed and max_replans reached; failing turn",
                            failed_step.id,
                        )
                        stop_reason = "plan_failed"
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    if stop_reason != "plan_failed":
                        stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    # Reflection: capture lesson learned on fatal tool/plan error.
                    if reflection is not None:
                        await reflection.reflect(
                            trigger=(
                                "plan_failed" if stop_reason == "plan_failed" else "tool_error"
                            ),
                            iteration=iteration,
                            context_summary=error,
                            messages=messages,
                            session_key=spec.session_key,
                        )
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec,
                        messages,
                        None,
                        injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
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
                empty_content_retries = 0
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    None,
                    injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                # Periodic reflection (every reflection_interval iterations).
                # Non-blocking: fire-and-forget so the main loop isn't slowed.
                if (
                    reflection is not None
                    and (iteration + 1) % getattr(spec, "reflection_interval", 5) == 0
                ):
                    # Supervised fire-and-forget: the supervisor owns the
                    # strong reference and logs any unhandled exception.
                    self._reflection_supervisor.create(
                        reflection.reflect(
                            trigger="periodic",
                            iteration=iteration,
                            context_summary=f"Periodic reflection at iteration {iteration}",
                            messages=messages,
                            session_key=spec.session_key,
                        ),
                        name=f"reflection:{spec.session_key or 'default'}:{iteration}",
                    )
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if response.finish_reason != "error" and is_blank_text(clean):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                response = await self._request_finalization_retry(spec, messages_for_model)
                retry_usage = self._usage_dict(response.usage)
                self._accumulate_usage(usage, retry_usage)
                # Budget check: stop early if cumulative usage exceeds limits.
                _fc, _sr, _err = self._handle_budget_exceeded(
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
                    final_content, stop_reason, error = _fc, _sr, _err
                    self._append_final_message(messages, final_content)
                    await hook.after_iteration(context)
                    break
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                if retry_usage:
                    last_call_usage = dict(retry_usage)
                # Mirror updated usage into the bound TurnRuntime (retry path).
                _runtime = current_turn_runtime()
                if _runtime is not None:
                    _runtime.usage = dict(usage)
                    if retry_usage:
                        _runtime.last_call_usage = dict(retry_usage)
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
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
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                assistant_message,
                injection_cycles,
                phase="after final response",
                iteration=iteration,
                allow_goal_continue=True,
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                # Reflection: capture lesson learned on LLM error.
                if reflection is not None:
                    await reflection.reflect(
                        trigger="llm_error",
                        iteration=iteration,
                        context_summary=final_content or "LLM error",
                        messages=messages,
                        session_key=spec.session_key,
                    )
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    None,
                    injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    None,
                    injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(
                assistant_message
                or build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
            )
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            # Plan-and-Execute: the LLM produced a non-tool response, which
            # we interpret as "current step done". Mark it COMPLETED. If more
            # pending steps remain, continue to the next step (the response
            # stays in messages as a step result). Only when all steps are
            # done (or no plan) do we set final_content and break — the last
            # step's response becomes the turn's final_content.
            if plan is not None and plan.current_step is not None:
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
                    continue
                logger.info(
                    "All plan steps completed (last: {})",
                    completed_step.action,
                )
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            if spec.max_iterations_message:
                final_content = spec.max_iterations_message.format(
                    max_iterations=spec.max_iterations,
                )
            else:
                final_content = render_template(
                    "agent/max_iterations_message.md",
                    strip=True,
                    max_iterations=spec.max_iterations,
                )
            self._append_final_message(messages, final_content)
            # Reflection: capture lesson learned on max_iterations exhaustion.
            if reflection is not None:
                await reflection.reflect(
                    trigger="max_iterations",
                    iteration=spec.max_iterations - 1,
                    context_summary=f"Hit max_iterations ({spec.max_iterations})",
                    messages=messages,
                    session_key=spec.session_key,
                )
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We ignore should_continue here because the for-loop has already
            # exhausted all iterations.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec,
                messages,
                None,
                injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True

        _provider_attempt_observer.reset(_observer_token)
        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            budget_exceeded=(stop_reason == "budget_exceeded"),
            plan=plan,
            last_call_usage=last_call_usage,
        )

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ):
        return await self._model_requester.request(spec, messages, hook, context)

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        return await self._model_requester.request_finalization(spec, messages)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        return ModelRequester._usage_dict(usage)

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        ModelRequester._accumulate_usage(target, addition)

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        return ModelRequester._merge_usage(left, right)

    def _handle_budget_exceeded(
        self,
        budget: Any,
        usage: dict[str, int],
        model: str,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
        context: AgentHookContext,
        hook: AgentHook,
    ) -> tuple[str | None, str | None, str | None]:
        """Check budget; if exceeded, set up final_content/stop_reason/error.

        Returns (final_content, stop_reason, error) or (None, None, None) to
        continue. Caller is responsible for breaking out of the loop if
        non-None. When *budget* is None (legacy callers), returns all-None
        without any work, preserving the original unbounded behavior.
        """
        if budget is None:
            return None, None, None
        budget.accumulate(usage, model)
        exceeded = budget.check()
        if exceeded is None:
            return None, None, None
        logger.warning(
            "Turn budget exceeded on iter {} for {}: {}",
            iteration,
            spec.session_key or "default",
            budget.summary(),
        )
        fc = (
            f"I've reached the turn's token budget ({exceeded}). "
            "Please narrow the task or raise the budget to continue."
        )
        self._append_final_message(messages, fc)
        context.final_content = fc
        context.error = fc
        context.stop_reason = "budget_exceeded"
        return fc, "budget_exceeded", fc

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        """Thin facade delegating to ``self._tool_executor`` (Task 11).

        Returns the legacy ``(results, events, fatal_error)`` tuple so
        existing callers and monkeypatch points continue to work. The
        ToolExecutor owns batching, gateway routing, violation
        classification, normalization, result budget, and history snipping.
        """
        batch = await self._tool_executor.execute(
            spec,
            tool_calls,
            external_lookup_counts,
            workspace_violation_counts,
        )
        return batch.results, batch.events, batch.fatal_error

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        """Classify SSRF rejections (delegates to ``ToolExecutor``)."""
        return ToolExecutor._is_ssrf_violation(text)

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        """Thin facade delegating to ``self._tool_executor`` (Task 11)."""
        return self._tool_executor._normalize_tool_result(spec, tool_call_id, tool_name, result)

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Thin facade delegating to ``self._tool_executor`` (Task 11)."""
        return self._tool_executor._apply_tool_result_budget(spec, messages)

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Thin facade delegating to ``self._tool_executor`` (Task 11)."""
        return self._tool_executor._snip_history(spec, messages)

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        """Thin facade delegating to ``self._tool_executor`` (Task 11)."""
        return self._tool_executor._partition_tool_batches(spec, tool_calls)
