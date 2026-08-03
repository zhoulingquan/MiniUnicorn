"""Model request collaborator for AgentRunner (Task 10).

``ModelRequester`` owns the Provider-facing request/stream/retry/timeout
behavior that was previously inlined in ``AgentRunner``. It resolves the
Provider through a getter on every call so that swapping
``AgentRunner.provider`` between runs is observed by the next run.

``AgentRunner`` constructs a ``ModelRequester`` with
``ModelRequester(lambda: self.provider)`` and delegates
``_request_model`` / ``_request_finalization_retry`` to it as
one-return-statement façades, preserving existing monkeypatch points.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import Any, Callable

from miniunicorn.agent.hook import AgentHook, AgentHookContext
from miniunicorn.agent.runner_types import AgentRunSpec
from miniunicorn.agent.telemetry import LlmCallMetric
from miniunicorn.agent.turn_runtime import current_turn_runtime
from miniunicorn.providers.base import LLMProvider, LLMResponse
from miniunicorn.utils.file_edit_events import StreamingFileEditTracker
from miniunicorn.utils.helpers import IncrementalThinkExtractor, strip_think
from miniunicorn.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from miniunicorn.utils.runtime import build_finalization_retry_message


class ModelRequester:
    """Owns Provider request/stream/retry/timeout behavior (Task 10).

    The Provider is resolved through ``provider_getter`` on every call so
    that mutating ``AgentRunner.provider`` between runs takes effect on the
    next request without re-wiring the collaborator.
    """

    def __init__(self, provider_getter: Callable[[], LLMProvider]) -> None:
        self._provider_getter = provider_getter

    @property
    def provider(self) -> LLMProvider:
        return self._provider_getter()

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

    async def request(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ) -> LLMResponse:
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
        _llm_call_start = time.monotonic()
        _llm_error: str | None = None
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
            _llm_error = "timeout"
            if outer_timeout_s is None:
                response = LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            else:
                response = LLMResponse(
                    content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                    finish_reason="error",
                    error_kind="timeout",
                )
        # Record one LlmCallMetric on the bound TurnRuntime. Safe-by-default:
        # no runtime means no metric (e.g. unit tests calling _request_model
        # directly without a coordinator scope).
        _runtime = current_turn_runtime()
        if _runtime is not None:
            _runtime.llm_calls.append(
                LlmCallMetric(
                    iteration=context.iteration,
                    duration_ms=max(0.0, (time.monotonic() - _llm_call_start) * 1000),
                    usage=self._usage_dict(getattr(response, "usage", None)),
                    finish_reason=getattr(response, "finish_reason", None),
                    error=_llm_error,
                )
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    async def request_finalization(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
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
