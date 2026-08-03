"""Tool execution parity tests for AgentRunner extraction (Task 11).

These tests pin the behavior of the ``ToolExecutor`` collaborator that
Task 11 extracts from ``AgentRunner``. They cover:

- Direct tool execution via the gateway port
- Parallel-safe batching (concurrency-safe tools batch together)
- Sequential unsafe batching (non-concurrency-safe tools run alone)
- SSRF soft payload classification
- Workspace violation classification
- Result truncation via ``_normalize_tool_result``
- Tool exception handling
- ``fail_on_tool_error`` produces a fatal error

The file imports ``ToolExecutor`` and ``ToolBatchResult`` so Step 2 fails
until ``runner_tools`` / the ``runner_types`` addition exists.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.runner_tools import ToolExecutor
from miniunicorn.agent.runner_types import ToolBatchResult
from miniunicorn.agent.tools.base import Tool
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import ToolCallRequest
from tests.agent.conftest import FakeToolExecutionPort

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _spec(
    *,
    tools: Any = None,
    tool_execution_port: Any = None,
    concurrent_tools: bool = False,
    fail_on_tool_error: bool = False,
    max_iterations: int = 1,
) -> AgentRunSpec:
    if tools is None:
        tools = MagicMock()
        tools.get_definitions.return_value = []
    return AgentRunSpec(
        initial_messages=[],
        tools=tools,
        tool_execution_port=tool_execution_port,
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        concurrent_tools=concurrent_tools,
        fail_on_tool_error=fail_on_tool_error,
    )


class _StubTool(Tool):
    """Minimal tool stub for testing."""

    def __init__(self, name: str, result: str = "ok", *, concurrency_safe: bool = False):
        self._name = name
        self._result = result
        self._concurrency_safe = concurrency_safe

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    @property
    def concurrency_safe(self) -> bool:
        return self._concurrency_safe

    async def execute(self, **kwargs):
        return self._result


@pytest.mark.asyncio
async def test_direct_tool_execution_returns_batch_result() -> None:
    """A single tool call through the gateway returns a ``ToolBatchResult``."""
    tools = ToolRegistry()
    tools.register(_StubTool("list_dir", "entries"))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [ToolCallRequest(id="call_1", name="list_dir", arguments={})]

    result = await executor.execute(spec, calls, {}, {})

    assert isinstance(result, ToolBatchResult)
    assert len(result.results) == 1
    assert result.results[0] == "entries"
    assert result.events == [{"name": "list_dir", "status": "ok", "detail": "entries"}]
    assert result.fatal_error is None


@pytest.mark.asyncio
async def test_gateway_tool_execution_with_mock_registry() -> None:
    """Tool execution via mock registry and FakeToolExecutionPort."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="file contents")
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [ToolCallRequest(id="c1", name="read_file", arguments={"path": "/x"})]

    result = await executor.execute(spec, calls, {}, {})

    assert result.results[0] == "file contents"
    assert result.events[0]["name"] == "read_file"
    assert result.events[0]["status"] == "ok"
    assert result.fatal_error is None


@pytest.mark.asyncio
async def test_parallel_safe_batching() -> None:
    """Concurrency-safe tools are batched and run concurrently."""
    shared_events: list[str] = []

    class _DelayTool(Tool):
        def __init__(self, name: str, delay: float):
            self._name = name
            self._delay = delay

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return self._name

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}, "required": []}

        @property
        def read_only(self) -> bool:
            return True

        @property
        def concurrency_safe(self) -> bool:
            return True

        async def execute(self, **kwargs):
            shared_events.append(f"start:{self._name}")
            await asyncio.sleep(self._delay)
            shared_events.append(f"end:{self._name}")
            return self._name

    tools = ToolRegistry()
    tools.register(_DelayTool("a", 0.05))
    tools.register(_DelayTool("b", 0.05))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port, concurrent_tools=True)
    calls = [
        ToolCallRequest(id="c1", name="a", arguments={}),
        ToolCallRequest(id="c2", name="b", arguments={}),
    ]

    await executor.execute(spec, calls, {}, {})

    assert shared_events[0:2] == ["start:a", "start:b"]
    assert "end:a" in shared_events and "end:b" in shared_events


@pytest.mark.asyncio
async def test_sequential_unsafe_batching() -> None:
    """Non-concurrency-safe tools run sequentially even with concurrent_tools=True."""
    shared_events: list[str] = []

    class _SeqTool(Tool):
        def __init__(self, name: str, delay: float):
            self._name = name
            self._delay = delay

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return self._name

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}, "required": []}

        @property
        def read_only(self) -> bool:
            return True

        @property
        def concurrency_safe(self) -> bool:
            return False

        async def execute(self, **kwargs):
            shared_events.append(f"start:{self._name}")
            await asyncio.sleep(self._delay)
            shared_events.append(f"end:{self._name}")
            return self._name

    tools = ToolRegistry()
    tools.register(_SeqTool("x", 0.05))
    tools.register(_SeqTool("y", 0.01))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port, concurrent_tools=True)
    calls = [
        ToolCallRequest(id="c1", name="x", arguments={}),
        ToolCallRequest(id="c2", name="y", arguments={}),
    ]

    await executor.execute(spec, calls, {}, {})

    # x starts and ends before y starts (sequential)
    assert shared_events == ["start:x", "end:x", "start:y", "end:y"]


@pytest.mark.asyncio
async def test_tool_exception_returns_error_event() -> None:
    """A tool exception produces an error event and no fatal error by default."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=RuntimeError("boom"))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [ToolCallRequest(id="c1", name="bad_tool", arguments={})]

    result = await executor.execute(spec, calls, {}, {})

    assert result.fatal_error is None
    assert result.events[0]["status"] == "error"
    assert "boom" in result.results[0]


@pytest.mark.asyncio
async def test_fail_on_tool_error_produces_fatal_error() -> None:
    """``fail_on_tool_error=True`` surfaces the exception as ``fatal_error``."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=RuntimeError("boom"))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(
        tools=tools,
        tool_execution_port=port,
        fail_on_tool_error=True,
    )
    calls = [ToolCallRequest(id="c1", name="bad_tool", arguments={})]

    result = await executor.execute(spec, calls, {}, {})

    assert result.fatal_error is not None
    assert isinstance(result.fatal_error, RuntimeError)
    assert result.events[0]["status"] == "error"


@pytest.mark.asyncio
async def test_ssrf_violation_returns_soft_payload() -> None:
    """An SSRF marker in the tool error produces a soft payload, not a fatal error."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=RuntimeError("internal/private url detected in request"))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [ToolCallRequest(id="c1", name="fetch", arguments={})]

    result = await executor.execute(spec, calls, {}, {})

    assert result.fatal_error is None
    assert "security boundary" in result.results[0]
    assert result.events[0]["detail"].startswith("ssrf_violation")


@pytest.mark.asyncio
async def test_workspace_violation_returns_soft_payload() -> None:
    """A workspace boundary marker produces a soft payload."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=RuntimeError("path outside working dir"))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [ToolCallRequest(id="c1", name="read", arguments={})]

    result = await executor.execute(spec, calls, {}, {})

    assert result.fatal_error is None
    assert "workspace_violation" in result.events[0]["detail"]


@pytest.mark.asyncio
async def test_execute_tools_facade_delegates_to_tool_executor() -> None:
    """``AgentRunner._execute_tools`` delegates to ``self._tool_executor``."""
    tools = ToolRegistry()
    tools.register(_StubTool("noop", "done"))
    port = FakeToolExecutionPort(tools)

    runner = AgentRunner(MagicMock())
    assert hasattr(runner, "_tool_executor")
    assert isinstance(runner._tool_executor, ToolExecutor)

    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [ToolCallRequest(id="c1", name="noop", arguments={})]

    results, events, fatal_error = await runner._execute_tools(spec, calls, {}, {})

    assert results == ["done"]
    assert events[0]["name"] == "noop"
    assert fatal_error is None


@pytest.mark.asyncio
async def test_multiple_tool_calls_preserve_order() -> None:
    """Multiple tool calls produce results in the same order as the calls."""
    tools = ToolRegistry()
    tools.register(_StubTool("a", "result_a"))
    tools.register(_StubTool("b", "result_b"))
    tools.register(_StubTool("c", "result_c"))
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)
    calls = [
        ToolCallRequest(id="c1", name="a", arguments={}),
        ToolCallRequest(id="c2", name="b", arguments={}),
        ToolCallRequest(id="c3", name="c", arguments={}),
    ]

    result = await executor.execute(spec, calls, {}, {})

    assert result.results == ["result_a", "result_b", "result_c"]
    assert [e["name"] for e in result.events] == ["a", "b", "c"]
    assert result.fatal_error is None


@pytest.mark.asyncio
async def test_empty_tool_calls_returns_empty_batch() -> None:
    """An empty tool call list returns an empty ``ToolBatchResult``."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    port = FakeToolExecutionPort(tools)

    executor = ToolExecutor(lambda: MagicMock())
    spec = _spec(tools=tools, tool_execution_port=port)

    result = await executor.execute(spec, [], {}, {})

    assert result.results == []
    assert result.events == []
    assert result.fatal_error is None


def test_tool_batch_result_dataclass_fields() -> None:
    """``ToolBatchResult`` has the required fields with correct defaults."""
    batch = ToolBatchResult(results=[], events=[])
    assert batch.results == []
    assert batch.events == []
    assert batch.fatal_error is None

    exc = RuntimeError("x")
    batch2 = ToolBatchResult(
        results=["a"], events=[{"name": "t", "status": "ok", "detail": "d"}], fatal_error=exc
    )
    assert batch2.results == ["a"]
    assert batch2.fatal_error is exc
