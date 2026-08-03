"""Tool execution collaborator for AgentRunner (Task 11).

``ToolExecutor`` owns the tool execution pipeline previously inlined in
``AgentRunner``: batching, gateway routing, violation classification,
result normalization, result budget, and history snipping.

``AgentRunner`` constructs a ``ToolExecutor`` with
``ToolExecutor(lambda: self.provider, self._emit_checkpoint)`` and delegates
``_execute_tools`` / ``_normalize_tool_result`` / ``_apply_tool_result_budget``
/ ``_snip_history`` to it as thin façade delegates, preserving existing
monkeypatch points.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable

from loguru import logger

from miniunicorn.agent.ports import build_tool_execution_request
from miniunicorn.agent.runner_types import AgentRunSpec, ToolBatchResult
from miniunicorn.agent.telemetry import ToolCallMetric
from miniunicorn.agent.turn_runtime import current_turn_runtime
from miniunicorn.providers.base import LLMProvider, ToolCallRequest
from miniunicorn.utils.file_edit_events import (
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from miniunicorn.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    maybe_persist_tool_result,
    truncate_text,
)
from miniunicorn.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from miniunicorn.utils.runtime import (
    ensure_nonempty_tool_result,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)

_SNIP_SAFETY_BUFFER = 1024


def _append_tool_metric(
    tool_name: str,
    start: float,
    status: str,
    error: str | None = None,
) -> None:
    """Append a ``ToolCallMetric`` to the bound ``TurnRuntime`` if one exists."""

    runtime = current_turn_runtime()
    if runtime is not None:
        runtime.tool_calls.append(
            ToolCallMetric(
                name=tool_name,
                duration_ms=max(0.0, (time.monotonic() - start) * 1000),
                status=status,
                error=error,
            )
        )


class ToolExecutor:
    """Owns tool execution, violation classification, and result normalization (Task 11).

    The Provider is resolved through ``provider_getter`` for history snipping
    (token estimation). The checkpoint emitter is injected so the executor
    can emit checkpoints without holding a reference to the full Runner.
    """

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

    def __init__(
        self,
        provider_getter: Callable[[], LLMProvider],
        checkpoint_emitter: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._provider_getter = provider_getter
        self._checkpoint_emitter = checkpoint_emitter

    @property
    def provider(self) -> LLMProvider:
        return self._provider_getter()

    async def execute(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> ToolBatchResult:
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
        return ToolBatchResult(results=results, events=events, fatal_error=fatal_error)

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
        # ToolExecutionPort is mandatory (design §20, WP4 hard cutover).
        # Every tool call routes through the gateway so no Agent tool
        # bypasses approval policy, resource leases, and attempt journaling.
        if spec.tool_execution_port is None:
            raise RuntimeError("ToolExecutionPort is required")
        return await self._run_tool_via_gateway(
            spec, tool_call, tool, params, hint, workspace_violation_counts
        )

    async def _run_tool_via_gateway(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        tool: Any | None,
        params: Any,
        hint: str,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        """Execute one tool call through the durable ToolExecutionPort (design §20).

        The gateway owns approval policy, resource leases, attempt journaling,
        and result durability. This method builds the
        :class:`ToolExecutionRequest` from the prepared (normalized)
        arguments and the tool's declarative policy, delegates to the port,
        and maps the durable result back into the Runner's (result, event,
        error) contract.

        On ``SUCCEEDED`` the in-memory ``content`` echo is returned to the
        LLM. On ``WAITING_APPROVAL`` / ``FAILED`` / ``OUTCOME_UNKNOWN`` a
        conversational error string is returned so the turn recovers without
        aborting (matching the legacy direct-execution error contract).
        """
        port = spec.tool_execution_port
        assert port is not None  # guarded by caller
        task_id = self._current_task_id()
        normalized = params if isinstance(params, dict) else dict(tool_call.arguments or {})
        policy = self._effective_policy_for(tool)
        request = build_tool_execution_request(
            task_id=task_id,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            arguments=normalized,
            policy=policy,
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
        _tool_start = time.monotonic()
        try:
            result = await port.execute(request)
        except asyncio.CancelledError:
            _append_tool_metric(tool_call.name, _tool_start, "cancelled")
            raise
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
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                _append_tool_metric(tool_call.name, _tool_start, "error", type(exc).__name__)
                return handled
            if spec.fail_on_tool_error:
                _append_tool_metric(tool_call.name, _tool_start, "error", type(exc).__name__)
                return payload, event, exc
            _append_tool_metric(tool_call.name, _tool_start, "error", type(exc).__name__)
            return payload, event, None

        if result.state == "SUCCEEDED":
            content = result.content if result.content is not None else ""

            if isinstance(content, str) and content.startswith("Error"):
                if file_edit_trackers and progress_callback is not None:
                    await invoke_file_edit_progress(
                        progress_callback,
                        [
                            build_file_edit_error_event(file_edit_tracker, content)
                            for file_edit_tracker in file_edit_trackers
                        ],
                    )
                event = {
                    "name": tool_call.name,
                    "status": "error",
                    "detail": content.replace("\n", " ").strip()[:120],
                }
                handled = self._classify_violation(
                    raw_text=content,
                    soft_payload=content + hint,
                    event=event,
                    tool_call=tool_call,
                    workspace_violation_counts=workspace_violation_counts,
                )
                if handled is not None:
                    _append_tool_metric(tool_call.name, _tool_start, "error", "error_result")
                    return handled
                if spec.fail_on_tool_error:
                    _append_tool_metric(tool_call.name, _tool_start, "error", "error_result")
                    return content + hint, event, RuntimeError(content)
                _append_tool_metric(tool_call.name, _tool_start, "error", "error_result")
                return content + hint, event, None

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
            detail = str(content).replace("\n", " ").strip()
            if not detail:
                detail = "(empty)"
            elif len(detail) > 120:
                detail = detail[:120] + "..."
            _append_tool_metric(tool_call.name, _tool_start, "ok")
            return content, {"name": tool_call.name, "status": "ok", "detail": detail}, None

        # WAITING_APPROVAL / FAILED / OUTCOME_UNKNOWN / REJECTED: surface a
        # conversational error so the LLM can adapt, mirroring the legacy
        # soft-error contract. The durable fact is already committed by the
        # gateway before this point.
        err = result.error
        code = err.error_code if err else str(result.state)
        summary = err.error_summary if err else f"Tool {tool_call.name} {result.state}"
        _append_tool_metric(tool_call.name, _tool_start, "error", code)
        payload = f"Error: {code}: {summary}"
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": summary[:120],
        }
        if spec.fail_on_tool_error:
            return payload, event, RuntimeError(payload)
        return payload + hint, event, None

    def _current_task_id(self) -> str:
        """Return the durable task_id bound to the current TurnRuntime, or ''."""
        runtime = current_turn_runtime()
        if runtime is not None and runtime.task_id is not None:
            return runtime.task_id
        return ""

    @staticmethod
    def _effective_policy_for(tool: Any | None) -> Any:
        """Build an :class:`EffectiveToolPolicy` from a tool's declarative metadata.

        When ``tool`` is None (registry-level prepare failed), use the
        conservative EXTERNAL_WRITE defaults from design §20.2. Tools that
        don't expose declarative policy attributes also get the conservative
        defaults.
        """
        from miniunicorn.agent.ports import EffectiveToolPolicy

        defaults = EffectiveToolPolicy(
            effect_class="EXTERNAL_WRITE",
            risk_class="HIGH",
            idempotency_mode="NONE",
            approval_policy="POLICY",
            recovery_policy="MANUAL",
            concurrency_scope="NONE",
        )
        if tool is None:
            return defaults
        try:
            return EffectiveToolPolicy(
                effect_class=tool.effect_class,
                risk_class=tool.risk_class,
                idempotency_mode=tool.idempotency_mode,
                approval_policy=tool.approval_policy,
                recovery_policy=tool.recovery_policy,
                concurrency_scope=tool.concurrency_scope,
                progress_required=getattr(tool, "progress_required", False),
                timeout_s=getattr(tool, "timeout_s", None),
            )
        except AttributeError:
            return defaults

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
