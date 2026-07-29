# MiniUnicorn Long-Running Agent Runtime Design

Date: 2026-07-29

Status: Approved design

Audience: implementation agents and reviewers

## 1. Purpose

This design strengthens MiniUnicorn for unattended, long-running operation on
one machine. It preserves the existing Agent outer state machine and inner
LLM/tool loop while adding durable scheduling, process isolation, resumable
checkpoints, side-effect safety, bounded resources, and operational visibility.

The selected approach is:

> One reliability protocol, one local durable task ledger, and two deployment
> modes: a lightweight single-process mode and a supervised three-worker mode.

The stable production topology is:

- one Supervisor process;
- one Control Plane process;
- three Worker processes;
- one active root task per Worker;
- one local `runtime.sqlite` operational ledger;
- the existing session and memory files;
- the existing `memory/memory.db` vector-memory index.

This is not a distributed cluster design. All processes run on one host and
share one local workspace.

## 2. Existing Baseline

The implementation must build on, rather than replace, these existing
capabilities:

- `TurnExecutor` drives
  `RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE`.
- `AgentRunner` performs the inner LLM/tool loop.
- `ContextGovernor` governs model context before LLM calls.
- `TurnRuntime` uses task-local state and prevents cross-turn usage leakage.
- `TurnCoordinator` serializes the same session and limits in-process
  concurrency.
- `TurnTelemetry` emits bounded, content-free per-turn metrics.
- typed Agent events validate outbound event payloads.
- `turn_persistence.py` stores current runtime checkpoints in session metadata.
- `TaskSupervisor` owns and drains process-local background coroutines.
- provider fallback and turn budgets already exist.
- session, memory, and cron writes already use atomic-write techniques in
  several important paths.
- `MemoryStore`, `Consolidator`, and `Dream` manage durable memory files.
- `memory/memory.db` is the optional `sqlite-vec` index for semantic recall.

The principal gaps are:

- accepted turns do not have a host-crash-safe durable queue;
- current session locks and the global semaphore are process-local;
- runtime checkpoints are not a complete cross-process task journal;
- a restored pending tool call becomes a synthetic error instead of being
  classified and safely resumed;
- required background work can still be owned only by an in-memory coroutine;
- tool permission, idempotency, and uncertain-side-effect handling are not one
  mandatory execution boundary;
- Channels are coupled closely enough to turn execution that a Worker cannot
  be treated as replaceable compute;
- process liveness, task liveness, and user-visible delivery are not modeled as
  separate durable concerns.

## 3. Goals

The first production version must satisfy all of the following:

1. A task acknowledged as accepted is not lost after a process or host restart.
2. The same session remains strictly ordered.
3. Three Workers can execute three unrelated root tasks concurrently.
4. A fourth runnable task waits in the durable queue.
5. A dead Worker is replaceable; its task resumes from the last safe
   checkpoint.
6. A Worker with no heartbeat for 180 seconds loses its lease.
7. A live task with no progress for 600 seconds is treated as stalled.
8. A stalled safe operation receives at most one automatic recovery attempt.
9. A dangerous or non-idempotent external action is never silently repeated
   when its outcome is unknown.
10. Final replies survive Channel or Control Plane outages.
11. Required maintenance work survives process restarts.
12. CPU, memory, Token, iteration, subagent, and Provider use are bounded.
13. The runtime can complete a seven-day fault-injected soak without unbounded
    growth in memory, threads, file handles, background tasks, or SQLite WAL.
14. Lightweight and supervised modes use the same task, checkpoint, tool, and
    delivery contracts.

## 4. Non-Goals

This design does not authorize:

- multi-host scheduling;
- Redis, RabbitMQ, Kafka, or another external queue;
- replacing SQLite with PostgreSQL;
- replacing the existing outer or inner Agent loops;
- replacing `memory/memory.db` with another vector database;
- building an external document knowledge base;
- unrestricted multi-tenant hosting;
- exactly-once guarantees from external services that provide neither an
  idempotency key nor a reliable receipt;
- storing every streamed LLM token durably;
- restoring a vanished Python stack frame or coroutine object;
- a new Channel-specific implementation of the Agent core;
- opportunistic refactors unrelated to runtime reliability.

Multiple independent users may share the same service only when their
principal, workspace, Agent, and session scopes are separated. A supervised
deployment is not automatically a secure multi-tenant deployment.

## 5. Non-Negotiable Invariants

The implementation must preserve these invariants:

1. SQLite is the authority for current operational task ownership.
2. `task_events` is append-only.
3. Only the holder of the current lease token and lease epoch may advance a
   running task.
4. A stale Worker result is rejected even if the old process becomes responsive
   again.
5. The same session has at most one active root task.
6. A waiting, retrying, or recovering task continues to hold its logical place
   in the session order.
7. An LLM response is resumable only after the complete response is persisted.
8. A tool result is reusable only after its terminal result is persisted.
9. Every external write passes through Tool Gateway.
10. Every final reply passes through the durable outbox.
11. Required work is never owned only by `asyncio.create_task()`.
12. Telemetry and ordinary logs never contain prompt content, response
    content, tool arguments, tool results, credentials, or media.
13. `memory/memory.db` remains separate from `runtime.sqlite`.
14. Agent Core does not import SQLite infrastructure.
15. A database transaction never remains open across an LLM, tool, Channel, or
    filesystem call.

## 6. Deployment Modes

### 6.1 Lightweight mode

Lightweight mode runs Gateway, scheduling, execution, outbox, and maintenance
inside one process. It still uses:

- `runtime.sqlite`;
- the durable task model;
- lease and fencing checks;
- Turn Journal;
- Tool Gateway;
- durable outbox;
- the same recovery code as supervised mode.

The default lightweight execution-slot count is one. It may be configured up
to three, but extra in-process slots do not provide process isolation.

CLI, tests, and development launchers default to lightweight mode unless
explicitly configured otherwise.

### 6.2 Supervised mode

The long-running service launcher defaults to supervised mode:

```text
Supervisor
├── Control Plane
│   ├── Channel Gateway
│   ├── Task service
│   ├── Outbox sender
│   ├── Control-message handler
│   └── realtime AgentEvent bridge
├── Worker 1
├── Worker 2
└── Worker 3
```

Each Worker executes one root task at a time. Workers may use bounded
subagents, but subagents count against root-task and global quotas.

Worker processes use spawn semantics on every supported platform. They do not
inherit SQLite connections, Provider clients, event loops, open Channel
sockets, or mutable Agent instances from Supervisor.

### 6.3 Same engine, different assembly

No `if supervised: use a different Agent` branch is allowed. Deployment mode
may change:

- process boundaries;
- IPC transport;
- number of execution slots;
- process monitoring.

It may not change:

- task states;
- checkpoint formats;
- lease rules;
- tool safety rules;
- outbox semantics;
- session ordering;
- Agent outer or inner loop behavior.

## 7. Component Boundaries

### 7.1 Channel Gateway

Responsibilities:

- authenticate or identify the Channel sender;
- normalize inbound Channel payloads;
- create stable request identifiers;
- submit an `InboundTaskEnvelope`;
- acknowledge acceptance only after the task transaction commits;
- forward transient typed Agent events;
- deliver durable outbox messages.

Forbidden responsibilities:

- calling `AgentLoop` or `AgentRunner` directly;
- deciding tool permissions;
- owning task recovery;
- writing session history.

### 7.2 Task Service and Durable Scheduler

Responsibilities:

- inbound deduplication;
- per-session sequence allocation;
- priority and availability ordering;
- atomic task claim;
- session-slot ownership;
- task lease renewal;
- retry and recovery scheduling;
- terminal task transitions.

The scheduler is a logical module backed by SQLite. It need not be a dedicated
process. Workers pull work through this module. No correctness property may
depend on an in-memory dispatch queue.

### 7.3 Supervisor

Responsibilities:

- start and monitor Control Plane and Worker processes;
- assign Worker generation numbers;
- restart failed processes;
- quarantine crash-looping Worker slots;
- coordinate drain and shutdown;
- expose process-health information.

Forbidden responsibilities:

- interpreting Agent checkpoints;
- retrying tools;
- writing user replies;
- deciding whether an external side effect occurred.

### 7.4 Worker Host

Responsibilities:

- claim one task;
- bind one isolated `TurnRuntime`;
- restore the latest durable checkpoint;
- run `TurnExecutor` and `AgentRunner`;
- renew its lease and report progress;
- stop on lease loss;
- release or complete the task at a safe boundary.

A Worker is replaceable compute, not a backup copy of session or memory data.

### 7.5 Agent Core

Agent Core contains:

- `TurnExecutor`;
- `AgentLoop`;
- `AgentRunner`;
- `ContextGovernor`;
- Planner and Agent strategies;
- hook and progress abstractions.

It reasons and emits typed decisions. It reads or writes durable runtime state
only through ports supplied by the host.

### 7.6 Tool Gateway

Responsibilities:

- tool lookup and schema validation;
- permission policy;
- risk classification;
- idempotency keys;
- approval;
- durable tool-call state;
- resource locks;
- retry classification;
- timeout and cancellation;
- result normalization.

No Agent or subagent may execute an external-write tool through another path.

### 7.7 Session and Memory Stores

Responsibilities remain with existing storage:

- session JSONL: conversation;
- `history.jsonl`: consolidated historical summaries;
- `SOUL.md`, `USER.md`, and `MEMORY.md`: durable meaning;
- GitStore: version history;
- `memory.db`: semantic recall index.

They do not own task leases, task retries, or delivery state.

### 7.8 Outbox

Responsibilities:

- persist final and approval messages before delivery;
- lease messages to a sender;
- retry supported Channel delivery;
- record Provider message receipts;
- expose unknown delivery outcomes.

Worker code never sends a final response directly to a Channel.

## 8. Contract and Identifier Model

All cross-component models use strict typed validation and reject unknown
fields. Contracts carry `protocol_version=1`.

### 8.1 Identifiers

The runtime uses separate identifiers for separate concerns:

- `channel_message_id`: identifier supplied by a Channel;
- `task_id`: one durable scheduled unit;
- `turn_id`: one logical user/assistant turn;
- `event_id`: one immutable durable or realtime event;
- `llm_call_id`: one persisted model request decision;
- `tool_call_id`: one logical tool action from a persisted LLM response;
- `approval_id`: one user decision request;
- `delivery_id`: one logical outbound message;
- `worker_id`: stable Worker slot identity;
- `worker_generation`: process incarnation of a Worker slot;
- `lease_token`: unguessable token for the current claim;
- `lease_epoch`: monotonically increasing claim number for a task.

UUID4 is sufficient for globally unique IDs. Ordering uses explicit integer
sequence and timestamp fields, not lexical UUID order.

`tool_call_id` is a runtime-generated ID. A Provider-supplied tool-call ID is
stored separately and namespaced by LLM call and ordinal; it is not assumed to
be globally unique.

### 8.2 Time representation

Persistent timestamps use UTC Unix milliseconds in SQLite. Human-readable
ISO-8601 timestamps may be derived at API boundaries.

Lease comparisons use database wall time. In-process duration metrics use a
monotonic clock. Code must not compare monotonic values written by different
processes.

### 8.3 Inbound task envelope

Required fields:

- protocol version;
- Channel and Channel account;
- Channel message ID;
- tenant, principal, Agent, workspace, and session scope;
- normalized user content reference;
- media references;
- task kind;
- priority;
- received time;
- trace identifiers;
- optional reply-to and control metadata.

The full user payload is stored in protected runtime data. The queue row stores
only references, hashes, routing metadata, and bounded summaries.

### 8.4 Durable event envelope

Required fields:

- protocol version;
- event ID;
- task ID and turn ID;
- event sequence;
- task attempt number;
- Worker ID and generation when applicable;
- lease epoch when applicable;
- event type;
- payload reference;
- UTC creation time.

### 8.5 Port contracts

The initial ports are:

- `TaskQueue.accept(envelope) -> AcceptResult`
- `TaskQueue.enqueue_internal(envelope) -> EnqueueResult`
- `TaskQueue.claim(worker) -> TaskLease | None`
- `TaskQueue.renew(lease) -> RenewResult`
- `TaskQueue.release(lease, reason, available_at) -> None`
- `TaskQueue.pause(lease, approval) -> None`
- `TaskQueue.complete(lease, result) -> None`
- `TurnJournal.append(lease, expected_version, event) -> new_version`
- `CheckpointStore.load(task_id) -> TurnCheckpoint | None`
- `CheckpointStore.save(lease, expected_version, checkpoint) -> new_version`
- `ToolGateway.execute(context, request) -> ToolOutcome`
- `Outbox.enqueue(task_id, message) -> delivery_id`
- `Outbox.claim(sender_id) -> DeliveryLease | None`
- `Outbox.finish(lease, outcome) -> None`
- `WorkerRegistry.heartbeat(worker, task, progress) -> None`
- `ResourceQuota.acquire(subject, resource) -> QuotaLease`

Python names may vary during implementation, but the separation and semantics
may not.

## 9. Task, Session, and Lease State

### 9.1 Task states

Allowed task states:

- `QUEUED`
- `LEASED`
- `RUNNING`
- `WAITING_USER`
- `RETRY_WAIT`
- `COMPLETED`
- `FAILED`
- `CANCELLED`

Terminal states are `COMPLETED`, `FAILED`, and `CANCELLED`.

Worker death is not a task state. A recoverable task returns to `QUEUED` or
`RETRY_WAIT`. A side effect with an unknown outcome enters `WAITING_USER`.

### 9.2 Allowed transitions

```text
QUEUED      -> LEASED | CANCELLED
LEASED      -> RUNNING | QUEUED | CANCELLED
RUNNING     -> COMPLETED | RETRY_WAIT | WAITING_USER | FAILED | CANCELLED
RETRY_WAIT  -> QUEUED | CANCELLED | FAILED
WAITING_USER-> QUEUED | CANCELLED | FAILED
```

No other transition is valid. The transition table is centralized and tested.

### 9.3 Session ordering

Gateway atomically assigns a monotonically increasing `session_sequence`.

`session_slots` stores one `active_task_id`. The earliest non-terminal task
becomes active. That task remains the active task while it is:

- leased;
- running;
- waiting for retry;
- waiting for user approval;
- recovering.

Normal later messages wait behind it. Approval, cancellation, and stop messages
use the control path and may target the active task immediately.

On terminal completion, the session slot is cleared and the next sequence may
run.

### 9.4 Lease fencing

A claim creates:

- `leased_by`;
- `lease_token`;
- `lease_epoch += 1`;
- `lease_until`;
- `state_version += 1`.

Every running write checks all of:

- task ID;
- Worker ID;
- Worker generation;
- lease token;
- lease epoch;
- expected state version.

If any check fails, the write returns `LEASE_LOST` and the Worker stops before
performing another LLM or tool action.

Fencing prevents stale local writes. External side effects still require
idempotency or uncertain-outcome handling because a database fence cannot
unsend a network request already accepted by a third party.

## 10. Outer and Inner Agent Loops

### 10.1 Outer turn state machine

The durable path is:

```text
RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
```

Each completed state writes a checkpoint event before advancing.

#### RESTORE

- load session, user, Soul, Memory, Goal, and legacy checkpoint;
- inspect whether the turn was partially committed;
- record session and memory source revisions;
- load the latest durable turn checkpoint.

This state is deterministic and does not call the primary chat model.

#### COMPACT

- enforce context budgets;
- run deterministic consolidation and repair first;
- use existing summaries and embedding recall;
- fall back safely when recall is unavailable.

Any LLM-assisted compaction is an explicit bounded call with its own
`llm_call_id`, timeout, budget, result, and checkpoint. Its failure falls back
to deterministic compaction.

#### COMMAND

- recognize built-in commands;
- produce a typed shortcut result where applicable.

A shortcut must still pass through durable save and outbox steps. The durable
transition is `COMMAND -> SAVE`, not an unrecorded jump to `DONE`.

#### BUILD

- create an immutable `context_snapshot`;
- record source revisions, selected memory IDs, tools, Provider/model, and
  context-governor version;
- store the protected serialized context and its hash.

Recovery after `CONTEXT_BUILT` uses the same snapshot. It does not perform a new
memory search that could produce a different prompt.

#### RUN

- invoke `AgentRunner` with the context snapshot;
- pass budgets, Tool Gateway, checkpoint callback, progress callback, and
  control-message callback.

The user's main reasoning happens here, inside the inner loop.

#### SAVE

- prepare the exact user and assistant messages;
- write them idempotently with `turn_id`;
- persist usage and stop reason;
- advance the session version.

#### RESPOND

- enqueue the final response or approval prompt in outbox;
- do not call Channel code.

#### DONE

- mark the root task complete;
- clear the session slot;
- release leases;
- schedule retention and maintenance work.

### 10.2 Inner LLM/tool loop

Each iteration performs:

1. check cancellation, lease, and budget;
2. govern the current model messages;
3. create and persist `LLM_STARTED`;
4. call the Provider;
5. persist the complete response as `LLM_COMPLETED`;
6. parse final response or tool requests;
7. if final, persist `FINAL_RESPONSE` and return;
8. if tools, persist `TOOL_PROPOSED` records;
9. call Tool Gateway;
10. persist each terminal tool result;
11. append normalized tool messages;
12. checkpoint and continue.

Partial streaming output is transient. It may be displayed, but it does not
become a resumable model result.

### 10.3 Planner, Reflection, Dream, and injection

- Planner is an optional inner-loop strategy. It may propose steps but may not
  bypass Tool Gateway.
- Reflection is durable background work and never delays the final user reply.
- Dream and Consolidator are durable low-priority maintenance tasks.
- ordinary new user messages become later session tasks;
- explicit steering may be injected only after it is durably recorded and only
  at an LLM/tool boundary;
- cancellation and approval are control events, not ordinary queued chat.

## 11. Checkpoint and Recovery Semantics

The runtime restores known business state, not Python execution state.

Recovery uses this matrix:

| Last durable fact | Recovery action |
|---|---|
| Task accepted only | Start at `RESTORE` |
| `RESTORED` | Continue at `COMPACT` |
| `COMPACTED` | Continue at `COMMAND` |
| `CONTEXT_BUILT` | Reuse snapshot and enter `RUN` |
| `LLM_STARTED` only | Retry the LLM call under Provider retry policy |
| `LLM_COMPLETED` | Parse stored response; do not call the model again |
| `TOOL_PROPOSED` | Re-run policy and idempotency checks |
| `TOOL_RUNNING` | Apply tool risk recovery matrix |
| `TOOL_SUCCEEDED` | Reuse stored result |
| `FINAL_RESPONSE` | Skip AgentRunner and enter `SAVE` |
| `TURN_COMMIT_PREPARED` | Inspect session by `turn_id` and finish commit |
| `TURN_SAVED` | Enter `RESPOND` |
| `RESPONSE_READY` | Leave delivery to outbox |

Checkpoints store:

- outer state;
- inner iteration;
- next action;
- context reference and hash;
- persisted assistant message reference;
- completed tool result references;
- active plan reference;
- cumulative usage;
- stop reason;
- source versions;
- created time.

Checkpoint writes are compare-and-set operations protected by the active lease.

## 12. Runtime SQLite Ledger

### 12.1 Location and connection settings

The default file is:

```text
workspace/runtime/runtime.sqlite
```

The directory is private runtime state and is excluded from Git.

Every process uses its own connection or connection pool with:

- `journal_mode=WAL`;
- `synchronous=FULL`;
- `foreign_keys=ON`;
- `busy_timeout=5000`;
- short explicit transactions;
- bounded retry with jitter for `BUSY` errors.

The file must reside on a local disk. Network shares are unsupported.

### 12.2 Schema ownership

Control Plane is the migration leader. It takes a single migration lock,
applies ordered schema migrations, verifies the resulting version, and only
then reports ready. Workers refuse to start task execution against an unknown
schema version.

### 12.3 Required tables

#### `tasks`

- `task_id TEXT PRIMARY KEY`
- `turn_id TEXT UNIQUE`
- `tenant_id TEXT NOT NULL`
- `principal_id TEXT NOT NULL`
- `agent_id TEXT NOT NULL`
- `workspace_id TEXT NOT NULL`
- `session_key TEXT NOT NULL`
- `session_sequence INTEGER NOT NULL`
- `channel TEXT`
- `channel_message_id TEXT`
- `task_kind TEXT NOT NULL`
- `priority INTEGER NOT NULL`
- `payload_ref TEXT NOT NULL`
- `payload_hash TEXT NOT NULL`
- `state TEXT NOT NULL`
- `checkpoint_phase TEXT`
- `attempt_count INTEGER NOT NULL`
- `max_attempts INTEGER NOT NULL`
- `leased_by TEXT`
- `worker_generation INTEGER`
- `lease_token TEXT`
- `lease_epoch INTEGER NOT NULL`
- `lease_until_ms INTEGER`
- `last_progress_at_ms INTEGER`
- `available_at_ms INTEGER NOT NULL`
- `state_version INTEGER NOT NULL`
- `error_code TEXT`
- `error_summary TEXT`
- `created_at_ms INTEGER NOT NULL`
- `updated_at_ms INTEGER NOT NULL`
- `completed_at_ms INTEGER`

Unique constraints:

- `(channel, channel_message_id)` when both values are non-null;
- `(session_key, session_sequence)`.

User turns require `turn_id`, `channel`, and `channel_message_id`. Internal
maintenance tasks leave Channel fields null, use a system-scoped session key,
and use `task_id` as their logical execution identifier. This avoids inventing
fake Channel messages while keeping all tasks in one scheduler.

Indexes cover:

- runnable state, availability, priority, and creation time;
- session and sequence;
- lease expiration;
- last progress;
- task kind and state.

#### `session_slots`

- `session_key TEXT PRIMARY KEY`
- `next_sequence INTEGER NOT NULL`
- `active_task_id TEXT`
- `state_version INTEGER NOT NULL`
- `updated_at_ms INTEGER NOT NULL`

#### `task_events`

- `event_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `event_sequence INTEGER NOT NULL`
- `attempt_no INTEGER NOT NULL`
- `event_type TEXT NOT NULL`
- `step_id TEXT`
- `worker_id TEXT`
- `worker_generation INTEGER`
- `lease_epoch INTEGER`
- `summary TEXT`
- `payload_ref TEXT`
- `created_at_ms INTEGER NOT NULL`

`(task_id, event_sequence)` is unique. Rows are append-only.

#### `checkpoints`

- `checkpoint_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `checkpoint_sequence INTEGER NOT NULL`
- `outer_state TEXT NOT NULL`
- `inner_iteration INTEGER`
- `context_ref TEXT`
- `context_hash TEXT`
- `assistant_message_ref TEXT`
- `completed_tool_results_ref TEXT`
- `next_action TEXT NOT NULL`
- `usage_ref TEXT`
- `source_versions_ref TEXT`
- `created_at_ms INTEGER NOT NULL`

#### `llm_calls`

- `llm_call_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `iteration INTEGER NOT NULL`
- `purpose TEXT NOT NULL`
- `provider TEXT NOT NULL`
- `model TEXT NOT NULL`
- `request_ref TEXT NOT NULL`
- `request_hash TEXT NOT NULL`
- `state TEXT NOT NULL`
- `response_ref TEXT`
- `response_hash TEXT`
- `prompt_tokens INTEGER`
- `completion_tokens INTEGER`
- `cost_usd REAL`
- `started_at_ms INTEGER NOT NULL`
- `finished_at_ms INTEGER`
- `error_code TEXT`

This row represents one logical model decision. Individual network and
fallback attempts are stored separately.

#### `llm_call_attempts`

- `attempt_id TEXT PRIMARY KEY`
- `llm_call_id TEXT NOT NULL`
- `attempt_no INTEGER NOT NULL`
- `provider TEXT NOT NULL`
- `model TEXT NOT NULL`
- `state TEXT NOT NULL`
- `provider_request_id TEXT`
- `started_at_ms INTEGER NOT NULL`
- `finished_at_ms INTEGER`
- `prompt_tokens INTEGER`
- `completion_tokens INTEGER`
- `cost_usd REAL`
- `error_code TEXT`

`(llm_call_id, attempt_no)` is unique.

#### `tool_calls`

- `tool_call_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `iteration INTEGER NOT NULL`
- `tool_name TEXT NOT NULL`
- `arguments_ref TEXT NOT NULL`
- `arguments_hash TEXT NOT NULL`
- `risk_class TEXT NOT NULL`
- `idempotency_key TEXT`
- `state TEXT NOT NULL`
- `approval_id TEXT`
- `resource_key TEXT`
- `result_ref TEXT`
- `result_hash TEXT`
- `external_receipt TEXT`
- `started_at_ms INTEGER`
- `finished_at_ms INTEGER`
- `error_code TEXT`
- `error_summary TEXT`

Non-null idempotency keys are unique.

#### `tool_call_attempts`

- `attempt_id TEXT PRIMARY KEY`
- `tool_call_id TEXT NOT NULL`
- `attempt_no INTEGER NOT NULL`
- `state TEXT NOT NULL`
- `idempotency_key TEXT`
- `external_request_id TEXT`
- `external_receipt TEXT`
- `started_at_ms INTEGER NOT NULL`
- `finished_at_ms INTEGER`
- `error_code TEXT`
- `error_summary TEXT`

`(tool_call_id, attempt_no)` is unique. All attempts for one logical write use
the same idempotency key.

#### `approvals`

- `approval_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `tool_call_id TEXT NOT NULL`
- `state TEXT NOT NULL`
- `question_ref TEXT NOT NULL`
- `requested_at_ms INTEGER NOT NULL`
- `expires_at_ms INTEGER NOT NULL`
- `decided_at_ms INTEGER`
- `decided_by TEXT`
- `decision TEXT`
- `decision_reason TEXT`

#### `outbox`

- `delivery_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `channel TEXT NOT NULL`
- `target_ref TEXT NOT NULL`
- `payload_ref TEXT NOT NULL`
- `payload_hash TEXT NOT NULL`
- `state TEXT NOT NULL`
- `attempt_count INTEGER NOT NULL`
- `available_at_ms INTEGER NOT NULL`
- `leased_by TEXT`
- `lease_token TEXT`
- `lease_until_ms INTEGER`
- `provider_message_id TEXT`
- `last_error TEXT`
- `created_at_ms INTEGER NOT NULL`
- `delivered_at_ms INTEGER`

Allowed outbox states:

- `READY`
- `LEASED`
- `SENDING`
- `RETRY_WAIT`
- `DELIVERED`
- `UNKNOWN`
- `CANCELLED`

The sender writes `SENDING` before the network call and `DELIVERED` only after
a reliable Channel receipt. After a sender crash:

- a Channel supporting an idempotency key receives the same `delivery_id`;
- an explicit failure enters `RETRY_WAIT`;
- a successful receipt already stored remains `DELIVERED`;
- an unsupported or ambiguous send becomes `UNKNOWN` rather than being
  blindly sent again.

#### `workers`

- `worker_id TEXT PRIMARY KEY`
- `generation INTEGER NOT NULL`
- `pid INTEGER NOT NULL`
- `state TEXT NOT NULL`
- `capabilities_ref TEXT`
- `started_at_ms INTEGER NOT NULL`
- `heartbeat_at_ms INTEGER NOT NULL`
- `current_task_id TEXT`
- `last_error TEXT`

#### `resource_locks`

- `resource_type TEXT NOT NULL`
- `resource_key TEXT NOT NULL`
- `holder_id TEXT NOT NULL`
- `lease_token TEXT NOT NULL`
- `lease_until_ms INTEGER NOT NULL`
- `created_at_ms INTEGER NOT NULL`

The primary key is `(resource_type, resource_key)`, which enforces one exclusive
holder for a mutable resource.

#### `quota_leases`

- `quota_lease_id TEXT PRIMARY KEY`
- `resource_type TEXT NOT NULL`
- `scope_key TEXT NOT NULL`
- `holder_id TEXT NOT NULL`
- `lease_token TEXT NOT NULL`
- `lease_until_ms INTEGER NOT NULL`
- `created_at_ms INTEGER NOT NULL`

Quota acquisition counts non-expired rows for `(resource_type, scope_key)` in
one transaction and inserts only when the configured limit permits it.

#### `runtime_blobs`

- `blob_id TEXT PRIMARY KEY`
- `sha256 TEXT NOT NULL`
- `content_type TEXT NOT NULL`
- `codec TEXT NOT NULL`
- `data BLOB NOT NULL`
- `size_bytes INTEGER NOT NULL`
- `created_at_ms INTEGER NOT NULL`
- `expires_at_ms INTEGER`

Runtime blobs are protected content, not telemetry.

### 12.4 Large artifact rule

Bounded JSON payloads and normal context snapshots are compressed and stored in
`runtime_blobs`. Very large file artifacts remain in the controlled workspace
and are referenced by:

- path;
- content hash;
- size;
- creation time.

Recovery validates the path remains in the allowed workspace, the file exists,
and its hash matches. A mismatched artifact is a permanent recovery error, not
permission to read an arbitrary replacement path.

## 13. Transaction Boundaries

### 13.1 Accept

One transaction:

1. detect duplicate Channel message;
2. allocate session sequence;
3. store protected inbound payload;
4. insert `tasks`;
5. append `TASK_ACCEPTED`;
6. commit.

Gateway acknowledges only after commit.

### 13.2 Claim

One `BEGIN IMMEDIATE` transaction:

1. select the highest-priority eligible task;
2. verify the session slot is empty or already names this task;
3. allocate lease token and increment epoch;
4. set `LEASED`;
5. update the session slot;
6. append `TASK_LEASED`;
7. commit.

### 13.3 Checkpoint

One transaction:

1. verify all fencing fields;
2. store referenced blobs;
3. insert checkpoint;
4. append durable event;
5. update task phase, progress, and state version;
6. commit.

### 13.4 Session commit

SQLite and JSONL cannot form one transaction. Use an idempotent recoverable
commit:

1. persist `TURN_COMMIT_PREPARED` with exact messages and `turn_id`;
2. write session history idempotently by `turn_id`;
3. persist `TURN_SAVED`.

Recovery checks whether each message role for the `turn_id` already exists and
writes only missing parts.

### 13.5 Complete and enqueue reply

One transaction:

1. require `TURN_SAVED`;
2. insert outbox row;
3. append `RESPONSE_READY` and `TASK_COMPLETED`;
4. mark task `COMPLETED`;
5. clear session slot;
6. commit.

Logical task completion and Channel delivery are independent.

### 13.6 External tool call

Before the call:

1. classify and approve;
2. persist tool state `RUNNING`;
3. commit.

After the call:

1. persist receipt and result;
2. set a terminal tool state;
3. append event;
4. commit.

No transaction spans the external call.

## 14. Tool Gateway Safety Model

### 14.1 Required tool metadata

Every tool declares:

- risk class;
- approval policy;
- retry policy;
- timeout;
- whether parallel execution is safe;
- whether external idempotency is supported;
- resource-key builder;
- sensitive argument fields;
- progress support.

Legacy tools without metadata default to conservative write behavior.

### 14.2 Risk classes

- `READ_ONLY`
- `IDEMPOTENT_WRITE`
- `NON_IDEMPOTENT_WRITE`
- `DESTRUCTIVE`

### 14.3 Policy outcomes

Policy Engine returns exactly one:

- `ALLOW`
- `DENY`
- `ASK_USER`

Approval binds the exact task, tool call, argument hash, principal, and expiry.
The default approval expiry is 30 minutes.

### 14.4 Idempotency

The runtime derives:

```text
task_id + persisted_llm_call_id + tool_call_id + normalized_argument_hash
```

The model does not choose the idempotency key.

If the external Provider supports idempotency, the same key is forwarded on
every retry. Local atomic file writes detect completion by path and content
hash.

### 14.5 Recovery matrix

| Tool state and class | Automatic action |
|---|---|
| Proposed, not running | Validate and execute |
| Running read-only | Retry |
| Running idempotent write | Retry with same key |
| Running atomic file write | Inspect path and hash, then finish or retry |
| Running non-idempotent write | Mark `UNKNOWN` |
| Running destructive action | Mark `UNKNOWN` |
| Succeeded | Reuse result |
| Explicit retryable failure | Retry within policy |
| Explicit permanent failure | Return failure to Agent |

`UNKNOWN` pauses the task and offers:

- already completed;
- retry;
- skip.

No Worker remains occupied while waiting for the answer.

### 14.6 Tool concurrency

- only explicitly `parallel_safe` read-only calls run concurrently;
- writes are serial by default;
- mutable resources use durable resource locks;
- an unkeyed write tool is globally serial by tool name;
- subagents use the same locks and Tool Gateway.

### 14.7 Retry defaults

- LLM transient request: three total attempts with backoff;
- read-only tool transient failure: three total attempts;
- idempotent write: two total attempts with the same key;
- non-idempotent unknown result: zero automatic retries;
- stalled safe action: one recovery attempt;
- root task: three Worker attempts before pause/failure.

## 15. Existing Memory System

This design does not create another memory database.

The existing memory system remains:

- `SOUL.md`: Agent voice and style;
- `USER.md`: stable user knowledge;
- `memory/MEMORY.md`: durable project facts and decisions;
- `memory/history.jsonl`: append-only consolidated history;
- GitStore: version history;
- `memory/memory.db`: optional `sqlite-vec` semantic-recall index.

`memory.db` is a derived search index, not the operational task ledger.
`runtime.sqlite` is an operational task ledger, not a semantic-memory store.

### 15.1 Multi-process hardening of `memory.db`

The vector store must:

- use a connection per process;
- enable WAL and a bounded busy timeout;
- serialize its own writes;
- use a stable source key and content hash for idempotent indexing;
- record source kind and source revision;
- support tombstones for deleted or superseded content;
- reject mixed embedding-model fingerprints;
- degrade to existing non-vector memory behavior when unavailable.

The implementation extends the existing `VectorMemoryStore`; it does not add a
second vector subsystem.

### 15.2 Context recall

Recall order:

1. current request and Goal;
2. recent complete messages;
3. current session summary;
4. vector candidates filtered by principal, Agent, workspace, and memory kind;
5. source, recency, importance, and similarity reranking;
6. selection within the context budget.

`context_snapshot` records selected memory IDs, hashes, source revisions,
scores, and strategy version.

### 15.3 Consolidator and Dream

Consolidator and Dream become durable low-priority task kinds. They:

- record the source cursor and file revisions they read;
- commit only if source revisions remain compatible;
- never overwrite newer memory from a stale snapshot;
- write GitStore history after successful memory changes;
- enqueue idempotent vector-index updates.

Their existing memory behavior remains authoritative. This design does not add
a parallel `memory_candidate` database as a prerequisite.

## 16. Required Background Work

Durable task kinds include:

- `USER_TURN`
- `MEMORY_CONSOLIDATION`
- `MEMORY_INDEX`
- `REFLECTION`
- `DREAM`
- `MAINTENANCE`

Priority order:

1. cancellation and approval;
2. non-terminal user tasks marked for recovery;
3. user turns;
4. memory consolidation and indexing;
5. reflection, Dream, and cleanup.

Outbox delivery has its own durable delivery queue and sender in Control Plane;
it does not consume one of the three Agent Worker slots.

Recovery normally resumes the original task ID. It is not represented as a
second task that could race or reorder the session.

At most one low-priority maintenance task runs at once. New maintenance is not
claimed while user work is queued. Maintenance yields at checkpoints.

`TaskSupervisor` remains for disposable or reconstructable process-local
coroutines. It is not the sole owner of required work.

## 17. Realtime IPC and Durable Events

### 17.1 Durable events

SQLite stores:

- task transitions;
- complete LLM responses;
- tool transitions and results;
- checkpoints;
- approvals;
- final responses;
- delivery state.

### 17.2 Transient events

A bounded local IPC channel carries:

- Token stream;
- reasoning-progress notifications;
- tool progress;
- UI animations;
- non-critical status updates.

It reuses the existing typed Agent event protocol.

The first implementation uses spawn-safe bounded multiprocessing queues owned
by Supervisor:

- Worker-to-Control-Plane realtime event queue;
- Control-Plane-to-Worker wake/control queue.

Messages are typed serialized values, not live Python Agent objects. Because
Supervisor owns the queue endpoints, Control Plane can restart without forcing
an otherwise healthy Worker to abandon durable work. If Supervisor restarts,
it restarts the complete child-process set and durable state resumes from
SQLite.

### 17.3 Backpressure

- Token deltas may be coalesced;
- repetitive progress may be dropped;
- state changes and errors receive higher priority;
- final responses never depend on IPC;
- a slow Channel cannot block Worker execution.

### 17.4 Control path

Cancellation, approval, and stop commands commit to SQLite first. IPC is only a
wakeup hint. Workers check control state:

- before and after LLM calls;
- before and after tools;
- before the next inner iteration;
- before final save.

## 18. Liveness, Stalls, and Process Recovery

Defaults:

- Worker heartbeat: 15 seconds;
- lease timeout: 180 seconds;
- expired-lease scan: 15 seconds;
- task progress timeout: 600 seconds;
- Worker concurrency: one root task;
- supervised Worker count: three.

The default realtime event queue is bounded to 1,000 envelopes. Repetitive
Token and progress events are coalesced before enqueueing. Queue capacity is
configuration-backed, but increasing it is not a substitute for backpressure.

### 18.1 Dead Worker

Supervisor restarts a visibly exited Worker immediately with a new generation.
If death cannot be proven, the lease expires after 180 seconds. The recovery
reaper then:

1. validates current lease and task state;
2. inspects the latest LLM and tool state;
3. moves safe running work to `RETRY_WAIT` with immediate availability;
4. moves unknown external effects to `WAITING_USER`;
5. appends a recovery event.

The scheduler promotes an available `RETRY_WAIT` task to `QUEUED` before it can
be claimed. This preserves the centralized transition table.

### 18.2 Live but stalled task

If Worker heartbeats continue but task progress does not change for 600
seconds:

1. request cooperative cancellation;
2. wait a bounded grace period;
3. terminate the contained operation if supported;
4. classify possible side effects;
5. retry one safe attempt or pause.

A known long-running tool must emit progress. A heartbeat alone does not allow
an uninstrumented tool to run forever.

### 18.3 Crash loops

A Worker slot that exits five times in ten minutes becomes `QUARANTINED`.
Other slots continue. A task that causes three Worker attempts to fail at the
same checkpoint is paused with a stable error code rather than rotated forever.

### 18.4 Control Plane failure

Workers continue durable work. Transient streaming may be lost. Final replies
remain in outbox. Supervisor restarts Control Plane, which reacquires a
single-instance lease and resumes Channel and outbox service.

### 18.5 Supervisor and host failure

An outer service manager must restart Supervisor. On host restart,
`runtime.sqlite` restores all non-terminal work. Supervisor itself is not an
operational source of truth.

### 18.6 Graceful shutdown

1. stop accepting new tasks;
2. mark Control Plane draining;
3. stop new claims;
4. reach a safe checkpoint;
5. release or complete current leases;
6. drain outbox within a grace period;
7. exit;
8. leave unfinished work non-terminal for next startup.

Shutdown never converts unfinished work to `FAILED`.

### 18.7 Ledger unavailable or disk full

The runtime ledger is the safety boundary. When it cannot commit:

- Gateway does not acknowledge a new task;
- no Worker starts another LLM or external tool call;
- a Worker holding an already returned result retries the short ledger commit
  with bounded backoff while retaining its lease when possible;
- readiness becomes false and a high-priority alert is emitted;
- no component writes an uncoordinated second task journal as a fallback.

If an external tool has returned but its result cannot be committed, the Worker
keeps the result in memory and stops advancing. If the Worker later dies,
recovery sees the durable pre-call `RUNNING` record and applies the normal
side-effect recovery matrix. A non-idempotent call may therefore become
`UNKNOWN`; the runtime must not guess.

Disk-full handling may remove only expired, unreferenced runtime blobs through
the normal retention rules. It must not delete active WAL, task, approval, or
tool rows to make room.

### 18.8 Corrupt ledger

On an integrity-check failure:

1. stop accepting and claiming work;
2. preserve the corrupt database and WAL files for diagnosis;
3. expose not-ready health;
4. restore through the most recent verified SQLite online backup;
5. replay only operations that are proven non-terminal and safe;
6. require review of any tool or delivery state that was `RUNNING` at the
   backup boundary.

The service must not automatically delete and recreate `runtime.sqlite`.
`memory.db` remains different: because it is a derived index, it may degrade and
be rebuilt without discarding source memory files.

## 19. Budgets and Global Resource Control

### 19.1 Stable-profile task defaults

- maximum inner iterations: 50;
- maximum cumulative input Tokens: 200,000;
- maximum cumulative output Tokens: 50,000;
- maximum tool-result injection: existing 16,000 characters;
- maximum root-turn wall time: two hours;
- warning threshold: 70%;
- root Worker attempts: three.

The legacy `max_tool_iterations=200` remains parseable for compatibility, but
the supervised stable profile recommends 50.

At the wall-time limit, save a checkpoint and ask whether to continue. An
approved continuation becomes a new durable continuation task.

### 19.2 Global quotas

`ResourceQuotaManager` controls across all Workers:

- Provider concurrent calls;
- requests per minute;
- Tokens per minute;
- active subagents;
- local embedding calls;
- browsers and other heavy tools;
- per-service external API concurrency.

Quota permits are leases and expire after Worker death.

### 19.3 Subagents

Stable defaults:

- two concurrent subagents per cloud-backed root task;
- one per local-model root task;
- four active subagents globally;
- existing recursion depth one;
- all subagent usage counts against the root budget.

### 19.4 Worker recycling

Optional configuration:

- maximum Worker RSS;
- maximum Worker uptime;
- maximum tasks before recycle.

A Worker exceeding a configured threshold drains and restarts at a checkpoint.
Only one Worker recycles at a time.

## 20. Error Model

Stable machine-readable error codes include:

- `RUNTIME_DB_UNAVAILABLE`
- `RUNTIME_DB_BUSY_EXHAUSTED`
- `SCHEMA_VERSION_UNSUPPORTED`
- `TASK_LEASE_LOST`
- `TASK_STALLED`
- `TASK_ATTEMPTS_EXHAUSTED`
- `SESSION_VERSION_CONFLICT`
- `CONTEXT_SNAPSHOT_MISSING`
- `PROVIDER_TRANSIENT`
- `PROVIDER_PERMANENT`
- `PROVIDER_RATE_LIMITED`
- `BUDGET_EXCEEDED`
- `TOOL_POLICY_DENIED`
- `TOOL_RETRY_EXHAUSTED`
- `TOOL_OUTCOME_UNKNOWN`
- `APPROVAL_EXPIRED`
- `RESOURCE_QUOTA_EXHAUSTED`
- `OUTBOX_DELIVERY_UNKNOWN`
- `MEMORY_RECALL_DEGRADED`
- `ARTIFACT_HASH_MISMATCH`

Each error records:

- stable code;
- retry class;
- bounded safe summary;
- causal component;
- task and step identifiers;
- timestamp.

Raw exception messages that might contain user data remain in protected local
diagnostics or are redacted.

## 21. Observability and Health

Extend existing `TurnTelemetry`; do not replace it.

### 21.1 Task metrics

- queue depth and oldest age by kind and priority;
- task state counts;
- completion latency;
- recovery count and duration;
- stall count;
- waiting-approval count.

### 21.2 Worker metrics

- state and heartbeat age;
- active task;
- restarts and quarantine;
- CPU and RSS;
- thread and file-handle count;
- event-loop delay;
- tasks completed.

### 21.3 LLM and tool metrics

- calls, latency, errors, Tokens, and cost;
- fallback and 429 count;
- iteration count;
- tool retry, approval, unknown, lock wait, and cancellation count.

### 21.4 Storage and delivery metrics

- SQLite commit and lock-wait latency;
- WAL and database size;
- checkpoint and blob cleanup backlog;
- outbox depth and oldest age;
- vector-index lag and degraded recall count.

### 21.5 Health endpoints

- `live`: process responds;
- `ready`: schema valid, ledger writable, required locks acquired, service can
  accept work;
- `healthy`: detailed Worker, Channel, outbox, Provider, and memory status.

Provider or vector recall failure produces a degraded health report; it does
not automatically make Gateway dead.

### 21.6 Minimum alerts

- fewer than two available Workers;
- Worker crash loop;
- oldest user task exceeds five minutes;
- any dangerous `UNKNOWN` tool outcome;
- outbox item older than two minutes;
- ledger write failure;
- continually growing WAL;
- repeated task crash at one checkpoint;
- unbounded RSS/thread/handle trend;
- persistent vector-index backlog.

## 22. Security and Privacy

- runtime and memory databases use owner-only filesystem permissions where the
  operating system supports them;
- credentials remain in the credential provider and are referenced, not copied;
- sensitive tool fields are redacted from events and logs;
- full recovery payloads are stored only in protected runtime blobs;
- workspace artifact references are path-validated and hash-validated;
- approvals bind principal, operation, arguments, and expiry;
- Channel identity linking is explicit;
- vector recall filters by tenant, principal, Agent, workspace, and kind before
  similarity ranking;
- `unified_session` remains a single-owner convenience and must not collapse
  unrelated users into one memory scope.

At-rest encryption of all local runtime content is a future enhancement, not a
hidden first-version requirement. The first version relies on operating-system
permissions and secret references.

## 23. Retention and Maintenance

Defaults:

- successful task checkpoints and large runtime blobs: seven days;
- failed, approval, and unknown-outcome records: 30 days;
- compact aggregate metrics: deployment log policy;
- terminal outbox payloads: seven days unless Channel policy requires less.

Cleanup:

- runs as low-priority durable maintenance;
- deletes in bounded batches;
- verifies no non-terminal row references a blob;
- checkpoints WAL during quiet periods;
- performs `VACUUM` only in an explicit maintenance window;
- uses SQLite online backup, never filesystem copy of an open WAL database.

## 24. Configuration Contract

Recommended configuration shape:

```json
{
  "runtime": {
    "mode": "supervised",
    "databasePath": "runtime/runtime.sqlite",
    "workerCount": 3,
    "workerConcurrency": 1,
    "heartbeatIntervalS": 15,
    "leaseTimeoutS": 180,
    "leaseScanIntervalS": 15,
    "progressTimeoutS": 600,
    "taskMaxAttempts": 3,
    "queuePollMinMs": 250,
    "queuePollMaxMs": 2000,
    "realtimeEventQueueCapacity": 1000,
    "shutdownGraceS": 60,
    "approvalTimeoutM": 30,
    "successfulRetentionD": 7,
    "failureRetentionD": 30,
    "backupIntervalH": 6,
    "backupRetentionD": 7,
    "maxTurnWallTimeM": 120,
    "stableMaxToolIterations": 50,
    "globalMaxSubagents": 4,
    "workerMaxRssMb": null,
    "workerMaxUptimeH": null,
    "workerMaxTasksBeforeRecycle": null
  }
}
```

Validation rules:

- supervised `workerCount` defaults to three and must be at least two;
- supervised version one requires `workerConcurrency=1`;
- heartbeat interval must be less than one third of lease timeout;
- progress timeout must exceed heartbeat interval;
- queue maximum poll must be less than the lease scan interval;
- retention values must be positive;
- lightweight mode defaults to one execution slot;
- explicit user configuration wins over launcher defaults.

Service launcher defaults to supervised. CLI and development launchers default
to lightweight.

## 25. Current Module Integration Map

Implementation should evolve existing files through narrow adapters:

### `miniunicorn/agent/turn_dispatcher.py`

- remain the inbound compatibility boundary;
- submit durable tasks instead of directly owning execution in supervised mode;
- lightweight mode may submit and await through the same service.

### `miniunicorn/agent/turn_coordinator.py`

- retain task-local `TurnRuntime` binding;
- retain lightweight in-process admission control;
- stop being the authoritative cross-process session-order mechanism;
- authoritative ordering moves to `session_slots`.

### `miniunicorn/agent/turn_executor.py`

- remain the outer-state executor;
- emit a checkpoint after each durable state;
- route command shortcuts through reliable save/respond.

### `miniunicorn/agent/turn_persistence.py`

- expose the checkpoint port;
- add the `runtime.sqlite` implementation outside Agent Core;
- retain a legacy session-metadata checkpoint reader for migration;
- do not dual-write forever.

### `miniunicorn/agent/turn_runtime.py`

- add task, attempt, Worker, and lease context without reintroducing shared
  mutable state;
- continue to feed `TurnTelemetry`.

### `miniunicorn/agent/runner.py`

- persist complete LLM decisions;
- checkpoint at every LLM/tool boundary;
- execute tools through Tool Gateway;
- preserve context governance, Planner, budget, injection, and hook behavior.

### `miniunicorn/agent/memory.py` and `vector_memory.py`

- preserve current memory semantics;
- make required background work durable;
- harden vector indexing for multi-process idempotency and source versions.

### Channel and bus modules

- normalize inbound tasks;
- consume validated Worker Agent events;
- deliver outbox messages;
- stop treating Worker-local streaming as the final source of truth.

### New runtime infrastructure package

A focused package under `miniunicorn/runtime/` should own:

- contracts and state transitions;
- SQLite migrations and repositories;
- durable scheduler;
- Worker registry and lease reaper;
- Worker host;
- Supervisor;
- Tool Gateway policy and journal adapters;
- outbox;
- global quotas;
- health and maintenance.

Core Agent files may depend on protocols from this package's dependency-light
contract layer. They may not depend on its SQLite implementation.

## 26. Migration and Rollout

The feature is delivered in phases. Do not perform a big-bang three-process
rewrite.

### Phase 0: contracts and characterization

- freeze current outer and inner behavior with tests;
- add typed runtime contracts and centralized state transitions;
- add SQLite migrations and repository tests;
- add no production process split.

Exit criterion: current behavior passes through contract adapters.

### Phase 1: durable lightweight runtime

- create `runtime.sqlite`;
- route inbound turns through durable accept/claim;
- add checkpoints and idempotent session commit;
- retain one process.

Exit criterion: kill and restart the single process at every outer state
without losing or duplicating a turn.

### Phase 2: Tool Gateway and outbox

- centralize tool policy and execution journal;
- add risk metadata to existing tools;
- add approval and unknown-outcome flow;
- route final replies through outbox.

Exit criterion: the side-effect crash matrix passes with fake external
services.

### Phase 3: supervised Workers

- add Supervisor and Control Plane;
- add three one-task Worker processes;
- add realtime IPC and durable control messages;
- add lease reaper, generation fencing, and crash-loop quarantine.

Exit criterion: random Worker termination recovers safely under concurrent
sessions.

### Phase 4: durable maintenance and memory hardening

- move required reflection, Dream, consolidation, indexing, and cleanup to
  durable task kinds;
- harden existing `memory.db`;
- add revision-aware memory updates and fallback metrics.

Exit criterion: maintenance restarts safely and cannot overwrite newer memory.

### Phase 5: stable-profile default

- add service-launcher default;
- add dashboards and minimum alerts;
- complete chaos, load, upgrade, and seven-day soak;
- document operating and recovery procedures.

Exit criterion: all acceptance criteria in this design pass.

### Legacy checkpoint migration

When a session has legacy `runtime_checkpoint` metadata and no corresponding
ledger task:

1. create one migration task with a new task and turn ID;
2. import the safe completed portions;
3. treat unresolved pending external tools conservatively;
4. complete through the new runtime;
5. remove the legacy checkpoint only after success.

New work does not dual-write both checkpoint formats.

### Upgrade procedure

Before enabling supervised mode:

1. stop accepting new tasks;
2. drain or checkpoint current in-process work;
3. back up workspace and runtime databases;
4. apply runtime schema migrations;
5. start Control Plane and one Worker in canary mode;
6. validate queue, outbox, and memory;
7. expand to three Workers;
8. enable stable default.

Rollback returns to lightweight mode using the same ledger. It does not require
converting task data.

## 27. Testing Strategy

Implementation follows test-driven development and keeps each phase
independently reviewable.

### 27.1 Unit tests

Cover:

- every valid and invalid task transition;
- inbound deduplication;
- session sequence allocation;
- atomic claim races;
- lease renewal and expiration;
- stale generation, token, epoch, and version rejection;
- checkpoint serialization and recovery matrix;
- idempotent turn commit;
- tool classification and retry matrix;
- approval binding and expiry;
- outbox claim and deduplication;
- quota lease expiration;
- typed IPC event validation;
- retention reference safety;
- configuration validation.

### 27.2 SQLite concurrency tests

Use separate processes and connections to prove:

- two Workers cannot claim one task;
- one session cannot run two root tasks;
- different sessions can claim concurrently;
- stale Workers cannot write;
- bounded `BUSY` retry works;
- WAL checkpoints do not corrupt active work;
- schema migration excludes Worker startup.

### 27.3 Agent integration tests

Inject process termination:

- before and after every outer state;
- after `LLM_STARTED`;
- after `LLM_COMPLETED`;
- before and after tool execution;
- between session write and `TURN_SAVED`;
- between outbox insert and task completion;
- after Channel send but before delivery receipt.

Assert:

- no accepted turn is lost;
- no session message is duplicated;
- persisted LLM results are reused;
- safe tools retry correctly;
- dangerous unknown outcomes pause;
- final response remains deliverable.

### 27.4 Multi-Worker tests

Run three real processes and verify:

- three different sessions run concurrently;
- the fourth waits;
- same-session tasks remain ordered;
- killing one Worker does not stop the others;
- a replacement Worker resumes after fencing;
- an old Worker cannot publish late results;
- Control Plane restart loses only transient streaming;
- Supervisor restart reconstructs process state.

### 27.5 Tool-effect tests

Use a fake external service supporting:

- idempotency receipts;
- success followed by lost response;
- explicit failure;
- delayed response;
- rate limiting;
- duplicate requests.

Also test a fake service without idempotency support. The expected result after
a lost response is `UNKNOWN`, not an automatic replay.

### 27.6 Memory tests

Verify:

- existing non-vector fallback remains;
- `memory.db` uses one compatible fingerprint;
- repeated indexing is idempotent;
- tombstones suppress recall;
- cross-principal recall is impossible;
- stale Dream output cannot overwrite a newer revision;
- vector failure does not block a user turn;
- index rebuild does not modify source memory files.

### 27.7 Load and soak tests

The pre-release soak runs seven days with:

- three Workers;
- mixed short and long tasks;
- multiple sessions and Channels;
- periodic Provider timeouts and 429s;
- random Worker termination every 5–15 minutes;
- periodic Control Plane restart;
- background Dream, consolidation, indexing, and cleanup;
- Channel delivery failures;
- repeated duplicate inbound messages.

Measure after warmup:

- zero lost accepted tasks;
- zero same-session ordering violations;
- zero duplicate idempotent side effects;
- every non-idempotent uncertain effect enters `WAITING_USER`;
- dead-Worker recovery completes within lease timeout plus one scan interval;
- final eight-hour average RSS remains within 15% of the first stable
  eight-hour average unless workload size changes;
- thread and file-handle counts remain within a fixed tested bound;
- WAL returns below its configured maintenance threshold;
- no required background task exists only in process memory.

CI may run an accelerated shorter chaos test. It does not replace the seven-day
release soak.

## 28. Acceptance Criteria

The design is implemented only when all are true:

1. Both deployment modes pass the same runtime contract suite.
2. An accepted task survives forced process and host-style restart.
3. Three root tasks run concurrently and a fourth remains durably queued.
4. Same-session ordering is never violated.
5. A dead Worker is detected within 180 seconds and safely recovered.
6. A 600-second no-progress task is interrupted and receives at most one safe
   automatic recovery.
7. Stale Worker updates are rejected by generation, lease, epoch, and version.
8. A complete persisted LLM response is not called again after recovery.
9. A successful tool call is not repeated.
10. A dangerous uncertain call pauses for explicit user direction.
11. Final replies survive Control Plane and Channel outages.
12. Required background work survives process restart.
13. Existing `memory.db` recall works or degrades without blocking chat.
14. Logs and telemetry pass content-leak tests.
15. Seven-day soak criteria pass.

## 29. Implementation Guardrails

The implementation agent must:

- preserve unrelated user work and untracked files;
- implement in phases and commits;
- add focused tests before each behavior change;
- keep SQLite infrastructure out of Agent Core;
- avoid a second vector-memory database;
- avoid external queue dependencies;
- avoid changing Channel payloads without protocol versioning;
- avoid hidden automatic retry of uncertain external writes;
- avoid storing final delivery only in transient IPC;
- avoid broad rewrites of `runner.py` or `loop.py` without characterization
  tests;
- return a changed-file list, migration notes, test evidence, and residual
  risks for each phase.

Recommended review checkpoints:

1. contracts and schema;
2. single-process durable recovery;
3. Tool Gateway and outbox;
4. supervised process topology;
5. memory/background durability;
6. chaos and soak results.

## 30. Final Design Decision

MiniUnicorn keeps its current cognitive core:

- the outer loop prepares, restores, saves, and responds;
- the inner loop asks the LLM, executes tools, and reasons again.

The enhancement surrounds that core with a replaceable-execution harness:

- durable queue;
- fine-grained checkpoints;
- three isolated Workers;
- lease and fencing;
- safe tool effects;
- reliable delivery;
- durable maintenance;
- bounded resources;
- operational evidence.

This provides most of the practical reliability benefits associated with
checkpointed graph runtimes, event-driven Agent systems, permission-oriented
workers, and supervised execution without replacing MiniUnicorn with a full
event-sourced graph engine.
