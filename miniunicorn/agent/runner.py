"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from loguru import logger

from miniunicorn.agent.call_ledger import (
    CallLedger,
    bind_call_ledger,
    current_call_ledger,
)
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
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.provider_registry import ProviderRegistry
from miniunicorn.agent.step_acceptance import ToolObservation
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

if TYPE_CHECKING:
    from miniunicorn.agent.plan_snapshot import PlanSnapshot
    from miniunicorn.agent.progress_policy import ProgressTracker

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_NO_PROGRESS_FINAL_MESSAGE = (
    "Stopped: no detectable progress across consecutive plan step evaluations."
)

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
    # 三态高风险审批: "allow"(默认) | "deny"(静态拒绝)
    high_risk_policy: str = "allow"
    # 执行前审批回调: async def approval(info: dict) -> bool; False = 拒绝执行
    approval_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: str | None = None
    # Optional ContextGovernor override. When None, AgentRunner uses a default
    # governor that reproduces the legacy hardcoded pipeline. Typed as Any to
    # avoid a circular import with miniunicorn.agent.context_governor.
    context_governor: Any | None = None
    # P2-T4: schema-cropped tool definitions produced by governance under RED
    # pressure. When set, the model request path prefers these over the raw
    # registry definitions. Not serialized; request-local.
    effective_tool_definitions: list[dict[str, Any]] | None = None
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
    # Explicit PlanningPolicy (P1). When set, it takes priority over the
    # use_planner/planner_model/planner_max_replans legacy fields above.
    planning_policy: PlanningPolicy | None = None
    # Reflection: when enabled, produce a "lesson learned" on failure or every
    # reflection_interval iterations, appended to memory/reflections.jsonl for
    # Dream to consolidate. Default False = no reflection overhead.
    enable_reflection: bool = False
    reflection_interval: int = 5  # periodic reflection every N iterations
    # Governed user identity for this run (e.g. "user:alice"). Forwarded to
    # reflections so Dream can partition evidence by the exact identity tuple.
    user_key: str | None = None
    # P1-T7: Per-turn wall-clock limit in seconds. When set, the runner stops
    # with stop_reason="turn_timeout" once the deadline is reached.
    # None = unlimited (P0 behavior).
    max_turn_wall_time_s: float | None = None
    # T5: Enable LLM verifier fallback when step acceptance rules are inconclusive.
    enable_step_verifier: bool = False

    def __post_init__(self) -> None:
        if self.high_risk_policy not in ("allow", "deny"):
            raise ValueError(
                f"high_risk_policy must be 'allow' or 'deny', got {self.high_risk_policy!r}"
            )


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    had_injections: bool = False
    budget_exceeded: bool = False
    plan: Any | None = None  # Plan | None, populated when use_planner=True
    # Usage from the last LLM call in this run (not cumulative). Represents
    # the actual context window footprint at the end of the turn.
    last_call_usage: dict[str, int] = field(default_factory=dict)
    # W0-A1: audit surface for the structured tool evidence of this run.
    tool_observations: list[dict[str, Any]] = field(default_factory=list)


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
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    last_call_usage: dict[str, int] = field(default_factory=dict)
    # Per-turn throttle for repeated attempts against the same outside target.
    external_lookup_counts: dict[str, int] = field(default_factory=dict)
    workspace_violation_counts: dict[str, int] = field(default_factory=dict)
    # P1-T2: per-turn identifier and the latest durable plan snapshot.
    turn_id: str | None = None
    plan_snapshot: PlanSnapshot | None = None
    # P1-T4: per-turn no-progress detector (MANAGED mode only).
    progress_tracker: ProgressTracker | None = None
    # T5: per-turn escalation guard.
    escalated_this_turn: bool = False
    # T5: FAST mode stall detection - consecutive iterations without tool calls.
    consecutive_nontool_iterations: int = 0
    # W0-A1: cross-iteration evidence accumulator, filtered per step at
    # completion time. Cleared whenever the plan is replaced (step ids restart
    # at 1 after a replan, so stale observations would leak across plans).
    tool_observations: list[ToolObservation] = field(default_factory=list)


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
        self.model_request = ModelRequestExecutor(self)
        self._model_request = self.model_request  # compat alias
        self.tool_execution = ToolExecutionCoordinator(self)
        self._tool_execution = self.tool_execution  # compat alias
        # PR-5b: context governance and turn-recovery services own the
        # migrated logic; AgentRunner keeps thin delegation methods of the
        # same name.
        self.context_governance = ContextGovernanceService(self)
        self._context_governance = self.context_governance  # compat alias
        self.recovery = TurnRecoveryPolicy(self)
        self._recovery = self.recovery  # compat alias
        # PR-5c: planning and reflection service owns the plan-and-execute /
        # reflection logic (and its _reflection_tasks tracking set);
        # AgentRunner keeps thin delegation methods of the same name.
        self.planning = PlanningReflectionService(self)
        self._planning = self.planning  # compat alias

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

    def get_governor(self, spec: AgentRunSpec) -> Any:
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

    _get_governor = get_governor  # compat alias

    def build_tools_summary(self, tools: ToolRegistry) -> str:
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

    _build_tools_summary = build_tools_summary  # compat alias

    @staticmethod
    def extract_task_from_messages(messages: list[dict[str, Any]]) -> str:
        """Extract the user's task from the initial messages (last user msg)."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    for block in reversed(content):
                        if isinstance(block, dict) and block.get("type") == "text":
                            return str(block.get("text", ""))
        return "(task)"

    _extract_task_from_messages = extract_task_from_messages  # compat alias

    @staticmethod
    def inject_step_guidance(
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

    _inject_step_guidance = inject_step_guidance  # compat alias

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

    async def try_drain_injections(
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
            injections = await self.drain_injections(spec)
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
                await self.emit_checkpoint(
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

    _try_drain_injections = try_drain_injections  # compat alias

    async def drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
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

    _drain_injections = drain_injections  # compat alias

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        """Public entry point: bind a CallLedger if none is active, then delegate."""
        ledger = current_call_ledger()
        if ledger is not None:
            return await self._run_with_ledger(spec, ledger)
        ledger = CallLedger(budget=getattr(spec, "turn_budget", None))
        async with bind_call_ledger(ledger):
            return await self._run_with_ledger(spec, ledger)

    async def _run_with_ledger(self, spec: AgentRunSpec, ledger: CallLedger) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        state = _TurnState()
        # Budget from ledger if attached, else from spec (legacy fallback)
        budget = ledger.budget if ledger.budget is not None else getattr(spec, "turn_budget", None)
        state.turn_id = uuid4().hex[:12]
        # P1-T7: Per-turn wall-clock deadline.
        turn_deadline = (
            time.monotonic() + spec.max_turn_wall_time_s if spec.max_turn_wall_time_s else None
        )
        planner, plan, planner_task_text, planner_tools_summary = await self._init_planner(spec)
        if plan is not None:
            state.plan_snapshot = await self._emit_plan_snapshot(spec, plan, state.turn_id)
            from miniunicorn.agent.progress_policy import ProgressPolicy, ProgressTracker

            state.progress_tracker = ProgressTracker(ProgressPolicy())
        reflection = self._init_reflection(spec)

        for iteration in range(spec.max_iterations):
            # P1-T7: Check wall-clock deadline before each iteration.
            if turn_deadline is not None and time.monotonic() >= turn_deadline:
                state.stop_reason = "turn_timeout"
                state.final_content = "Turn timed out (wall-clock limit reached)."
                context = AgentHookContext(iteration=iteration, messages=messages)
                context.stop_reason = state.stop_reason
                context.final_content = state.final_content
                await hook.after_iteration(context)
                break

            # T5: FAST -> MANAGED escalation check (before model request).
            (
                planner,
                plan,
                planner_task_text,
                planner_tools_summary,
            ) = await self._maybe_escalate_to_managed(
                spec,
                state,
                planner,
                plan,
                planner_task_text,
                planner_tools_summary,
            )

            messages_for_model = await self._govern_messages(spec, messages, iteration)
            # Inject managed-plan guidance only after governance has repaired
            # historical messages. The returned copy is request-local and does
            # not pollute persisted conversation history.
            if plan is not None and plan.current_step is not None:
                messages_for_model = await self._apply_plan_step_guidance(
                    messages_for_model, plan, spec=spec, turn_id=state.turn_id
                )
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            # Planner, compaction, or another pre-executor call may already
            # have exhausted the turn-wide budget.
            _fc, _sr, _err = self.handle_budget_exceeded(
                budget,
                {},
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
            response = await self.request_model(spec, messages_for_model, hook, context)
            raw_usage = self.usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self.accumulate_usage(state.usage, raw_usage)
            if raw_usage:
                state.last_call_usage = dict(raw_usage)
            # The provider boundary has already recorded this response. Check
            # the ledger without accumulating the response a second time.
            _fc, _sr, _err = self.handle_budget_exceeded(
                budget,
                {},
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
                action, plan = await self._execute_tool_iteration(
                    spec,
                    state,
                    hook,
                    messages,
                    context,
                    plan,
                    planner,
                    planner_task_text,
                    planner_tools_summary,
                    reflection,
                    iteration,
                    response,
                )
                if action == "break":
                    break
                continue

            action, plan = await self._finalize_nontool_iteration(
                spec,
                state,
                hook,
                messages,
                messages_for_model,
                budget,
                context,
                plan,
                planner,
                planner_task_text,
                planner_tools_summary,
                reflection,
                iteration,
                response,
                raw_usage,
            )
            if action == "break":
                break
        else:
            await self._finalize_max_iterations(spec, state, messages, reflection)

        # P1-T6: Terminal-only reflection — fires once after the loop exits.
        await self._fire_terminal_reflection(reflection, spec, state, messages, iteration)

        # Compatibility for injected test/dummy providers that override the
        # retry boundary itself and therefore cannot populate the ledger.
        result_usage = dict(ledger.total_usage) if ledger.records else dict(state.usage)
        result_last_usage = (
            dict(ledger.last_call_usage) if ledger.records else dict(state.last_call_usage)
        )
        return AgentRunResult(
            final_content=state.final_content,
            messages=messages,
            tools_used=state.tools_used,
            usage=result_usage,
            stop_reason=state.stop_reason,
            error=state.error,
            tool_events=state.tool_events,
            had_injections=state.had_injections,
            budget_exceeded=(state.stop_reason == "budget_exceeded"),
            plan=plan,
            last_call_usage=result_last_usage,
            tool_observations=[o.to_dict() for o in state.tool_observations],
        )

    # -- run() 阶段 helper (自 run 拆出, 语义与原内联实现逐句对应) ------------

    async def _maybe_escalate_to_managed(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        planner: Any,
        plan: Any,
        planner_task_text: str | None,
        planner_tools_summary: str | None,
    ) -> tuple[Any, Any, str | None, str | None]:
        """FAST 停滞检测 → MANAGED 升级。返回 (planner, plan, task_text, tools_summary)。

        条件不满足或升级失败时原样返回入参（plan 不变即无升级）。
        """
        # Only in FAST mode (plan is None), not already escalated this turn,
        # and after at least 2 consecutive non-tool iterations (stall).
        if not (
            plan is None
            and not state.escalated_this_turn
            and spec.planning_policy is not None
            and state.consecutive_nontool_iterations >= 2
        ):
            return planner, plan, planner_task_text, planner_tools_summary
        new_mode = spec.planning_policy.escalate(
            PlanningMode.FAST,
            stall_detected=True,
            already_escalated=state.escalated_this_turn,
        )
        if new_mode is PlanningMode.MANAGED:
            try:
                # Create a plan using the existing planner infrastructure
                (
                    planner,
                    new_plan,
                    planner_task_text,
                    planner_tools_summary,
                ) = await self._init_planner(spec)
                if new_plan is not None:
                    plan = new_plan
                    state.plan_snapshot = await self._emit_plan_snapshot(
                        spec, plan, state.turn_id, stop_reason=None
                    )
                    # Update snapshot origin to "escalated"
                    if state.plan_snapshot is not None:
                        state.plan_snapshot = state.plan_snapshot.with_origin("escalated")
                    from miniunicorn.agent.progress_policy import (
                        ProgressPolicy,
                        ProgressTracker,
                    )

                    state.progress_tracker = ProgressTracker(ProgressPolicy())
                    state.escalated_this_turn = True
                    state.consecutive_nontool_iterations = 0
                    logger.info(
                        "Escalated FAST -> MANAGED for turn {}",
                        state.turn_id,
                    )
            except Exception:
                logger.warning(
                    "Escalation FAST->MANAGED failed; staying FAST",
                    exc_info=True,
                )
                # Fall through to original FAST behavior
        return planner, plan, planner_task_text, planner_tools_summary

    async def _execute_tool_iteration(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        context: AgentHookContext,
        plan: Any,
        planner: Any,
        planner_task_text: str | None,
        planner_tools_summary: str | None,
        reflection: Any,
        iteration: int,
        response: Any,
    ) -> tuple[str, Any]:
        """工具响应迭代。返回 (action, plan)：action ∈ {"continue", "break"}。"""
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
        await self.emit_checkpoint(
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

        results, new_events, fatal_error = await self.execute_tools(
            spec,
            response.tool_calls,
            state.external_lookup_counts,
            state.workspace_violation_counts,
            step_id=(
                plan.current_step.id if plan is not None and plan.current_step is not None else None
            ),
        )
        # 先摘出观察（其中会 pop 掉 receipt），再把已清干净的事件入列，
        # 保证 tool_events 任何时刻都不持有回执。
        state.tool_observations.extend(
            self.tool_execution.build_observations(
                response.tool_calls,
                results,
                new_events,
                step_id=(
                    plan.current_step.id
                    if plan is not None and plan.current_step is not None
                    else None
                ),
            )
        )
        state.tool_events.extend(new_events)
        context.tool_results = list(results)
        context.tool_events = list(new_events)
        completed_tool_results = self.build_tool_result_messages(spec, response.tool_calls, results)
        messages.extend(completed_tool_results)
        if fatal_error is not None:
            plan_before_error = plan
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
            if plan is not None and plan is not plan_before_error:
                # Successful replan returned a replacement plan.
                state.tool_observations.clear()
                state.plan_snapshot = await self._emit_plan_snapshot(spec, plan, state.turn_id)
            elif (
                plan is not None
                and plan_before_error is not None
                and state.stop_reason == "plan_failed"
            ):
                # Replans exhausted; the plan failed terminally.
                state.plan_snapshot = await self._emit_plan_snapshot(
                    spec, plan, state.turn_id, stop_reason="plan_failed"
                )
            return action, plan
        await self.emit_checkpoint(
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
        # Checkpoint 1: drain injections after tools, before the next model call.
        _drained, state.injection_cycles = await self.try_drain_injections(
            spec,
            messages,
            None,
            state.injection_cycles,
            phase="after tool execution",
        )
        if _drained:
            state.had_injections = True
        await hook.after_iteration(context)
        # T5: Reset FAST stall counter on tool execution.
        state.consecutive_nontool_iterations = 0
        # A tool batch may have activated a plan through ``activate_plan``;
        # adopt it now so the next iteration's step guidance drives it.
        from miniunicorn.agent.tools.activate_plan import take_pending_plan

        activated = take_pending_plan()
        if activated is not None:
            plan = await self._adopt_activated_plan(spec, state, plan, activated)
        return "continue", plan

    async def _finalize_nontool_iteration(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        messages_for_model: list[dict[str, Any]],
        budget: Any,
        context: AgentHookContext,
        plan: Any,
        planner: Any,
        planner_task_text: str | None,
        planner_tools_summary: str | None,
        reflection: Any,
        iteration: int,
        response: Any,
        raw_usage: dict[str, int],
    ) -> tuple[str, Any]:
        """非工具响应迭代（含终止与验收）。返回 (action, plan)。"""
        if response.has_tool_calls:
            logger.warning(
                "Ignoring tool calls under finish_reason='{}' for {}",
                response.finish_reason,
                spec.session_key or "default",
            )

        # T5: Track consecutive non-tool iterations in FAST mode for stall detection.
        if plan is None:
            state.consecutive_nontool_iterations += 1

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
            return "continue", plan
        if action == "break":
            return "break", plan

        if response.finish_reason == "length" and not is_blank_text(clean):
            if await self._handle_length_recovery(
                spec, state, context, hook, messages, response, clean, iteration
            ):
                return "continue", plan

        assistant_message: dict[str, Any] | None = None
        if response.finish_reason != "error" and not is_blank_text(clean):
            assistant_message = build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )

        # Drain mid-turn injections before stream-end notification so a
        # resumed stream is not finalized prematurely by channel clients.
        should_continue, state.injection_cycles = await self.try_drain_injections(
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
            return "continue", plan

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
            # Capture a failure lesson only when reflection is enabled.
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
                return "continue", plan
            return "break", plan
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
                return "continue", plan
            return "break", plan

        messages.append(
            assistant_message
            or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            )
        )
        await self.emit_checkpoint(
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
        # A non-tool response completes the current managed step. Continue
        # through the ordinary ReAct loop until no pending plan step remains.
        if plan is not None and plan.current_step is not None:
            if await self._complete_plan_step(
                plan,
                context,
                hook,
                clean,
                state.stop_reason,
                spec=spec,
                turn_id=state.turn_id,
                tool_observations=[
                    o for o in state.tool_observations if o.step_id == plan.current_step.id
                ],
            ):
                # P1-T4: Check no-progress after step evidence evaluation.
                if state.progress_tracker is not None:
                    verdict = self._planning.evaluate_step_progress(plan, state.progress_tracker)
                    if verdict is not None:
                        from miniunicorn.agent.progress_policy import (
                            ProgressAction,
                        )

                        if verdict.action is ProgressAction.ABORT:
                            state.stop_reason = "no_progress"
                            state.final_content = _NO_PROGRESS_FINAL_MESSAGE
                            context.stop_reason = state.stop_reason
                            context.final_content = state.final_content
                            await hook.after_iteration(context)
                            return "break", plan
                        if verdict.action is ProgressAction.REPLAN:
                            if plan.can_replan:
                                replan_result = await planner.replan(
                                    plan,
                                    plan.current_step,
                                    f"ProgressPolicy: {verdict.reason}",
                                    planner_task_text or "",
                                    planner_tools_summary or "",
                                )
                                from miniunicorn.agent.planner import (
                                    PlannerStatus as _PlannerStatus,
                                )

                                if replan_result.status is _PlannerStatus.VALID:
                                    plan = replan_result.plan
                                    state.tool_observations.clear()
                                    state.plan_snapshot = await self._emit_plan_snapshot(
                                        spec, plan, state.turn_id
                                    )
                            else:
                                state.stop_reason = "plan_failed"
                                state.final_content = (
                                    "Plan failed: replans exhausted with no progress."
                                )
                                context.stop_reason = state.stop_reason
                                context.final_content = state.final_content
                                await hook.after_iteration(context)
                                return "break", plan
                return "continue", plan
        state.final_content = clean
        context.final_content = state.final_content
        context.stop_reason = state.stop_reason
        await hook.after_iteration(context)
        return "break", plan

    async def init_planner(self, spec: AgentRunSpec) -> tuple[Any, Any, str | None, str | None]:
        """Plan-and-Execute 初始化, 返回 (planner, plan, task_text, tools_summary)。

        创建计划失败时回退 ReAct-only (planner/plan 均为 None)。Typed as Any
        to avoid importing planner at module load time (keeps runner.py
        import-light).

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.planning.init_planner(spec)

    _init_planner = init_planner  # compat alias

    def init_reflection(self, spec: AgentRunSpec) -> Any | None:
        """Optional reflection: produces "lesson learned" entries on failure or
        every reflection_interval iterations. Default False keeps the legacy
        behavior with zero reflection overhead.

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return self.planning.init_reflection(spec)

    _init_reflection = init_reflection  # compat alias

    async def govern_messages(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Context governance for this iteration; falls back to raw messages.

        Migrated to :class:`ContextGovernanceService` (PR-5b); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.context_governance.govern_messages(spec, messages, iteration)

    _govern_messages = govern_messages  # compat alias

    async def apply_plan_step_guidance(
        self,
        messages_for_model: list[dict[str, Any]],
        plan: Any,
        *,
        spec: AgentRunSpec | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mark the current plan step IN_PROGRESS and append step guidance.

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.planning.apply_plan_step_guidance(
            messages_for_model, plan, spec=spec, turn_id=turn_id
        )

    _apply_plan_step_guidance = apply_plan_step_guidance  # compat alias

    def build_tool_result_messages(
        self,
        spec: AgentRunSpec,
        tool_calls: list[Any],
        results: list[Any],
    ) -> list[dict[str, Any]]:
        """Normalize tool execution results into tool-role messages (ordered)."""
        return self.tool_execution.build_tool_result_messages(spec, tool_calls, results)

    _build_tool_result_messages = build_tool_result_messages  # compat alias

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

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.recovery.handle_fatal_tool_error(
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

    _handle_fatal_tool_error = handle_fatal_tool_error  # compat alias

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
        """after_iteration + injection drain. Returns "continue" or "break".

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.recovery.end_turn_with_drain(
            spec, state, context, hook, messages, phase=phase
        )

    _end_turn_with_drain = end_turn_with_drain  # compat alias

    def fire_periodic_reflection(
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
        self.planning.fire_periodic_reflection(reflection, spec, messages, iteration)

    _fire_periodic_reflection = fire_periodic_reflection  # compat alias

    async def fire_terminal_reflection(
        self,
        reflection: Any | None,
        spec: AgentRunSpec,
        state: _TurnState,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> None:
        """Terminal reflection — fires once after the loop exits (P1-T6).

        Thin delegation to ``PlanningReflectionService``.
        """
        await self.planning.fire_terminal_reflection(reflection, spec, state, messages, iteration)

    _fire_terminal_reflection = fire_terminal_reflection  # compat alias

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

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.recovery.retry_empty_response(
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

    _retry_empty_response = retry_empty_response  # compat alias

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
        """Truncated-output recovery. Returns True to retry the iteration.

        Migrated to :class:`TurnRecoveryPolicy` (PR-5b); this method is a
        thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.recovery.handle_length_recovery(
            spec,
            state,
            context,
            hook,
            messages,
            response,
            clean,
            iteration,
        )

    _handle_length_recovery = handle_length_recovery  # compat alias

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
        """Mark the current plan step COMPLETED.

        Returns True when pending steps remain (continue the loop); False
        when all steps are done (caller finalizes the turn).

        Migrated to :class:`PlanningReflectionService` (PR-5c); this method
        is a thin delegation keeping the AgentRunner surface unchanged.
        """
        return await self.planning.complete_plan_step(
            plan,
            context,
            hook,
            clean,
            stop_reason,
            spec=spec,
            turn_id=turn_id,
            tool_observations=tool_observations,
        )

    _complete_plan_step = complete_plan_step  # compat alias

    async def emit_plan_snapshot(
        self,
        spec: AgentRunSpec,
        plan: Any,
        turn_id: str,
        stop_reason: str | None = None,
        origin: str = "planner",
    ) -> Any:
        """Serialize *plan* and emit a ``plan_snapshot`` checkpoint payload.

        Owned by :class:`PlanningReflectionService` (P1-T2); thin delegation
        keeping the AgentRunner surface unchanged.
        """
        return await self.planning.emit_plan_snapshot(
            spec, plan, turn_id, stop_reason=stop_reason, origin=origin
        )

    _emit_plan_snapshot = emit_plan_snapshot  # compat alias

    async def _adopt_activated_plan(
        self,
        spec: AgentRunSpec,
        state: _TurnState,
        current_plan: Any,
        activated: Any,
    ) -> Any:
        """Adopt a plan activated via the ``activate_plan`` tool.

        The tool only parsed/validated the plan and stashed it on a contextvar;
        this method does the real mounting on the turn state: it guards replan
        budget, clears stale tool observations, ensures a progress tracker, and
        emits an ``origin="activated"`` snapshot. Returns the plan the turn
        should now drive.
        """
        if current_plan is not None:
            if not current_plan.can_replan:
                logger.warning(
                    "activate_plan: replan budget exhausted on the active plan; "
                    "activation rejected, keeping the existing plan"
                )
                return current_plan
            activated.replan_count = current_plan.replan_count + 1
            activated.max_replans = current_plan.max_replans
        else:
            activated.replan_count = 0

        state.tool_observations.clear()

        if state.progress_tracker is None:
            from miniunicorn.agent.progress_policy import ProgressPolicy, ProgressTracker

            state.progress_tracker = ProgressTracker(ProgressPolicy())

        state.plan_snapshot = await self._emit_plan_snapshot(
            spec, activated, state.turn_id, origin="activated"
        )
        return activated

    async def finalize_max_iterations(
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
        return await self.recovery.finalize_max_iterations(spec, state, messages, reflection)

    _finalize_max_iterations = finalize_max_iterations  # compat alias

    def build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        return self.model_request.build_request_kwargs(spec, messages, tools=tools)

    _build_request_kwargs = build_request_kwargs  # compat alias

    async def request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ):
        return await self.model_request.request_model(spec, messages, hook, context)

    _request_model = request_model  # compat alias

    async def request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        return await self.model_request.request_finalization_retry(spec, messages)

    _request_finalization_retry = request_finalization_retry  # compat alias

    @staticmethod
    def usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        return ModelRequestExecutor.usage_dict(usage)

    _usage_dict = usage_dict  # compat alias

    @staticmethod
    def accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        ModelRequestExecutor.accumulate_usage(target, addition)

    _accumulate_usage = accumulate_usage  # compat alias

    @staticmethod
    def merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        return ModelRequestExecutor.merge_usage(left, right)

    _merge_usage = merge_usage  # compat alias

    def handle_budget_exceeded(
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

        When a CallLedger is active, this method delegates to
        ledger.check_budget() which handles accumulation and checking
        idempotently. The *usage* and *model* args are ignored in that path
        to avoid double-counting (ledger already recorded the call).
        """
        ledger = current_call_ledger()
        if ledger is not None:
            # Ledger path: check_budget accumulates any pending records and checks.
            # Do NOT call budget.accumulate again — ledger already recorded.
            exceeded = ledger.check_budget(budget)
            if exceeded is None:
                return None, None, None
            logger.warning(
                "Turn budget exceeded on iter {} for {}: {}",
                iteration,
                spec.session_key or "default",
                budget.summary() if budget is not None else exceeded,
            )
            fc = (
                f"I've reached the turn's token budget ({exceeded}). "
                "Please narrow the task or raise the budget to continue."
            )
            self.append_final_message(messages, fc)
            context.final_content = fc
            context.error = fc
            context.stop_reason = "budget_exceeded"
            return fc, "budget_exceeded", fc

        # No-ledger compatibility fallback (legacy callers)
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
        self.append_final_message(messages, fc)
        context.final_content = fc
        context.error = fc
        context.stop_reason = "budget_exceeded"
        return fc, "budget_exceeded", fc

    _handle_budget_exceeded = handle_budget_exceeded  # compat alias

    async def execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        *,
        step_id: int | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]], BaseException | None]:
        return await self.tool_execution.execute_tools(
            spec,
            tool_calls,
            external_lookup_counts,
            workspace_violation_counts,
            step_id=step_id,
        )

    _execute_tools = execute_tools  # compat alias

    async def run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        *,
        step_id: int | None = None,
    ) -> tuple[Any, dict[str, Any], BaseException | None]:
        return await self.tool_execution.run_tool(
            spec,
            tool_call,
            external_lookup_counts,
            workspace_violation_counts,
            step_id=step_id,
        )

    _run_tool = run_tool  # compat alias

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

    def classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, Any],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, Any], BaseException | None] | None:
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

    _classify_violation = classify_violation  # compat alias

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    _emit_checkpoint = emit_checkpoint  # compat alias

    @staticmethod
    def append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
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

    _append_final_message = append_final_message  # compat alias

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        return self.tool_execution.normalize_tool_result(spec, tool_call_id, tool_name, result)

    _normalize_tool_result = normalize_tool_result  # compat alias

    def apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Migrated to :class:`ContextGovernanceService` (PR-5b); thin delegation."""
        return self.context_governance.apply_tool_result_budget(spec, messages)

    _apply_tool_result_budget = apply_tool_result_budget  # compat alias

    def snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Migrated to :class:`ContextGovernanceService` (PR-5b); thin delegation."""
        return self.context_governance.snip_history(spec, messages)

    _snip_history = snip_history  # compat alias

    def partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        return self.tool_execution.partition_tool_batches(spec, tool_calls)

    _partition_tool_batches = partition_tool_batches  # compat alias
