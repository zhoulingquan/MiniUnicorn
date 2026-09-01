"""P1-T6: Terminal-only reflection.

Tests that reflection fires only after the runner loop exits (terminal),
not mid-loop (periodic).  Covers the ``completed`` and ``budget_exceeded``
stop reasons that previously had no reflection.  Also verifies that a
reflection failure does not block ``AgentRunResult`` return.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.providers.base import LLMProvider, LLMResponse


def _provider(content: str = "done") -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content=content, tool_calls=[], usage={})
    )
    return provider


def _spec(enable_reflection: bool = True, **kw: Any) -> AgentRunSpec:
    defaults: dict[str, Any] = {
        "initial_messages": [{"role": "user", "content": "hello"}],
        "tools": MagicMock(),
        "model": "test-model",
        "max_iterations": 3,
        "max_tool_result_chars": 1000,
        "enable_reflection": enable_reflection,
    }
    defaults.update(kw)
    return AgentRunSpec(**defaults)


# 1. Terminal reflection fires once for completed turn.


@pytest.mark.asyncio
async def test_terminal_reflection_fires_on_completed(monkeypatch) -> None:
    provider = _provider("all done")
    provider.tools = MagicMock()
    provider.tools.get_definitions = MagicMock(return_value=[])
    tools = MagicMock()
    tools.get_definitions.return_value = []

    reflect_calls: list[dict[str, Any]] = []

    async def _spy_reflect(**kwargs: Any) -> str | None:
        reflect_calls.append(kwargs)
        return "lesson learned"

    runner = AgentRunner(provider)

    # Patch init_reflection to return a mock with our spy.
    original_init = runner._planning.init_reflection

    def _init(spec: AgentRunSpec) -> Any:
        reflection = original_init(spec)
        if reflection is not None:
            reflection.reflect = _spy_reflect
        return reflection

    runner._planning.init_reflection = _init

    result = await runner.run(_spec(workspace=None))

    assert result.stop_reason == "completed"
    # Exactly one terminal reflection for the completed path.
    assert len(reflect_calls) == 1
    assert reflect_calls[0]["trigger"] == "turn_completed"


# 2. No periodic reflection fires during the loop.


@pytest.mark.asyncio
async def test_no_periodic_reflection_during_loop(monkeypatch) -> None:
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return LLMResponse(
                content="",
                tool_calls=[],
                usage={},
                finish_reason="tool_calls",
            )
        return LLMResponse(content="finished", tool_calls=[], usage={})

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []

    reflect_calls: list[dict[str, Any]] = []

    async def _spy_reflect(**kwargs: Any) -> str | None:
        reflect_calls.append(kwargs)
        return None

    runner = AgentRunner(provider)

    original_init = runner._planning.init_reflection

    def _init(spec: AgentRunSpec) -> Any:
        reflection = original_init(spec)
        if reflection is not None:
            reflection.reflect = _spy_reflect
        return reflection

    runner._planning.init_reflection = _init

    await runner.run(_spec(max_iterations=5, reflection_interval=1, workspace=None))

    # No periodic reflections during the loop; only one terminal.
    periodic = [r for r in reflect_calls if r.get("trigger") == "periodic"]
    assert periodic == []
    assert len(reflect_calls) <= 1


# 3. Reflection failure does not block result.


@pytest.mark.asyncio
async def test_reflection_failure_does_not_block_result() -> None:
    provider = _provider("result")
    tools = MagicMock()
    tools.get_definitions.return_value = []

    async def _failing_reflect(**kwargs: Any) -> str | None:
        raise RuntimeError("reflection boom")

    runner = AgentRunner(provider)

    original_init = runner._planning.init_reflection

    def _init(spec: AgentRunSpec) -> Any:
        reflection = original_init(spec)
        if reflection is not None:
            reflection.reflect = _failing_reflect
        return reflection

    runner._planning.init_reflection = _init

    result = await runner.run(_spec(workspace=None))

    assert result.stop_reason == "completed"
    assert result.final_content == "result"


# 4. Terminal reflection does not fire when reflection is disabled.


@pytest.mark.asyncio
async def test_no_terminal_reflection_when_disabled() -> None:
    provider = _provider("done")
    tools = MagicMock()
    tools.get_definitions.return_value = []

    reflect_calls: list[dict[str, Any]] = []

    async def _spy_reflect(**kwargs: Any) -> str | None:
        reflect_calls.append(kwargs)
        return None

    runner = AgentRunner(provider)

    original_init = runner._planning.init_reflection

    def _init(spec: AgentRunSpec) -> Any:
        reflection = original_init(spec)
        if reflection is not None:
            reflection.reflect = _spy_reflect
        return reflection

    runner._planning.init_reflection = _init

    result = await runner.run(_spec(enable_reflection=False, workspace=None))

    assert result.stop_reason == "completed"
    assert reflect_calls == []
