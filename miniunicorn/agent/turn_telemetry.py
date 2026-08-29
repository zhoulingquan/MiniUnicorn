"""Per-turn telemetry context (Phase 6 convergence).

``_last_usage`` / ``_last_call_usage`` / ``_current_iteration`` previously
lived as cross-session shared mutable state on the loop / response assembler.
Phase 6 moves the *turn-scoped* copies into :class:`TurnTelemetry`, bound per
dispatched turn.  The response snapshot keeps the trailing display surface used
by ``/status`` and the ``my`` tool, which run inside command turns where the
per-turn telemetry is intentionally empty.

The pending-turn-latency mechanism (``ResponseAssembler.pending_turn_latency_ms``)
is intentionally untouched.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptComponentTokens:
    """Estimated per-component prompt tokens for the current iteration.

    Estimates are approximate (char-to-token heuristics) and are used for
    telemetry and pressure signals only, never billing.
    """

    system_prompt: int = 0
    tool_definitions: int = 0
    conversation_history: int = 0
    step_guidance: int = 0  # managed-plan step guidance (P1)
    reflection_context: int = 0
    compacted_context: int = 0  # compacted tool results carved out of history
    total_estimated: int = 0


@dataclass
class TurnTelemetry:
    """Usage / iteration telemetry scoped to a single dispatched turn."""

    usage: dict[str, int] = field(default_factory=dict)
    last_call_usage: dict[str, int] = field(default_factory=dict)
    iteration: int = 0
    # P2-T2: per-component prompt estimates + governance pressure, set by
    # ContextGovernanceService after each governance pass. Typed Any to avoid
    # a circular import with miniunicorn.agent.context_governor.
    prompt_components: PromptComponentTokens | None = None
    governance_pressure: Any | None = None


_current: ContextVar[TurnTelemetry | None] = ContextVar("turn_telemetry", default=None)


def current() -> TurnTelemetry | None:
    """Return the telemetry bound to the current turn, if any."""
    return _current.get()


def bind(turn: TurnTelemetry) -> Any:
    """Bind *turn* for the current context; returns a reset token."""
    return _current.set(turn)


def reset(token: Any) -> None:
    """Reset the context to the value held before :func:`bind`."""
    _current.reset(token)
