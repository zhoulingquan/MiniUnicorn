# MiniUnicorn Thin Durable Runtime Design

**Status:** Approved architecture, implementation-ready specification
**Date:** 2026-07-29
**Supersedes:** The earlier full-runtime version of this document
**Primary decision:** Add one thin durable runtime kernel around the existing
Agent Core. Do not build a second Agent system.

## 1. Purpose

MiniUnicorn already has a capable Agent loop, Provider abstraction, tool
registry, session store, memory system, channels, typed Agent events, telemetry,
and process-local concurrency controls. The missing capability is a durable
coordination layer that can answer five questions after a process crash:

1. Which user or maintenance tasks were accepted?
2. Which task may run next for each session?
3. Which LLM and tool boundaries were durably completed?
4. Which session mutations were committed?
5. Which user-visible messages still need delivery?

This design adds that capability without replacing the existing Agent Core.
The durable runtime is intentionally thin:

- it owns task coordination, recovery facts, fencing, and reliable delivery;
- it calls the current Agent loop through narrow ports;
- it reuses the current Tool Registry, Session Manager, Channel Manager,
  memory behavior, Provider clients, and Agent event protocol;
- it supports lightweight and supervised deployment through different hosts
  around the same runtime kernel;
- it assigns exactly one authority to every durable fact.

This document is detailed enough to hand to an implementation Agent. It fixes
the module boundaries, contracts, schema, transactions, recovery behavior,
migration order, tests, and acceptance criteria. Implementation may rename
private helpers, but it must not change the public responsibilities or safety
semantics without revising this specification.

## 2. Existing Baseline

The implementation must evolve the current code rather than bypass it.

Current useful boundaries include:

- `miniunicorn/agent/turn_dispatcher.py`
  - accepts inbound messages;
  - owns process-local pending queues and task creation today;
- `miniunicorn/agent/turn_coordinator.py`
  - provides process-local per-session locks and a global semaphore;
- `miniunicorn/agent/turn_executor.py`
  - owns the outer turn state machine;
- `miniunicorn/agent/turn_persistence.py`
  - persists current session metadata checkpoints;
- `miniunicorn/agent/turn_runtime.py`
  - provides task-local `contextvars` state;
- `miniunicorn/agent/runner.py`
  - owns the inner LLM/tool loop, Context Governor, Planner integration,
    budgets, hooks, and tool-result injection;
- `miniunicorn/agent/tools/registry.py`
  - owns tool discovery, schema, preparation, and dispatch;
- `miniunicorn/session/manager.py`
  - owns session transcript persistence and its process-local cache;
- `miniunicorn/agent/memory.py` and `vector_memory.py`
  - own long-term memory semantics and the derived vector index;
- `miniunicorn/channels/manager.py`
  - owns Channel lifecycle, formatting, retry, and delivery adapters;
- `miniunicorn/bus/queue.py`
  - provides bounded in-memory inbound and outbound queues;
- `miniunicorn/bus/agent_events.py`
  - defines the strict versioned Agent event protocol;
- `miniunicorn/utils/task_supervisor.py`
  - owns disposable process-local background coroutines;
- `miniunicorn/agent/telemetry.py`
  - owns current turn telemetry.

The runtime must preserve these domain behaviors. It changes who owns durable
coordination, not how MiniUnicorn reasons.

### 2.1 Problems being corrected

The current process-local design has the following failure modes:

- accepted inbound work can exist only in an `asyncio.Queue`;
- pending session work can exist only in an in-memory deque;
- `asyncio.create_task()` can be the sole owner of required work;
- per-session locks and global semaphores do not coordinate processes;
- a final response can be generated but lost before Channel delivery;
- a tool can finish while its result is not durably recorded;
- a Provider retry or fallback attempt is hidden from the turn journal;
- two `SessionManager` instances can overwrite each other from stale caches;
- required Dream, consolidation, indexing, or cleanup work can be lost;
- killing a Worker can leave tool subprocesses or browser processes alive.

The durable runtime exists only to correct these failure modes.

## 3. Goals

1. Every accepted user turn is durable before acknowledgement.
2. Every required maintenance operation is represented as a durable task.
3. Tasks for one session execute in strict `session_sequence` order.
4. Different sessions may execute concurrently.
5. A Worker crash cannot let stale work commit after its lease is reclaimed.
6. Recovery resumes from the latest safe boundary.
7. A non-idempotent external effect is never guessed to be safe after an
   uncertain outcome.
8. Final replies and Agent-originated outbound messages are reliably delivered
   through one outbox.
9. Lightweight and supervised modes use the same state machine, store,
   scheduler, checkpoint format, Tool Gateway, and delivery semantics.
10. The existing Agent Core remains testable without SQLite or multiprocessing.
11. Existing session and memory formats remain the content authorities.
12. The design remains efficient for one host and one to three Workers.
13. Every migration phase is independently testable and reversible before
    durable traffic is accepted.

## 4. Non-Goals

This version does not provide:

- multi-host distributed scheduling;
- remote database support;
- exactly-once execution for arbitrary third-party side effects;
- transparent live migration between lightweight and supervised mode;
- automatic mode selection from Prompt text, task duration, CPU load, or queue
  depth;
- a replacement for the Agent loop, Provider system, Tool Registry, Channel
  adapters, Session Manager, or memory system;
- a general workflow language or DAG engine;
- unbounded Worker, subagent, or tool parallelism;
- durable storage of every streamed Token;
- permanent dual-write compatibility between the legacy and durable paths.

## 5. Terms

- **Agent Core:** Existing turn execution and reasoning code.
- **Runtime Kernel:** Task Service, Scheduler, Runtime Store ports,
  Worker Adapter, Tool Gateway, and Outbox Sender.
- **Runtime Store:** One transactional façade implemented by SQLite. Consumers
  use narrow protocol views of the same façade.
- **Task:** One durable unit of user or internal work.
- **Turn:** A user-facing interaction identified by `turn_id`.
- **Run segment:** One bounded execution period for a task. A continued task
  keeps its `task_id` and increments `run_segment`.
- **Lease:** Time-bounded permission for one Worker to mutate one task.
- **Fencing:** Rejection of writes from an expired or superseded lease.
- **Checkpoint:** Durable Agent execution state at a defined safe boundary.
- **Durable event:** A bounded recovery or audit fact stored in SQLite.
- **Agent event:** Existing strict realtime UI protocol. It may be transient.
- **External effect:** A tool or Channel operation visible outside runtime
  bookkeeping.
- **Session commit:** Idempotent mutation of the authoritative session
  transcript.
- **Authority:** The only component allowed to decide the current truth for a
  class of data.

## 6. Non-Negotiable Invariants

1. `runtime.sqlite` is the authority for task state, task order, leases,
   durable execution facts, control requests, and outbound delivery state.
2. `SessionManager` is the authority for session transcript content.
3. The existing memory source files and Git history are authoritative for
   long-term memory; `memory.db` is a rebuildable index.
4. `ToolRegistry` is the authority for tool discovery, schema, argument
   preparation, and actual invocation.
5. `ToolGateway` is the authority for effective risk, approval, idempotency,
   execution journaling, and crash recovery of Agent-originated tool calls.
6. Outbox is the authority for whether a final or Agent-originated message was
   delivered.
7. Message Bus and multiprocessing IPC carry wake hints and realtime events
   only. Their contents are never required for recovery.
8. At most one non-terminal task is active for a session.
9. Only the earliest non-terminal `session_sequence` is claimable.
10. Every task mutation from a Worker includes `task_id`, `lease_token`, and
    `lease_epoch`.
11. Lease renewal does not grant permission to a stale token and does not
    advance task business-state version.
12. No SQLite transaction remains open across a Provider, tool, Channel,
    session filesystem, memory filesystem, or arbitrary user-code call.
13. An LLM response is not consumed by Agent Core until its completed attempt
    is durable.
14. A tool result is not consumed by Agent Core until its terminal result is
    durable.
15. A final reply is not considered complete until it is durable in Outbox.
16. Required work is never owned only by `asyncio.create_task()`.
17. Agent Core does not import SQLite, multiprocessing, Supervisor, or Channel
    infrastructure.
18. Lightweight and supervised modes never select different Agent logic.
19. The deployment mode is selected at startup and does not change while work
    is active.
20. Durable events and normal logs never contain raw prompts, responses, tool
    arguments, tool results, credentials, or media.
21. Runtime Store write methods are the only code allowed to perform
    multi-table runtime transactions.
22. New durable tasks never dual-write legacy `runtime_checkpoint` or
    `pending_user_turn` metadata as a second recovery authority.
23. `runtime.sqlite` and `memory.db` remain separate files with separate
    connections, migrations, backup policy, and rebuild semantics.

## 7. Authority and Dependency Model

### 7.1 Unique authority table

| Fact | Authority | Non-authoritative consumers |
|---|---|---|
| Task lifecycle | Runtime Store | Dispatcher, Worker, UI |
| Session execution order | Runtime Store `session_slots` | Coordinator |
| Lease ownership | Runtime Store | Supervisor, Worker |
| Durable checkpoints | Runtime Store | Turn Persistence |
| LLM attempt facts | Runtime Store | Provider observer, telemetry |
| Tool safety and result state | Tool Gateway plus Runtime Store | Runner |
| Session transcript | Session Manager | Runtime Store commit record |
| Final delivery state | Outbox | Message Bus, Channel Manager |
| Realtime Agent events | Agent event protocol | WebUI, Channel bridge |
| Tool definitions and schemas | Tool Registry | Tool Gateway |
| Long-term memory source | Existing memory files/GitStore | Agent Core |
| Vector search index | `memory.db` | Recall path |
| Worker process liveness | Supervised Host | Health reporting |

### 7.2 Allowed dependency direction

```text
CLI / Channels / Cron
        |
        v
Runtime contracts and Runtime Kernel
        |
        v
Agent Core interfaces and existing Agent Core
        |
        v
Provider / Tool Registry / Session / Memory interfaces

SQLite / multiprocessing / OS process containment
        ^
        |
Runtime infrastructure implementations only
```

Agent Core defines the ports it needs from execution infrastructure. Runtime
implements those ports. This consumer-owned-port rule prevents Agent Core from
depending on the durable runtime implementation.

### 7.3 No service per table

`tasks`, `task_events`, `checkpoints`, `tool_calls`, and Outbox are tables with
different semantics. They are not separate deployable services.

One `SqliteRuntimeStore` implements several narrow protocol views:

- `TaskIngressStore`;
- `WorkerLedger`;
- `ExecutionJournal`;
- `SessionCommitLedger`;
- `DeliveryLedger`;
- `ResourceLedger`;
- `MaintenanceLedger`.

The views exist for interface segregation and tests. They share one connection
factory, migration owner, transaction policy, and database file.

### 7.4 Convergence from the earlier design

| Earlier concept | Thin-kernel decision |
|---|---|
| Durable Task Queue service | Task rows plus Task Service and stateless Scheduler |
| Turn Journal service | `ExecutionJournal` view of Runtime Store |
| Checkpoint Store service | Checkpoint methods on the same Runtime Store |
| Session Slot service | Claim transaction over `session_slots` |
| Worker Registry table/service | Removed; Host owns processes, task leases fence work |
| Approval subsystem | Merged into `task_controls` |
| Resource Lock Manager plus Quota Manager | Merged into `resource_leases` |
| Durable event bus | Removed; facts use SQLite, realtime remains transient |
| Separate maintenance scheduler | Existing triggers submit typed durable tasks |
| Separate memory candidate store | Removed; existing memory remains authoritative |

This convergence removes duplicate deployment units without collapsing
interfaces. Consumers still depend on narrow protocols, while one
implementation owns each transactional fact.

## 8. Component Boundaries

### 8.1 Task Service

`TaskService` is the only normal ingress into durable work.

Responsibilities:

- normalize Channel, CLI, SDK, cron, and maintenance requests;
- validate protocol version and required scope;
- store protected payloads;
- deduplicate Channel messages;
- allocate `session_sequence`;
- create tasks;
- append cancellation, approval, rejection, steering, and continuation
  controls;
- notify local or remote Workers with a best-effort wake hint.

It does not:

- execute Agent turns;
- own an in-memory pending-work queue;
- select a Worker;
- publish final replies directly;
- edit session files directly.

The existing `TurnDispatcher` becomes a compatibility adapter over this
service.

### 8.2 Scheduler

`Scheduler` is a stateless library over `WorkerLedger`.

Responsibilities:

- atomically claim the highest-priority eligible session head;
- issue a random lease token and increment `lease_epoch`;
- renew valid leases;
- release or reclaim expired work;
- move elapsed `RETRY_WAIT` tasks back to `QUEUED`;
- prevent maintenance claims while user work is queued;
- acquire generic cross-process resource leases where required.

There is no central in-memory dispatch queue. In supervised mode each Worker
calls the same Scheduler against SQLite. SQLite serializes the short claim
transaction. Wake IPC only reduces polling latency.

### 8.3 Worker Adapter

`AgentTaskWorker` adapts one claimed durable task to the existing Agent Core.

Responsibilities:

- load the protected task payload;
- construct a task-scoped `TurnRuntime`;
- construct or reset an Agent execution instance through `AgentFactory`;
- restore the latest valid checkpoint;
- apply pending control requests at safe boundaries;
- call the existing `TurnExecutor`;
- provide Agent Core with journal, tool execution, and session commit ports;
- checkpoint progress;
- map terminal outcomes to Runtime Store operations;
- release task-local resources.

One Worker handles one root task at a time in supervised version one. Provider
clients and immutable configuration may be reused within that Worker process.
Mutable Agent turn state may not leak between tasks.

### 8.4 Runtime Store

`SqliteRuntimeStore` owns:

- schema migrations;
- SQLite connection configuration;
- all task and session-slot transactions;
- append-only durable events;
- checkpoints and protected runtime blobs;
- LLM and tool attempt records;
- task control requests;
- session commit coordination records;
- Outbox records and delivery leases;
- generic resource leases;
- retention and online backup bookkeeping.

It does not own session transcript content, memory content, Provider clients,
tools, Channels, or Agent behavior.

### 8.5 Tool Gateway

`ToolGateway` implements Agent Core's `ToolExecutionPort`.

It combines:

- existing Tool Registry preparation and invocation;
- invocation-specific risk classification;
- approval policy;
- stable idempotency keys;
- durable logical-call and attempt records;
- resource locking;
- recovery decisions.

It is not a second registry and does not duplicate tool schemas.

### 8.6 Session Committer

`SessionCommitter` implements Agent Core's `SessionCommitPort`.

It coordinates a Runtime Store commit intent with an idempotent, revision-aware
`SessionManager.commit_turn()` operation. It does not store transcript content
in SQLite.

### 8.7 Outbox Sender

`OutboxSender`:

- claims the earliest pending record per delivery target;
- invokes the existing `ChannelManager`;
- stores a provider receipt when available;
- retries transient failures with bounded backoff;
- marks permanent failures and raises an operational alert.

Outbox delivery does not consume an Agent Worker slot.

### 8.8 Realtime Event Bridge

The bridge consumes current typed Agent events from Workers and forwards them
to WebUI or Channel streaming adapters.

It is explicitly lossy:

- Token deltas may be coalesced or dropped;
- progress updates may be replaced by the latest value;
- state changes may be reconstructed from Runtime Store;
- final complete messages are delivered only from Outbox.

### 8.9 Hosts

Hosts are composition roots. They choose process boundaries, not behavior.

- `LightweightHost` constructs all components in one process.
- `SupervisedHost` starts Supervisor, Control Plane, and Worker processes.

Only host and configuration modules inspect `runtime.mode`.

## 9. Deployment Modes

### 9.1 Lightweight mode

Lightweight mode runs in one process:

```text
LightweightHost
├── Channel or CLI ingress
├── TaskService
├── one SqliteRuntimeStore connection factory
├── Scheduler
├── 1..3 AgentTaskWorker coroutines
├── OutboxSender
└── maintenance enqueue loop
```

It still uses:

- `runtime.sqlite`;
- durable tasks;
- the same leases and fencing;
- the same checkpoints;
- the same Tool Gateway;
- the same Outbox;
- the same session commit protocol;
- the same recovery paths.

Default execution slots: one. Configurable maximum: three. Extra slots provide
concurrency but no process isolation.

CLI, tests, and development launchers default to lightweight mode.

### 9.2 Supervised mode

```text
Supervisor
├── Control Plane process
│   ├── Channel Gateway
│   ├── TaskService
│   ├── OutboxSender
│   ├── control API
│   ├── realtime AgentEvent bridge
│   └── maintenance enqueue loop
├── Worker process 1
│   └── Scheduler + AgentTaskWorker
├── Worker process 2
│   └── Scheduler + AgentTaskWorker
└── Worker process 3
    └── Scheduler + AgentTaskWorker
```

Every child uses spawn semantics and creates its own:

- event loop;
- SQLite connections;
- Provider clients;
- Tool instances;
- Agent factory;
- IPC endpoints.

No child inherits open Channel sockets, SQLite connections, Provider clients,
or mutable Agent instances.

The long-running service launcher defaults to supervised mode.

### 9.3 Mode selection

Mode is selected once at startup using:

```text
CLI flag
> environment variable
> configuration file
> launcher default
```

Required forms:

- CLI: `--runtime-mode lightweight|supervised`;
- environment: `MINIUNICORN_RUNTIME_MODE`;
- configuration: `runtime.mode`.

The runtime never infers mode from Prompt text or expected task duration. A
one-second task sent to a supervised Gateway runs in supervised mode. A
two-hour task started by a lightweight CLI runs in lightweight mode unless the
operator explicitly selects supervised mode.

Mode changes require a graceful stop followed by a new start. The database and
task model remain compatible across modes.

### 9.4 Same engine, different assembly

Mode may change only:

- process boundaries;
- IPC implementation;
- execution slot count;
- process monitoring and containment.

Mode may not change:

- task states or transitions;
- database schema;
- claim ordering;
- checkpoint format;
- lease or fencing rules;
- Agent outer or inner loop;
- tool policy;
- session commit behavior;
- Outbox behavior;
- recovery decisions.

## 10. Code Layout

### 10.1 New Agent-owned ports

Create:

```text
miniunicorn/agent/ports.py
```

It contains dependency-light Protocols used by Agent Core:

- `TurnJournalPort`;
- `ToolExecutionPort`;
- `SessionCommitPort`;
- `ControlInboxPort`;
- `ProgressPort`.

It may import standard library types and existing Agent DTOs only. It must not
import `miniunicorn.runtime`, SQLite, Channels, or multiprocessing.

### 10.2 New runtime package

Create:

```text
miniunicorn/runtime/
├── __init__.py
├── config.py
├── contracts.py
├── models.py
├── store.py
├── task_service.py
├── scheduler.py
├── worker.py
├── tool_gateway.py
├── session_committer.py
├── outbox.py
├── maintenance.py
├── events.py
├── ipc.py
├── supervisor.py
├── hosts/
│   ├── __init__.py
│   ├── lightweight.py
│   └── supervised.py
└── sqlite/
    ├── __init__.py
    ├── connection.py
    ├── migrations.py
    ├── schema.py
    └── store.py
```

File responsibilities:

- `contracts.py`: Runtime Store protocols and host-neutral service protocols;
- `models.py`: immutable enums and DTOs;
- `store.py`: façade type and narrow view composition, no SQLite;
- `task_service.py`: submit and control use cases;
- `scheduler.py`: claim, lease, retry promotion, resource-lease use cases;
- `worker.py`: durable task-to-Agent adapter;
- `tool_gateway.py`: implementation of `ToolExecutionPort`;
- `session_committer.py`: two-store idempotent commit coordinator;
- `outbox.py`: Outbox Sender loop;
- `maintenance.py`: enqueue and retention use cases;
- `events.py`: mapping durable facts to existing telemetry/events;
- `ipc.py`: wake hints and transient Agent event transport;
- `supervisor.py`: process lifecycle and containment;
- `hosts/*`: composition roots;
- `sqlite/connection.py`: connection creation and pragmas;
- `sqlite/migrations.py`: numbered migrations;
- `sqlite/schema.py`: schema constants and validation;
- `sqlite/store.py`: the only production SQL implementation.

Private SQL helpers may be split further if `sqlite/store.py` exceeds roughly
1,000 lines, but the public Runtime Store façade remains one implementation.

### 10.3 No parallel legacy runtime package

Do not create:

- a second Agent loop;
- a second Tool Registry;
- a second Channel Manager;
- a second session format;
- a durable replacement for the entire Message Bus;
- separate deployable Queue, Journal, Checkpoint, Lease, or Quota services.

## 11. Core Port Contracts

Signatures below are normative at the semantic level. Concrete Python typing
may use dataclasses and existing project result types.

### 11.1 Agent-owned ports

```python
class TurnJournalPort(Protocol):
    async def load_restore_point(
        self, task: TaskIdentity
    ) -> RestorePoint | None: ...

    async def record_model_started(
        self, call: ModelAttemptStarted
    ) -> AttemptIdentity: ...

    async def record_model_completed(
        self, attempt: AttemptIdentity, result: ModelAttemptResult
    ) -> ModelDecision: ...

    async def record_model_failed(
        self, attempt: AttemptIdentity, error: SafeError
    ) -> None: ...

    async def save_checkpoint(
        self, checkpoint: TurnCheckpoint
    ) -> CheckpointIdentity: ...

    async def record_progress(
        self, progress: DurableProgress
    ) -> None: ...
```

```python
class ToolExecutionPort(Protocol):
    async def execute(
        self, request: ToolExecutionRequest
    ) -> ToolExecutionResult: ...
```

```python
class SessionCommitPort(Protocol):
    async def commit_turn(
        self, request: SessionCommitRequest
    ) -> SessionCommitResult: ...
```

```python
class ControlInboxPort(Protocol):
    async def pending_controls(
        self, cursor: int
    ) -> ControlBatch: ...

    async def acknowledge(
        self, control_id: str, outcome: ControlOutcome
    ) -> None: ...
```

```python
class ProgressPort(Protocol):
    async def emit(self, event: AgentEvent) -> None: ...
```

Agent Core tests use in-memory fakes for these ports.

### 11.2 Runtime Store views

```python
class TaskIngressStore(Protocol):
    def submit_task(self, envelope: InboundTaskEnvelope) -> SubmitResult: ...
    def append_control(self, control: TaskControlRequest) -> ControlResult: ...
    def read_task(self, task_id: str) -> TaskRecord | None: ...
```

```python
class WorkerLedger(Protocol):
    def claim_next(self, request: ClaimRequest) -> ClaimedTask | None: ...
    def mark_running(self, claim: TaskClaim) -> TaskRecord: ...
    def renew_lease(self, claim: TaskClaim, lease_until_ms: int) -> bool: ...
    def checkpoint(self, claim: TaskClaim, value: CheckpointWrite) -> str: ...
    def record_progress(self, claim: TaskClaim, value: DurableProgress) -> None: ...
    def enter_retry_wait(self, claim: TaskClaim, retry: RetryDecision) -> None: ...
    def enter_waiting_user(
        self, claim: TaskClaim, wait: WaitDecision
    ) -> WaitResult: ...
    def fail_task(self, claim: TaskClaim, failure: TaskFailure) -> None: ...
    def cancel_task(self, claim: TaskClaim, reason: SafeError) -> None: ...
    def complete_with_outbox(
        self, claim: TaskClaim, completion: CompletionWrite
    ) -> CompletionResult: ...
    def complete_internal(
        self, claim: TaskClaim, completion: InternalCompletionWrite
    ) -> CompletionResult: ...
    def promote_due_retries(self, now_ms: int, limit: int) -> int: ...
    def reclaim_expired(self, now_ms: int, limit: int) -> ReclaimResult: ...
```

```python
class ExecutionJournal(Protocol):
    def load_restore_point(self, task_id: str) -> RestorePoint | None: ...
    def begin_model_attempt(self, claim: TaskClaim, value: ModelAttemptWrite) -> str: ...
    def finish_model_attempt(
        self, claim: TaskClaim, attempt_id: str, value: ModelResultWrite
    ) -> ModelDecisionRef: ...
    def fail_model_attempt(
        self, claim: TaskClaim, attempt_id: str, error: SafeError
    ) -> None: ...
    def prepare_tool_call(
        self, claim: TaskClaim, value: PreparedToolWrite
    ) -> ToolCallRecord: ...
    def begin_tool_attempt(
        self, claim: TaskClaim, value: ToolAttemptWrite
    ) -> ToolAttemptRecord: ...
    def finish_tool_attempt(
        self, claim: TaskClaim, attempt_id: str, value: ToolResultWrite
    ) -> ToolExecutionResult: ...
    def mark_tool_unknown(
        self, claim: TaskClaim, attempt_id: str, error: SafeError
    ) -> None: ...
    def list_pending_controls(
        self, claim: TaskClaim, after_control_seq: int
    ) -> list[TaskControlRecord]: ...
    def acknowledge_control(
        self, claim: TaskClaim, control_id: str, outcome: ControlOutcome
    ) -> None: ...
```

```python
class SessionCommitLedger(Protocol):
    def prepare_session_commit(
        self, claim: TaskClaim, value: SessionCommitWrite
    ) -> SessionCommitRecord: ...
    def confirm_session_commit(
        self, claim: TaskClaim, commit_id: str, revision: int
    ) -> SessionCommitRecord: ...
    def mark_session_conflict(
        self, claim: TaskClaim, commit_id: str, error: SafeError
    ) -> SessionCommitRecord: ...
    def read_session_commit(
        self, task_id: str, commit_kind: str
    ) -> SessionCommitRecord | None: ...
```

```python
class DeliveryLedger(Protocol):
    def claim_next_delivery(
        self, sender_id: str, now_ms: int, lease_ms: int
    ) -> OutboxClaim | None: ...
    def renew_delivery_lease(self, claim: OutboxClaim, until_ms: int) -> bool: ...
    def mark_delivered(self, claim: OutboxClaim, receipt: DeliveryReceipt) -> None: ...
    def retry_delivery(self, claim: OutboxClaim, retry: RetryDecision) -> None: ...
    def fail_delivery(self, claim: OutboxClaim, error: SafeError) -> None: ...
    def resolve_unknown_delivery(
        self, outbox_id: int, resolution: DeliveryResolution
    ) -> None: ...
```

```python
class ResourceLedger(Protocol):
    def acquire_resource(
        self, request: ResourceLeaseRequest
    ) -> ResourceLease | None: ...
    def renew_resource(
        self, lease: ResourceLease, until_ms: int
    ) -> bool: ...
    def release_resource(self, lease: ResourceLease) -> bool: ...
```

```python
class MaintenanceLedger(Protocol):
    def list_retention_batch(
        self, policy: RetentionPolicy, limit: int
    ) -> RetentionBatch: ...
    def delete_retention_batch(self, batch: RetentionBatch) -> RetentionResult: ...
    def list_unreferenced_blobs(self, limit: int) -> list[str]: ...
    def delete_unreferenced_blobs(self, blob_ids: list[str]) -> int: ...
```

All mutating Worker and delivery methods reject a stale token. Rejection is a
normal fencing outcome, not an internal exception to retry blindly.

### 11.3 Task Service API

```python
class DurableTaskService(Protocol):
    async def submit(
        self, envelope: InboundTaskEnvelope
    ) -> TaskHandle: ...
    async def submit_internal(
        self, envelope: InternalTaskEnvelope
    ) -> TaskHandle: ...
    async def control(
        self, request: TaskControlRequest
    ) -> ControlResult: ...
    async def get_status(
        self, scope: RequestScope, task_id: str
    ) -> TaskSnapshot: ...
    async def wait_terminal(
        self, scope: RequestScope, task_id: str, timeout_s: float | None
    ) -> TaskSnapshot: ...
```

`TaskHandle` contains opaque identity and initial durable status, not a
reference to an `asyncio.Task`. `wait_terminal()` may use process-local
notifications to reduce latency, but always rereads Runtime Store and remains
correct if the notification is lost or the waiting process restarts.

## 12. Identifiers, Time, and Payloads

### 12.1 Identifiers

- `task_id`: UUIDv7 generated at durable acceptance.
- `turn_id`: stable user-turn UUID. Null for internal maintenance.
- `channel_message_id`: stable Channel-supplied inbound id.
- `session_key`: canonical globally unique session identity derived from tenant,
  principal, Agent, workspace, Channel/account, and conversation scope; a raw
  provider chat id alone is not sufficient.
- `session_sequence`: monotonically increasing integer allocated per session.
- `lease_token`: cryptographically random 128-bit value encoded as text.
- `lease_epoch`: integer incremented on every new claim or reclaim.
- `event_id`: UUIDv7.
- `checkpoint_id`: UUIDv7.
- `logical_call_id`: stable id for one logical model call.
- `model_attempt_id`: UUIDv7 for one network attempt.
- `tool_call_id`: stable id from the model, normalized by Runner.
- `tool_attempt_id`: UUIDv7 for one invocation attempt.
- `control_id`: UUIDv7.
- `session_commit_id`: deterministic hash of task id plus commit kind, such as
  `sha256(task_id + ":inbound")` or `sha256(task_id + ":final")`.
- `outbox_id`: SQLite integer primary key used as durable delivery order.
- `outbox_dedup_key`: stable hash of logical message identity.
- `resource_lease_token`: cryptographically random token.

UUIDv7 generation must be monotonic within a process when the system clock does
not advance. Correctness must not depend on UUID ordering.

### 12.2 Time

- Persist UTC Unix milliseconds.
- Compare leases using SQLite wall time from the same claim or renewal
  transaction.
- Use monotonic clocks for in-process elapsed durations only.
- Never persist a monotonic timestamp.
- Never compare monotonic values produced by different processes.

### 12.3 Protected payloads

Raw user content, model responses, tool arguments, tool results, and full
checkpoints are stored as protected runtime blobs or existing artifact
references. Queue and event rows contain only:

- references;
- hashes;
- type and routing metadata;
- bounded redacted summaries;
- numeric usage;
- safe error codes.

Runtime blobs use content hashes for deduplication. A blob larger than the
configured inline limit is stored through the existing artifact/media storage
and represented by a reference.

Inbound media or attachments must be copied to durable, scoped,
content-addressed storage before the task row commits. A temporary Channel path
is not an acceptable task payload reference. The copy occurs outside the
acceptance transaction; a crash before task insertion may leave an unreferenced
artifact that bounded retention later removes.

## 13. Task and Control Contracts

### 13.1 Inbound task envelope

```text
protocol_version
task_kind
priority
tenant_id
principal_id
agent_id
workspace_id
session_key
channel                  nullable for internal work
channel_account          nullable
channel_message_id       nullable for internal work
dedup_key                required for internal work, optional for user work
normalized_payload_ref
payload_hash
media_refs
reply_to                 nullable
trace_id
received_at_ms
```

Required behavior:

- user tasks require Channel identity and a stable message id;
- direct CLI creates a stable local message id before submission;
- internal tasks use a system-scoped session key and a deterministic dedup key;
- duplicate `(tenant_id, channel, channel_account, channel_message_id)` returns
  the original `task_id`;
- duplicate task submission never allocates another session sequence.

Internal dedup keys identify one logical occurrence, not a task kind forever.
They include the relevant source revision, schedule fire time, cleanup window,
or index revision. Repeated submission of the same occurrence deduplicates;
later legitimate occurrences use a different key.

### 13.2 Task kinds

- `USER_TURN`;
- `MEMORY_CONSOLIDATION`;
- `MEMORY_INDEX`;
- `REFLECTION`;
- `DREAM`;
- `MAINTENANCE`.

There is no `TEMPORARY` or `LONG_RUNNING` task kind. Duration is observed while
the task runs. User tasks share the same recovery semantics from the start.

### 13.3 Task control requests

Control kinds:

- `CANCEL`;
- `APPROVE_TOOL`;
- `REJECT_TOOL`;
- `RESOLVE_EFFECT`;
- `STEER`;
- `CONTINUE`;
- `STOP_AFTER_CHECKPOINT`.

Every control includes:

```text
control_id
task_id
kind
dedup_key
payload_ref
requested_by
requested_at_ms
```

Controls are written to SQLite before a wake hint is emitted. The Worker reads
and acknowledges them at safe boundaries. `CANCEL` of an unclaimed task may be
applied transactionally by Task Service without a Worker.

`CONTINUE` resumes the same task:

- `WAITING_USER -> QUEUED`;
- increment `run_segment`;
- clear the segment wall-clock budget;
- retain cumulative usage and existing tool results;
- keep the same session sequence and task id.

This avoids a continuation task racing with later messages.

### 13.4 Control routing

A structured control response is not a new `USER_TURN` and does not allocate a
session sequence.

Task Service recognizes controls from:

- WebUI or API payload containing `task_id`, `control_id`, and action;
- Channel interactive-action metadata;
- a reply to an Outbox approval/continuation message containing a signed,
  opaque control token;
- an explicit CLI command naming the task.

The token binds task, expected control kind, pending object id, and expiry. It
contains no raw tool arguments.

Task Service does not use an LLM to guess whether arbitrary Prompt text means
approval or continuation. An uncorrelated message remains a normal user turn.
Channels without structured actions must include the opaque token or an exact
documented control command in the reply.

## 14. Task State Machine

### 14.1 States

- `QUEUED`;
- `LEASED`;
- `RUNNING`;
- `RETRY_WAIT`;
- `WAITING_USER`;
- `COMPLETED`;
- `FAILED`;
- `CANCELLED`.

Worker death is not a state.

### 14.2 Allowed transitions

```text
QUEUED       -> LEASED | CANCELLED
LEASED       -> RUNNING | QUEUED | CANCELLED
RUNNING      -> COMPLETED | RETRY_WAIT | WAITING_USER | FAILED | CANCELLED
RETRY_WAIT   -> QUEUED | CANCELLED | FAILED
WAITING_USER -> QUEUED | CANCELLED | FAILED
```

No terminal state transitions to another state.

### 14.3 Execution phases

`checkpoint_phase` uses:

- `ACCEPTED`;
- `INBOUND_SESSION_COMMITTED`;
- `RESTORED`;
- `COMPACTED`;
- `COMMAND_DONE`;
- `CONTEXT_BUILT`;
- `MODEL_DECISION_COMMITTED`;
- `TOOLS_COMMITTED`;
- `SESSION_COMMIT_PREPARED`;
- `SESSION_COMMITTED`;
- `REPLY_ENQUEUED`;
- `TERMINAL`.

Phase advances monotonically within one `run_segment`. A restored task may
re-enter Agent code from the latest phase but must not delete earlier facts.

### 14.4 Business state version versus lease

`state_version` changes on:

- state transition;
- phase transition;
- applied control;
- terminal error update.

Lease renewal changes only:

- `lease_until_ms`;
- `last_heartbeat_at_ms`.

It does not increment `state_version`.

Every Worker mutation checks:

```text
task_id matches
lease_token matches
lease_epoch matches
state is compatible with the operation
```

Optimistic business-state checks may additionally compare `state_version`.

## 15. Session Ordering and Claim Algorithm

### 15.1 Session allocation

Task acceptance uses `BEGIN IMMEDIATE`:

1. insert or read `session_slots`;
2. allocate current `next_sequence`;
3. increment `next_sequence`;
4. insert task;
5. append `TASK_ACCEPTED`;
6. commit.

Deduplication is checked before sequence allocation inside the transaction.

### 15.2 Claimable session head

A task is eligible only when:

- state is `QUEUED`;
- `available_at_ms <= now`;
- it is the smallest non-terminal `session_sequence` for its session;
- `session_slots.active_task_id` is null or already points to it;
- task-kind priority rules permit it;
- required global capacity can be acquired.

The query must select session heads before applying global priority. A later
high-priority task in one session may not overtake an earlier task in that same
session.

Equivalent selection rule:

```sql
SELECT t.task_id
FROM tasks AS t
WHERE t.state = 'QUEUED'
  AND t.available_at_ms <= :now_ms
  AND NOT EXISTS (
      SELECT 1
      FROM tasks AS earlier
      WHERE earlier.session_key = t.session_key
        AND earlier.session_sequence < t.session_sequence
        AND earlier.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
  )
  AND NOT EXISTS (
      SELECT 1
      FROM session_slots AS s
      WHERE s.session_key = t.session_key
        AND s.active_task_id IS NOT NULL
        AND s.active_task_id <> t.task_id
  )
ORDER BY
  CASE WHEN t.recovery_pending = 1 THEN 0 ELSE 1 END,
  t.priority DESC,
  t.available_at_ms ASC,
  t.created_at_ms ASC
LIMIT 1;
```

The production SQL may optimize this with indexes or a CTE but must preserve
the rule.

### 15.3 Claim transaction

Within one `BEGIN IMMEDIATE` transaction:

1. promote a bounded number of due retry rows if needed;
2. select one eligible task;
3. verify or set `session_slots.active_task_id`;
4. generate a new lease token;
5. increment `lease_epoch`;
6. set `LEASED`, owner, lease deadline, and heartbeat;
7. increment task attempt count only when the claim is charged as a new root
   attempt;
8. append `TASK_LEASED`;
9. commit.

No Agent, Provider, filesystem, or IPC call occurs inside this transaction.

Root-attempt charging is deterministic:

- the first claim increments `root_attempt_count`;
- a claim with `recovery_pending=1` increments it and clears
  `recovery_pending`;
- promotion from task-level `RETRY_WAIT` sets `recovery_pending=1`;
- lease reclaim sets `recovery_pending=1`;
- resuming from approval, steering, or an approved run-segment continuation
  does not increment it;
- graceful release at a completed safe checkpoint does not increment it;
- no claim is issued when the next increment would exceed
  `max_root_attempts`; the task fails terminally instead.

### 15.4 Session slot release

The slot is cleared in the same transaction that:

- completes;
- fails terminally;
- cancels terminally; or
- returns a task to `QUEUED` or `RETRY_WAIT` after releasing its lease.

`WAITING_USER` also releases the active slot, but later session tasks remain
blocked because the waiting task is still the earliest non-terminal sequence.
This lets the Worker run another session without violating order.

## 16. SQLite Runtime Ledger

### 16.1 File and connection settings

Default path:

```text
<workspace>/runtime/runtime.sqlite
```

Every process creates its own connections with:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=FULL;
PRAGMA busy_timeout=5000;
PRAGMA temp_store=MEMORY;
```

Rules:

- one connection is never used concurrently by multiple threads;
- async callers use a dedicated DB executor or short synchronous calls outside
  the event loop;
- transactions are short and use parameterized SQL;
- `BEGIN IMMEDIATE` is reserved for allocation, claim, reclaim, session commit
  bookkeeping, task completion, and Outbox claim;
- Lightweight Host or Supervisor performs migration before starting execution
  slots or child processes;
- migration takes an exclusive startup lock; Workers validate version but never
  migrate;
- a process refuses work when its schema version differs from the binary.

### 16.2 Required tables

The first implementation uses the following tables:

1. `schema_migrations`;
2. `tasks`;
3. `session_slots`;
4. `task_events`;
5. `checkpoints`;
6. `model_attempts`;
7. `tool_calls`;
8. `tool_attempts`;
9. `task_controls`;
10. `session_commits`;
11. `outbox`;
12. `resource_leases`;
13. `runtime_blobs`.

There is deliberately no durable `workers` table. Supervisor process state is
owned by Supervised Host, and active work is fenced by task leases. This avoids
a second liveness authority.

### 16.3 `schema_migrations`

```text
version             INTEGER PRIMARY KEY
name                TEXT NOT NULL
checksum            TEXT NOT NULL
applied_at_ms       INTEGER NOT NULL
```

Migration files are immutable after merge. Startup validates checksums.

### 16.4 `tasks`

```text
task_id                 TEXT PRIMARY KEY
turn_id                 TEXT UNIQUE
protocol_version        INTEGER NOT NULL
tenant_id               TEXT NOT NULL
principal_id            TEXT NOT NULL
agent_id                 TEXT NOT NULL
workspace_id             TEXT NOT NULL
session_key              TEXT NOT NULL
session_sequence         INTEGER NOT NULL
channel                  TEXT
channel_account          TEXT
channel_message_id       TEXT
dedup_key                TEXT
task_kind                TEXT NOT NULL
priority                 INTEGER NOT NULL
payload_blob_id          TEXT NOT NULL
payload_hash             TEXT NOT NULL
state                    TEXT NOT NULL
checkpoint_phase         TEXT NOT NULL
run_segment              INTEGER NOT NULL DEFAULT 0
root_attempt_count       INTEGER NOT NULL DEFAULT 0
max_root_attempts        INTEGER NOT NULL
recovery_pending         INTEGER NOT NULL DEFAULT 0
leased_by                TEXT
lease_token              TEXT
lease_epoch              INTEGER NOT NULL DEFAULT 0
lease_until_ms           INTEGER
last_heartbeat_at_ms     INTEGER
last_progress_at_ms      INTEGER
available_at_ms          INTEGER NOT NULL
state_version            INTEGER NOT NULL DEFAULT 0
control_cursor           INTEGER NOT NULL DEFAULT 0
cumulative_input_tokens  INTEGER NOT NULL DEFAULT 0
cumulative_output_tokens INTEGER NOT NULL DEFAULT 0
error_code               TEXT
error_summary            TEXT
waiting_reason           TEXT
waiting_ref              TEXT
wait_until_ms            INTEGER
created_at_ms            INTEGER NOT NULL
updated_at_ms            INTEGER NOT NULL
completed_at_ms          INTEGER
```

Constraints:

- unique `(session_key, session_sequence)`;
- unique `(tenant_id, channel, channel_account, channel_message_id)` when
  Channel fields are non-null;
- unique `(tenant_id, agent_id, workspace_id, task_kind, dedup_key)` when
  `dedup_key` is non-null;
- state and kind checked against enumerated values;
- terminal rows have no lease;
- a leased or running row has owner, token, epoch, and deadline;
- counters are non-negative.

Indexes:

- `(state, available_at_ms, priority, created_at_ms)`;
- `(session_key, session_sequence, state)`;
- `(lease_until_ms)` for leased or running rows;
- `(recovery_pending, state, priority)`;
- `(task_kind, state)`;
- `(state, wait_until_ms)` for waiting tasks;
- `(tenant_id, agent_id, workspace_id, task_kind, dedup_key)`;
- `(tenant_id, channel, channel_account, channel_message_id)`.

### 16.5 `session_slots`

```text
session_key          TEXT PRIMARY KEY
next_sequence        INTEGER NOT NULL
active_task_id       TEXT
state_version        INTEGER NOT NULL DEFAULT 0
updated_at_ms        INTEGER NOT NULL
```

`active_task_id` references `tasks(task_id)`. It is a fast ownership guard, not
the only ordering check.

### 16.6 `task_events`

```text
event_seq            INTEGER PRIMARY KEY AUTOINCREMENT
event_id             TEXT UNIQUE NOT NULL
task_id              TEXT NOT NULL
event_type           TEXT NOT NULL
phase                TEXT
safe_payload_json    TEXT
payload_blob_id      TEXT
lease_epoch          INTEGER
created_at_ms        INTEGER NOT NULL
```

Rules:

- immutable while retained;
- a trigger rejects `UPDATE`;
- normal Runtime Store APIs expose insert and read only;
- the private retention transaction may delete events only after their parent
  task is terminal and past retention;
- raw content is not placed in `safe_payload_json`;
- high-frequency Token deltas are not durable events.

Required event types:

- `TASK_ACCEPTED`;
- `TASK_LEASED`;
- `TASK_RUNNING`;
- `CHECKPOINT_SAVED`;
- `MODEL_STARTED`;
- `MODEL_COMPLETED`;
- `MODEL_FAILED`;
- `TOOL_PREPARED`;
- `TOOL_STARTED`;
- `TOOL_COMPLETED`;
- `TOOL_FAILED`;
- `TOOL_OUTCOME_UNKNOWN`;
- `CONTROL_RECEIVED`;
- `CONTROL_APPLIED`;
- `SESSION_COMMIT_PREPARED`;
- `SESSION_COMMITTED`;
- `OUTBOX_ENQUEUED`;
- `TASK_RETRY_WAIT`;
- `TASK_WAITING_USER`;
- `TASK_COMPLETED`;
- `TASK_FAILED`;
- `TASK_CANCELLED`;
- `LEASE_RECLAIMED`.

### 16.7 `checkpoints`

```text
checkpoint_id        TEXT PRIMARY KEY
task_id              TEXT NOT NULL
format_version       INTEGER NOT NULL
phase                TEXT NOT NULL
run_segment          INTEGER NOT NULL
ordinal              INTEGER NOT NULL
payload_blob_id      TEXT NOT NULL
payload_hash         TEXT NOT NULL
lease_epoch          INTEGER NOT NULL
created_at_ms        INTEGER NOT NULL
```

Constraints:

- unique `(task_id, run_segment, ordinal)`;
- immutable after insert;
- a checkpoint is visible only after its row and event commit.

Index `(task_id, run_segment DESC, ordinal DESC)` supports restore.

Retention keeps the latest terminal checkpoint until the task retention window
expires. Older non-terminal checkpoints may be compacted only after a newer
checkpoint is verified.

### 16.8 `model_attempts`

One table replaces separate logical-call and attempt services.

```text
model_attempt_id     TEXT PRIMARY KEY
task_id              TEXT NOT NULL
logical_call_id      TEXT NOT NULL
attempt_no           INTEGER NOT NULL
provider_name        TEXT NOT NULL
model_name           TEXT NOT NULL
request_hash         TEXT NOT NULL
state                TEXT NOT NULL
response_blob_id     TEXT
response_hash        TEXT
input_tokens         INTEGER
output_tokens        INTEGER
finish_reason        TEXT
error_code           TEXT
error_summary        TEXT
started_at_ms        INTEGER NOT NULL
finished_at_ms       INTEGER
```

Constraints:

- unique `(task_id, logical_call_id, attempt_no)`;
- state is `STARTED`, `COMPLETED`, or `FAILED`;
- terminal fields match state.

Indexes:

- `(task_id, logical_call_id, attempt_no)`;
- `(state, started_at_ms)`.

Provider calls may be repeated after an unknown crash because they do not
perform Agent-visible external writes. Duplicate billing is observable but not
treated as exactly-once.

### 16.9 `tool_calls`

```text
task_id              TEXT NOT NULL
tool_call_id         TEXT NOT NULL
tool_name            TEXT NOT NULL
arguments_blob_id    TEXT NOT NULL
arguments_hash       TEXT NOT NULL
effect_class         TEXT NOT NULL
risk_class           TEXT NOT NULL
idempotency_mode     TEXT NOT NULL
idempotency_key      TEXT NOT NULL
approval_policy      TEXT NOT NULL
recovery_policy      TEXT NOT NULL
concurrency_scope    TEXT NOT NULL
state                TEXT NOT NULL
attempt_count        INTEGER NOT NULL DEFAULT 0
result_blob_id       TEXT
result_hash          TEXT
effect_receipt_ref   TEXT
error_code           TEXT
error_summary        TEXT
created_at_ms        INTEGER NOT NULL
updated_at_ms        INTEGER NOT NULL
PRIMARY KEY (task_id, tool_call_id)
```

Logical states:

- `PREPARED`;
- `WAITING_APPROVAL`;
- `RUNNING`;
- `SUCCEEDED`;
- `FAILED`;
- `OUTCOME_UNKNOWN`;
- `REJECTED`.

The same logical tool call always has the same normalized arguments hash and
idempotency key. A mismatch is `TOOL_CALL_ID_CONFLICT` and fails safely.

Indexes:

- `(task_id, state)`;
- `(idempotency_key)`;
- `(state, updated_at_ms)`.

### 16.10 `tool_attempts`

```text
tool_attempt_id      TEXT PRIMARY KEY
task_id              TEXT NOT NULL
tool_call_id         TEXT NOT NULL
attempt_no           INTEGER NOT NULL
state                TEXT NOT NULL
resource_token       TEXT
effect_receipt_ref   TEXT
error_code           TEXT
error_summary        TEXT
started_at_ms        INTEGER NOT NULL
finished_at_ms       INTEGER
```

Constraints:

- foreign key `(task_id, tool_call_id)`;
- unique `(task_id, tool_call_id, attempt_no)`;
- state is `STARTED`, `SUCCEEDED`, `FAILED`, or `OUTCOME_UNKNOWN`.

Index `(task_id, tool_call_id, attempt_no)` supports restore.

### 16.11 `task_controls`

This table replaces a separate approvals subsystem.

```text
control_seq          INTEGER PRIMARY KEY AUTOINCREMENT
control_id           TEXT UNIQUE NOT NULL
task_id              TEXT NOT NULL
kind                 TEXT NOT NULL
dedup_key            TEXT NOT NULL
payload_blob_id      TEXT
requested_by         TEXT NOT NULL
state                TEXT NOT NULL
outcome_code         TEXT
requested_at_ms      INTEGER NOT NULL
applied_at_ms        INTEGER
```

Constraints:

- unique `(task_id, dedup_key)`;
- state is `PENDING`, `APPLIED`, `REJECTED`, or `EXPIRED`.

Index `(task_id, control_seq, state)` supports ordered consumption.

Approval payload identifies the exact tool call and arguments hash. Approval of
one call never authorizes modified arguments.

### 16.12 `session_commits`

```text
session_commit_id    TEXT PRIMARY KEY
task_id              TEXT NOT NULL
commit_kind          TEXT NOT NULL
session_key          TEXT NOT NULL
base_revision        INTEGER NOT NULL
target_revision      INTEGER NOT NULL
content_hash         TEXT NOT NULL
state                TEXT NOT NULL
error_code           TEXT
created_at_ms        INTEGER NOT NULL
committed_at_ms      INTEGER
```

States:

- `PREPARED`;
- `COMMITTED`;
- `CONFLICT`.

The transcript is not stored here. `content_hash` covers the normalized turn
mutation supplied to Session Manager.

Constraints:

- unique `(task_id, commit_kind)`;
- `commit_kind` is `INBOUND` or `FINAL`;
- one claimed user task has exactly one committed inbound mutation before its
  first model or tool call;
- a successful user task has exactly one committed final mutation before
  completion.

Index `(session_key, state, created_at_ms)` supports recovery and conflict
diagnostics.

### 16.13 `outbox`

```text
outbox_id            INTEGER PRIMARY KEY AUTOINCREMENT
task_id              TEXT NOT NULL
channel              TEXT NOT NULL
channel_account      TEXT NOT NULL
target_key           TEXT NOT NULL
message_kind         TEXT NOT NULL
payload_blob_id      TEXT NOT NULL
payload_hash         TEXT NOT NULL
dedup_key            TEXT UNIQUE NOT NULL
state                TEXT NOT NULL
attempt_count        INTEGER NOT NULL DEFAULT 0
max_attempts         INTEGER NOT NULL
available_at_ms      INTEGER NOT NULL
leased_by            TEXT
lease_token          TEXT
lease_epoch          INTEGER NOT NULL DEFAULT 0
lease_until_ms       INTEGER
provider_receipt_ref TEXT
error_code           TEXT
error_summary        TEXT
created_at_ms        INTEGER NOT NULL
delivered_at_ms      INTEGER
```

States:

- `PENDING`;
- `SENDING`;
- `RETRY_WAIT`;
- `OUTCOME_UNKNOWN`;
- `DELIVERED`;
- `FAILED`.

`outbox_id` is the delivery sequence. A row is claimable only when no earlier
non-terminal row exists for the same `(channel, channel_account, target_key)`.
This prevents a retrying message from being overtaken by a later final reply.
`OUTCOME_UNKNOWN` is non-terminal and blocks later messages to that target
until an operator or user explicitly resolves it.

`target_key` is an opaque canonical delivery identity that includes tenant and
Channel conversation scope. It is safe for logs and does not expose a raw phone
number, email address, or provider recipient id.

Indexes:

- `(state, available_at_ms, outbox_id)`;
- `(channel, channel_account, target_key, outbox_id, state)`;
- `(lease_until_ms)` for `SENDING` rows.

Normative dedup-key forms:

- final reply: `sha256(task_id + ":final-reply")`;
- terminal failure reply: `sha256(task_id + ":terminal-reply")`;
- Message Tool: `sha256(task_id + ":" + tool_call_id + ":message")`;
- waiting prompt:
  `sha256(task_id + ":" + waiting_reason + ":" + waiting_ref)`.

Payload hash mismatch for an existing dedup key is a correctness error and
never overwrites the original row.

### 16.14 `resource_leases`

```text
resource_key         TEXT NOT NULL
holder_kind          TEXT NOT NULL
holder_id            TEXT NOT NULL
units                INTEGER NOT NULL
lease_token          TEXT NOT NULL
lease_until_ms       INTEGER NOT NULL
created_at_ms        INTEGER NOT NULL
updated_at_ms        INTEGER NOT NULL
PRIMARY KEY (resource_key, holder_kind, holder_id)
```

This one generic table replaces separate resource-lock and quota-lease tables.
It supports:

- globally exclusive tools;
- workspace-scoped tools;
- Provider quotas;
- global subagent capacity;
- bounded maintenance capacity.

Expired rows are reclaimable. Every protected operation checks its lease token
before recording success.

Each configured `resource_key` has a capacity supplied by runtime
configuration or tool policy. Acquisition uses one `BEGIN IMMEDIATE`
transaction:

1. delete or ignore expired holders for the key;
2. sum units for unexpired holders;
3. reject when `used + requested > capacity`;
4. insert or replace only the requesting holder's lease with a new token;
5. commit.

Renewal and release match the exact holder and token. An exclusive resource is
capacity one with a one-unit request. Capacity configuration is not stored as a
second authority in SQLite.

Index `(resource_key, lease_until_ms)` supports capacity and expiry scans.

### 16.15 `runtime_blobs`

```text
blob_id              TEXT PRIMARY KEY
scope_key            TEXT NOT NULL
blob_kind            TEXT NOT NULL
content_hash         TEXT NOT NULL
encoding             TEXT NOT NULL
compression          TEXT
encryption_key_id    TEXT
inline_content       BLOB
external_ref         TEXT
size_bytes           INTEGER NOT NULL
created_at_ms        INTEGER NOT NULL
```

Constraints:

- unique `(scope_key, blob_kind, content_hash)`;
- exactly one of `inline_content` or `external_ref` is present;
- sensitive blobs use the project's protected storage policy;
- blob deletion verifies no non-terminal or retained row references it.

Index `(created_at_ms)` supports bounded retention scans.

Blob deduplication never crosses tenant/principal protection scope or encryption
key scope. `blob_id` is opaque and is not the raw content hash.

### 16.16 Foreign keys and deletion order

All task-owned rows reference `tasks(task_id)` with `ON DELETE RESTRICT`.
Blob-reference columns reference `runtime_blobs(blob_id)` with
`ON DELETE RESTRICT`. Composite tool-attempt references use the tool-call
primary key.

`session_slots.active_task_id` uses `ON DELETE SET NULL`, but terminalization is
still required to clear it explicitly in the same business transaction.

Retention deletes in this order:

1. expired model and tool attempts;
2. expired controls, checkpoints, session commits, and task events;
3. delivered or waived Outbox rows;
4. terminal tasks;
5. unreferenced blobs.

Non-terminal tasks, non-terminal Outbox rows, and active resource leases are
never retention targets. Foreign-key failure aborts the batch and emits a safe
maintenance error.

## 17. Transaction Boundaries

### 17.1 Accept task

One transaction:

1. insert or reuse payload blob;
2. find duplicate inbound identity;
3. allocate session sequence only when new;
4. insert task;
5. append `TASK_ACCEPTED`;
6. commit.

Only after commit may ingress acknowledge acceptance or emit a Worker wake
hint. The protected task payload is the durable source for an accepted but not
yet executed user request.

The Worker commits the triggering user message only after the task becomes the
session head and is claimed. It does so before any model or tool call. This
prevents a later queued message from advancing the transcript revision while
an earlier task is still preparing its final session commit.

If a user task is cancelled before its first claim, the request remains visible
in Runtime Store and task history but is not appended to the Agent transcript.
This deliberate behavior avoids creating a transcript entry that was never
presented to Agent Core.

### 17.2 Claim task

One transaction performs the claim algorithm in section 15. No work outside
SQLite occurs until commit returns a `TaskClaim`.

### 17.3 Start running

One short transaction:

- validate token and epoch;
- transition `LEASED -> RUNNING`;
- set initial progress time;
- append `TASK_RUNNING`.

### 17.4 Save checkpoint

Outside transaction:

- serialize and hash the complete checkpoint;
- store any external large blob.

One transaction:

- validate lease;
- insert or reuse runtime blob;
- insert immutable checkpoint;
- advance compatible phase;
- update progress;
- append `CHECKPOINT_SAVED`.

### 17.5 Model attempt

Before network call, one transaction:

- validate lease;
- insert `STARTED` model attempt;
- append `MODEL_STARTED`.

Network call and Provider retries occur without a database transaction.

For each attempt completion, one transaction:

- validate lease;
- store protected response;
- mark attempt terminal;
- update usage totals;
- append `MODEL_COMPLETED` or `MODEL_FAILED`.

Runner receives a completed model decision only after commit.

### 17.6 Tool call

Preparation transaction:

- validate lease;
- store normalized arguments;
- insert or verify logical `tool_calls` row;
- append `TOOL_PREPARED`;
- if approval is required, set `WAITING_APPROVAL`.

Attempt-start transaction:

- validate task lease and resource lease;
- insert attempt;
- set logical call `RUNNING`;
- append `TOOL_STARTED`.

The tool runs with no SQLite transaction open.

Attempt-finish transaction:

- validate task lease and resource lease;
- store protected result or safe error;
- mark attempt and logical call terminal;
- append terminal tool event.

Runner receives a tool result only after this transaction commits.

### 17.7 Session commit

The session filesystem and SQLite cannot share a transaction. Use an
idempotent prepare/apply/confirm protocol:

1. build the normalized session mutation outside SQLite;
2. derive the stable commit id from task id and commit kind;
3. transactionally validate the task lease, insert or verify
   `session_commits(PREPARED)`, and append `SESSION_COMMIT_PREPARED`;
4. call `SessionManager.commit_turn()` with:
   - `session_key`;
   - `session_commit_id`;
   - `commit_kind`;
   - `base_revision`;
   - normalized messages and metadata;
   - content hash;
5. transactionally revalidate the task lease, mark the commit `COMMITTED`,
   advance the compatible task phase, and append `SESSION_COMMITTED`.

Crash behavior:

- before step 3: repeat from task payload or checkpoint;
- after step 3 but before filesystem commit: repeat `commit_turn`;
- after filesystem commit but before step 5: `commit_turn` detects the commit id
  and returns the committed revision; then step 5 is repeated;
- if the Worker becomes fenced after the filesystem commit, it stops; the next
  lease holder detects the same commit id and performs step 5;
- revision mismatch without the same commit id: never overwrite; record
  `CONFLICT` and follow section 23.4.

Commit-kind behavior:

- `INBOUND` appends the sanitized triggering user message after claim and
  before Agent Core performs a model or tool call;
- `FINAL` appends only messages produced after that inbound message, including
  assistant and tool transcript entries;
- a final mutation never appends the triggering user message a second time.

The `PREPARED` row immutably binds commit id, base revision, target revision,
and content hash. A Worker that loses its lease after preparation may still
finish the already-started atomic filesystem replace, because a filesystem and
SQLite cannot share a fence transaction. This is safe only because:

- Session Manager accepts exactly the prepared commit id and content hash;
- a later Worker must reuse the same prepared mutation;
- a different hash for that commit id is rejected;
- the stale Worker cannot confirm the Runtime Store transition.

No unprepared or changed session mutation from a stale Worker may be applied.

### 17.8 Complete and enqueue final reply

After session commit is confirmed, one transaction:

1. validate task lease;
2. when a final reply is present, insert or reuse its protected payload;
3. insert its Outbox row with stable dedup key, or, when current Message Tool
   semantics suppress the final reply, verify that the referenced Message Tool
   Outbox row is already durable;
4. set task `COMPLETED`, phase `TERMINAL`, and terminal time;
5. clear lease and session active slot;
6. append `OUTBOX_ENQUEUED` when a new row was inserted, then append
   `TASK_COMPLETED`;
7. commit.

`COMPLETED` means Agent execution and durable enqueue succeeded. Delivery
status is queried from Outbox.

A user task may complete without creating a separate final-reply row only when
its structured completion explicitly sets `suppress_final=True` and references
at least one durable Message Tool Outbox row for that task. An empty accidental
final response is not sufficient.

### 17.9 Outbox delivery

Claim transaction:

- select the earliest eligible row whose target has no earlier non-terminal
  row;
- issue a new delivery lease;
- set `SENDING`.

Channel call occurs outside SQLite.

Outbox Sender renews the delivery lease while the bounded Channel call is in
progress. Every adapter has a finite send timeout. Loss of the delivery lease
prevents that sender from recording a result and invokes the recovery policy in
section 17.13.

Finish transaction:

- validate delivery token and epoch;
- mark `DELIVERED` with receipt, or schedule `RETRY_WAIT`, or mark `FAILED`.

### 17.10 Terminal failure or cancellation

One transaction:

- validate task lease when Worker-owned;
- write safe error metadata;
- transition terminally;
- clear lease and session active slot;
- append terminal event.

If a final user-visible failure message is required, enqueue it in the same
transaction.

Internal tasks without a user-visible response use `complete_internal()`:

- validate the task lease;
- record the internal result reference when required;
- set `COMPLETED`;
- clear the lease and session active slot;
- append `TASK_COMPLETED`;
- do not create a fake Channel or Outbox row.

### 17.11 Enter `WAITING_USER`

One transaction:

1. validate task lease;
2. persist the exact pending approval, continuation, conflict, or unknown-effect
   fact;
3. create a signed opaque control token;
4. insert the user-facing question into Outbox with a stable dedup key;
5. set `waiting_reason`, `waiting_ref`, and the policy-specific
   `wait_until_ms`;
6. transition `RUNNING -> WAITING_USER`;
7. clear the task lease and `session_slots.active_task_id`;
8. append `OUTBOX_ENQUEUED` and `TASK_WAITING_USER`;
9. commit.

The task remains the earliest non-terminal session task, so later turns do not
overtake it. Outbox delivery proceeds independently. Repeating the transaction
after a crash reuses the same pending fact and Outbox dedup key.

Timeout policy:

- tool approval expiry creates an idempotent `REJECT_TOOL` control and resumes
  the task so Runner receives a policy rejection;
- continuation expiry cancels the task with
  `TASK_CONTINUATION_EXPIRED`;
- session conflicts and unknown external effects have no automatic expiry and
  require explicit resolution;
- every waiting task older than its operational alert threshold raises an
  alert even when it has no automatic expiry.

### 17.12 Append and apply control

Task Service appends a control in one transaction:

1. validate the task exists and is non-terminal;
2. deduplicate `(task_id, dedup_key)`;
3. for tool approval or rejection, validate the exact `tool_call_id` and
   arguments hash;
4. insert `task_controls(PENDING)`;
5. append `CONTROL_RECEIVED`;
6. when the task is `WAITING_USER`, transition it to `QUEUED` only for:
   - `APPROVE_TOOL` or `REJECT_TOOL` matching its pending tool;
   - `CONTINUE` matching its run-segment continuation request;
   - an explicit resolution control for its recorded unknown effect;
   set `available_at_ms=now` and leave `recovery_pending=0`;
7. when the task is `QUEUED` and the control is `CANCEL`, transition directly
   to `CANCELLED`; a `WAITING_USER` task may cancel directly only when its
   waiting reason proves no external effect is unknown; otherwise cancellation
   remains pending until `RESOLVE_EFFECT`;
8. commit, then emit a wake hint.

On restore, the Worker applies controls in `control_seq` order:

- `APPROVE_TOOL` changes the matching logical tool call from
  `WAITING_APPROVAL` to `PREPARED`;
- `REJECT_TOOL` changes it to `REJECTED` and returns a durable policy result to
  Runner;
- `RESOLVE_EFFECT` records whether an uncertain external tool effect should be
  treated as completed, failed, or explicitly retried;
- `CONTINUE` increments `run_segment` exactly once, resets the segment
  wall-clock counter, and sets the new segment phase to `RESTORED` while
  retaining the prior checkpoint and cumulative budgets;
- `STEER` adds a protected context reference for the next model boundary;
- `STOP_AFTER_CHECKPOINT` sets a task-local stop flag;
- `CANCEL` follows the cancellation safety rules.

The acknowledgement transaction updates the control state, advances
`tasks.control_cursor`, increments `state_version`, and appends
`CONTROL_APPLIED`. Reprocessing an already acknowledged control is a no-op.

### 17.13 Recover Outbox send

An expired `SENDING` lease is resolved according to Channel capability:

- `NATIVE_IDEMPOTENCY`: return to `RETRY_WAIT` and reuse `dedup_key`;
- `QUERYABLE_RECEIPT`: query by stable key; mark delivered when found,
  otherwise retry;
- `NONE`: move to `OUTCOME_UNKNOWN` and do not automatically resend.

The capability is declared by each Channel adapter and validated at startup.
Resolving `OUTCOME_UNKNOWN` requires an explicit operational action:

- `MARK_DELIVERED` with an optional provider receipt; or
- `RETRY`, which records who accepted duplicate-delivery risk before returning
  the row to `PENDING`.

## 18. Agent Outer and Inner Loop Integration

### 18.1 Outer turn state machine

The existing Turn Executor remains authoritative:

```text
RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
```

Allowed shortcuts:

```text
RESTORE -> COMMAND
RESTORE -> BUILD
COMMAND -> SAVE
BUILD   -> RUN
RUN     -> SAVE
SAVE    -> RESPOND
RESPOND -> DONE
```

All user-visible commands pass through `SAVE` and `RESPOND`. The existing
`COMMAND -> DONE` shortcut must be removed because it bypasses durable session
commit and Outbox.

### 18.2 Phase responsibilities

#### RESTORE

- load protected inbound task payload;
- for a user task, load the fresh session revision and idempotently commit the
  sanitized triggering user message as `INBOUND` when it is not already
  committed;
- ensure task phase is at least `INBOUND_SESSION_COMMITTED` without regressing a
  later restored phase;
- reload the revision-aware session and verify the triggering user message is
  present exactly once;
- load the latest valid durable checkpoint;
- reconstruct completed model and tool decisions by reference;
- load pending controls after `control_cursor`;
- set task-scoped `TurnRuntime`.

#### COMPACT

- use existing Context Governor and memory semantics;
- write a checkpoint after durable compaction output is known;
- never replace authoritative memory behavior.

#### COMMAND

- execute existing command shortcuts;
- journal any tool or external effect through the same ports;
- continue to `SAVE`.

#### BUILD

- build model context using current session, memory, media, Planner, injection,
  and budget logic;
- treat the already committed inbound session message as the triggering user
  message and do not append the protected payload a second time;
- do not store raw context in task events;
- checkpoint only references and hashes required for deterministic restore.

#### RUN

- invoke the existing Runner;
- journal each Provider attempt through the Provider observer;
- execute every Agent tool request through `ToolExecutionPort`;
- checkpoint after a committed model decision and after each committed tool
  batch.

#### SAVE

- build one normalized idempotent `FINAL` session mutation containing only
  assistant/tool messages produced after the inbound commit;
- call `SessionCommitPort`;
- checkpoint `SESSION_COMMITTED`.

#### RESPOND

- build the final complete message;
- call Runtime completion, which atomically enqueues Outbox;
- realtime events remain advisory.

#### DONE

- release task-local objects;
- clear `TurnRuntime`;
- stop Worker lease renewal for the task.

### 18.3 Inner loop recovery boundaries

Required safe boundaries:

1. before a logical model call;
2. after a model attempt result is durable;
3. before each tool call is externally invoked;
4. after each tool result is durable;
5. after a tool batch is complete;
6. before session commit;
7. after session commit;
8. after final reply enqueue.

If a process dies between boundaries, recovery uses the last durable fact and
the operation-specific policy. It never fabricates a synthetic tool error as a
substitute for an unknown real effect.

### 18.4 Checkpoint contents

A checkpoint contains enough information to continue without raw log replay:

- checkpoint format version;
- task, turn, session, sequence, and run segment;
- outer phase and inner loop iteration;
- session base revision;
- normalized inbound payload reference;
- context-governor state and bounded context references;
- Planner state;
- budget counters;
- logical model call cursor;
- completed tool call ids and result references;
- pending prepared tool call ids;
- control cursor;
- response draft reference when applicable;
- memory source revision references;
- safe deterministic flags.

Do not copy full artifacts into each checkpoint. Use content-addressed blob
references.

## 19. Provider Attempt Visibility

Current Provider retry and fallback behavior may remain inside Provider
implementations, but it must expose an observer:

```python
class ProviderAttemptObserver(Protocol):
    async def started(self, value: ProviderAttemptStarted) -> str: ...
    async def completed(
        self, attempt_id: str, value: ProviderAttemptCompleted
    ) -> None: ...
    async def failed(
        self, attempt_id: str, value: ProviderAttemptFailed
    ) -> None: ...
```

Runner derives `logical_call_id` deterministically from:

```text
task_id + run_segment + inner_loop_iteration + model_call_ordinal
```

It must not generate a new logical id when replaying the same checkpoint.

Rules:

- Base retry and fallback Provider call the observer for every network attempt;
- Provider does not write SQLite directly;
- Runtime supplies an observer backed by `TurnJournalPort`;
- tests and legacy mode may supply a no-op observer;
- a completed response is returned to Runner only after the observer's
  `completed()` succeeds;
- if journaling fails, the response is discarded and the task enters a
  storage-related retry path;
- safe metrics include provider, model, latency, usage, and error code;
- raw request and response content stay in protected blobs.

Recovery of a logical model call is deterministic:

- a `COMPLETED` attempt is restored and its durable response is reused;
- a `FAILED` attempt is included in retry/fallback accounting;
- a `STARTED` attempt with no terminal record after lease loss is marked
  `FAILED` with `MODEL_ATTEMPT_LOST`;
- because a model request has no Agent-visible write effect, the next bounded
  attempt may repeat it;
- a repeated attempt uses the same `logical_call_id` and the next
  `attempt_no`;
- attempt and cumulative Token budgets are checked before repeating.

## 20. Tool Gateway Safety Model

### 20.1 Invocation-specific metadata

Existing static tool metadata remains supported:

- `read_only`;
- `concurrency_safe`;
- `exclusive`.

Preparation adds effective invocation metadata:

```text
effect_class       READ | LOCAL_WRITE | EXTERNAL_WRITE
risk_class         LOW | MEDIUM | HIGH
idempotency_mode   REPLAY_SAFE | NATIVE_KEY | RUNTIME_RESULT | NONE
approval_policy    NEVER | POLICY | ALWAYS
recovery_policy    REPLAY | QUERY_THEN_RETRY | REUSE_RESULT | MANUAL
concurrency_scope  NONE | SESSION | WORKSPACE | GLOBAL
progress_required  bool
timeout_s          positive integer
```

`ToolRegistry.prepare()` or a narrow classifier hook returns one
`PreparedToolCall` containing normalized arguments plus this effective
metadata. Tool Gateway does not independently parse tool arguments.

### 20.2 Conservative compatibility defaults

Until a tool has explicit metadata:

- `read_only=True`
  - `effect_class=READ`;
  - `risk_class=LOW`;
  - `idempotency_mode=REPLAY_SAFE`;
  - `recovery_policy=REPLAY`;
- other tools
  - `effect_class=EXTERNAL_WRITE`;
  - `risk_class=HIGH`;
  - `idempotency_mode=NONE`;
  - `approval_policy=POLICY`;
  - `recovery_policy=MANUAL`.

Rollout includes an explicit metadata audit for every built-in tool. A tool may
not receive a less conservative classification only to preserve old tests.

### 20.3 Stable idempotency key

Default:

```text
sha256(
  task_id + "\n" +
  tool_call_id + "\n" +
  tool_name + "\n" +
  normalized_arguments_hash
)
```

The same logical call reuses this key across Worker attempts and restarts.

### 20.4 Policy outcomes

- `ALLOW`: execute immediately.
- `WAIT_APPROVAL`: persist exact call and move task to `WAITING_USER`.
- `DENY`: return a durable policy error to Runner.
- `REUSE`: return an already durable terminal result.
- `MANUAL_RECOVERY`: move task to `WAITING_USER` because effect outcome is
  uncertain.

### 20.5 Recovery matrix

| Durable state at recovery | Policy | Action |
|---|---|---|
| `PREPARED` | any | Start first attempt when allowed |
| `WAITING_APPROVAL` | any | Wait for exact approval or rejection |
| `SUCCEEDED` | any | Reuse durable result |
| `FAILED` | retry allowed | Create next bounded attempt |
| `RUNNING` without terminal attempt | `REPLAY_SAFE` | Mark prior attempt unknown, retry |
| `RUNNING` without terminal attempt | `NATIVE_KEY` | Query provider or retry with same key |
| `RUNNING` without terminal attempt | `RUNTIME_RESULT` | Reuse only if receipt/result is durable |
| `RUNNING` without terminal attempt | `NONE` | Mark unknown and wait for user |

### 20.6 Message Tool

`MessageTool` no longer calls a Channel callback directly.

Its external effect is:

1. validate and prepare message;
2. enqueue one Outbox record with a stable dedup key;
3. durably complete the tool call with `outbox_id` as its receipt;
4. let Outbox Sender perform actual Channel delivery.

Steps 2 and 3 occur in one Runtime Store transaction under the task lease.

This gives final replies and tool-originated messages one delivery authority and
one ordering rule.

### 20.7 Shell and child processes

Shell risk is invocation-specific. A command classifier may raise risk but
never lower an explicit tool policy.

Every spawned process belongs to the task's containment group:

- Windows: Job Object with kill-on-close;
- POSIX: dedicated process group plus parent-death behavior where available;
- containers: preserve the host PID limit and terminate the task group.

On Worker termination, cancellation, or hard timeout, the entire child tree is
terminated before the task lease is released. Unknown external effects still
follow the recovery matrix.

### 20.8 Tool retry ownership

Tool Gateway owns application-level retry count.

Defaults:

- replay-safe read: up to two retries for classified transient failures;
- native-idempotent write: up to two retries with the same key;
- runtime-result write: no retry after invocation unless a durable result or
  receipt proves the outcome;
- non-idempotent write: one attempt, then manual recovery if outcome is
  unknown.

A tool implementation may perform protocol-internal retries only when they use
the same native idempotency key or are side-effect-free. It reports the final
classified attempt result to Tool Gateway; it does not maintain a competing
unbounded retry loop.

## 21. Session Persistence

### 21.1 Required Session Manager API

Add:

```python
class SessionSnapshot:
    session_key: str
    revision: int
    messages: list[Message]
    applied_commit_ids: BoundedCommitIndex
```

```python
async def load_fresh(session_key: str) -> SessionSnapshot: ...

async def commit_turn(
    session_key: str,
    commit_id: str,
    commit_kind: Literal["INBOUND", "FINAL"],
    base_revision: int,
    mutation: SessionMutation,
    content_hash: str,
) -> SessionCommitOutcome: ...
```

Outcomes:

- `COMMITTED(revision)`;
- `ALREADY_COMMITTED(revision)`;
- `REVISION_CONFLICT(current_revision)`;
- `IO_FAILURE(safe_error)`.

### 21.2 Cross-process correctness

`commit_turn()` must:

1. acquire an OS-visible per-session file lock;
2. reload the current on-disk session, bypassing process cache;
3. return `ALREADY_COMMITTED` if the same commit id and hash exist;
4. reject reuse of a commit id with a different hash;
5. compare `base_revision`;
6. apply the normalized mutation once;
7. increment revision exactly once;
8. write a temporary file in the destination directory;
9. flush and `fsync` the file;
10. atomically replace the destination;
11. `fsync` the parent directory when supported;
12. refresh or invalidate the process-local cache;
13. release the lock.

Plain cached `get_or_create()` data may not be the source of a supervised
session commit.

### 21.3 Commit-id retention

Session files retain a bounded commit-id index sufficient for runtime task
retention. If transcript format cannot hold the index cleanly, store an
adjacent per-session commit sidecar managed atomically by Session Manager.
Runtime must not create an unrelated session content store.

### 21.4 Revision conflicts

A revision conflict is never resolved by overwriting the newer transcript.

Behavior:

1. reload fresh session;
2. if the commit id is present, confirm Runtime Store and continue;
3. if the task is still the session head and the conflict came from a known
   legacy writer during migration, retry the entire `RESTORE -> SAVE` segment
   using durable completed tool results;
4. retry at most once;
5. otherwise enter `WAITING_USER` with `SESSION_REVISION_CONFLICT` and raise an
   operational alert.

After durable mode becomes the only writer, any conflict without the same
commit id is treated as a correctness defect.

### 21.5 Legacy metadata

For new durable tasks:

- do not write `pending_user_turn`;
- do not write `runtime_checkpoint`;
- do not restore pending tool calls as synthetic errors.

Legacy fields remain readable only during the migration window described in
section 31.

## 22. Memory and Required Background Work

### 22.1 Memory authority

Preserve:

- current memory extraction and recall behavior;
- existing memory source files;
- GitStore history;
- Context Governor integration;
- `memory.db` as a derived vector index.

Do not add a parallel memory-candidate database.

### 22.2 Vector store hardening

Each process creates its own vector database connection.

Add:

- WAL mode and busy timeout;
- bounded write transactions;
- deterministic source identity and source revision;
- unique idempotency keys for index updates;
- tombstones or revision checks for deletes;
- principal, tenant, Agent, and workspace scope in every lookup;
- rebuild tooling from authoritative memory sources.

### 22.3 Durable maintenance tasks

Existing Cron, Dream idle trigger, consolidation trigger, auto-compaction
trigger, and index trigger may decide that work is due. They may only call:

```python
TaskService.submit_internal(kind, scope, dedup_key, priority, payload_ref)
```

They may not own required work through `asyncio.create_task()`.

`TaskSupervisor` remains for:

- telemetry flush;
- lossy realtime coalescing;
- reconnect delays;
- reconstructable poll loops.

### 22.4 Priority

Effective order:

1. persisted cancel and approval controls;
2. recoverable user tasks;
3. new user turns;
4. memory consolidation and index work;
5. reflection, Dream, retention, backup, and cleanup.

At most one low-priority maintenance task runs globally. Maintenance does not
claim while an eligible user task waits and yields at checkpoints.

Default numeric priorities:

- user turn: 100;
- memory consolidation and index: 50;
- reflection and Dream: 20;
- retention, backup, and cleanup: 10.

Recovery precedence comes from `recovery_pending`, not by mutating stored
priority. External callers may request only a validated user-priority band;
they cannot impersonate recovery or control work.

## 23. Realtime, Control, and Delivery

### 23.1 Durable versus transient events

Durable events exist for recovery and bounded audit. Existing Agent events
exist for user experience.

Do not duplicate the full typed Agent event stream into SQLite.

Durable:

- accepted;
- leased/running;
- model and tool boundaries;
- checkpoint;
- control received/applied;
- session commit;
- Outbox enqueue;
- retry/wait/terminal transitions.

Transient:

- Token deltas;
- reasoning deltas;
- replaceable progress detail;
- typing indicators;
- debug diagnostics.

### 23.2 IPC

Supervised mode uses:

- Control Plane-to-Worker wake hints;
- Worker-to-Control Plane typed Agent events;
- Supervisor lifecycle signals.

IPC messages include protocol version, process instance id, task id when
applicable, and trace id.

Correctness does not depend on delivery. Workers poll SQLite with exponential
idle backoff. Control Plane can reconstruct state from Runtime Store.

Because critical state is not sent through IPC, one bounded queue need not
pretend to prioritize critical and lossy messages. If the realtime queue is
full:

- drop or coalesce Token deltas first;
- keep only latest progress per task;
- increment a dropped-event metric;
- never block task checkpoint or lease renewal.

### 23.3 Control application

Workers poll controls:

- before a Provider call;
- after a Provider result;
- before a tool call;
- after a tool result;
- before session commit;
- during known long-running tool progress callbacks.

Cancellation rules:

- queued tasks can become `CANCELLED` immediately;
- a safe interruptible read tool may be stopped and cancelled;
- a non-idempotent running external write is not declared cancelled until its
  effect is known;
- forced process termination followed by unknown effect becomes
  `WAITING_USER`, not `CANCELLED`;
- steering content is applied at the next model boundary and advances
  `control_cursor`.

### 23.4 Session conflict and unknown effect

Both conditions enter `WAITING_USER` with:

- a safe explanation;
- the exact recovery choice required;
- no raw secrets;
- a durable control request path.

Approval or continuation transitions the same task back to `QUEUED`.

### 23.5 Channel delivery result

Extend Channel delivery internally to return:

```python
class DeliveryReceipt:
    status: DELIVERED | RETRYABLE_FAILURE | PERMANENT_FAILURE
    provider_message_id: str | None
    safe_error_code: str | None
    retry_after_ms: int | None
```

Every Channel adapter also declares:

```text
delivery_recovery = NATIVE_IDEMPOTENCY | QUERYABLE_RECEIPT | NONE
```

Adapters that cannot provide a receipt return `DELIVERED` only after their send
call completes successfully. A crash between that return and the durable
Outbox update is still treated according to `delivery_recovery`.

Channel Manager retains formatting and provider-specific retry hints. Durable
retry count and final authority live in Outbox. For runtime delivery, one
`ChannelManager.send_with_receipt()` call represents one bounded logical
attempt. It may perform only transport-internal retries that are proven
idempotent; it may not run a second independent application-level retry loop.

For multipart Channel sends, the adapter must either:

- provide native idempotency for the logical message or each stable part;
- provide receipt lookup for the logical message; or
- declare `NONE`, causing an interrupted send to become
  `OUTCOME_UNKNOWN`.

The runtime guarantees durable at-least-once intent. It guarantees automatic
duplicate suppression only when the Channel exposes idempotency or receipt
lookup. It never silently claims exactly-once delivery for a Channel that lacks
both.

## 24. Liveness, Recovery, and Supervision

### 24.1 Heartbeat and progress

- lease heartbeat proves the Worker event loop can renew;
- durable progress proves the task crossed a meaningful boundary;
- tool progress proves a declared long-running tool is alive.

Heartbeat alone does not permit infinite execution.

Streaming Provider activity may update durable progress at a rate-limited
interval using byte/Token counts only. A non-streaming Provider request timeout
must be below `progressTimeoutS`, or the Provider must expose a bounded
operation deadline that stall detection understands. Raw stream content is
never written as progress.

Defaults:

- heartbeat: 15 seconds;
- task lease timeout: 180 seconds;
- lease scan: 15 seconds;
- progress timeout: 600 seconds.

Heartbeat must be less than one third of lease timeout.

### 24.2 Expired task lease

Reaper transaction:

1. select expired `LEASED` or `RUNNING` rows;
2. clear session active slot if it points to that task;
3. clear lease fields;
4. set `recovery_pending=1`;
5. choose:
   - `QUEUED` when checkpoint and operation state are safe;
   - `RETRY_WAIT` for bounded transient failure;
   - `WAITING_USER` for unknown non-idempotent effect;
   - `FAILED` after root attempt budget exhaustion;
6. append `LEASE_RECLAIMED`;
7. commit.

A late Worker write fails token or epoch validation.

### 24.3 Live but stalled

When `last_progress_at_ms` exceeds timeout:

1. request `STOP_AFTER_CHECKPOINT`;
2. wait configured grace;
3. if no checkpoint, Supervisor terminates the Worker tree;
4. lease expiry recovery applies;
5. repeated stalls count toward root attempt budget.

Known long-running tools must emit progress. Tools marked
`progress_required=True` fail configuration validation if they cannot.

### 24.4 Worker crash loop

Supervisor tracks process exits in memory:

- exponential restart backoff;
- bounded restarts per rolling window;
- readiness false when minimum Worker capacity is unavailable;
- alert with exit code and safe metadata;
- no durable `workers` row that could become a second truth.

### 24.5 Control Plane crash

Supervisor restarts Control Plane independently.

While it is unavailable:

- Workers may finish or claim already accepted tasks;
- new Channel intake is unavailable;
- Outbox delivery pauses;
- durable task results remain safe;
- realtime events may be lost.

### 24.6 Supervisor crash

All children must terminate with the Supervisor:

- Windows Job Object configured kill-on-close;
- POSIX process group and parent-death mechanism;
- container init configured to reap children.

On restart, a new Supervisor:

- opens and validates Runtime Store;
- starts Control Plane and Workers;
- reclaims expired leases;
- resumes Outbox;
- does not delete or recreate the ledger.

### 24.7 Graceful shutdown

1. stop accepting new Channel or API work;
2. stop new claims;
3. let active tasks reach a checkpoint;
4. flush prepared session commits;
5. release or shorten task leases;
6. finish or release Outbox sends;
7. terminate Worker child trees;
8. checkpoint WAL when safe;
9. close connections and Channels;
10. exit before shutdown grace expires.

Required work remains represented in SQLite if grace expires.

### 24.8 Database unavailable or disk full

- stop accepting work before acknowledgement;
- stop claiming new work;
- do not continue Agent execution when a durable boundary cannot commit;
- retain current lease until safe grace expires;
- expose not-ready health;
- retry bounded transient lock errors;
- alert on disk-full or repeated I/O failure;
- never fall back to an in-memory authority.

### 24.9 Corrupt ledger

1. stop all claims and acceptance;
2. preserve database and WAL for diagnosis;
3. expose not-ready;
4. restore only from verified SQLite online backup;
5. inspect tool calls and deliveries that were running at the backup boundary;
6. never automatically delete and recreate `runtime.sqlite`.

`memory.db` may be rebuilt because it is derived.

## 25. Budgets, Quotas, and Worker Recycling

### 25.1 Stable task defaults

- inner tool iterations: 50;
- cumulative input Tokens: 200,000;
- cumulative output Tokens: 50,000;
- existing tool-result injection limit: 16,000 characters;
- run-segment wall time: 120 minutes;
- warning threshold: 70%;
- root Worker attempts: three.

At the segment wall-time limit:

1. checkpoint;
2. stop further external effects;
3. enter `WAITING_USER`;
4. enqueue a question asking whether to continue;
5. on approval, resume the same task with incremented `run_segment`.

Cumulative Token limits do not reset automatically. An explicit continuation
policy may raise them through a control payload.

### 25.2 Capacity

Use:

- Worker count for root-task capacity;
- process-local semaphores for coroutine safety;
- `resource_leases` for cross-process quotas and exclusive resources.

Default global quotas:

- root tasks: Worker count;
- subagents: four total;
- low-priority maintenance: one;
- exclusive tool scope: one lease for its resource key.

Subagents:

- execute inside their owning root Worker in version one;
- receive task/turn lineage and scoped `TurnRuntime`;
- count against global subagent lease capacity;
- cannot commit the root session or final Outbox directly;
- return results to the root Agent;
- are cancelled and drained before Worker task release.

### 25.3 Recycling

Optional thresholds:

- maximum RSS;
- maximum Worker uptime;
- maximum tasks per Worker.

Recycle only between tasks. A Worker over a hard memory threshold during a task
is asked to checkpoint, then terminated if it cannot.

## 26. Error Model

Safe error codes:

- `RUNTIME_DB_BUSY`;
- `RUNTIME_DB_IO`;
- `RUNTIME_DB_FULL`;
- `RUNTIME_DB_CORRUPT`;
- `STALE_LEASE`;
- `LEASE_EXPIRED`;
- `TASK_ATTEMPTS_EXHAUSTED`;
- `TASK_STALLED`;
- `TASK_CONTINUATION_EXPIRED`;
- `MODEL_RETRYABLE`;
- `MODEL_PERMANENT`;
- `MODEL_JOURNAL_FAILED`;
- `TOOL_POLICY_DENIED`;
- `TOOL_APPROVAL_REQUIRED`;
- `TOOL_CALL_ID_CONFLICT`;
- `TOOL_RETRYABLE`;
- `TOOL_PERMANENT`;
- `TOOL_OUTCOME_UNKNOWN`;
- `SESSION_REVISION_CONFLICT`;
- `SESSION_COMMIT_IO`;
- `DELIVERY_RETRYABLE`;
- `DELIVERY_PERMANENT`;
- `RESOURCE_CAPACITY`;
- `CANCELLED_BY_USER`.

Rules:

- Runtime Store errors are classified at the SQLite boundary;
- raw SQL, credentials, prompts, arguments, results, and paths outside the
  workspace are not placed in `error_summary`;
- transient errors use bounded exponential backoff with jitter;
- permanent errors do not enter infinite retry loops;
- state transition conflicts are reread and resolved, not blindly retried;
- stale lease is terminal for that Worker attempt;
- user-visible errors are produced through Outbox.

## 27. Configuration Contract

Recommended shape:

```json
{
  "runtime": {
    "enabled": true,
    "mode": "supervised",
    "databasePath": "runtime/runtime.sqlite",
    "backupPath": "runtime/backups",
    "workerCount": 3,
    "lightweightExecutionSlots": 1,
    "workerConcurrency": 1,
    "heartbeatIntervalS": 15,
    "leaseTimeoutS": 180,
    "leaseScanIntervalS": 15,
    "progressTimeoutS": 600,
    "taskMaxAttempts": 3,
    "queuePollMinMs": 250,
    "queuePollMaxMs": 2000,
    "sqliteBusyTimeoutMs": 5000,
    "realtimeEventQueueCapacity": 1000,
    "shutdownGraceS": 60,
    "approvalTimeoutM": 30,
    "waitingAlertM": 60,
    "outboxLeaseTimeoutS": 120,
    "outboxMaxAttempts": 8,
    "channelSendTimeoutS": 60,
    "successfulRetentionD": 7,
    "failureRetentionD": 30,
    "backupIntervalH": 6,
    "backupRetentionD": 7,
    "inlineBlobMaxBytes": 1048576,
    "minimumFreeDiskMb": 1024,
    "maxTurnWallTimeM": 120,
    "stableMaxToolIterations": 50,
    "globalMaxSubagents": 4,
    "workerMaxRssMb": null,
    "workerMaxUptimeH": null,
    "workerMaxTasksBeforeRecycle": null
  }
}
```

Validation:

- mode is `lightweight` or `supervised`;
- supervised Worker count is at least two, default three;
- supervised version one requires `workerConcurrency=1`;
- lightweight slots are one to three;
- heartbeat is less than one third of lease timeout;
- progress timeout exceeds heartbeat;
- maximum queue poll is below lease scan interval;
- Outbox lease timeout exceeds Channel send timeout;
- SQLite busy timeout is positive and shorter than task lease timeout;
- all attempts, timeouts, and retention values are positive;
- database path resolves inside the configured runtime data root unless
  explicitly allowed;
- backup path differs from the live database directory and resolves inside an
  allowed persistent data root;
- Worker memory recommendations are checked against container limits;
- explicit user configuration wins over launcher defaults.

`runtime.enabled=false` selects the complete legacy path during rollout. It
must not make a single task partly durable and partly legacy.

Initial container sizing guidance:

- lightweight, one slot: current one-CPU/one-GiB profile may be retained after
  measured validation;
- supervised, three Workers: start with at least two CPUs, two GiB memory, and
  PID limit 512;
- local models, browsers, MCP servers, and heavy tools require additional
  measured capacity;
- deployment must leave enough disk for active blobs, WAL growth, and one
  online backup.

These are starting profiles, not correctness constants. Readiness reports
resource exhaustion instead of silently reducing durable guarantees.

## 28. Entrypoint Behavior

### 28.1 CLI one-shot

Default:

```text
runtime.enabled=true
runtime.mode=lightweight
execution_slots=1
```

Flow:

1. start Lightweight Host;
2. submit stable local inbound envelope;
3. await task terminal state and required Outbox delivery;
4. stop gracefully.

If interrupted, the next CLI start opens the same ledger and can resume or
report the task.

### 28.2 Development Gateway and tests

Default lightweight. Tests may use an in-memory fake store for Agent unit tests
or a temporary SQLite file for runtime tests.

### 28.3 Long-running Gateway

Default supervised. All Channel and API messages enter Task Service.

### 28.4 SDK and direct processing

Existing `process_direct` and SDK entrypoints must route through Task Service
when runtime is enabled. A compatibility method may submit and await, but may
not call Agent execution directly.

### 28.5 Cron

Cron submits either:

- a `USER_TURN` with a configured system Channel target; or
- a typed internal task.

Cron does not create a detached Agent coroutine.

## 29. Current Module Integration Map

### 29.1 `miniunicorn/agent/turn_dispatcher.py`

Keep:

- inbound normalization compatibility;
- duplicate-response futures for callers awaiting a result;
- mapping current message types to runtime envelopes.

Change:

- replace in-memory pending-session ownership with `TaskService.submit()`;
- replace direct `asyncio.create_task()` execution with submit-and-await;
- read completion through task/Outbox status;
- never publish final outbound messages directly.

Remove as authority:

- `_active_tasks`;
- `_pending_by_session`;
- process-local task ordering.

### 29.2 `miniunicorn/agent/turn_coordinator.py`

Keep:

- process-local execution semaphore;
- local cancellation/drain helpers.

Rename responsibility conceptually to local execution limiter.

Remove as authority:

- global task capacity;
- cross-process session serialization.

Do not delete it until all legacy callers are migrated.

### 29.3 `miniunicorn/agent/turn_executor.py`

Keep the outer state machine.

Change:

- inject Agent-owned ports;
- route command results through `SAVE` and `RESPOND`;
- check controls at specified boundaries;
- return a structured completion to Worker Adapter instead of publishing.

### 29.4 `miniunicorn/agent/turn_persistence.py`

Split responsibilities without duplicating state:

- legacy checkpoint reader for migration;
- durable checkpoint adapter implementing `TurnJournalPort`;
- Session Committer adapter implementing `SessionCommitPort`.

For runtime tasks:

- do not write `runtime_checkpoint`;
- do not write `pending_user_turn`;
- do not convert pending calls into synthetic errors.

### 29.5 `miniunicorn/agent/turn_runtime.py`

Keep as task-local context.

Add immutable identifiers:

- `task_id`;
- `turn_id`;
- `session_sequence`;
- `lease_epoch`;
- `run_segment`;
- trace id.

Do not store SQLite connections or mutable global singletons in contextvars.

### 29.6 `miniunicorn/agent/runner.py`

Keep:

- inner loop;
- context governance;
- Planner;
- budget tracking;
- injection behavior;
- hooks;
- tool batching semantics.

Change:

- accept `TurnJournalPort`, `ToolExecutionPort`, and `ControlInboxPort`;
- create stable logical model-call ids;
- consume model output only after durable observer completion;
- replace direct `tool.execute()` with Tool Gateway port;
- checkpoint after completed model and tool boundaries;
- propagate safe progress.

### 29.7 Provider modules

Keep retry and fallback policy.

Change:

- accept optional `ProviderAttemptObserver`;
- notify every physical attempt;
- await durable completion callback before returning response;
- create clients after process spawn.

### 29.8 `miniunicorn/agent/tools/base.py`

Keep existing compatibility metadata.

Add optional effective-policy hooks or declarative defaults for:

- effect;
- risk;
- idempotency;
- approval;
- recovery;
- concurrency scope;
- progress and timeout.

### 29.9 `miniunicorn/agent/tools/registry.py`

Keep discovery, schema, preparation, and invocation.

Change:

- return `PreparedToolCall` with normalized hash and effective policy;
- expose actual invocation only to Tool Gateway for runtime tasks;
- preserve a test-only or legacy direct execution path while runtime is
  disabled.

### 29.10 `miniunicorn/agent/tools/message.py`

Replace direct Channel callback with an injected Outbox-enqueue effect.

The tool returns the durable `outbox_id`, not a guessed delivery result.

### 29.11 `miniunicorn/session/manager.py`

Add:

- persisted revision;
- fresh reload;
- cross-process lock;
- commit-id deduplication;
- `commit_turn`;
- cache invalidation after external revision change.

Preserve current session file content and atomic replace behavior.

### 29.12 `miniunicorn/channels/base.py`

Extend internal send result to `DeliveryReceipt`. Public compatibility wrappers
may continue returning `None` after a successful receipt.

### 29.13 `miniunicorn/channels/manager.py`

Keep:

- Channel lifecycle;
- formatting;
- provider adapters;
- provider retry hints;
- streaming behavior.

Change:

- expose receipt-bearing send to Outbox Sender;
- disable the current process-local application-level retry loop on the runtime
  delivery path and return retry classification to Outbox;
- remove process-local final-message dedup as authority;
- do not consume Worker-local completion as final truth.

### 29.14 `miniunicorn/bus/queue.py`

Keep bounded queues for:

- wake hints;
- inbound adapter handoff before durable submission only when acknowledgement
  waits for commit;
- realtime Agent events.

Do not use them as the durable inbound or outbound source of truth.

### 29.15 Agent event and telemetry modules

Keep strict protocol version one and current telemetry.

Add:

- mapping from durable state to snapshot events;
- queue-drop counters;
- lease/recovery/Outbox metrics.

Do not create a second telemetry stack.

### 29.16 Dream, consolidation, indexing, and Task Supervisor

Change required triggers to enqueue durable internal tasks.

Keep `TaskSupervisor` for disposable, reconstructable coroutines only.

### 29.17 Gateway composition

Current Gateway composition passes mutable Agent and Channel callbacks through
one process. Replace it with Host construction:

- Control Plane owns Channels;
- Workers own Agent factories and Provider/tool clients;
- Runtime contracts bridge them;
- WebSocket control uses Task Service, not direct Agent mutation;
- final replies come from Outbox;
- realtime events come from the event bridge.

## 30. Implementation Work Packages

Work packages are ordered. Each package must pass its listed tests before the
next begins. Do not implement as one big-bang change.

### WP0: Characterization and dependency rules

Files:

- existing Agent, Session, Channel, Provider, Tool, and Gateway tests;
- add `tests/architecture/test_runtime_dependencies.py`;
- add crash-boundary characterization fixtures.

Tasks:

1. characterize current command, model, tool, session, and final response flow;
2. freeze current Agent event protocol behavior;
3. reproduce stale two-`SessionManager` overwrite;
4. assert Agent modules do not import SQLite or multiprocessing;
5. inventory every direct `process_direct`, `tool.execute`, Channel send,
   maintenance `create_task`, and final outbound publication.

Exit:

- characterization tests pass;
- inventory is represented as failing or skipped migration tests with explicit
  target work package markers;
- no production behavior changes.

### WP1: Runtime contracts, configuration, and SQLite foundation

Create:

- `miniunicorn/agent/ports.py`;
- runtime package structure from section 10;
- enums and immutable DTOs;
- Runtime Store protocols;
- configuration parser and validation;
- migration 001 with all schema tables and indexes;
- connection factory and `SqliteRuntimeStore`.

Implement first:

- blob insert/read;
- task submit/dedup;
- control append;
- session sequence allocation;
- claim/renew/fence;
- task events;
- retry promotion and lease reclaim.

Tests:

- every allowed and forbidden transition;
- duplicate inbound races;
- session head claim ordering;
- priority across different sessions;
- stale token and epoch rejection;
- renewal without state-version change;
- event immutability trigger and retention guard;
- migration checksum and concurrent startup;
- WAL/busy timeout configuration.

Exit:

- store concurrency tests pass with multiple processes;
- no Agent code uses it yet.

### WP2: Revision-aware Session Manager

Files:

- `miniunicorn/session/manager.py`;
- session model/serialization helpers;
- new `runtime/session_committer.py`.

Tasks:

1. add session revision and commit-id index;
2. add OS-visible per-session lock;
3. implement `load_fresh`;
4. implement idempotent `INBOUND` and `FINAL` `commit_turn`;
5. preserve existing format compatibility;
6. implement prepare/apply/confirm coordinator;
7. invalidate process caches on revision change.

Tests:

- two independent managers cannot lose writes;
- identical commit is applied once;
- inbound user message is committed once after claim and before Agent
  execution;
- final mutation does not duplicate the inbound user message;
- same commit id with different hash fails;
- stale revision never overwrites;
- crash after prepare;
- crash after filesystem replace before SQLite confirm;
- file and parent directory fsync behavior;
- Windows and POSIX lock adapters where supported.

Exit:

- stale-cache regression passes;
- existing session tests remain green.

### WP3: Durable lightweight task path

Files:

- `runtime/task_service.py`;
- `runtime/scheduler.py`;
- `runtime/worker.py`;
- `runtime/hosts/lightweight.py`;
- adapters in dispatcher, executor, persistence, and runtime context.

Tasks:

1. submit user turns durably;
2. make Worker commit the inbound user message after claim and before Agent
   execution;
3. run one Worker coroutine through the Scheduler;
4. restore/checkpoint Agent outer phases;
5. commit final session mutations through Session Committer;
6. expose submit-and-await compatibility;
7. keep final response temporarily behind an internal fake delivery ledger
   until WP5, without direct Channel delivery in runtime tests;
8. remove `COMMAND -> DONE` for runtime tasks.

Tests:

- process restart after each outer phase;
- same-session strict ordering;
- different-session concurrency with two lightweight slots;
- cancellation at safe boundaries;
- no runtime task exists only in `_active_tasks` or pending queues;
- legacy path still works when `runtime.enabled=false`.

Exit:

- CLI can submit, crash, restart, and finish one task in lightweight mode.

### WP4: Provider journaling and Tool Gateway

Files:

- Agent Runner;
- Provider base/fallback implementations;
- Tool base/registry;
- `runtime/tool_gateway.py`;
- Runtime Store model/tool methods.

Tasks:

1. add Provider observer;
2. expose every retry and fallback attempt;
3. checkpoint completed model decisions;
4. add prepared invocation policy;
5. implement logical tool call and attempt journal;
6. implement approval controls;
7. implement resource leases;
8. route Runner through `ToolExecutionPort`;
9. add task child-process containment.

Tests:

- Provider crash before and after durable completion;
- fallback attempts recorded in order;
- completed model response not consumed before journal commit;
- read tool replay;
- native idempotency retry;
- result reuse;
- non-idempotent unknown effect enters `WAITING_USER`;
- exact approval hash required;
- concurrent exclusive tools serialize;
- stale Worker cannot commit tool result;
- child process tree dies with Worker.

Exit:

- no runtime Agent tool bypasses Tool Gateway.

### WP5: Durable Outbox and Channel integration

Files:

- `runtime/outbox.py`;
- Runtime Store delivery methods;
- Channel base and manager;
- Message Tool;
- bus final delivery path.

Tasks:

1. implement atomic complete-and-enqueue;
2. implement target-head Outbox claim;
3. add Channel receipts;
4. implement retry, permanent failure, and delivery fencing;
5. route Message Tool through Outbox;
6. retain transient streaming on Message Bus;
7. remove direct final Channel sends for runtime tasks.

Tests:

- crash before and after enqueue;
- crash after Channel success before durable receipt;
- native idempotency, receipt lookup, and no-capability recovery behavior;
- retrying earlier message blocks later same-target message;
- different targets send concurrently;
- idempotent Channels deliver one logical final reply;
- non-idempotent ambiguous sends become `OUTCOME_UNKNOWN` without automatic
  resend;
- Message Tool returns stable Outbox receipt;
- realtime queue overflow does not lose final reply.

Exit:

- all user-visible runtime messages use Outbox.

### WP6: Supervised Host

Files:

- `runtime/ipc.py`;
- `runtime/supervisor.py`;
- `runtime/hosts/supervised.py`;
- Gateway launcher and shutdown integration.

Tasks:

1. spawn Control Plane and Worker children;
2. initialize all process-local dependencies after spawn;
3. let Workers claim directly from SQLite;
4. implement wake hints and realtime event bridge;
5. implement process restart backoff;
6. implement Windows Job Object and POSIX process-group containment;
7. implement graceful shutdown;
8. expose readiness from Control Plane and Worker capacity.

Tests:

- spawn semantics on Windows and POSIX CI;
- Worker killed during model, read tool, write tool, session commit, and
  completion;
- Control Plane restart while Worker runs;
- Supervisor death terminates descendants;
- stale Worker fencing;
- no inherited SQLite, socket, Provider, or Agent object;
- lightweight and supervised golden-flow parity.

Exit:

- three Workers process different sessions concurrently and one session
  serially across repeated crashes.

### WP7: Durable maintenance and memory hardening

Files:

- Dream trigger;
- consolidator;
- memory index integration;
- Cron service;
- vector memory store;
- `runtime/maintenance.py`.

Tasks:

1. enqueue required work with deterministic dedup keys;
2. apply maintenance priority and one-task quota;
3. add source revision checks;
4. add vector index idempotency, WAL, busy timeout, and scope;
5. add retention, blob GC, backup, and WAL checkpoint tasks.

Tests:

- restart during every maintenance kind;
- stale consolidation cannot overwrite newer memory;
- duplicate index task is harmless;
- user work preempts new maintenance claims;
- `memory.db` rebuild reproduces searchable state;
- required work has a durable task row.

Exit:

- no required background operation is owned only by Task Supervisor.

### WP8: Cutover, observability, and hardening

Tasks:

1. route every ingress through Host and Task Service;
2. add migration reader and cutover tooling;
3. add metrics, health, safe alerts, online backups, and retention;
4. update operator documentation and example configuration;
5. update container CPU, memory, PID, and shutdown defaults;
6. run load, fault-injection, and soak suites;
7. remove legacy authority after the rollback window.

Exit:

- all acceptance criteria in section 34 pass;
- legacy direct path is deleted or available only behind an explicitly
  unsupported migration flag.

## 31. Migration and Rollout

### 31.1 No per-task dual write

During rollout:

- `runtime.enabled=false` uses the complete legacy path;
- `runtime.enabled=true` uses the complete durable path;
- one task never writes both durable checkpoints and legacy session checkpoint
  metadata;
- Message Bus may mirror realtime events, but mirrored events are not
  authorities.

### 31.2 Legacy checkpoint reader

At first durable startup, while intake is stopped:

1. scan sessions for `pending_user_turn` and `runtime_checkpoint`;
2. if no incomplete metadata exists, record migration complete;
3. if `pending_user_turn` has stable inbound identity and no evidence of
   execution, convert it into one `USER_TURN` and register its existing
   persisted user message as the committed `INBOUND` mutation without appending
   it again;
4. if a legacy checkpoint contains a completed final response not delivered,
   require operator review before Outbox import;
5. if a legacy checkpoint contains pending or running tool calls, do not replay
   them; preserve metadata and create a `WAITING_USER` task describing the
   uncertain effect;
6. mark imported legacy metadata read-only;
7. never fabricate a tool failure to continue automatically.

The migration adapter is removed after the configured rollback window and a
successful backup.

### 31.3 Upgrade procedure

1. announce maintenance;
2. stop intake;
3. drain or checkpoint legacy active tasks;
4. back up session, memory, and configuration data;
5. start binary with runtime disabled and validate configuration;
6. initialize and verify Runtime Store migration;
7. run legacy checkpoint scan;
8. enable durable lightweight mode with one slot;
9. run smoke tasks and verify session/outbox;
10. enable supervised mode;
11. monitor recovery, DB, delivery, and memory metrics;
12. close rollback window only after verified backup and soak.

### 31.4 Rollback

Before durable traffic:

- stop and restore configuration; no data conversion is needed.

After durable traffic:

- do not restart a legacy writer against sessions with non-terminal durable
  tasks;
- stop intake and Workers;
- preserve Runtime Store;
- finish or explicitly cancel durable tasks using the durable binary;
- export committed session state;
- restore prior binary only after the durable queue is terminal and Outbox is
  drained or explicitly waived.

Rollback is operationally controlled, never automatic.

## 32. Testing Strategy

### 32.1 Unit tests

- all task transitions;
- control transitions and dedup;
- checkpoint version compatibility;
- tool policy mapping;
- recovery matrix;
- retry/backoff classification;
- Outbox target-head ordering;
- configuration precedence and validation;
- safe error redaction;
- authority/dependency import rules.

### 32.2 SQLite transaction tests

Use temporary real SQLite databases, not mocked SQL:

- simultaneous duplicate submission;
- session sequence allocation under contention;
- head-only claim;
- cross-session priority;
- stale lease write rejection;
- renewal versus completion race;
- reclaim versus late completion race;
- Outbox claim and receipt race;
- resource lease expiry and reuse;
- session commit prepare/confirm recovery;
- event immutability and retention enforcement;
- migration interruption and restart;
- WAL busy behavior.

### 32.3 Agent integration tests

- command path uses SAVE and Outbox;
- simple final response;
- multi-iteration model/tool loop;
- context compaction;
- Planner and injection behavior unchanged;
- cancellation and steering;
- Provider retry/fallback journaling;
- checkpoint restore at every safe boundary;
- no synthetic interrupted-tool result;
- runtime-disabled legacy characterization.

### 32.4 Crash-injection matrix

Inject a hard process exit:

- after inbound commit before wake;
- after lease commit before RUNNING;
- before model request;
- after model network response before journal;
- after model journal before Runner consumes;
- after tool prepare;
- after tool external effect before result journal;
- after tool result journal;
- after checkpoint blob before checkpoint row;
- after session commit prepare;
- after session file replace before Runtime Store confirm;
- after Outbox enqueue before task completion response;
- after Channel send before durable receipt.

Each test asserts the exact expected state, replay decision, and user-visible
result.

### 32.5 Multiprocess tests

- use spawn even on POSIX tests;
- three Workers claim different sessions;
- one session never overlaps;
- Worker crash and fencing;
- Control Plane restart;
- Supervisor child containment;
- separate SQLite and vector connections;
- session revision conflict regression;
- global tool and subagent leases;
- process recycling between tasks.

### 32.6 Tool-effect tests

For every built-in tool, maintain a metadata fixture asserting:

- effective effect class;
- risk;
- idempotency;
- approval;
- recovery;
- concurrency scope;
- timeout/progress behavior.

Test each recovery branch with a fake side-effect service that records actual
effects and idempotency keys.

### 32.7 Channel and Outbox tests

- receipt conversion for every Channel;
- retryable and permanent errors;
- per-target ordering;
- cross-target concurrency;
- duplicate Outbox enqueue;
- crash-after-send ambiguity;
- Channel reconnect;
- streaming event loss with final delivery intact.

### 32.8 Session and memory tests

- two-process stale cache regression;
- commit-id idempotency;
- revision conflict handling;
- concurrent vector writes;
- source-version stale write rejection;
- tenant/principal/workspace isolation;
- memory index rebuild;
- maintenance dedup and restart.

### 32.9 Load and soak

Pre-release:

- at least 1,000 mixed tasks over at least 100 sessions;
- bursts of duplicate inbound messages;
- repeated Worker kills;
- repeated Channel failures;
- database lock pressure;
- maintenance with user traffic;
- child subprocess creation and cancellation;
- 24-hour automated soak for every merge candidate;
- seven-day soak before making supervised mode the stable service default.

Track:

- missing or duplicate final replies;
- duplicate external effects;
- session order violations;
- stale lease commit attempts;
- queue age;
- memory/RSS, threads, handles, child processes;
- SQLite WAL size and busy errors;
- vector index lag;
- Outbox retry age.

## 33. Observability, Security, and Maintenance

### 33.1 Reuse existing telemetry

Extend `TurnTelemetry`; do not create a parallel telemetry system.

Metrics:

- task count and age by kind/state/priority;
- claim latency and attempt count;
- lease renew failures and reclaims;
- task progress age;
- model attempts, latency, usage, and fallback;
- tool calls by policy/outcome;
- waiting approvals and unknown effects;
- session commit conflicts;
- Outbox pending age, retries, and permanent failures;
- SQLite transaction latency, busy count, WAL bytes, backup age;
- Worker restarts, RSS, and task count;
- realtime events dropped/coalesced;
- maintenance age and vector index lag.

### 33.2 Health

Liveness:

- process event loop responds;
- Supervisor can observe required children.

Readiness:

- schema version matches;
- Runtime Store accepts a short read transaction;
- disk free space is above threshold;
- at least one Worker is available in supervised mode;
- lease reaper is current;
- Outbox Sender is current;
- Channel requirement is met for configured service;
- backup age is within policy when backups are required.

### 33.3 Safe logs

Task, control, status, artifact, and Outbox APIs require the envelope tenant,
principal, Agent, workspace, and session scope. An opaque `task_id` alone is
not authorization. Worker-internal claims run under a trusted local runtime
identity, while every external status or control request revalidates scope.

Logs and metrics may contain:

- opaque ids;
- state, phase, kind, provider/tool name;
- durations and counts;
- safe error codes;
- bounded redacted summaries.

They may not contain:

- prompts or responses;
- tool arguments or results;
- credentials;
- raw media;
- full external addresses;
- unredacted session content.

Control tokens are HMAC-signed with a stable runtime secret from the existing
credential/configuration mechanism. The secret is not stored in Runtime Store
or logs. Rotation accepts the previous key only for a bounded overlap window.
Tokens are scoped to principal, task, action, pending object, and expiry;
replay is stopped by `task_controls.dedup_key` and terminal control state.

### 33.4 Retention

Defaults:

- successful task facts: 7 days;
- failed/waiting task facts: 30 days;
- verified backups: 7 days;
- transient Agent events: not persisted;
- latest necessary blobs: retain with referencing rows.

Retention runs as bounded durable maintenance:

1. delete terminal child rows in batches;
2. verify no retained reference before blob deletion;
3. checkpoint WAL during quiet periods;
4. use `VACUUM` only in an explicit maintenance window;
5. use SQLite online backup, never filesystem copy of an open WAL database.

## 34. Acceptance Criteria

The design is implemented only when all are true:

1. Acknowledged inbound work survives immediate process termination.
2. Duplicate inbound messages produce one task and one session sequence.
3. The inbound user message is present exactly once before Agent Core performs
   its first model or tool call.
4. One session never has two concurrently running tasks.
5. Different sessions run concurrently up to configured capacity.
6. Stale Workers cannot checkpoint, confirm a session commit, record tool
   completion, or complete tasks; an already prepared idempotent session
   replace may land only with its immutable commit id and content hash.
7. Every completed model decision is durable before Runner consumes it.
8. Every terminal tool result is durable before Runner consumes it.
9. A non-idempotent unknown effect enters `WAITING_USER`.
10. Session commits are revision-aware and idempotent across processes.
11. The demonstrated two-manager stale-cache overwrite no longer occurs.
12. Every final reply is enqueued atomically with task completion.
13. Outbox preserves same-target order, suppresses duplicates when the Channel
    supports it, and surfaces non-idempotent ambiguity without automatic
    resend.
14. Message Tool uses Outbox rather than direct Channel callbacks.
15. Realtime queue loss cannot lose the final complete response.
16. Lightweight and supervised golden flows produce equivalent durable facts.
17. Mode is selected at startup and never inferred from task duration.
18. A Worker crash does not orphan its child process tree.
19. Control Plane restart does not lose accepted work or completed replies.
20. Supervisor failure terminates children and restart recovers expired work.
21. Required Dream, consolidation, indexing, and cleanup work is durable.
22. `memory.db` concurrent writes are safe and the index is rebuildable.
23. Agent Core imports no SQLite or multiprocessing implementation.
24. Runtime tasks do not dual-write legacy checkpoint authority.
25. Database transactions never span external calls.
26. Logs and telemetry pass content-redaction tests.
27. Multiprocess, fault-injection, load, and soak gates pass.

## 35. Implementation Guardrails

1. Do not create a second Agent loop.
2. Do not implement a central in-memory dispatch queue.
3. Do not make IPC correctness-critical.
4. Do not give each table a deployable service or independent transaction
   owner.
5. Do not let Agent Core execute runtime tools directly.
6. Do not let Runtime Store become a session or memory content database.
7. Do not let Message Bus or Channel Manager decide durable completion.
8. Do not hold SQLite transactions across external work.
9. Do not auto-retry uncertain non-idempotent writes.
10. Do not resolve session revision conflicts by last-writer-wins.
11. Do not switch deployment modes while tasks are active.
12. Do not add a second full event protocol.
13. Do not use `asyncio.create_task()` as the only owner of required work.
14. Do not start supervised children with inherited mutable runtime objects.
15. Do not delete or recreate a corrupt ledger automatically.
16. Do not permanently maintain both legacy and durable authorities.

## 36. Final Decision

MiniUnicorn adopts a **thin durable runtime kernel**.

The implementation keeps the existing Agent Core and adds:

- one SQLite Runtime Store façade with narrow consumer ports;
- one durable Task Service;
- one stateless Scheduler used by every Worker;
- one Agent Task Worker adapter;
- one Tool Gateway around the existing Tool Registry;
- one revision-aware Session Committer around Session Manager;
- one durable Outbox Sender around Channel Manager;
- two Hosts that differ only in process assembly.

This is not a second MiniUnicorn runtime. It is the minimum durable control
layer required to make the existing MiniUnicorn safe for unattended,
long-running operation while preserving a simple, decoupled architecture.
