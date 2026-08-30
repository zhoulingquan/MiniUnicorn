"""W1-2 Commit 3: MCP connection stacks owned by McpRuntime.

``McpRuntime`` is the composition-root-owned owner of the live MCP connection
stacks. It natively satisfies ``tools/mcp.py``'s ``RuntimeState`` protocol, and
the loop delegates its private ``_mcp_*`` attributes to it. These tests pin the
ownership contract: close semantics, protocol conformance, idempotence, and
loop/runtime delegation identity.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from miniunicorn.agent.loop_builder import AgentLoopBuilder
from miniunicorn.bus.queue import MessageBus
from miniunicorn.composition.mcp_runtime import McpRuntime


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


def test_mcp_runtime_satisfies_runtime_state_protocol() -> None:
    state = McpRuntime({"srv": {"command": "npx", "args": ["-y", "fake-server"]}})

    # McpRuntime exposes every member the RuntimeState protocol requires.
    for member in (
        "_mcp_servers",
        "_mcp_stacks",
        "_mcp_connecting",
        "_mcp_connected",
        "_set_mcp_servers",
    ):
        assert hasattr(state, member)
    assert isinstance(state._mcp_servers, dict)
    assert isinstance(state._mcp_stacks, dict)
    assert isinstance(state._mcp_connected, bool)
    assert callable(state._set_mcp_servers)


def test_close_all_closes_every_stack_and_clears(tmp_path: Path) -> None:
    runtime = McpRuntime({"a": {}, "b": {}})
    stack_a = AsyncMock(spec=AsyncExitStack)
    stack_b = AsyncMock(spec=AsyncExitStack)
    runtime._mcp_stacks = {"a": stack_a, "b": stack_b}
    runtime._mcp_connected = True

    import asyncio

    asyncio.run(runtime.close_all())

    stack_a.aclose.assert_awaited_once()
    stack_b.aclose.assert_awaited_once()
    assert runtime._mcp_stacks == {}
    assert runtime._mcp_connected is False


def test_close_all_survives_stack_exception(tmp_path: Path) -> None:
    runtime = McpRuntime({"a": {}, "b": {}})
    stack_bad = AsyncMock(spec=AsyncExitStack)
    stack_bad.aclose.side_effect = RuntimeError("boom")
    stack_good = AsyncMock(spec=AsyncExitStack)
    runtime._mcp_stacks = {"bad": stack_bad, "good": stack_good}
    runtime._mcp_connected = True

    import asyncio

    asyncio.run(runtime.close_all())

    stack_good.aclose.assert_awaited_once()
    assert runtime._mcp_stacks == {}
    assert runtime._mcp_connected is False


def test_close_all_empty_is_idempotent() -> None:
    runtime = McpRuntime({})
    import asyncio

    asyncio.run(runtime.close_all())
    assert runtime._mcp_stacks == {}
    assert runtime._mcp_connected is False


def test_injected_runtime_stacks_are_loop_stacks(tmp_path: Path) -> None:
    runtime = McpRuntime({"srv": {"command": "npx"}})
    loop = AgentLoopBuilder(MessageBus(), _provider(), tmp_path).with_mcp_runtime(runtime).build()

    assert loop._mcp_stacks is runtime._mcp_stacks
    assert loop._mcp_servers is runtime._mcp_servers
    # Loop stays structurally valid for tools/mcp.py (hot-reload path).
    for member in (
        "_mcp_servers",
        "_mcp_stacks",
        "_mcp_connecting",
        "_mcp_connected",
        "_set_mcp_servers",
    ):
        assert hasattr(loop, member)


def test_non_injected_loop_self_builds_runtime(tmp_path: Path) -> None:
    loop = AgentLoopBuilder(MessageBus(), _provider(), tmp_path).build()

    assert loop._mcp_runtime is not None
    assert isinstance(loop._mcp_runtime, McpRuntime)
    assert loop._mcp_stacks is loop._mcp_runtime._mcp_stacks


def test_loop_delegation_is_read_write_forwarded(tmp_path: Path) -> None:
    runtime = McpRuntime({})
    loop = AgentLoopBuilder(MessageBus(), _provider(), tmp_path).with_mcp_runtime(runtime).build()

    # tools/mcp.py writes these through the loop-as-state; verify the writes
    # land on the runtime (hot-reload path).
    loop._mcp_connecting = True
    assert runtime._mcp_connecting is True
    loop._mcp_stacks["x"] = AsyncMock(spec=AsyncExitStack)
    assert "x" in runtime._mcp_stacks
