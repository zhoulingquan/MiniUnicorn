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
from miniunicorn.agent.runner_control import RunController
from miniunicorn.agent.runner_model import ModelRequester
from miniunicorn.agent.runner_tools import ToolExecutor
from miniunicorn.agent.runner_types import (
    AgentRunResult,
    AgentRunSpec,
)
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.providers.base import LLMProvider, ToolCallRequest
from miniunicorn.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from miniunicorn.utils.helpers import (
    build_assistant_message,
    merge_message_content,
)
from miniunicorn.utils.runtime import build_goal_continue_message
from miniunicorn.utils.task_supervisor import TaskSupervisor

_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5

# Backward-compatible re-exports for tests/extensions that import these
# constants from runner.py. The authoritative copies live in runner_control.py
# (Task 12); these mirror the values so existing imports keep working.
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3


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
        # ReAct control flow collaborator (Task 12). Owns the main iteration
        # loop; receives the façade so existing delegate methods and
        # monkeypatch seams remain effective.
        self._run_controller: RunController = RunController(self, self._reflection_supervisor)

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
        """Drain supervised reflection tasks with a bounded timeout."""
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
        """Delegate to ``RunController`` (Task 12).

        The ReAct iteration loop, including setup, governance, model request,
        tool execution, finalization, and exhaustion handling, lives in
        ``runner_control.py``. This façade preserves the public ``run(spec)``
        signature and existing monkeypatch seams (delegates like
        ``_request_model``, ``_execute_tools``, ``_emit_checkpoint``, etc.).
        """
        return await self._run_controller.run(spec)

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
        """Check budget; return (fc, sr, err) or (None, None, None) to continue."""
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
        """Facade delegating to ``self._tool_executor`` (Task 11)."""
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
