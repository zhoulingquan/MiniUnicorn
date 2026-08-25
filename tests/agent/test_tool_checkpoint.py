"""P1-T5: Per-tool intent/result checkpoints.

Tests that ``ToolExecutionCoordinator.run_tool()`` emits a ``ToolCheckpoint``
record through ``spec.checkpoint_callback`` after each tool completes
(success, error, or skipped).  The checkpoint includes the tool call id,
name, arguments (intent), a result summary, status, duration, and the
associated plan step id (when in MANAGED mode).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.execution.tool_execution import ToolExecutionCoordinator
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.tool_checkpoint import ToolCheckpoint
from miniunicorn.providers.base import LLMProvider, ToolCallRequest


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


# 1. Successful tool emits checkpoint with status "ok".


@pytest.mark.asyncio
async def test_successful_tool_emits_checkpoint() -> None:
    checkpoints: list[dict[str, Any]] = []
    spec = _spec(checkpoints)
    spec.tools.execute = MagicMock(return_value=_async("ok result"))

    coord = _coordinator()
    await coord.run_tool(spec, _tool_call(), {}, {})

    tc_payloads = [c for c in checkpoints if c.get("phase") == "tool_completed"]
    assert len(tc_payloads) == 1
    cp = tc_payloads[0]["tool_checkpoint"]
    assert cp["tool_call_id"] == "call_abc"
    assert cp["tool_name"] == "echo"
    assert cp["status"] == "ok"
    assert cp["intent"] == {"text": "hello"}
    assert "ok result" in cp["result_summary"]
    assert cp["duration_ms"] >= 0
    assert cp["step_id"] is None


# 2. Errored tool emits checkpoint with status "error".


@pytest.mark.asyncio
async def test_errored_tool_emits_checkpoint() -> None:
    checkpoints: list[dict[str, Any]] = []
    spec = _spec(checkpoints)
    spec.tools.execute = MagicMock(return_value=_async_error(ValueError("boom")))

    coord = _coordinator()
    await coord.run_tool(spec, _tool_call(), {}, {})

    tc_payloads = [c for c in checkpoints if c.get("phase") == "tool_completed"]
    assert len(tc_payloads) == 1
    cp = tc_payloads[0]["tool_checkpoint"]
    assert cp["status"] == "error"
    assert "boom" in cp["result_summary"]
    assert cp["tool_name"] == "echo"


# 3. Step id is passed through in MANAGED mode.


@pytest.mark.asyncio
async def test_step_id_associated_when_managed() -> None:
    checkpoints: list[dict[str, Any]] = []
    spec = _spec(checkpoints)
    spec.tools.execute = MagicMock(return_value=_async("ok"))

    coord = _coordinator()
    await coord.run_tool(spec, _tool_call(), {}, {}, step_id=3)

    cp = checkpoints[-1]["tool_checkpoint"]
    assert cp["step_id"] == 3


# 4. ToolCheckpoint.to_dict round-trips all fields.


def test_tool_checkpoint_to_dict() -> None:
    tc = ToolCheckpoint(
        tool_call_id="call_1",
        tool_name="search",
        intent={"query": "test"},
        result_summary="found 3 results",
        status="ok",
        duration_ms=42.5,
        step_id=2,
    )
    d = tc.to_dict()
    assert d["tool_call_id"] == "call_1"
    assert d["tool_name"] == "search"
    assert d["intent"] == {"query": "test"}
    assert d["result_summary"] == "found 3 results"
    assert d["status"] == "ok"
    assert d["duration_ms"] == 42.5
    assert d["step_id"] == 2


# 5. No checkpoint when callback is None.


@pytest.mark.asyncio
async def test_no_checkpoint_when_callback_none() -> None:
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
    )
    spec.tools.execute = MagicMock(return_value=_async("ok"))

    coord = _coordinator()
    result, event, error = await coord.run_tool(spec, _tool_call(), {}, {})
    assert error is None
    assert event["status"] == "ok"


# 6. Result summary is truncated.


@pytest.mark.asyncio
async def test_result_summary_truncated() -> None:
    checkpoints: list[dict[str, Any]] = []
    spec = _spec(checkpoints)
    long_result = "x" * 500
    spec.tools.execute = MagicMock(return_value=_async(long_result))

    coord = _coordinator()
    await coord.run_tool(spec, _tool_call(), {}, {})

    cp = checkpoints[-1]["tool_checkpoint"]
    assert len(cp["result_summary"]) <= 200


# --- helpers ---


async def _async(value: str) -> str:
    await asyncio.sleep(0)
    return value


async def _async_error(exc: Exception) -> str:
    await asyncio.sleep(0)
    raise exc
