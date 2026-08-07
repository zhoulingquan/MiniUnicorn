"""Tests for the canonical progress-callback signature detection helper."""

from __future__ import annotations

from typing import Any

import pytest

# Import the agent package first to avoid a pre-existing circular import between
# ``miniunicorn.utils.progress_events`` and ``miniunicorn.agent.runner``.
import miniunicorn.agent.runner  # noqa: F401
from miniunicorn.utils.progress_events import (
    on_progress_accepts,
    on_progress_accepts_file_edit_events,
    on_progress_accepts_tool_events,
)


def _accepts_reasoning(content: str, *, reasoning: bool = False) -> None: ...
def _accepts_kwargs(content: str, **kwargs: Any) -> None: ...
def _no_kwargs(content: str, *, tool_hint: bool = False) -> None: ...
def _positional_only(content: str) -> None: ...


class TestOnProgressAccepts:
    """Canonical ``on_progress_accepts`` preserves the original failure behavior."""

    def test_explicit_named_parameter_accepted(self) -> None:
        assert on_progress_accepts(_accepts_reasoning, "reasoning") is True

    def test_missing_named_parameter_rejected(self) -> None:
        assert on_progress_accepts(_no_kwargs, "reasoning") is False

    def test_var_kwargs_accepts_any_name(self) -> None:
        assert on_progress_accepts(_accepts_kwargs, "reasoning") is True
        assert on_progress_accepts(_accepts_kwargs, "tool_events") is True
        assert on_progress_accepts(_accepts_kwargs, "anything_else") is True

    def test_positional_only_rejected(self) -> None:
        assert on_progress_accepts(_positional_only, "reasoning") is False

    def test_uninspectable_callable_returns_false(self) -> None:
        # ``inspect.signature`` raises ``ValueError`` for some builtins (e.g.
        # ``int``); the helper must return ``False`` rather than propagate.
        assert on_progress_accepts(int, "reasoning") is False

    def test_tool_events_wrapper_uses_canonical_helper(self) -> None:
        assert on_progress_accepts_tool_events(_accepts_kwargs) is True
        assert on_progress_accepts_tool_events(_no_kwargs) is False

    def test_file_edit_events_wrapper_uses_canonical_helper(self) -> None:
        assert on_progress_accepts_file_edit_events(_accepts_kwargs) is True
        assert on_progress_accepts_file_edit_events(_no_kwargs) is False


@pytest.mark.asyncio
async def test_invoke_on_progress_routes_tool_events_when_accepted() -> None:
    """``invoke_on_progress`` forwards ``tool_events`` only when the callback
    declares support for it; otherwise it falls back to the plain call."""
    from miniunicorn.utils.progress_events import invoke_on_progress

    received: list[dict[str, Any]] = []

    async def accepts_tool_events(
        content: str, *, tool_hint: bool = False, tool_events: list[dict[str, Any]] | None = None
    ) -> None:
        received.append({"content": content, "tool_events": tool_events})

    async def plain(content: str, *, tool_hint: bool = False) -> None:
        received.append({"content": content, "tool_events": "skipped"})

    events = [{"phase": "start"}]
    await invoke_on_progress(accepts_tool_events, "hint", tool_events=events)
    assert received[-1]["tool_events"] == events

    await invoke_on_progress(plain, "hint", tool_events=events)
    assert received[-1]["tool_events"] == "skipped"
