"""Slash command routing and built-in handlers."""

from erza.command.builtin import register_builtin_commands
from erza.command.router import CommandContext, CommandRouter
from erza.command.service import CommandApplicationService

__all__ = [
    "CommandApplicationService",
    "CommandContext",
    "CommandRouter",
    "register_builtin_commands",
]
