"""AgentLoop MCP lifecycle mixin.

Holds the small set of methods that register the default tool set and manage
the MCP (Model Context Protocol) server connections owned by an
:class:`AgentLoop`. Extracted from ``miniunicorn.agent.loop.AgentLoop`` purely
to keep that module focused on orchestration; ``AgentLoop`` re-combines them
through multiple inheritance (see :class:`McpLifecycleMixin`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from miniunicorn.agent import context as agent_context
from miniunicorn.agent.tools.self import MyTool

if TYPE_CHECKING:
    from miniunicorn.agent.loop import AgentLoop


class McpLifecycleMixin:
    """Default-tool registration and MCP server lifecycle for :class:`AgentLoop`.

    Reads several ``self`` attributes that are owned by :class:`AgentLoop`
    (``tools``, ``tools_config``, ``workspace``, ``bus``, ``subagents``,
    ``cron_service``, ``sessions``, ``_provider_snapshot_loader``,
    ``workspace_scopes``, ``_vector_recall``, ``context``,
    ``subagent_registry``, ``_mcp_stacks``, ``_background_supervisor``).
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
            memory_store=self.context.memory if self._vector_recall else None,
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

    async def _connect_mcp(self: "AgentLoop") -> None:
        """Connect configured MCP servers."""
        await agent_context.connect_mcp(self, self.tools)

    async def close_mcp(self: "AgentLoop") -> None:
        """Drain pending background archives, then close MCP connections."""
        # Drain supervised background jobs (archives/consolidation) with a
        # bounded timeout so a stuck job cannot block shutdown indefinitely.
        await self._background_supervisor.close(cancel=False, timeout_s=30)
        # Give the runner's reflection supervisor a chance to flush too.
        await self.runner.aclose()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except Exception:
                # 兼容老版本 Python（无 BaseExceptionGroup），aclose 通常只 raise Exception 子类
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()
