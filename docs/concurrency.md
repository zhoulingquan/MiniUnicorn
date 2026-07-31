# Turn Concurrency

MiniUnicorn serializes turns within a session and allows concurrent turns
across different sessions, bounded by the number of Workers. This document
defines the contract that operators and extension authors can rely on.

> [!IMPORTANT]
> The durable Runtime Store (SQLite) is the serialization authority. The
> legacy `process_direct()` entry path and the in-process `TurnDispatcher`
> pending queues are no longer production entry paths — they were removed by
> the hard cutover. Do not recommend `process_direct()` to extension authors.

## Concurrency Contract

- Tasks with the same effective session key are serialized by the Runtime
  Store — they execute one at a time, in `session_sequence` order.
- Tasks with different effective session keys may run concurrently across the
  Workers. In supervised mode (default `workerCount=3`, each Worker fixed at
  `workerConcurrency=1`) this means up to 3 active tasks by default. In
  lightweight mode, concurrency is bounded by `lightweightExecutionSlots`
  (default `1`, range `1..3`).
- Waiting for a same-session slot does not consume a Worker's execution slot.
- Per-turn iteration, usage, hooks, latency, and traces are task-local and are
  reset even when a turn is cancelled or raises.
- Provider rate limits remain global and may further constrain throughput.

## How It Works

### Runtime Store (serialization authority)

The durable Runtime Store (SQLite) is the serialization authority. Incoming
requests become durable tasks in the store; the Control Plane leases tasks to
Workers, and same-session tasks are dispatched in `session_sequence` order.
Because the store — not an in-process lock or pending queue — owns ordering,
serialization survives process restarts and is consistent across all Workers.

The legacy `process_direct()` SDK entry path and the in-process
`TurnDispatcher` pending queues are **removed/legacy** and are no longer
production entry paths. They are mentioned below only for historical context.

### TurnCoordinator (in-process, per-Worker)

Within a single Worker, a `TurnCoordinator` instance owns task-local turn
state. It is no longer the cross-process serialization authority — that role
belongs to the Runtime Store:

1. **Per-session locks** — one `asyncio.Lock` per effective session key,
   held weakly so idle sessions are garbage-collected. Same-session ordering
   is already guaranteed by the store's `session_sequence`; the in-process
   lock guards against re-entrancy within a Worker.

2. **Concurrency gate** — bounded by the Worker count and
   `workerConcurrency` (fixed at `1`) in supervised mode, or by
   `lightweightExecutionSlots` in lightweight mode. A task waiting for its
   session slot does not consume a Worker's execution slot.

### TurnRuntime

Each turn's mutable state — turn ID, iteration count, cumulative usage,
last-call usage, latency, stop reason, and tool/LLM call logs — lives in a
`TurnRuntime` dataclass bound to the current task via `contextvars`. This
ensures concurrent turns never share mutable state.

The runtime is bound inside the coordinator's `scope()` and is
automatically reset when the scope exits, even if the turn is cancelled or
raises an exception.

### Turn Telemetry

After each turn, `AgentLoop` builds one `TurnTelemetry` record from the
bound `TurnRuntime` and forwards it to a `TelemetrySink`. The record
captures turn-level timing, per-state duration breakdown, per-call LLM and
tool metrics, cumulative usage, and stop reason. Telemetry is emitted in
the success, cancellation, and exception paths; sink failures are logged
and suppressed so they can never break an outbound turn.

See [Telemetry](telemetry.md) for the full field reference and privacy
boundaries.

### Background Task Supervision

Durable maintenance work — DREAM, consolidation, indexing, retention, blob
GC, WAL checkpoint, and backup — dispatches through the durable TaskService
as `MAINTENANCE`-kind tasks executed by Workers, not as untracked background
coroutines. This keeps maintenance observable, lease-protected, and restart-safe.

Within a single turn, fire-and-forget jobs are still tracked by
`TaskSupervisor` instances owned by `AgentLoop` and `AgentRunner` respectively.
The supervisor:

- Holds strong references to every spawned task so the garbage collector
  cannot reclaim a running task before it completes.
- Logs any unhandled exception exactly once via a done-callback.
- Drains or cancels all outstanding tasks on shutdown via `close()`.

`AgentLoop.close_mcp()` drains the loop's background supervisor (30-second
timeout) and then calls `AgentRunner.aclose()` to drain the reflection
supervisor (10-second timeout). Stuck tasks are force-cancelled after the
timeout expires.

### Self-Tool Compatibility

The `/my` tool's `_current_iteration` and `_last_usage` keys are read-only
properties backed by the bound `TurnRuntime`. During a running turn, they
reflect that turn's own values. Outside a turn (no runtime bound), they
return `0` and `{}` respectively.

## Configuration

Cross-session concurrency is now controlled by the `runtime` config block, not
by an environment variable. See [Configuration → Runtime](configuration.md#runtime)
for the full field list.

| Option | Default | Description |
|--------|---------|-------------|
| `runtime.workerCount` | `3` | Number of Worker child processes in supervised mode. With `workerConcurrency=1` (fixed) this is the cap on concurrently active tasks across different sessions. Minimum `2`. |
| `runtime.lightweightExecutionSlots` | `1` | Execution slots in lightweight mode. Range `1..3`. |
| `runtime.workerConcurrency` | `1` | Per-Worker task concurrency. Fixed at `1` — do not raise. |

> The legacy `MINIUNICORN_MAX_CONCURRENT_REQUESTS` environment variable is
> superseded by the `runtime` block above. Tune concurrency through
> `runtime.workerCount` (supervised) or `runtime.lightweightExecutionSlots`
> (lightweight) instead.

## What This Means for Extension Authors

- **Do not** store per-turn state on the `AgentLoop` instance. Use
  `current_turn_runtime()` to access the bound `TurnRuntime` for the
  current turn.
- **Do not** assume a turn's state persists after the turn completes. The
  `TurnRuntime` is unbound when the coordinator scope exits.
- **Do not** use `process_direct()` — it is a removed/legacy entry path. The
  durable Runtime Store is the serialization authority; submit work through
  the normal channel/ingress path so it becomes a durable task leased to a
  Worker. Same-session serialization is handled by the store's
  `session_sequence`, not by an in-process call.
- **Do** be aware that provider rate limits are global and may further
  constrain throughput regardless of the Worker count.

## Agent loop ownership

Workers own Agent execution — they drive the agent loop, make Provider calls,
execute tools through the ToolGateway, and commit the session. A task-local
`TurnRuntime` is bound while a Worker drives a normal turn.

The durable Runtime Store — not the legacy `TurnDispatcher` pending queues —
is the serialization authority. `TurnDispatcher` and its task registries are
process-lifecycle compatibility state only, not the production entry path;
iteration, usage, latency, hooks, and state traces never live in those
registries.

See [Agent Loop Architecture](agent-loop-architecture.md) for the full facade
ownership table, compatibility call chain, and downstream batch guidance.
