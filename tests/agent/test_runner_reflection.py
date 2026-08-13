"""Runner-level tests for structured Reflection mode propagation (C2 plan B task 3).

The Reflection module already supports ``structured_mode``; these tests prove the
mode reaches it through ``AgentRunSpec.structured_memory_mode`` and the
``AgentRunner`` construction path, producing program-generated ``reflection_id``
entries only for structured modes (shadow/governed).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars

_REFLECTION_ID_RE = re.compile(r"^rfl_[0-9a-f]{32}$")


def _reflections(tmp_path: Path) -> list[dict]:
    path = tmp_path / "memory" / "reflections.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _ReflectionProvider:
    """One tool-calling turn (hits max_iterations), then a reflection response."""

    def __init__(self, reflection_content: str):
        self.calls = 0
        self.reflection_content = reflection_content

    async def chat_with_retry(self, *, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="working",
                tool_calls=[
                    ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})
                ],
                usage={},
            )
        return LLMResponse(content=self.reflection_content, tool_calls=[], usage={})


@pytest.mark.asyncio
async def test_runner_governed_reflection_persists_structured_entry(tmp_path):
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec

    provider = _ReflectionProvider('{"lesson": "Verify exact evidence IDs."}')
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    runner = AgentRunner(provider)
    await runner.run(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            enable_reflection=True,
            workspace=tmp_path,
            structured_memory_mode="governed",
        )
    )

    entries = _reflections(tmp_path)
    assert len(entries) == 1
    assert _REFLECTION_ID_RE.fullmatch(entries[0]["reflection_id"])
    assert entries[0]["lesson"] == "Verify exact evidence IDs."


@pytest.mark.parametrize(
    ("mode", "expected_structured"),
    [("shadow", True), ("legacy", False), (None, False)],
)
@pytest.mark.asyncio
async def test_runner_reflection_mode_shapes_entry(tmp_path, mode, expected_structured):
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec

    lesson = "A durable lesson."
    provider = _ReflectionProvider(f'{{"lesson": "{lesson}"}}')
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    runner = AgentRunner(provider)
    await runner.run(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            enable_reflection=True,
            workspace=tmp_path,
            structured_memory_mode=mode,
        )
    )

    entries = _reflections(tmp_path)
    assert len(entries) == 1
    assert ("reflection_id" in entries[0]) is expected_structured
    if expected_structured:
        assert _REFLECTION_ID_RE.fullmatch(entries[0]["reflection_id"])
        assert entries[0]["lesson"] == lesson
    else:
        assert "reflection_id" not in entries[0]
        assert entries[0]["reflection"] == f'{{"lesson": "{lesson}"}}'
