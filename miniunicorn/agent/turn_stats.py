"""Per-turn runtime statistics bound to the executing task.

``AgentLoop`` historically kept ``_last_usage``, ``_last_call_usage`` and
``_current_iteration`` as instance attributes, so concurrent turns from
different sessions clobbered each other: one session's turn could report
another session's token usage (e.g. ``context_usage`` in webui turn
tracking), and the ``my`` tool could read another session's iteration
progress.

These values are now task-local.  ``_run_agent_loop`` binds a fresh
``TurnStats`` holder for the current task via :func:`bind_turn_stats`;
readers (the loop itself, the ``my`` tool, webui turn tracking) use
:func:`current_turn_stats` and fall back to the legacy instance attributes
when no turn is running in the current task.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

_TURN_STATS_VAR: contextvars.ContextVar["TurnStats | None"] = contextvars.ContextVar(
    "miniunicorn_turn_stats", default=None
)


@dataclass
class TurnStats:
    """Token usage and iteration progress of a single agent turn."""

    iteration: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    last_call_usage: dict[str, int] = field(default_factory=dict)


def bind_turn_stats() -> TurnStats:
    """Create and bind a fresh ``TurnStats`` holder for the current task.

    The binding is task-scoped (contextvars do not propagate into child
    tasks), so one dispatch task per turn is safe.  Re-binding simply
    shadows the previous holder; the old value dies with its task.
    """
    stats = TurnStats()
    _TURN_STATS_VAR.set(stats)
    return stats


def current_turn_stats() -> TurnStats | None:
    """Return the ``TurnStats`` bound to the current task, if any."""
    return _TURN_STATS_VAR.get()
