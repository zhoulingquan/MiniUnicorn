"""Delegate tool: invoke a declaratively-defined subagent by name.

This mirrors TRAE's Agent → Subagent dispatch: the main agent's system
prompt lists available subagents (from agents/*.md), and the LLM calls
this tool to autonomously delegate when a task matches a subagent's
description.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from miniunicorn.agent.safety_policy import RiskLevel
from miniunicorn.agent.tools.base import Tool, tool_parameters
from miniunicorn.agent.tools.context import ContextAware, RequestContext
from miniunicorn.agent.tools.schema import StringSchema, tool_parameters_schema
from miniunicorn.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from miniunicorn.agent.subagent import SubagentManager
    from miniunicorn.agent.subagent_registry import SubagentRegistry


@tool_parameters(
    tool_parameters_schema(
        subagent=StringSchema(
            "Name of the subagent to delegate to (must match a name from the "
            "Available Subagents list in the system prompt)."
        ),
        task=StringSchema("The task description to delegate to the subagent."),
        required=["subagent", "task"],
    )
)
class DelegateTool(Tool, ContextAware):
    """Delegate a task to a specialized subagent by name."""

    # 允许主代理（core）和子代理（subagent）都加载此工具。
    # 递归深度由 execute() 中的 _current_depth ContextVar 在运行时动态检查，
    # 而非通过 scope 硬性限制。这样 max_subagent_recursion_depth 配置项可以
    # 在不重启的情况下灵活调整递归层数。
    _scopes = {"core", "subagent"}

    def __init__(
        self,
        manager: "SubagentManager | None" = None,
        registry: "SubagentRegistry | None" = None,
    ):
        self._manager = manager
        self._registry = registry
        self._origin_channel: ContextVar[str] = ContextVar("dl_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("dl_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("dl_session_key", default="cli:direct")

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # registry/manager are attached to ToolContext by the loop; use getattr
        # so contexts that don't carry them (e.g. minimal test contexts) don't
        # crash with AttributeError at tool construction time.
        registry = getattr(ctx, "subagent_registry", None)
        manager = getattr(ctx, "subagent_manager", None)
        return cls(manager=manager, registry=registry)

    def set_context(self, ctx: RequestContext) -> None:
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to a specialized subagent. The subagent runs in its "
            "own context with a dedicated system prompt and tool set. Use this "
            "when the task matches one of the Available Subagents listed in the "
            "system prompt. Returns the subagent's final result."
        )

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    async def execute(
        self,
        subagent: str | None = None,
        task: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not subagent or not task:
            return "Error: both 'subagent' and 'task' are required"
        if self._manager is None:
            return "Error: subagent manager not available"
        if self._registry is None:
            return "Error: no subagent registry configured (no agents/ directory)"

        # 递归深度检查：current_depth > max_subagent_recursion_depth 时禁止。
        # 语义：max_subagent_recursion_depth = 子代理可以再 delegate 的层数
        #   主代理 depth=0：0 > max 永远为 False（除非 max<0），总是允许
        #   子代理 depth=1：max=0 禁止（1>0），max=1 允许（1>1=False）
        #   孙代理 depth=2：max=1 禁止（2>1）
        # 即 max=1 表示"允许一层递归"——子代理可以再 delegate 一次。
        from miniunicorn.agent.subagent import get_current_subagent_depth

        current_depth = get_current_subagent_depth()
        max_depth = self._manager.max_subagent_recursion_depth
        if current_depth > max_depth:
            return (
                f"Cannot delegate: recursion depth limit reached "
                f"(current depth {current_depth}, max allowed {max_depth}). "
                f"Complete the current task and return the result to the caller."
            )

        defn = self._registry.get(subagent)
        if defn is None:
            available = ", ".join(d.name for d in self._registry.list_all()) or "(none)"
            return f"Error: unknown subagent '{subagent}'. Available: {available}"

        # Concurrency check
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Cannot delegate: concurrency limit reached "
                f"({running}/{limit} running). Wait for a subagent to complete."
            )

        ws_scope = current_workspace_scope()
        status, content = await self._manager.spawn_and_wait(
            task=task,
            label=defn.name,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            workspace_scope=ws_scope,
            system_prompt_override=defn.system_prompt,
            model_override=defn.model,
            tools_whitelist=defn.tools,
        )
        prefix = "Subagent completed" if status == "ok" else "Subagent failed"
        return f"{prefix} [{defn.name}]:\n{content}"
