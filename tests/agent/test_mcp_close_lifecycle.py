"""MCP close lifecycle behavior tests.

Locks the ``close_mcp()`` behavior (W1-2, McpRuntime ownership):

- ``close_mcp()`` drains background tasks and clears ``_mcp_stacks``;
- it resets ``_mcp_connected`` (the previous "flag stays True" bug was fixed
  when MCP stack ownership moved to ``McpRuntime.close_all``, which resets it);
- a subsequent ``_connect_mcp()`` reconnects configured servers.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from unittest.mock import MagicMock

import pytest

from tests.agent.conftest import make_loop


def _make_loop(tmp_path, mcp_servers):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return make_loop(tmp_path, provider=provider, mcp_servers=mcp_servers)


def _fake_connect():
    """Return a connect_mcp_servers fake that opens a real AsyncExitStack per server."""

    async def _connect(servers, registry):
        stacks = {}
        for name in servers:
            stack = AsyncExitStack()
            await stack.__aenter__()
            stacks[name] = stack
        return stacks

    return _connect


@pytest.mark.asyncio
async def test_close_mcp_clears_stacks_and_can_reconnect(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path, {"test": object()})
    monkeypatch.setattr("miniunicorn.tools.mcp.connect_mcp_servers", _fake_connect())

    await loop._connect_mcp()
    assert loop._mcp_connected is True
    assert set(loop._mcp_stacks) == {"test"}

    await loop.close_mcp()
    assert loop._mcp_stacks == {}

    await loop._connect_mcp()
    assert set(loop._mcp_stacks) == {"test"}
    assert loop._mcp_connected is True


@pytest.mark.asyncio
async def test_close_mcp_resets_connected_flag(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path, {"test": object()})
    monkeypatch.setattr("miniunicorn.tools.mcp.connect_mcp_servers", _fake_connect())

    await loop._connect_mcp()
    assert loop._mcp_connected is True

    await loop.close_mcp()
    assert loop._mcp_connected is False
