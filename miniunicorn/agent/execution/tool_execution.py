"""Tool execution coordination service split out of ``AgentRunner``.

``ToolExecutionCoordinator`` owns the tool-execution path that used to live
on ``AgentRunner``: batching tool calls, running individual tools (with
violation classification and file-edit progress events), normalizing tool
results into tool-role messages, and partitioning calls into concurrency
batches.

Policy-boundary classification (``_classify_violation`` and its helpers)
remains on the host ``AgentRunner``; this service reaches it through the
runner reference it is constructed with.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent.safety_policy import RiskLevel, SafetyPolicy
from miniunicorn.agent.step_acceptance import ToolObservation
from miniunicorn.agent.tool_checkpoint import ToolCheckpoint
from miniunicorn.providers.base import ToolCallRequest
from miniunicorn.utils.file_edit_events import (
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from miniunicorn.utils.helpers import maybe_persist_tool_result, truncate_text
from miniunicorn.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from miniunicorn.utils.runtime import (
    ensure_nonempty_tool_result,
    repeated_external_lookup_error,
)

if TYPE_CHECKING:
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec


class ToolExecutionCoordinator:
    """Coordinate tool execution for a single agent turn.

    Constructed with the host ``AgentRunner``; safety-boundary classification
    (``_classify_violation``) remains on the host and is reached through it.
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner
        self._safety = SafetyPolicy()

    def build_tool_result_messages(
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
                    "content": self.normalize_tool_result(
                        spec,
                        tool_call.id,
                        tool_call.name,
                        result,
                    ),
                }
            )
        return tool_messages

    async def execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        *,
        step_id: int | None = None,
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        batches = self.partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(
                    *(
                        self.run_tool(
                            spec,
                            tool_call,
                            external_lookup_counts,
                            workspace_violation_counts,
                            step_id=step_id,
                        )
                        for tool_call in batch
                    )
                )
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self.run_tool(
                        spec,
                        tool_call,
                        external_lookup_counts,
                        workspace_violation_counts,
                        step_id=step_id,
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

    async def run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        *,
        step_id: int | None = None,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        get_tool = getattr(spec.tools, "get", None)
        tool = get_tool(tool_call.name) if callable(get_tool) else None
        verdict = self._safety.evaluate(tool_call.name, tool)
        if verdict.risk_level is RiskLevel.HIGH:
            policy = getattr(spec, "high_risk_policy", "allow") or "allow"
            approval = getattr(spec, "approval_callback", None)
            denied_reason = None
            if policy == "deny":
                denied_reason = "high_risk_policy=deny"
            elif approval is not None:
                approved = False
                with suppress(Exception):
                    result = approval(
                        {
                            "tool_name": tool_call.name,
                            "arguments": dict(tool_call.arguments) if tool_call.arguments else {},
                            "risk_level": verdict.risk_level.value,
                            "session_key": spec.session_key,
                            "step_id": step_id,
                        }
                    )
                    # 支持同步与异步两种审批回调形态：同步回调若直接
                    # ``await`` 会 TypeError 被 suppress 吞掉而静默拒绝。
                    if inspect.isawaitable(result):
                        result = await result
                    approved = bool(result)
                if not approved:
                    denied_reason = "approval_callback denied"
            if denied_reason is not None:
                blocked = (
                    f"Error: Tool '{tool_call.name}' blocked before execution ({denied_reason}). "
                    "This is a hard policy boundary; do not retry. "
                    "Tell the user this action requires approval or a policy change."
                )
                event = {
                    "name": tool_call.name,
                    "status": "error",
                    "detail": f"blocked: {denied_reason}",
                }
                # 被拦截的高危尝试恰是最该留痕的事件：拒绝路径早返回、
                # 不经过任何执行期 checkpoint，这里补一条审计记录。
                if spec.checkpoint_callback is not None:
                    blocked_checkpoint = ToolCheckpoint(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        intent=dict(tool_call.arguments) if tool_call.arguments else {},
                        result_summary=f"blocked: {denied_reason}",
                        status="blocked",
                        duration_ms=0.0,
                        step_id=step_id,
                        risk_level=verdict.risk_level.value,
                    )
                    await self._runner.emit_checkpoint(
                        spec,
                        {
                            "phase": "tool_blocked",
                            "tool_checkpoint": blocked_checkpoint.to_dict(),
                        },
                    )
                if spec.fail_on_tool_error:
                    return blocked, event, RuntimeError(blocked)
                return blocked, event, None
            # 执行前审计 checkpoint(消费 requires_checkpoint,修复死字段)
            if spec.checkpoint_callback is not None and verdict.requires_checkpoint:
                checkpoint = ToolCheckpoint(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    intent=dict(tool_call.arguments) if tool_call.arguments else {},
                    result_summary="",
                    status="started",
                    duration_ms=0.0,
                    step_id=step_id,
                    risk_level=verdict.risk_level.value,
                )
                await self._runner.emit_checkpoint(
                    spec,
                    {"phase": "tool_started", "tool_checkpoint": checkpoint.to_dict()},
                )
            logger.warning("HIGH-risk tool '{}' in {}", tool_call.name, spec.session_key)
        start = time.perf_counter()
        result, event, error = await self._run_tool_impl(
            spec,
            tool_call,
            external_lookup_counts,
            workspace_violation_counts,
        )
        duration_ms = (time.perf_counter() - start) * 1000
        if spec.checkpoint_callback is not None:
            summary = ""
            if result is not None:
                summary = str(result).replace("\n", " ").strip()
                if len(summary) > 200:
                    summary = summary[:200]
            checkpoint = ToolCheckpoint(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                intent=dict(tool_call.arguments) if tool_call.arguments else {},
                result_summary=summary,
                status=event.get("status", "ok"),
                duration_ms=duration_ms,
                step_id=step_id,
                risk_level=verdict.risk_level.value,
            )
            await self._runner.emit_checkpoint(
                spec,
                {"phase": "tool_completed", "tool_checkpoint": checkpoint.to_dict()},
            )
        return result, event, error

    async def _run_tool_impl(
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
            handled = self._runner.classify_violation(
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
            handled = self._runner.classify_violation(
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
            handled = self._runner.classify_violation(
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

    def normalize_tool_result(
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

    def partition_tool_batches(
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

    def build_observations(
        self,
        tool_calls: list[ToolCallRequest],
        results: list[Any],
        events: list[dict[str, Any]],
        *,
        step_id: int | None = None,
    ) -> list[ToolObservation]:
        """Pair tool calls with their results/events into structured observations.

        ``execute_tools`` preserves request order (gather keeps batch order and
        batches are appended in sequence), so index-wise pairing is safe.
        """
        paired = min(len(tool_calls), len(results), len(events))
        if paired != len(tool_calls):
            logger.warning(
                "tool observation length mismatch: calls={} results={} events={}",
                len(tool_calls),
                len(results),
                len(events),
            )

        observations: list[ToolObservation] = []
        for tool_call, result, event in zip(tool_calls[:paired], results[:paired], events[:paired]):
            excerpt = ""
            if result is not None:
                excerpt = str(result).replace("\n", " ").strip()
                if len(excerpt) > 200:
                    excerpt = excerpt[:200]
            observations.append(
                ToolObservation(
                    tool_name=tool_call.name,
                    arguments=copy.deepcopy(tool_call.arguments) if tool_call.arguments else {},
                    status=event.get("status", "ok"),
                    result_excerpt=excerpt,
                    step_id=step_id,
                )
            )
        return observations
