"""Read-only runtime state view for prompt assembly.

Prompt building (``erza.agent.context``) needs to know which MCP
servers are configured and currently live so it can annotate model-visible
preset attachments.  This protocol exposes exactly those two read-only
attributes instead of letting consumers reach into the loop's private
runtime attributes directly.
"""

from __future__ import annotations

from typing import Any, Protocol


class RuntimeStateView(Protocol):
    """Read-only slice of the agent loop's MCP runtime state.

    ``AgentLoop`` satisfies this protocol through public read-only properties;
    tools / prompt builders receive the view and can never mutate the loop's
    private MCP tables from outside the MCP lifecycle module.
    """

    @property
    def mcp_servers(self) -> dict[str, Any]:
        """Configured MCP server table (name -> server config)."""
        ...

    @property
    def mcp_stacks(self) -> dict[str, Any]:
        """Live MCP connection stacks (name -> ``AsyncExitStack``)."""
        ...
