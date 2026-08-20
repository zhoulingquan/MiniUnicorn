"""Slash command routing and built-in handlers."""

from miniunicorn.command.builtin import register_builtin_commands
from miniunicorn.command.router import CommandContext, CommandRouter
from miniunicorn.command.service import CommandApplicationService

__all__ = [
    "CommandApplicationService",
    "CommandContext",
    "CommandRouter",
    "register_builtin_commands",
]
