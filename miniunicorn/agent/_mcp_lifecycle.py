"""AgentLoop MCP lifecycle mixin.

Holds the small set of methods that register the default tool set and manage
the MCP (Model Context Protocol) server connections owned by an
:class:`AgentLoop`. Extracted from ``miniunicorn.agent.loop.AgentLoop`` purely
to keep that module focused on orchestration; ``AgentLoop`` re-combines them
through multiple inheritance (see :class:`McpLifecycleMixin`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent import context as agent_context
from miniunicorn.agent.tools.self import MyTool

if TYPE_CHECKING:
    from miniunicorn.agent.loop import AgentLoop


class McpLifecycleMixin:
    """Default-tool registration and MCP server lifecycle for :class:`AgentLoop`.

    Reads several ``self`` attributes that are owned by :class:`AgentLoop``
    (``tools``, ``tools_config``, ``workspace``, ``bus``, ``subagents``,
    ``cron_service``, ``sessions``, ``_provider_snapshot_loader``,
    ``workspace_scopes``, ``subagent_registry``, ``_mcp_stacks``,
    ``_background_tasks``).
    """

    def _register_default_tools(self: "AgentLoop") -> None:
        """Register the default set of tools via plugin loader."""
        from miniunicorn.agent.tools.context import ToolContext
        from miniunicorn.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            timezone="UTC",
            workspace_sandbox=self.workspace_scopes.sandbox_status,
            subagent_registry=self.subagent_registry,
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    def _set_mcp_servers(self: "AgentLoop", servers: dict[str, Any]) -> None:
        """Replace the configured MCP server table (hot-reload write-back).

        MCP hot reload reconciles the live connections against the config
        file; the resulting server table is written back here so the write
        stays inside the MCP lifecycle module instead of reaching into the
        loop's private attribute from ``tools/mcp``.
        """
        self._mcp_servers = servers

    async def _connect_mcp(self: "AgentLoop") -> None:
        """Connect configured MCP servers."""
        await agent_context.connect_mcp(self, self.tools)

    async def close_mcp(self: "AgentLoop") -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except Exception:
                # 兼容老版本 Python（无 BaseExceptionGroup），aclose 通常只 raise Exception 子类
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()
