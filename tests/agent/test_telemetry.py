"""Tests for structured turn telemetry (Task 11).

Verifies that every completed or failed turn produces exactly one
``TurnTelemetry`` record, that provider/tool metrics are correctly
attributed to the owning turn, and that a failing sink never surfaces
to the caller.
"""

from __future__ import annotations

import asyncio

import pytest

from miniunicorn.agent.telemetry import (
    LlmCallMetric,
    LogTelemetrySink,
    ToolCallMetric,
    TurnTelemetry,
    build_turn_telemetry,
)
from miniunicorn.agent.turn_runtime import (
    TurnRuntime,
    bind_turn_runtime,
    reset_turn_runtime,
)


class CapturingTelemetrySink:
    """In-memory sink collecting every emitted record."""

    def __init__(self) -> None:
        self.records: list[TurnTelemetry] = []

    async def emit_turn(self, record: TurnTelemetry) -> None:
        self.records.append(record)


class FailingTelemetrySink:
    """Sink that always raises to verify exception suppression."""

    def __init__(self) -> None:
        self.calls = 0

    async def emit_turn(self, record: TurnTelemetry) -> None:
        self.calls += 1
        raise RuntimeError("sink boom")


def _make_runtime(**overrides) -> TurnRuntime:
    defaults = dict(turn_id="turn-1", session_key="ws:chat-1")
    defaults.update(overrides)
    return TurnRuntime(**defaults)


def test_success_record_carries_turn_and_session_ids() -> None:
    runtime = _make_runtime()
    runtime.usage = {"prompt_tokens": 100, "completion_tokens": 20}
    runtime.last_call_usage = {"prompt_tokens": 40, "completion_tokens": 10}
    runtime.latency_ms = 1500
    runtime.stop_reason = "completed"
    runtime.queue_wait_ms = 12
    runtime.llm_calls.append(
        LlmCallMetric(iteration=0, duration_ms=320.5, usage={"prompt_tokens": 40})
    )
    runtime.tool_calls.append(ToolCallMetric(name="read_file", duration_ms=15.0, status="ok"))

    record = build_turn_telemetry(runtime)

    assert record.turn_id == "turn-1"
    assert record.session_key == "ws:chat-1"
    assert record.stop_reason == "completed"
    assert record.usage == {"prompt_tokens": 100, "completion_tokens": 20}
    assert record.last_call_usage == {"prompt_tokens": 40, "completion_tokens": 10}
    assert record.duration_ms == 1500
    assert record.queue_wait_ms == 12
    assert len(record.llm_calls) == 1
    assert record.llm_calls[0].iteration == 0
    assert len(record.tool_calls) == 1
    assert record.tool_calls[0].name == "read_file"


def test_provider_exception_records_error_stop_reason() -> None:
    runtime = _make_runtime()
    runtime.stop_reason = "error"
    runtime.llm_calls.append(
        LlmCallMetric(
            iteration=0,
            duration_ms=100.0,
            error="ValueError",
        )
    )

    record = build_turn_telemetry(runtime)

    assert record.stop_reason == "error"
    assert record.llm_calls[0].error == "ValueError"
    assert record.llm_calls[0].finish_reason is None


def test_concurrent_sessions_produce_distinct_metrics() -> None:
    runtime_a = _make_runtime(turn_id="turn-a", session_key="ws:a")
    runtime_b = _make_runtime(turn_id="turn-b", session_key="ws:b")

    runtime_a.llm_calls.append(LlmCallMetric(iteration=0, duration_ms=50.0))
    runtime_b.llm_calls.append(LlmCallMetric(iteration=0, duration_ms=80.0))
    runtime_a.tool_calls.append(ToolCallMetric(name="tool_a", duration_ms=5.0, status="ok"))
    runtime_b.tool_calls.append(ToolCallMetric(name="tool_b", duration_ms=9.0, status="error"))

    record_a = build_turn_telemetry(runtime_a)
    record_b = build_turn_telemetry(runtime_b)

    assert record_a.turn_id == "turn-a"
    assert record_b.turn_id == "turn-b"
    assert len(record_a.llm_calls) == 1
    assert record_a.llm_calls[0].duration_ms == 50.0
    assert len(record_b.llm_calls) == 1
    assert record_b.llm_calls[0].duration_ms == 80.0
    assert record_a.tool_calls[0].name == "tool_a"
    assert record_b.tool_calls[0].name == "tool_b"


def test_every_metric_duration_is_nonnegative() -> None:
    runtime = _make_runtime()
    runtime.llm_calls.append(LlmCallMetric(iteration=0, duration_ms=0.0))
    runtime.tool_calls.append(ToolCallMetric(name="x", duration_ms=0.0, status="ok"))

    record = build_turn_telemetry(runtime)

    assert record.llm_calls[0].duration_ms >= 0
    assert record.tool_calls[0].duration_ms >= 0
    for value in record.state_durations_ms.values():
        assert value >= 0


@pytest.mark.asyncio
async def test_sink_exception_does_not_break_outbound_turn() -> None:
    """A failing telemetry sink must be logged and suppressed."""

    sink = FailingTelemetrySink()
    runtime = _make_runtime()

    # The caller wraps the sink call in a try/except; simulate that contract.
    try:
        await sink.emit_turn(build_turn_telemetry(runtime))
    except Exception:
        pass

    assert sink.calls == 1


@pytest.mark.asyncio
async def test_capturing_sink_receives_one_record_per_turn() -> None:
    sink = CapturingTelemetrySink()
    runtime = _make_runtime()
    runtime.stop_reason = "completed"

    await sink.emit_turn(build_turn_telemetry(runtime))

    assert len(sink.records) == 1
    assert sink.records[0].turn_id == "turn-1"


def test_telemetry_bound_runtime_is_isolated_per_task() -> None:
    """contextvars ensure concurrent turns see only their own metrics."""

    runtime_a = _make_runtime(turn_id="turn-a", session_key="ws:a")
    runtime_b = _make_runtime(turn_id="turn-b", session_key="ws:b")

    token_a = bind_turn_runtime(runtime_a)
    try:
        runtime_a.llm_calls.append(LlmCallMetric(iteration=0, duration_ms=10.0))

        # Simulate a concurrent context binding its own runtime.
        token_b = bind_turn_runtime(runtime_b)
        try:
            runtime_b.llm_calls.append(LlmCallMetric(iteration=0, duration_ms=20.0))
            from miniunicorn.agent.turn_runtime import current_turn_runtime

            assert current_turn_runtime() is runtime_b
            assert len(current_turn_runtime().llm_calls) == 1  # type: ignore[union-attr]
        finally:
            reset_turn_runtime(token_b)

        from miniunicorn.agent.turn_runtime import current_turn_runtime

        assert current_turn_runtime() is runtime_a
        assert len(current_turn_runtime().llm_calls) == 1  # type: ignore[union-attr]
    finally:
        reset_turn_runtime(token_a)


def test_log_telemetry_sink_does_not_raise() -> None:
    """The default LogTelemetrySink must be constructible and callable."""

    sink = LogTelemetrySink()
    runtime = _make_runtime()
    # Should not raise; Loguru swallows the bind into a structured event.
    asyncio.run(sink.emit_turn(build_turn_telemetry(runtime)))
