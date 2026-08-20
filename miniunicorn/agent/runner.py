"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from miniunicorn.agent.execution.context_governance import ContextGovernanceService
from miniunicorn.agent.execution.model_request import ModelRequestExecutor
from miniunicorn.agent.execution.planning import PlanningReflectionService
from miniunicorn.agent.execution.recovery import (
    _MAX_EMPTY_RETRIES,  # noqa: F401 — compat re-export for tests
    _MAX_LENGTH_RECOVERIES,  # noqa: F401 — compat re-export for tests
    _SNIP_SAFETY_BUFFER,  # noqa: F401 — compat re-export for tests
    TurnRecoveryPolicy,
)
from miniunicorn.agent.execution.tool_execution import ToolExecutionCoordinator
from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.provider_registry import ProviderRegistry
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from miniunicorn.utils.helpers import (
    build_assistant_message,
    extract_reasoning,
)
from miniunicorn.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_goal_continue_message,
    is_blank_text,
    repeated_workspace_violation_error,
)

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5

# Backward-compatible module attribute for tests/extensions that monkeypatch
# the former single-file tracker hook. Runtime uses prepare_file_edit_trackers.
prepare_file_edit_tracker = _prepare_file_edit_tracker


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: str | None = None
    # Optional ContextGovernor override. When None, AgentRunner uses a default
    # governor that reproduces the legacy hardcoded pipeline. Typed as Any to
    # avoid a circular import with miniunicorn.agent.context_governor.
    context_governor: Any | None = None
    # Optional per-turn budget; when exceeded, run() stops with
    # stop_reason="budget_exceeded". None = no budget tracking (legacy behavior).
    # Typed as Any to avoid a circular import with miniunicorn.agent.turn_budget.
    turn_budget: Any | None = None
    # Plan-and-Execute mode. When True, the runner first decomposes the task
    # into steps via a Planner LLM call, then executes each step via ReAct.
    # Failed steps trigger replan (up to planner_max_replans). Default False
    # preserves the legacy pure-ReAct behavior.
    use_planner: bool = False
    planner_model: str | None = None  # model for planning LLM calls; None = use spec.model
    planner_max_replans: int = 3
    # Reflection: when enabled, produce a "lesson learned" on failure or every
    # reflection_interval iterations, appended to memory/reflections.jsonl for
    # Dream to consolidate. Default False = no reflection overhead.
    enable_reflection: bool = False
    reflection_interval: int = 5  # periodic reflection every N iterations
    # Governed user identity for this run (e.g. "user:alice"). Forwarded to
    # reflections so Dream can partition evidence by the exact identity tuple.
    user_key: str | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    budget_exceeded: bool = False
    plan: Any | None = None  # Plan | None, populated when use_planner=True
    # Usage from the last LLM call in this run (not cumulative). Represents
    # the actual context window footprint at the end of the turn.
    last_call_usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _TurnState:
    """runner.run 主循环的可变状态。

    拆分 run() 后由各阶段方法共享; 字段与原内联局部变量一一对应。
    """

    final_content: str | None = None
    stop_reason: str = "completed"
    error: str | None = None
    had_injections: bool = False
    injection_cycles: int = 0
    empty_content_retries: int = 0
    length_recovery_count: int = 0
    tools_used: list[str] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    last_call_usage: dict[str, int] = field(default_factory=dict)
    # Per-turn throttle for repeated attempts against the same outside target.
    external_lookup_counts: dict[str, int] = field(default_factory=dict)
    workspace_violation_counts: dict[str, int] = field(default_factory=dict)


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        provider_registry: ProviderRegistry | None = None,
    ):
        self._provider_registry = provider_registry
        self._provider = provider
        # Lazily-constructed default ContextGovernor; built on first use so
        # that entry-point plugins are loaded at most once per runner.
        self._default_governor: Any | None = None
        # PR-5a: LLM request and tool-execution services own the migrated
        # logic; AgentRunner keeps thin delegation methods of the same name.
        self._model_request = ModelRequestExecutor(self)
        self._tool_execution = ToolExecutionCoordinator(self)
        # PR-5b: context governance and turn-recovery services own the
        # migrated logic; AgentRunner keeps thin delegation methods of the
        # same name.
        self._context_governance = ContextGovernanceService(self)
        self._recovery = TurnRecoveryPolicy(self)
        # PR-5c: planning and reflection service owns the plan-and-execute /
        # reflection logic (and its _reflection_tasks tracking set);
        # AgentRunner keeps thin delegation methods of the same name.
        self._planning = PlanningReflectionService(self)

    @property
    def provider(self) -> LLMProvider:
        """Current provider, reflecting the bound ``ProviderRegistry`` when set."""
        registry = self._provider_registry
        if registry is not None:
            return registry.provider
        return self._provider

    @provider.setter
    def provider(self, value: LLMProvider) -> None:
        registry = self._provider_registry
        if registry is not None:
            registry.provider = value
        else:
            self._provider = value

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
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

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
        state = _TurnState()
        # Optional per-turn budget tracking. Only enforced when the caller
        # explicitly passes a TurnBudget via spec.turn_budget; when None,
        # behavior is identical to the legacy unbounded loop.
        budget = getattr(spec, "turn_budget", None)
        # Plan-and-Execute / Reflection 由独立 helper 初始化;
        # plan 在 fatal 工具错误触发 replan 时会被重绑定。
        planner, plan, planner_task_text, planner_tools_summary = await self._init_planner(spec)
        reflection = self._init_reflection(spec)

        for iteration in range(spec.max_iterations):
            messages_for_model = await self._govern_messages(spec, messages, iteration)
            # Plan-and-Execute: inject current step as guidance before LLM call.
            # We do this AFTER governance so the governor's prior edits to
            # historical messages are preserved; only the last user message is
            # appended with step context (non-destructively) to focus the model
            # on the current step. Each step still flows through the full ReAct
            # loop (_request_model + _execute_tools), so context governor and
            # turn budget remain in effect per step.
            if plan is not None and plan.current_step is not None:
                messages_for_model = self._apply_plan_step_guidance(messages_for_model, plan)
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            response = await self._request_model(spec, messages_for_model, hook, context)
            raw_usage = self._usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self._accumulate_usage(state.usage, raw_usage)
            if raw_usage:
                state.last_call_usage = dict(raw_usage)
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
                state.final_content, state.stop_reason, state.error = _fc, _sr, _err
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
                state.tools_used.extend(tc.name for tc in response.tool_calls)
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
                    state.external_lookup_counts,
                    state.workspace_violation_counts,
                )
                state.tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results = self._build_tool_result_messages(
                    spec, response.tool_calls, results
                )
                messages.extend(completed_tool_results)
                if fatal_error is not None:
                    action, plan = await self._handle_fatal_tool_error(
                        spec,
                        state,
                        context,
                        hook,
                        messages,
                        plan=plan,
                        planner=planner,
                        planner_task_text=planner_task_text,
                        planner_tools_summary=planner_tools_summary,
                        fatal_error=fatal_error,
                        iteration=iteration,
                        reflection=reflection,
                    )
                    if action == "continue":
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
                state.empty_content_retries = 0
                state.length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, state.injection_cycles = await self._try_drain_injections(
                    spec,
                    messages,
                    None,
                    state.injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    state.had_injections = True
                await hook.after_iteration(context)
                self._fire_periodic_reflection(reflection, spec, messages, iteration)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            action, response, raw_usage, clean = await self._retry_empty_response(
                spec,
                state,
                context,
                hook,
                messages,
                messages_for_model,
                budget,
                response,
                raw_usage,
                clean,
                iteration,
            )
            if action == "continue":
                continue
            if action == "break":
                break

            if response.finish_reason == "length" and not is_blank_text(clean):
                if await self._handle_length_recovery(
                    spec, state, context, hook, messages, response, clean, iteration
                ):
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
            should_continue, state.injection_cycles = await self._try_drain_injections(
                spec,
                messages,
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
                continue

            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    state.final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    state.final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                state.stop_reason = "error"
                state.error = state.final_content
                self._append_model_error_placeholder(messages)
                context.final_content = state.final_content
                context.error = state.error
                context.stop_reason = state.stop_reason
                # Reflection: capture lesson learned on LLM error.
                if reflection is not None:
                    await reflection.reflect(
                        trigger="llm_error",
                        iteration=iteration,
                        context_summary=state.final_content or "LLM error",
                        messages=messages,
                        session_key=spec.session_key,
                        user_key=spec.user_key,
                    )
                if (
                    await self._end_turn_with_drain(
                        spec, state, context, hook, messages, phase="after LLM error"
                    )
                    == "continue"
                ):
                    continue
                break
            if is_blank_text(clean):
                state.final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                state.stop_reason = "empty_final_response"
                state.error = state.final_content
                self._append_final_message(messages, state.final_content)
                context.final_content = state.final_content
                context.error = state.error
                context.stop_reason = state.stop_reason
                if (
                    await self._end_turn_with_drain(
                        spec, state, context, hook, messages, phase="after empty response"
                    )
                    == "continue"
                ):
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
                if await self._complete_plan_step(plan, context, hook, clean, state.stop_reason):
                    continue
            state.final_content = clean
            context.final_content = state.final_content
            context.stop_reason = state.stop_reason
            await hook.after_iteration(context)
            break
        else:
            await self._finalize_max_iterations(spec, state, messages, reflection)

        return AgentRunResult(
            final_content=state.final_content,
            messages=messages,
            tools_used=state.tools_used,
            usage=state.usage,
            stop_reason=state.stop_reason,
            error=state.error,
            tool_events=state.tool_events,
            had_injections=state.had_injections,
            budget_exceeded=(state.stop_reason == "budget_exceeded"),
            plan=plan,
            last_call_usage=state.last_call_usage,
        )

    # -- run() 阶段 helper (自 run 拆出, 语义与原内联实现逐句对应) ------------

    async def _init_planner(
        self, spec: AgentRunSpec
    ) -> tuple[Any, Any, str | None, str | None]:
        """Plan-and-Execute 初始化, 返回 (planner, plan, task_text, tools_summary)。

        创建计划失败时回退 ReAct-only (planner/plan 均为 None)。Typed as Any
        to avoid importing planner at module load time (keeps runner.py
        import-light).

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._planning.init_planner(spec)

    def _init_reflection(self, spec: AgentRunSpec) -> Any | None:
        """Optional reflection: produces "lesson learned" entries on failure or
        every reflection_interval iterations. Default False keeps the legacy
        behavior with zero reflection overhead.

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return self._planning.init_reflection(spec)

    async def _govern_messages(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Context governance for this iteration; falls back to raw messages.

        Migrated to :class:`ContextGovernanceService` (PR-5b); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._context_governance.govern_messages(spec, messages, iteration)

    def _apply_plan_step_guidance(
        self, messages_for_model: list[dict[str, Any]], plan: Any
    ) -> list[dict[str, Any]]:
        """Mark the current plan step IN_PROGRESS and append step guidance.

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return self._planning.apply_plan_step_guidance(messages_for_model, plan)

    def _build_tool_result_messages(
        self,
        spec: AgentRunSpec,
        tool_calls: list[Any],
        results: list[Any],
    ) -> list[dict[str, Any]]:
        """Normalize tool execution results into tool-role messages (ordered)."""
        return self._tool_execution.build_tool_result_messages(spec, tool_calls, results)

    async def _handle_fatal_tool_error(
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

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._recovery.handle_fatal_tool_error(
            spec,
            state,
            context,
            hook,
            messages,
            plan=plan,
            planner=planner,
            planner_task_text=planner_task_text,
            planner_tools_summary=planner_tools_summary,
            fatal_error=fatal_error,
            iteration=iteration,
            reflection=reflection,
        )

    async def _end_turn_with_drain(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        context: AgentHookContext,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        *,
        phase: str,
    ) -> str:
        """after_iteration + injection drain. Returns "continue" or "break".

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._recovery.end_turn_with_drain(
            spec, state, context, hook, messages, phase=phase
        )

    def _fire_periodic_reflection(
        self,
        reflection: Any | None,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> None:
        """Periodic reflection (every reflection_interval iterations).

        Non-blocking: fire-and-forget so the main loop isn't slowed.

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        self._planning.fire_periodic_reflection(
            reflection, spec, messages, iteration
        )

    async def _retry_empty_response(
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

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._recovery.retry_empty_response(
            spec,
            state,
            context,
            hook,
            messages,
            messages_for_model,
            budget,
            response,
            raw_usage,
            clean,
            iteration,
        )

    async def _handle_length_recovery(
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
        """Truncated-output recovery. Returns True to retry the iteration.

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._recovery.handle_length_recovery(
            spec,
            state,
            context,
            hook,
            messages,
            response,
            clean,
            iteration,
        )

    async def _complete_plan_step(
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

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._planning.complete_plan_step(
            plan, context, hook, clean, stop_reason
        )

    async def _finalize_max_iterations(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        messages: list[dict[str, Any]],
        reflection: Any | None,
    ) -> None:
        """for-else branch: max_iterations exhausted.

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self._recovery.finalize_max_iterations(
            spec, state, messages, reflection
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        return self._model_request.build_request_kwargs(spec, messages, tools=tools)

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ):
        return await self._model_request.request_model(spec, messages, hook, context)

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        return await self._model_request.request_finalization_retry(spec, messages)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        return ModelRequestExecutor.usage_dict(usage)

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        ModelRequestExecutor.accumulate_usage(target, addition)

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        return ModelRequestExecutor.merge_usage(left, right)

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
        return await self._tool_execution.execute_tools(
            spec,
            tool_calls,
            external_lookup_counts,
            workspace_violation_counts,
        )

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        return await self._tool_execution.run_tool(
            spec,
            tool_call,
            external_lookup_counts,
            workspace_violation_counts,
        )

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """Classify safety-boundary failures, or return ``None`` to pass through."""
        if self._is_ssrf_violation(raw_text):
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

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
        return self._tool_execution.normalize_tool_result(
            spec, tool_call_id, tool_name, result
        )

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Migrated to :class:`ContextGovernanceService` (PR-5b); thin delegation."""
        return self._context_governance.apply_tool_result_budget(spec, messages)

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Migrated to :class:`ContextGovernanceService` (PR-5b); thin delegation."""
        return self._context_governance.snip_history(spec, messages)

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        return self._tool_execution.partition_tool_batches(spec, tool_calls)
