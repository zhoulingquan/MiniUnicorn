# Turn Telemetry

MiniUnicorn emits one structured telemetry record per turn. This document
describes the fields, privacy boundaries, and how to extend the sink.

## Overview

After each turn completes (or is cancelled), `AgentLoop` builds a
`TurnTelemetry` record from the bound `TurnRuntime` and forwards it to a
`TelemetrySink`. The default sink (`LogTelemetrySink`) writes one
structured Loguru event named `turn_completed` with every field bound as
context.

Telemetry is emitted in the success, cancellation, and exception paths.
Sink exceptions are logged and suppressed so a telemetry failure can never
break an outbound turn.

## Fields

### `TurnTelemetry`

| Field | Type | Description |
|-------|------|-------------|
| `turn_id` | `str` | Unique identifier for this turn. |
| `session_key` | `str` | Effective session key (channel-scoped). |
| `queue_wait_ms` | `int` | Wall-clock time spent waiting for the session lock and concurrency permit, in milliseconds. |
| `duration_ms` | `int \| None` | Total turn wall-clock duration, in milliseconds. `None` if the turn was interrupted before completion. |
| `state_durations_ms` | `dict[str, float]` | Per-state breakdown (`restore`, `compact`, `command`, `build`, `run`, `save`, `respond`) showing where the turn spent time. |
| `llm_calls` | `list[LlmCallMetric]` | One entry per provider call within the turn. |
| `tool_calls` | `list[ToolCallMetric]` | One entry per tool execution within the turn. |
| `usage` | `dict[str, int]` | Cumulative token usage across all LLM calls in the turn. |
| `last_call_usage` | `dict[str, int]` | Usage from the final LLM call (actual context window footprint at turn end). |
| `stop_reason` | `str` | Why the turn ended: `completed`, `cancelled`, `error`, `max_iterations`, `tool_error`, `budget_exceeded`, etc. |

### `LlmCallMetric`

| Field | Type | Description |
|-------|------|-------------|
| `iteration` | `int` | ReAct loop iteration index (0-based). |
| `duration_ms` | `float` | Provider call wall-clock duration. |
| `usage` | `dict[str, int]` | Token usage for this call. |
| `finish_reason` | `str \| None` | Provider's finish reason (`stop`, `length`, `tool_calls`, …). |
| `error` | `str \| None` | Exception type name if the call failed (`timeout`, `ConnectionError`, …). |

### `ToolCallMetric`

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Tool name (e.g. `read_file`, `exec`). |
| `duration_ms` | `float` | Tool execution wall-clock duration. |
| `status` | `str` | `ok`, `error`, or `cancelled`. |
| `error` | `str \| None` | Exception type name if the tool raised. |

## Privacy Boundaries

### Included

Telemetry records contain operational metadata only:

- Turn and session identifiers
- Timing (queue wait, duration, per-state breakdown, per-call latency)
- Token usage counts
- Stop reasons and provider finish reasons
- Tool names and bounded error summaries (exception type names, not messages)

### Excluded

Telemetry records NEVER include:

- Prompt text or response content
- Tool arguments or results
- Media (images, audio, video)
- Credentials, API keys, or tokens
- Environment variables
- User-identifiable information beyond the session key

## Custom Sinks

Implement the `TelemetrySink` protocol to route telemetry to an external
system (e.g. OpenTelemetry, Datadog, Prometheus):

```python
from miniunicorn.agent.telemetry import TelemetrySink, TurnTelemetry


class OTLPSink:
    async def emit_turn(self, record: TurnTelemetry) -> None:
        # Export record to your collector...
        ...
```

Pass the sink to `AgentLoop`:

```python
loop = AgentLoop(
    ...,
    telemetry_sink=OTLPSink(),
)
```

If no sink is provided, `LogTelemetrySink` is used by default.
