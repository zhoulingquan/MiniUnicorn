"""High-level programmatic interface to Erza."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from erza.agent.hook import AgentHook, SDKCaptureHook
from erza.agent.loop import AgentLoop
from erza.composition.agent_app import build_agent_application


@dataclass(slots=True)
class RunResult:
    """Result of a single agent run."""

    content: str
    tools_used: list[str]
    messages: list[dict[str, Any]]


class Erza:
    """Programmatic facade for running the Erza agent.

    Usage::

        bot = Erza.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Erza:
        """Create a Erza instance from a config file.

        Args:
            config_path: Path to ``config.json``.  Defaults to
                ``~/.erza/config.json``.
            workspace: Override the workspace directory from config.
        """
        from erza.config.loader import (
            load_config,
            resolve_config_env_vars,
            set_config_path,
        )
        from erza.config.schema import Config

        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")
            # Set the active config context BEFORE building the loop so that
            # media, artifacts, cron, logs and other runtime data resolve
            # under this instance directory rather than the default
            # ``~/.erza``. Without this, SDK callers passing a custom
            # config_path would still write runtime data to the default
            # instance directory.
            set_config_path(resolved)

        config: Config = resolve_config_env_vars(load_config(resolved))
        if workspace is not None:
            config.agents.defaults.workspace = str(Path(workspace).expanduser().resolve())

        loop = build_agent_application(config)
        return cls(loop)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
    ) -> RunResult:
        """Run the agent once and return the result.

        Args:
            message: The user message to process.
            session_key: Session identifier for conversation isolation.
                Different keys get independent history.
            hooks: Optional lifecycle hooks for this run.
        """
        capture = SDKCaptureHook()
        # Bind hooks to this single turn via process_direct's hooks parameter
        # instead of mutating the loop's shared _extra_hooks. This makes
        # concurrent run() calls with different hooks safe (no cross-talk).
        base_hooks = list(hooks) if hooks is not None else list(self._loop._extra_hooks)
        response = await self._loop.process_direct(
            message,
            session_key=session_key,
            hooks=[capture, *base_hooks],
        )

        content = (response.content if response else None) or ""
        return RunResult(
            content=content,
            tools_used=capture.tools_used,
            messages=capture.messages,
        )
