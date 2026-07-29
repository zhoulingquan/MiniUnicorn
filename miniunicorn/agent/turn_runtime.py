"""Per-turn runtime state bound through ``contextvars``.

A ``TurnRuntime`` owns all mutable data that is meaningful only for one turn
(turn ID, iteration, usage, latency, hooks, telemetry metrics). It is bound
inside the :class:`~miniunicorn.agent.turn_coordinator.TurnCoordinator` scope
so concurrent turns never share mutable state.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

SESSION_LAST_USAGE_KEY = "last_usage"
SESSION_LAST_CALL_USAGE_KEY = "last_call_usage"


@dataclass(slots=True)
class TurnRuntime:
    turn_id: str
    session_key: str
    iteration: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    last_call_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int | None = None
    queue_wait_ms: int = 0
    stop_reason: str = ""
    state_durations_ms: dict[str, float] = field(default_factory=dict)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


_CURRENT_TURN: ContextVar[TurnRuntime | None] = ContextVar(
    "miniunicorn_current_turn",
    default=None,
)


def bind_turn_runtime(runtime: TurnRuntime) -> Token[TurnRuntime | None]:
    return _CURRENT_TURN.set(runtime)


def reset_turn_runtime(token: Token[TurnRuntime | None]) -> None:
    _CURRENT_TURN.reset(token)


def current_turn_runtime() -> TurnRuntime | None:
    return _CURRENT_TURN.get()


def require_turn_runtime() -> TurnRuntime:
    runtime = current_turn_runtime()
    if runtime is None:
        raise RuntimeError("No turn runtime is bound to the current task")
    return runtime
