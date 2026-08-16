"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.utils.file_edit_events import (
    StreamingFileEditTracker,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from miniunicorn.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from miniunicorn.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    find_legal_message_start,
    maybe_persist_tool_result,
    strip_think,
    truncate_text,
)
from miniunicorn.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from miniunicorn.utils.prompt_templates import render_template
from miniunicorn.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_finalization_retry_message,
    build_goal_continue_message,
    build_length_recovery_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
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

    def __init__(self, provider: LLMProvider):
        self.provider = provider
        # Lazily-constructed default ContextGovernor; built on first use so
        # that entry-point plugins are loaded at most once per runner.
        self._default_governor: Any | None = None
        # 跟踪 reflection 后台任务，避免被 GC 回收
        self._reflection_tasks: set[asyncio.Task] = set()

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
        """
        if not getattr(spec, "use_planner", False):
            return None, None, None, None
        from miniunicorn.agent.planner import Planner as _Planner

        planner_model = getattr(spec, "planner_model", None) or spec.model
        planner = _Planner(self.provider, planner_model)
        task_text = self._extract_task_from_messages(spec.initial_messages)
        tools_summary = self._build_tools_summary(spec.tools)
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

    def _init_reflection(self, spec: AgentRunSpec) -> Any | None:
        """Optional reflection: produces "lesson learned" entries on failure or
        every reflection_interval iterations. Default False keeps the legacy
        behavior with zero reflection overhead."""
        if not getattr(spec, "enable_reflection", False):
            return None
        from miniunicorn.agent.reflection import Reflection

        return Reflection(
            self.provider,
            spec.model,
            spec.workspace,
        )

    async def _govern_messages(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Context governance for this iteration; falls back to raw messages.

        Keep the persisted conversation untouched. Context governance may
        repair or compact historical messages for the model, but those
        synthetic edits must not shift the append boundary used later when
        the caller saves only the new turn. The governor runs an ordered list
        of ContextStrategy; the default pipeline reproduces the legacy
        hardcoded steps (drop_orphan -> backfill -> microcompact -> budget ->
        snip -> drop_orphan -> backfill) and falls back to minimal repair on
        failure. Spec-provided governors override the default.
        """
        try:
            from miniunicorn.agent.context_governor import GovernanceContext

            governor = self._get_governor(spec)
            ctx_gov = GovernanceContext(
                spec=spec,
                tools=spec.tools,
                provider=self.provider,
                iteration=iteration,
                runner=self,
            )
            return governor.govern(messages, ctx_gov)
        except Exception:
            logger.exception(
                "Context governance failed on turn {} for {}; using raw messages",
                iteration,
                spec.session_key or "default",
            )
            return messages

    def _apply_plan_step_guidance(
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
        return self._inject_step_guidance(messages_for_model, guidance)

    def _build_tool_result_messages(
        self,
        spec: AgentRunSpec,
        tool_calls: list[Any],
        results: list[Any],
    ) -> list[dict[str, Any]]:
        """Normalize tool execution results into tool-role messages (ordered)."""
        tool_messages: list[dict[str, Any]] = []
        for tool_call, result in zip(tool_calls, results):
            tool_messages.append(
                {
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
            )
        return tool_messages

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

        Plan-and-Execute: mark current step failed and trigger replan with
        the failure reason. Successful steps are preserved by the planner;
        the failed step is excluded from the new plan's approach. When
        replans are exhausted we fall through to the normal failure exit.
        """
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
                # Drain tool-result messages are already appended; the loop
                # picks up the new plan's first pending step.
                await hook.after_iteration(context)
                return "continue", plan
            logger.warning(
                "Step {} failed and max_replans reached; failing turn",
                failed_step.id,
            )
            state.stop_reason = "plan_failed"
        state.error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
        state.final_content = state.error
        if state.stop_reason != "plan_failed":
            state.stop_reason = "tool_error"
        self._append_final_message(messages, state.final_content)
        context.final_content = state.final_content
        context.error = state.error
        context.stop_reason = state.stop_reason
        # Reflection: capture lesson learned on fatal tool/plan error.
        if reflection is not None:
            await reflection.reflect(
                trigger=(
                    "plan_failed" if state.stop_reason == "plan_failed" else "tool_error"
                ),
                iteration=iteration,
                context_summary=state.error,
                messages=messages,
                session_key=spec.session_key,
                user_key=spec.user_key,
            )
        action = await self._end_turn_with_drain(
            spec, state, context, hook, messages, phase="after tool error"
        )
        return action, plan

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
        """after_iteration + injection drain. Returns "continue" or "break"."""
        await hook.after_iteration(context)
        should_continue, state.injection_cycles = await self._try_drain_injections(
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

    def _fire_periodic_reflection(
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
        response = await self._request_finalization_retry(spec, messages_for_model)
        retry_usage = self._usage_dict(response.usage)
        self._accumulate_usage(state.usage, retry_usage)
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
            state.final_content, state.stop_reason, state.error = _fc, _sr, _err
            self._append_final_message(messages, state.final_content)
            await hook.after_iteration(context)
            return "break", response, raw_usage, clean
        raw_usage = self._merge_usage(raw_usage, retry_usage)
        context.response = response
        context.usage = dict(raw_usage)
        context.tool_calls = list(response.tool_calls)
        if retry_usage:
            state.last_call_usage = dict(retry_usage)
        clean = hook.finalize_content(context, response.content)
        return "proceed", response, raw_usage, clean

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

    async def _finalize_max_iterations(
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
        self._append_final_message(messages, state.final_content)
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
        drained, state.injection_cycles = await self._try_drain_injections(
            spec,
            messages,
            None,
            state.injection_cycles,
            phase="after max_iterations",
        )
        if drained:
            state.had_injections = True

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ):
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set MINIUNICORN_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("MINIUNICORN_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
        wants_streaming = hook.wants_streaming()
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(self.provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None
        live_file_edits: StreamingFileEditTracker | None = None

        if spec.progress_callback is not None and on_progress_accepts_file_edit_events(
            spec.progress_callback
        ):

            async def _emit_live_file_edits(events: list[dict[str, Any]]) -> None:
                await invoke_file_edit_progress(spec.progress_callback, events)

            live_file_edits = StreamingFileEditTracker(
                workspace=spec.workspace,
                tools=spec.tools,
                emit=_emit_live_file_edits,
            )

        async def _tool_call_delta(delta: dict[str, Any]) -> None:
            if live_file_edits is not None:
                await live_file_edits.update(delta)

        if wants_streaming:

            async def _stream(delta: str) -> None:
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                if not delta:
                    return
                context.streamed_reasoning = True
                await hook.emit_reasoning(delta)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean) :]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await spec.progress_callback(incremental)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

        # Streaming requests already have provider-level idle timeouts
        # (MINIUNICORN_STREAM_IDLE_TIMEOUT_S). Do not also apply the outer wall-clock
        # LLM timeout here, or healthy long reasoning streams can be killed just
        # because total elapsed time exceeded MINIUNICORN_LLM_TIMEOUT_S.
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        try:
            response = (
                await coro
                if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
            if live_file_edits is not None:
                await live_file_edits.flush()
                if response.should_execute_tools:
                    live_file_edits.apply_final_call_ids(response.tool_calls)
                await live_file_edits.error_unmatched(
                    response.tool_calls if response.should_execute_tools else [],
                    "Tool call did not complete.",
                )
        except asyncio.TimeoutError:
            # 超时情况下刷新 file edit trackers，标记未匹配的编辑为错误状态
            if live_file_edits is not None:
                with suppress(Exception):
                    await live_file_edits.error_unmatched([], "LLM timed out")
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        kwargs = self._build_request_kwargs(spec, retry_messages, tools=None)
        return await self.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

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
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(
                    *(
                        self._run_tool(
                            spec,
                            tool_call,
                            external_lookup_counts,
                            workspace_violation_counts,
                        )
                        for tool_call in batch
                    )
                )
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self._run_tool(
                        spec,
                        tool_call,
                        external_lookup_counts,
                        workspace_violation_counts,
                    )
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        hint = "\n\n[Analyze the error above and try a different approach.]"
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return (
                prep_error + hint,
                event,
                (RuntimeError(prep_error) if spec.fail_on_tool_error else None),
            )
        emit_file_edit_events = (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        )
        progress_callback = spec.progress_callback if emit_file_edit_events else None
        file_edit_trackers = (
            prepare_file_edit_trackers(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                tool=tool,
                workspace=spec.workspace,
                params=params if isinstance(params, dict) else None,
            )
            if progress_callback is not None
            else None
        )
        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [
                    build_file_edit_start_event(
                        file_edit_tracker,
                        params if isinstance(params, dict) else None,
                    )
                    for file_edit_tracker in file_edit_trackers
                ],
            )
        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            raise
        # 使用 Exception 而非 BaseException，避免吞掉 KeyboardInterrupt 等系统级中断
        except Exception as exc:
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, str(exc))
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            payload = f"Error: {type(exc).__name__}: {exc}"
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        if isinstance(result, str) and result.startswith("Error"):
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, result)
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            handled = self._classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [
                    build_file_edit_end_event(
                        file_edit_tracker,
                        params if isinstance(params, dict) else None,
                    )
                    for file_edit_tracker in file_edit_trackers
                ],
            )

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

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
        result = ensure_nonempty_tool_result(tool_name, result)
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            return truncate_text(content, spec.max_tool_result_chars)
        return content

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(
            getattr(self.provider, "generation", None), "max_tokens", 4096
        )
        max_output = (
            spec.max_tokens
            if isinstance(spec.max_tokens, int)
            else (provider_max_tokens if isinstance(provider_max_tokens, int) else 4096)
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        estimate, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            messages,
            spec.tools.get_definitions(),
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        fixed_tokens, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            system_messages,
            spec.tools.get_definitions(),
        )
        remaining_budget = max(0, budget - max(system_tokens, fixed_tokens))
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
