"""Shared fixtures and helpers for agent tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.ports import ToolExecutionResult
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMProvider


class FakeToolExecutionPort:
    """Test-only port that delegates to the tool registry for direct execution.

    For real ToolRegistry instances, resolves the tool by name and calls
    ``tool.execute(**arguments)``. For MagicMock registries (common in unit
    tests), falls back to ``tools.execute(name, arguments)`` so existing
    mock-based assertions (await_count, side_effect, etc.) keep working.

    Exceptions are re-raised so the Runner's gateway exception handler can
    apply violation classification and soft-error semantics.
    """

    def __init__(self, tools):
        self._tools = tools

    async def execute(self, request):
        from miniunicorn.agent.tools.registry import ToolRegistry

        if isinstance(self._tools, ToolRegistry):
            tool = self._tools.get(request.tool_name)
            if tool is not None:
                result = await tool.execute(**request.normalized_arguments)
            else:
                result = await self._tools.execute(
                    request.tool_name, request.normalized_arguments
                )
        else:
            result = await self._tools.execute(
                request.tool_name, request.normalized_arguments
            )
        return ToolExecutionResult(state="SUCCEEDED", content=result)


def make_provider(
    default_model: str = "test-model",
    *,
    max_tokens: int = 4096,
    spec: bool = True,
) -> MagicMock:
    """Create a spec-limited LLM provider mock."""
    mock_type = MagicMock(spec=LLMProvider) if spec else MagicMock()
    provider = mock_type
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(
        max_tokens=max_tokens,
        temperature=0.1,
        reasoning_effort=None,
    )
    # ``estimate_prompt_tokens`` is a free function in ``miniunicorn.utils.helpers``
    # and is accessed via ``getattr`` on the provider; it is not part of the
    # ``LLMProvider`` spec, so do not set it on a spec-limited mock.
    return provider


def make_loop(
    tmp_path: Path,
    *,
    model: str = "test-model",
    context_window_tokens: int = 128_000,
    session_ttl_minutes: int = 0,
    max_messages: int = 120,
    unified_session: bool = False,
    mcp_servers: dict | None = None,
    tools_config=None,
    model_presets: dict | None = None,
    hooks: list | None = None,
    provider: MagicMock | None = None,
    patch_deps: bool = False,
) -> AgentLoop:
    """Create a real AgentLoop for testing.

    Args:
        patch_deps: If True, patch ContextBuilder/SessionManager/SubagentManager
                    during construction (needed when workspace has no real files).
    """
    bus = MessageBus()
    if provider is None:
        provider = make_provider(default_model=model)

    kwargs = dict(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model=model,
        context_window_tokens=context_window_tokens,
        session_ttl_minutes=session_ttl_minutes,
        max_messages=max_messages,
        unified_session=unified_session,
    )
    if mcp_servers is not None:
        kwargs["mcp_servers"] = mcp_servers
    if tools_config is not None:
        kwargs["tools_config"] = tools_config
    if model_presets is not None:
        kwargs["model_presets"] = model_presets
    if hooks is not None:
        kwargs["hooks"] = hooks

    if patch_deps:
        with (
            patch("miniunicorn.agent.loop.ContextBuilder"),
            patch("miniunicorn.agent.loop.SessionManager"),
            patch("miniunicorn.agent.loop.SubagentManager") as MockSubMgr,
        ):
            MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
            return AgentLoop(**kwargs)
    return AgentLoop(**kwargs)


@pytest.fixture
def loop_factory(tmp_path):
    """Fixture providing a factory for creating AgentLoop instances."""

    def _factory(**kwargs):
        return make_loop(tmp_path, **kwargs)

    return _factory
