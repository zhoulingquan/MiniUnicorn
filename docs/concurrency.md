# Turn Concurrency

MiniUnicorn serializes turns within a session and allows concurrent turns
across different sessions, bounded by a global concurrency limit. This
document defines the contract that operators and extension authors can rely
on.

## Concurrency Contract

- Calls with the same effective session key are serialized, regardless of whether
  they enter through the message bus or `process_direct()`.
- Calls with different effective session keys may run concurrently up to
  `max_concurrent_requests`.
- Waiting for a same-session lock does not consume a global concurrency slot.
- Per-turn iteration, usage, hooks, latency, and traces are task-local and are
  reset even when a turn is cancelled or raises.
- Provider rate limits remain global and may further constrain throughput.

## How It Works

### TurnCoordinator

A single `TurnCoordinator` instance owns:

1. **Per-session locks** — one `asyncio.Lock` per effective session key,
   held weakly so idle sessions are garbage-collected. Both the message-bus
   dispatch path (`_dispatch`) and the direct SDK path (`process_direct`)
   acquire the same per-session lock, ensuring same-session calls serialize
   across entry points.

2. **Global concurrency gate** — an `asyncio.Semaphore` sized by
   `max_concurrent_requests`. Lock acquisition always precedes semaphore
   acquisition inside the coordinator's `scope()` context manager, so a task
   waiting for its session lock does not consume a global permit.

### TurnRuntime

Each turn's mutable state — turn ID, iteration count, cumulative usage,
last-call usage, latency, stop reason, and tool/LLM call logs — lives in a
`TurnRuntime` dataclass bound to the current task via `contextvars`. This
ensures concurrent turns never share mutable state.

The runtime is bound inside the coordinator's `scope()` and is
automatically reset when the scope exits, even if the turn is cancelled or
raises an exception.

### Self-Tool Compatibility

The `/my` tool's `_current_iteration` and `_last_usage` keys are read-only
properties backed by the bound `TurnRuntime`. During a running turn, they
reflect that turn's own values. Outside a turn (no runtime bound), they
return `0` and `{}` respectively.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `MINIUNICORN_MAX_CONCURRENT_REQUESTS` | `3` | Maximum number of concurrent turns across all sessions. Set to `0` or a negative value for unlimited concurrency. |

### Setting the limit

```bash
# Allow up to 5 concurrent turns
export MINIUNICORN_MAX_CONCURRENT_REQUESTS=5

# Unlimited (use with caution — no backpressure)
export MINIUNICORN_MAX_CONCURRENT_REQUESTS=0
```

## What This Means for Extension Authors

- **Do not** store per-turn state on the `AgentLoop` instance. Use
  `current_turn_runtime()` to access the bound `TurnRuntime` for the
  current turn.
- **Do not** assume a turn's state persists after the turn completes. The
  `TurnRuntime` is unbound when the coordinator scope exits.
- **Do** use `process_direct()` for SDK calls — it routes through the same
  coordinator as bus dispatch, so same-session calls are automatically
  serialized.
- **Do** be aware that provider rate limits are global and may further
  constrain throughput regardless of the `max_concurrent_requests` setting.
