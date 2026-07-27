"""HTTP API route handlers for declarative subagent management.

Subagents are defined as ``.md`` files with YAML frontmatter in the
workspace's ``agents/`` directory (see ``SubagentRegistry``). This module
exposes a stateless ``router`` object with handlers for the
``/api/agents*`` endpoints. The dispatcher in ``channels/websocket.py``
calls these handlers and wraps their return values in HTTP responses.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from miniunicorn.agent.subagent_registry import SubagentRegistry

if TYPE_CHECKING:
    from miniunicorn.providers.base import LLMProvider

__all__ = ("router",)

# Agent names are used directly as filenames (``agents/<name>.md``), so they
# must be conservative: letters, digits, dots, dashes, underscores only.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_agent_name(name: str) -> str | None:
    """Return the sanitized name, or ``None`` if it is invalid or unsafe."""
    if not name or not _NAME_RE.match(name):
        return None
    # Defense-in-depth against directory traversal even though the regex
    # already forbids slashes.
    safe = name.replace("/", "").replace("\\", "").replace("..", "")
    if safe != name:
        return None
    return safe


def _definition_to_dict(defn) -> dict[str, Any]:
    return {
        "name": defn.name,
        "description": defn.description,
        "model": defn.model,
        "tools": defn.tools,
        "avatar": defn.avatar,
        "system_prompt": defn.system_prompt,
        "path": str(defn.file_path) if defn.file_path else None,
    }


class _AgentsRouter:
    """Stateless handlers for ``/api/agents*`` routes.

    Each method returns plain Python data (dicts/lists/bools) so the caller
    in ``channels/websocket.py`` can wrap the result in an HTTP response
    without introducing a circular import on the websocket helpers.
    """

    @staticmethod
    def list_agents(workspace: Path) -> dict[str, Any]:
        registry = SubagentRegistry(workspace)
        registry.load()
        return {"agents": [_definition_to_dict(d) for d in registry.list_all()]}

    @staticmethod
    def read_agent(workspace: Path, name: str) -> dict[str, Any] | None:
        safe = _validate_agent_name(name)
        if not safe:
            return None
        registry = SubagentRegistry(workspace)
        registry.load()
        defn = registry.get(safe)
        if defn is None:
            return None
        content = ""
        if defn.file_path and defn.file_path.exists():
            content = defn.file_path.read_text(encoding="utf-8")
        result = _definition_to_dict(defn)
        result["content"] = content
        return result

    @staticmethod
    def save_agent(workspace: Path, name: str, content: str) -> str:
        """Write raw ``.md`` content to ``agents/<name>.md``.

        Returns the absolute file path written. Raises ``ValueError`` for
        invalid names or empty content.
        """
        safe = _validate_agent_name(name)
        if not safe:
            raise ValueError(f"invalid agent name: {name!r}")
        if not content.strip():
            raise ValueError("content must not be empty")
        agents_dir = workspace / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        path = agents_dir / f"{safe}.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    @staticmethod
    def delete_agent(workspace: Path, name: str) -> bool:
        """Delete ``agents/<name>.md``. Returns True if a file was removed."""
        safe = _validate_agent_name(name)
        if not safe:
            return False
        path = workspace / "agents" / f"{safe}.md"
        if not path.exists():
            return False
        path.unlink()
        return True

    @staticmethod
    async def generate_agent(
        workspace: Path,
        provider: "LLMProvider",
        model: str | None,
        description: str,
    ) -> dict[str, Any]:
        """Generate a ``.md`` definition via the LLM.

        Returns ``{"content": "...", "name": "..."}`` where ``content`` is
        the raw .md text and ``name`` is extracted from the generated
        frontmatter (may be ``None`` if the LLM output is malformed).

        Does NOT save to disk — the caller previews the result and then
        explicitly invokes :meth:`save_agent` to persist it.

        Raises :class:`ValueError` when *description* is empty or the
        provider returns content that cannot be shaped into valid
        frontmatter.
        """
        # Local import keeps the module-level dependency graph lean: the
        # generator pulls in provider typing only at call time.
        from miniunicorn.agent.agent_generator import AgentGenerator, extract_name

        if not provider:
            raise ValueError("provider is required for agent generation")
        if not description or not description.strip():
            raise ValueError("description must not be empty")

        generator = AgentGenerator(provider=provider, model=model)
        content = await generator.generate(description)
        name = extract_name(content)
        # Validate the extracted name against the same rules used for save_agent
        # so the caller can round-trip the preview into a save without surprises.
        safe_name = _validate_agent_name(name) if name else None
        return {"content": content, "name": safe_name}


router = _AgentsRouter
