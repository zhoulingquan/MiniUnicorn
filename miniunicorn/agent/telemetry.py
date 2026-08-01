"""Structured turn telemetry (Task 11).

Defines typed metric dataclasses for LLM and tool calls, a
:class:`TurnTelemetry` record summarizing one turn, a
:class:`TelemetrySink` protocol, and a default :class:`LogTelemetrySink`
that emits one structured Loguru event named ``turn_completed``.

The runner appends :class:`LlmCallMetric` / :class:`ToolCallMetric`
entries to the currently bound :class:`~miniunicorn.agent.turn_runtime.TurnRuntime`
as each call completes. After the turn finishes (or is cancelled),
``AgentLoop`` builds one :class:`TurnTelemetry` from the runtime and
forwards it to the configured sink. Sink exceptions are logged and
suppressed so a telemetry failure can never break an outbound turn.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from loguru import logger

from miniunicorn.agent.turn_runtime import TurnRuntime


@dataclass(slots=True)
class LlmCallMetric:
    """One provider (LLM) call within a turn."""

    iteration: int
    duration_ms: float
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ToolCallMetric:
    """One tool execution within a turn."""

    name: str
    duration_ms: float
    status: str
    error: str | None = None


@dataclass(slots=True)
class TurnTelemetry:
    """Final telemetry record emitted once per turn."""

    turn_id: str
    session_key: str
    queue_wait_ms: int
    duration_ms: int | None
    state_durations_ms: dict[str, float]
    llm_calls: list[LlmCallMetric]
    tool_calls: list[ToolCallMetric]
    usage: dict[str, int]
    last_call_usage: dict[str, int]
    stop_reason: str


class TelemetrySink(Protocol):
    """Protocol every telemetry sink must satisfy."""

    async def emit_turn(self, record: TurnTelemetry) -> None: ...


class LogTelemetrySink:
    """Default sink: one structured Loguru event per turn.

    Writes a ``turn_completed`` log event with every telemetry field bound
    as structured context. No network dependency.
    """

    async def emit_turn(self, record: TurnTelemetry) -> None:
        logger.bind(**asdict(record)).info("turn_completed")


def build_turn_telemetry(runtime: TurnRuntime) -> TurnTelemetry:
    """Build a :class:`TurnTelemetry` snapshot from a bound runtime.

    Safe to call from the coordinator scope after
    :func:`~miniunicorn.agent.turn_runtime.complete_turn_runtime` has copied
    cumulative metrics from the turn context.
    """

    return TurnTelemetry(
        turn_id=runtime.turn_id,
        session_key=runtime.session_key,
        queue_wait_ms=max(0, runtime.queue_wait_ms),
        duration_ms=runtime.latency_ms,
        state_durations_ms=dict(runtime.state_durations_ms),
        llm_calls=list(runtime.llm_calls),
        tool_calls=list(runtime.tool_calls),
        usage=dict(runtime.usage),
        last_call_usage=dict(runtime.last_call_usage),
        stop_reason=runtime.stop_reason or "unknown",
    )
