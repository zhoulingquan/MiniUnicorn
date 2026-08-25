"""create_agent tool: generate a subagent definition from natural language.

Mirrors TRAE's "smart-generate agent" flow: the main agent calls this tool
with a natural-language description of the subagent it wishes existed, the
LLM produces a properly formatted ``.md`` definition, and the tool saves
it to ``agents/<name>.md`` so it is immediately available for ``delegate``.

Only the main agent (``core`` scope) may call this — subagents must not
recursively spawn their own subagents.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from miniunicorn.agent.agent_generator import AgentGenerator, extract_name
from miniunicorn.agent.safety_policy import RiskLevel
from miniunicorn.agent.tools.base import Tool, tool_parameters
from miniunicorn.agent.tools.schema import ObjectSchema, StringSchema

if TYPE_CHECKING:
    from miniunicorn.agent.subagent_registry import SubagentRegistry
    from miniunicorn.providers.base import LLMProvider


@tool_parameters(
    ObjectSchema(
        properties={
            "description": StringSchema(
                "Natural-language description of the subagent to create: what it "
                "should do, when the main agent should delegate to it, and any "
                "constraints on its tools or behavior. The LLM will derive the "
                "agent name, frontmatter, and system prompt from this description."
            )
        },
        required=["description"],
    ).to_json_schema()
)
class CreateAgentTool(Tool):
    """Generate a new subagent definition file via the LLM and save it."""

    _scopes = {"core"}  # Only main agent can create subagents (no recursion)

    def __init__(
        self,
        provider: "LLMProvider | None" = None,
        model: str | None = None,
        workspace: str | Path | None = None,
        registry: "SubagentRegistry | None" = None,
    ):
        self._provider = provider
        self._model = model
        self._workspace = Path(workspace) if workspace else None
        self._registry = registry

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Provider is resolved lazily through the snapshot loader attached to
        # ToolContext so the tool always sees the current provider (it may
        # change at runtime via /api/settings/provider/update).
        provider = None
        model = None
        loader = getattr(ctx, "provider_snapshot_loader", None)
        if callable(loader):
            try:
                snapshot = loader()
            except Exception:
                snapshot = None
            if snapshot is not None:
                provider = getattr(snapshot, "provider", None)
                model = getattr(snapshot, "model", None)
        return cls(
            provider=provider,
            model=model,
            workspace=ctx.workspace,
            registry=getattr(ctx, "subagent_registry", None),
        )

    @property
    def name(self) -> str:
        return "create_agent"

    @property
    def description(self) -> str:
        return (
            "Generate a new subagent definition file from a natural-language "
            "description. The LLM produces the .md frontmatter (name, "
            "description, tools) and the system-prompt body, then saves it "
            "to agents/<name>.md so it can be invoked via `delegate`. "
            "Returns the generated agent name and a preview of the content. "
            "If an agent with the same name already exists, it is overwritten."
        )

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    async def execute(
        self,
        description: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not description or not description.strip():
            return "Error: 'description' is required"
        if self._provider is None:
            return "Error: LLM provider not available"
        if self._workspace is None:
            return "Error: workspace not configured"

        generator = AgentGenerator(provider=self._provider, model=self._model)
        try:
            content = await generator.generate(description)
        except ValueError as exc:
            return f"Error: failed to generate agent definition: {exc}"

        name = extract_name(content)
        if not name:
            return (
                "Error: generated content has no parseable 'name' field; "
                "refusing to save. Please retry with a clearer description."
            )

        # Reuse the route's save_agent so name validation, directory creation,
        # and overwrite semantics stay consistent with the HTTP API.
        from miniunicorn.api.routes_agents import router

        try:
            path = router.save_agent(self._workspace, name, content)
        except ValueError as exc:
            return f"Error: invalid generated name '{name}': {exc}"

        # Reload the registry in-place so the new agent is immediately
        # delegate-able within the same session.
        if self._registry is not None:
            try:
                self._registry.load()
            except Exception:
                # Reloading is best-effort; the file is already on disk and
                # will be picked up on the next workspace scan.
                pass

        preview = content if len(content) <= 1200 else content[:1200] + "\n...(truncated)"
        return (
            f"Created subagent '{name}' at {path}.\n\n"
            f"--- Preview ---\n{preview}\n--- End preview ---\n\n"
            f'You can now delegate tasks to it via: delegate(subagent="{name}", task="...")'
        )
