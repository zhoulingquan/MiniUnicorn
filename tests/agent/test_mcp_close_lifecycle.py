"""MCP close lifecycle behavior tests.

Locks the current ``close_mcp()`` behavior before the PR-2a extraction:

- ``close_mcp()`` drains background tasks and clears ``_mcp_stacks``;
- a subsequent ``_connect_mcp()`` reconnects configured servers.

Known current bug (NOT fixed in this PR, locked with ``xfail``): ``close_mcp()``
clears ``_mcp_stacks`` but does not reset ``_mcp_connected``, so the flag stays
``True`` from the last successful connect.
"""

from __future__ import annotations

import asyncio
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
    monkeypatch.setattr("miniunicorn.agent.tools.mcp.connect_mcp_servers", _fake_connect())

    await loop._connect_mcp()
    assert loop._mcp_connected is True
    assert set(loop._mcp_stacks) == {"test"}

    await loop.close_mcp()
    assert loop._mcp_stacks == {}

    await loop._connect_mcp()
    assert set(loop._mcp_stacks) == {"test"}
    assert loop._mcp_connected is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "close_mcp() clears _mcp_stacks but does not reset _mcp_connected "
        "(the flag stays True from the last connect). Existing bug locked here; "
        "not fixed in PR-2a."
    ),
)
@pytest.mark.asyncio
async def test_close_mcp_resets_connected_flag(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path, {"test": object()})
    monkeypatch.setattr("miniunicorn.agent.tools.mcp.connect_mcp_servers", _fake_connect())

    await loop._connect_mcp()
    assert loop._mcp_connected is True

    await loop.close_mcp()
    assert loop._mcp_connected is False
