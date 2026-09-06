"""MCP (Model Context Protocol) connection-stack ownership runtime.

Normally the long-lived services (cron, subagents, MCP connections) are created
by the composition root in ``erza/composition/`` rather than by
:class:`AgentLoop`. This module is the composition-root-owned owner of the live
MCP connection stacks. It natively satisfies the ``RuntimeState`` protocol from
``erza.tools.mcp``, so ``connect_missing_servers`` / ``reload``
can take it directly as their ``state`` argument with no change to tools/mcp.py.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from erza.tools.registry import ToolRegistry


class McpRuntime:
    """Owner of the live MCP connection stacks.

    Holds the mutable state that ``tools/mcp.py``'s ``RuntimeState`` protocol
    reads and writes (``_mcp_servers`` / ``_mcp_stacks`` / ``_mcp_connecting``
    / ``_mcp_connected`` / ``_set_mcp_servers``) so the loop can delegate its
    MCP surface to it without re-implementing connection logic.
    """

    def __init__(self, servers: dict[str, Any] | None = None) -> None:
        self._mcp_servers: dict[str, Any] = dict(servers or {})
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False

    def _set_mcp_servers(self, servers: dict[str, Any]) -> None:
        """Replace the configured MCP server table (hot-reload write-back)."""
        self._mcp_servers = servers

    async def connect_missing(self, registry: "ToolRegistry") -> None:
        """Connect any configured servers that do not yet have a live stack."""
        from erza.tools.mcp import connect_missing_servers

        await connect_missing_servers(self, registry)

    async def close_all(self) -> None:
        """Close every live MCP stack and reset connection state."""
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except Exception:
                # 兼容老版本 Python（无 BaseExceptionGroup），aclose 通常只 raise Exception 子类
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()
        self._mcp_connected = False
