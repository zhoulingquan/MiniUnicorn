"""T3 SafetyPolicy: tool risk classification and checkpoint recording."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from erza.agent.execution.tool_execution import ToolExecutionCoordinator
from erza.agent.runner import AgentRunner, AgentRunSpec
from erza.agent.safety_policy import RiskLevel, SafetyPolicy
from erza.providers.base import LLMProvider, ToolCallRequest
from erza.tools.base import Tool


def _spec(
    checkpoints: list[dict[str, Any]],
    fail_on_tool_error: bool = False,
) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
        checkpoint_callback=_make_collector(checkpoints),
        fail_on_tool_error=fail_on_tool_error,
    )


def _make_collector(checkpoints: list[dict[str, Any]]):
    async def _collect(payload: dict[str, Any]) -> None:
        checkpoints.append(payload)

    return _collect


def _tool_call(name: str = "echo", args: dict[str, Any] | None = None) -> ToolCallRequest:
    return ToolCallRequest(id="call_abc", name=name, arguments=args or {"text": "hello"})


def _coordinator() -> ToolExecutionCoordinator:
    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)
    return ToolExecutionCoordinator(runner)


class _ReadOnlyTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read a file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return "content"


class _WriteTool(Tool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write a file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _UnclassifiedTool(Tool):
    @property
    def name(self) -> str:
        return "some_tool"

    @property
    def description(self) -> str:
        return "Some tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _ExplicitHighTool(Tool):
    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute command"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


# 1. read_only tools default to LOW
def test_read_only_defaults_low() -> None:
    policy = SafetyPolicy()
    tool = _ReadOnlyTool()
    verdict = policy.evaluate("read_file", tool)
    assert verdict.risk_level == RiskLevel.LOW
    assert verdict.requires_checkpoint is False
    assert "read_only" in verdict.reason.lower()


# 2. Unclassified non-read_only defaults to MEDIUM
def test_unclassified_write_defaults_medium() -> None:
    policy = SafetyPolicy()
    tool = _UnclassifiedTool()
    verdict = policy.evaluate("some_tool", tool)
    assert verdict.risk_level == RiskLevel.MEDIUM
    assert verdict.requires_checkpoint is False
    assert "unclassified" in verdict.reason.lower() or "default" in verdict.reason.lower()


# 3. Explicit HIGH override
def test_explicit_high_override() -> None:
    policy = SafetyPolicy()
    tool = _ExplicitHighTool()
    verdict = policy.evaluate("exec", tool)
    assert verdict.risk_level == RiskLevel.HIGH
    assert verdict.requires_checkpoint is True
    assert "explicit" in verdict.reason.lower() or "declared" in verdict.reason.lower()


# 4. HIGH requires checkpoint
def test_high_requires_checkpoint() -> None:
    policy = SafetyPolicy()
    tool = _ExplicitHighTool()
    verdict = policy.evaluate("exec", tool)
    assert verdict.requires_checkpoint is True


# 5. Checkpoint records risk_level for HIGH-risk tool
@pytest.mark.asyncio
async def test_checkpoint_records_risk_level() -> None:
    checkpoints: list[dict[str, Any]] = []
    spec = _spec(checkpoints)
    spec.tools.get = MagicMock(return_value=_ExplicitHighTool())
    spec.tools.execute = MagicMock(return_value=_async("ok"))

    coord = _coordinator()
    await coord.run_tool(spec, _tool_call(name="exec"), {}, {})

    tc_payloads = [c for c in checkpoints if c.get("phase") == "tool_completed"]
    assert len(tc_payloads) == 1
    cp = tc_payloads[0]["tool_checkpoint"]
    assert cp["risk_level"] == "high"


# 6. HIGH risk only logs, doesn't block execution
@pytest.mark.asyncio
async def test_runner_executes_despite_high_risk() -> None:
    checkpoints: list[dict[str, Any]] = []
    spec = _spec(checkpoints)
    spec.tools.get = MagicMock(return_value=_ExplicitHighTool())
    spec.tools.execute = MagicMock(return_value=_async("executed"))

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(name="exec"), {}, {})

    assert error is None
    assert event["status"] == "ok"
    assert result == "executed"


# --- helpers ---
async def _async(value: str) -> str:
    await asyncio.sleep(0)
    return value
