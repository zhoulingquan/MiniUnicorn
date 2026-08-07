"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from miniunicorn.agent.hook import AgentHookContext
from miniunicorn.bus.agent_events import ToolProgressEvent


def on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    """Return whether ``cb`` accepts a keyword argument named ``name``.

    Single canonical helper for progress-callback signature detection.
    Preserves the original failure behavior:

    - uninspectable callables (``TypeError`` / ``ValueError`` from
      :func:`inspect.signature`) return ``False``;
    - callables declaring ``**kwargs`` accept any named parameter;
    - otherwise the parameter must appear explicitly in the signature.
    """
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return on_progress_accepts(cb, "tool_events")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return on_progress_accepts(cb, "file_edit_events")


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: Callable[..., Awaitable[None]],
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    return ToolProgressEvent(
        phase="start",
        call_id=str(getattr(tool_call, "id", "") or ""),
        name=getattr(tool_call, "name", ""),
        arguments=getattr(tool_call, "arguments", {}) or {},
    ).model_dump(mode="json")


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    embeds = result.get("embeds") if isinstance(result.get("embeds"), list) else []
    return files, embeds


def build_tool_event_finish_payloads(context: AgentHookContext) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx] if isinstance(context.tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        error: str | None = None
        if phase == "error":
            if isinstance(result, str) and result.strip():
                error = result.strip()
            else:
                error = str(event.get("detail") or "Tool execution failed")
        payload = ToolProgressEvent(
            phase=phase,
            call_id=str(getattr(tool_call, "id", "") or ""),
            name=getattr(tool_call, "name", ""),
            arguments=getattr(tool_call, "arguments", {}) or {},
            result=result if phase == "end" else None,
            error=error,
            files=files,
            embeds=embeds,
        ).model_dump(mode="json")
        payloads.append(payload)
    return payloads
