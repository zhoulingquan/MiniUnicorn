"""P2 integration: pressure-driven governance, telemetry, and schema crop.

End-to-end pass through ``ContextGovernanceService.govern_messages`` with a
small context window and a real ToolRegistry, verifying that under RED
pressure: telemetry is populated, tool-result budgets tighten, and tool
schemas are cropped onto the spec.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent import turn_telemetry
from miniunicorn.agent.context_governor import PressureLevel
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.agent.turn_telemetry import TurnTelemetry


class _FakeProvider:
    """Minimal provider stand-in with a fixed prompt-token estimator."""

    class Generation:
        max_tokens = 8192

    generation = Generation()

    def get_default_model(self) -> str:
        return "test-model"

    def estimate_prompt_tokens(self, messages, tools, model=None):
        estimate = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                estimate += len(content) // 4
        for tool in tools or []:
            estimate += len(json.dumps(tool, ensure_ascii=False)) // 4
        return estimate, "fake"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    tool = MagicMock()
    tool.compactable = True
    tool.importance = 0.5
    tool.to_schema = lambda: {
        "type": "function",
        "function": {
            "name": "alpha",
            "description": "a" * 500,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "verbose": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
        },
    }
    registry._tools["alpha"] = tool
    registry._cached_definitions = None
    return registry


def _spec(registry: ToolRegistry, context_window_tokens: int) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[],
        tools=registry,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=2000,
        context_window_tokens=context_window_tokens,
    )


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "please do the thing"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "alpha", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "alpha", "content": "x" * 400},
    ]


@pytest.fixture
def bound_telemetry() -> Any:
    telemetry = TurnTelemetry()
    token = turn_telemetry.bind(telemetry)
    yield telemetry
    turn_telemetry.reset(token)


async def test_red_pressure_end_to_end(bound_telemetry) -> None:
    registry = _registry()
    spec = _spec(registry, context_window_tokens=120)
    runner = AgentRunner(_FakeProvider())  # type: ignore[arg-type]

    governed = await runner._context_governance.govern_messages(spec, _messages(), 0)

    # Pressure signal recorded on telemetry.
    assert bound_telemetry.governance_pressure is not None
    assert bound_telemetry.governance_pressure.level is PressureLevel.RED

    # Component telemetry populated and self-consistent.
    pc = bound_telemetry.prompt_components
    assert pc is not None
    assert pc.system_prompt > 0
    assert pc.tool_definitions > 0
    total = (
        pc.system_prompt
        + pc.tool_definitions
        + pc.conversation_history
        + pc.step_guidance
        + pc.reflection_context
        + pc.compacted_context
    )
    assert total == pc.total_estimated

    # Schema crop wrote cropped definitions onto the spec.
    assert spec.effective_tool_definitions is not None
    cropped_fn = spec.effective_tool_definitions[0]["function"]
    assert len(cropped_fn["description"]) <= 201
    assert cropped_fn["parameters"]["properties"]["path"]["type"] == "string"
    assert "verbose" not in cropped_fn["parameters"]["properties"]

    # Tool result budget tightened on RED (halved to 1000 + notice).
    tool_content = [m for m in governed if m.get("role") == "tool"][0]["content"]
    assert len(tool_content) <= 1000 + 16


async def test_green_pressure_end_to_end(bound_telemetry) -> None:
    registry = _registry()
    spec = _spec(registry, context_window_tokens=100_000)
    runner = AgentRunner(_FakeProvider())  # type: ignore[arg-type]

    messages = _messages()
    governed = await runner._context_governance.govern_messages(spec, messages, 0)

    assert bound_telemetry.governance_pressure is not None
    assert bound_telemetry.governance_pressure.level is PressureLevel.GREEN
    # No schema crop on GREEN.
    assert spec.effective_tool_definitions is None
    # Messages pass through on GREEN (nothing to compact or snip).
    assert governed == messages
