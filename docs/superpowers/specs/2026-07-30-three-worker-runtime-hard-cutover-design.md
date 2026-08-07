# MiniUnicorn Three-Worker Runtime Hard-Cutover Design

**Status:** Implemented (2026-07-31). The hard cutover and the follow-up
production-readiness remediation
(`docs/superpowers/plans/2026-07-31-three-worker-production-readiness-remediation.md`)
are complete; the default supervised Gateway runs one Control Plane plus
three Worker processes and is verified by golden-flow, fault-injection,
1,000-task load, and soak gates.
**Date:** 2026-07-30
**Builds on:** `2026-07-29-long-running-agent-runtime-design.md`
**Primary decision:** Finish the existing durable Runtime construction, make it the
only production execution path, and remove the legacy in-process task authority
before merge.

## 1. Context

WP1 through WP8 added the intended Runtime contracts, SQLite ledger, Session
Committer, Task Service, Scheduler, Worker, Tool Gateway, Outbox, maintenance,
Supervisor, lightweight Host, supervised Host, observability, and tests.

The construction is not yet a production cutover:

- the Gateway still constructs `AgentLoop` directly;
- API and CLI paths still call `process_direct`;
- no production module constructs `LightweightHost`, `SupervisedHost`, or
  `SqliteRuntimeStore`;
- the root configuration schema does not expose `runtime`;
- production Control Plane and Worker child entrypoints do not exist;
- Agent Core imports Runtime implementations;
- direct tool execution, direct final publication, process-local pending queues,
  legacy checkpoint writers, and required-work `asyncio.create_task` paths
  remain;
- the architecture inventory keeps those paths as expected failures;
- `runtime/sqlite/store.py` has become a 3,260-line coupling center.

The project has not been released. There is no supported installed base and no
production rollback window. Maintaining a permanent `runtime.enabled=false`
path would therefore add two task authorities, two test matrices, and two
failure models without a corresponding compatibility benefit.

## 2. Goal

Produce one coherent runtime architecture with these properties:

1. the long-running Gateway starts one Control Plane and exactly three Worker
   processes by default;
2. every user, API, Channel, Cron, Dream, consolidation, indexing, retention,
   and backup task enters the durable Task Service;
3. the Lightweight Host and Supervised Host use the same Runtime Store,
   Scheduler, Worker, Tool Gateway, Session Committer, and Outbox semantics;
4. Agent Core depends only on Agent-owned ports and never imports Runtime
   implementations;
5. no required task or final reply is owned only by process memory;
6. the legacy execution path and its configuration switch are absent from the
   final code;
7. the Runtime Store remains one transactional façade but its SQLite
   implementation is split into understandable responsibility modules;
8. architecture, integration, multiprocess, failure-injection, and load gates
   prove the cutover.

## 3. Non-Goals

This cutover does not add:

- multi-host scheduling;
- a remote database;
- more than one concurrent task inside a Worker process;
- a second Agent loop, Tool Registry, Channel Manager, event protocol, or
  session format;
- exactly-once guarantees for arbitrary third-party side effects;
- live switching between lightweight and supervised modes;
- automatic runtime-mode selection;
- a permanent compatibility mode for legacy execution;
- unrelated WebUI redesign or provider behavior changes.

## 4. Alternatives

### 4.1 Permanent dual mode

Keep `runtime.enabled` and select either the legacy or durable implementation.

Rejected because the project is pre-release. This preserves every existing
coupling problem, makes correctness dependent on two execution paths, and
allows future features to drift between modes.

### 4.2 Big-bang deletion before wiring

Delete the legacy path first and then build production composition.

Rejected because intermediate commits cannot execute a complete user turn.
Failures in composition, durable execution, and deletion would be mixed
together.

### 4.3 Staged hard cutover

Within the implementation branch:

1. create production composition and route callers through it;
2. temporarily keep compatibility method names as thin Runtime adapters;
3. prove Runtime behavior through all entrypoints;
4. flip legacy inventory tests from expected failure to required pass;
5. delete the legacy implementation and compatibility adapters;
6. merge only after the branch contains one execution path.

Selected. Git commits provide rollback during construction; the shipped code
does not contain an operational rollback mode.

## 5. Target Process Topology

### 5.1 Long-running Gateway

```text
Launcher / Supervisor process
├── owns child lifecycle, containment, restart policy, and signal handling
├── Control Plane process
│   ├── ChannelManager and Web/API/WebSocket ingress
│   ├── TaskService and control/status API
│   ├── OutboxSender
│   ├── Cron and maintenance enqueue triggers
│   ├── realtime Agent-event receiver
│   └── its own Runtime Store connection
├── Worker process 0
│   ├── Scheduler + AgentTaskWorker
│   ├── Agent factory, Provider clients, Tool Registry, SessionManager
│   └── its own Runtime Store and memory-index connections
├── Worker process 1
│   └── same construction, no shared mutable objects
└── Worker process 2
    └── same construction, no shared mutable objects
```

The default supervised capacity is three Workers with
`worker_concurrency=1`. Different sessions may execute concurrently. The
Runtime Store guarantees that one session never overlaps.

The Supervisor process carries no task truth. IPC contains readiness,
wake hints, shutdown signals, and lossy realtime events only. The SQLite
ledger remains authoritative if any IPC message is lost.

### 5.2 Lightweight execution

One-shot CLI commands and ordinary development tests use `LightweightHost`
with one Worker coroutine by default. Lightweight execution is not a legacy
mode: it uses the same durable submission, ledger, leases, checkpoints,
Tool Gateway, Session Committer, Outbox, and recovery decisions.

The lightweight slot count may be configured from one to three. Its behavioral
golden flow must match supervised execution.

### 5.3 Child construction

Spawned child arguments contain only immutable, picklable configuration and
IPC handles. Every child reconstructs after spawn:

- event loop;
- Runtime Store connection;
- SessionManager;
- Provider and Tool Registry;
- Agent Core adapter;
- Tool Gateway and journals;
- Channel objects, only in the Control Plane;
- vector-memory connection, only where required.

No SQLite connection, Channel socket, Provider client, Agent instance, lock,
or mutable registry is created in the parent and inherited by children.

## 6. Composition Boundaries

### 6.1 Runtime bootstrap

Add one production composition module under `miniunicorn/runtime/` that owns:

- Runtime path resolution;
- migrations and schema validation;
- Store connection creation;
- Session Committer creation;
- Agent execution adapter creation;
- lightweight Host assembly;
- supervised Host assembly;
- Control Plane child entrypoint;
- Worker child entrypoint;
- lifecycle-safe startup and shutdown.

CLI, API, and Gateway modules select a mode and request a constructed runtime
application. They do not instantiate Store, Worker, Tool Gateway, or Agent
collaborators individually.

### 6.2 Application-facing façade

Expose one small façade for ingress and control:

```python
class RuntimeApplication(Protocol):
    task_service: TaskService

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit_and_wait(
        self,
        message: InboundMessage,
        *,
        scope: RequestScope,
        timeout_s: float | None = None,
    ) -> TaskSnapshot: ...
```

Long-running supervised ingress lives in the Control Plane and uses its local
`TaskService`. The parent Supervisor façade exposes lifecycle and readiness,
not direct Agent execution.

No API, SDK, Channel, CLI, or WebUI handler receives a raw `AgentLoop`.

### 6.3 Configuration precedence

The root `Config` model owns a `runtime` field. The final configuration has no
`enabled` property.

Mode selection precedence remains:

```text
CLI --runtime-mode
> MINIUNICORN_RUNTIME_MODE
> config runtime.mode
> launcher default
```

Defaults:

- long-running `gateway`: `supervised`;
- supervised Worker count: `3`;
- Worker concurrency: `1`;
- one-shot CLI: `lightweight`;
- development Gateway override: explicit `--runtime-mode lightweight`;
- tests: explicit mode or test fixture, never an implicit legacy fallback.

The standalone `runtime.config.RuntimeConfig` becomes the type used by the
root configuration schema rather than an unconnected parser.

## 7. Dependency Direction

The required production direction is:

```text
CLI / API / Channels / Cron
            |
            v
Runtime composition and contracts
            |
            v
Agent-owned ports and Agent Core
            |
            v
Provider / Tool Registry / Session / Memory
```

Runtime may import Agent-owned ports and Agent adapters. Agent Core must not
import `miniunicorn.runtime`, `sqlite3`, `multiprocessing`, Supervisor,
Channel infrastructure, or Runtime DTOs.

### 7.1 Provider journal

`AgentRunner` receives a `TurnJournalPort` or Provider observer through Agent
construction. It does not import `JournalProviderObserver`. Runtime creates
the observer and injects it through the Agent-owned port.

### 7.2 Tool execution

Tool request DTO construction and the `ToolExecutionPort` contract belong to
Agent-owned code. Runtime's Tool Gateway implements the port. Runner never
imports `runtime.tool_gateway` and never directly calls `tool.execute` for a
runtime turn.

### 7.3 Message Tool delivery

Message Tool depends on an Agent-owned outbound port. Runtime binds a
task-scoped implementation backed by Outbox. The tool does not import Runtime
session context, Runtime DTOs, Message Bus callbacks, or ChannelManager.

### 7.4 Shell containment

Shell Tool depends on an Agent-owned containment protocol and task-scoped
binding. Runtime supplies the OS-specific containment implementation. Shell
Tool does not import `runtime.containment`.

### 7.5 Runtime context

Context variables used by Agent tools are declared beside Agent-owned ports.
Runtime binds concrete implementations at the Worker boundary and clears them
in `finally` blocks. This preserves task isolation without reversing the
dependency direction.

## 8. Durable Ingress and Completion Flows

### 8.1 User or API turn

1. ingress authenticates and normalizes the request;
2. ingress constructs stable scope, session key, inbound identity, and dedup
   key;
3. `TaskService.submit()` commits the task and session sequence;
4. the caller acknowledges only after the durable commit;
5. a wake hint is sent, but Worker polling remains the correctness fallback;
6. one Worker claims the session head under a fenced lease;
7. Session Committer appends the inbound message exactly once;
8. Agent Core executes through injected journal, tool, progress, control,
   session, and outbound ports;
9. final session mutation and final Outbox row are committed;
10. OutboxSender delivers through ChannelManager and records a receipt;
11. synchronous API/CLI callers await terminal task state and read the durable
    result rather than receiving a direct Agent return value.

### 8.2 Streaming

Token, reasoning, and progress events remain transient and may use Message Bus
or IPC. They may be dropped or coalesced under pressure. The complete final
message never relies on this path and comes only from Outbox/durable task
state.

### 8.3 Required background work

Cron, Dream, consolidation, indexing, retention, backup, blob GC, and WAL
checkpoint triggers submit typed internal tasks with deterministic dedup keys.
`TaskSupervisor` and `asyncio.create_task()` remain allowed only for disposable
pollers, bridges, lease heartbeats, and reconstructable lifecycle helpers.

## 9. Legacy Removal

The final branch removes:

- `runtime.enabled` and all conditional legacy selection;
- process-local `pending_queues` as a task authority;
- turn-dispatch `asyncio.create_task()` as the owner of accepted work;
- direct `AgentLoop.process_direct()` execution;
- Runner direct `tool.execute` and registry execution;
- direct final `bus.publish_outbound`;
- Message Tool direct send callbacks;
- direct final `ChannelManager.send` outside OutboxSender;
- legacy `runtime_checkpoint` and `pending_user_turn` writers;
- required Dream and Cron detached Agent tasks;
- production code reachable only by the old task path.

Compatibility method names may exist in intermediate commits only. They must
delegate to Runtime and be deleted before the final acceptance commit.

Existing session transcript compatibility remains. Because the project is
pre-release, incomplete legacy runtime checkpoints are not imported as live
tasks. The cutover preserves the files for inspection, ignores their old
execution authority, and may remove obsolete metadata during an explicit
development migration.

## 10. Runtime Store Decomposition

`SqliteRuntimeStore` remains the single public transactional façade and the
only implementation of the narrow Runtime Store protocols. It is split
internally by cohesive responsibility:

```text
miniunicorn/runtime/sqlite/
├── store.py                 # façade, construction, view exposure
├── task_store.py            # submit, claim, lease, state, controls, events
├── execution_store.py       # checkpoints, model and tool attempts
├── session_store.py         # session commit prepare/confirm/conflict
├── outbox_store.py          # enqueue, claim, receipt, retry, ambiguity
├── resource_store.py        # resource leases
└── maintenance_store.py     # retention, blobs, WAL checkpoint, backup
```

Rules:

- all helpers share the same connection policy and migrations;
- cross-table atomic operations stay in one cohesive helper;
- no deployable service or independent database is created per table;
- no SQL escapes the SQLite implementation package;
- no external call occurs inside a database transaction;
- each module has focused real-SQL tests;
- `store.py` becomes a façade, not another delegation-heavy god object.

## 11. Failure and Shutdown Behavior

### 11.1 Worker failure

- expired or replaced leases fence every late mutation;
- safe journaled results may be reused;
- uncertain non-idempotent effects enter `WAITING_USER`;
- the Supervisor restarts the Worker with bounded exponential backoff;
- the replacement Worker reconstructs all dependencies from configuration;
- child process containment terminates tool descendants.

### 11.2 Control Plane failure

- accepted tasks, checkpoints, and Outbox rows survive;
- Workers may continue safe claimed work because SQLite is authoritative;
- the Supervisor restarts the Control Plane;
- Channel ingress and Outbox delivery resume from durable state;
- readiness remains false until the Control Plane and minimum Worker capacity
  report ready.

### 11.3 Graceful shutdown

1. stop accepting new ingress;
2. stop scheduling new claims;
3. allow in-flight work to checkpoint within `shutdown_grace_s`;
4. stop Outbox after its bounded delivery attempt;
5. send shutdown to children;
6. terminate remaining child trees after the deadline;
7. close process-local database and Channel resources.

## 12. Congestion and Backpressure

The cutover does not put every interaction onto one realtime queue:

- durable facts use SQLite;
- wake hints and streaming use bounded, lossy IPC;
- final delivery uses Outbox;
- synchronous callers wait on task state with bounded polling/notification;
- one session is intentionally serialized;
- three Workers provide cross-session concurrency.

The initial supported profile is local, low-to-moderate concurrency. Before
acceptance, load tests must measure:

- SQLite transaction latency and busy count;
- queue age and claim latency;
- Worker utilization and restart count;
- Outbox pending age;
- WAL size;
- dropped realtime events;
- event-loop stalls in the Control Plane;
- session order violations and duplicate effects.

The Store split improves maintainability but does not claim to remove SQLite's
single-writer limit. If the defined load gate fails, optimize bounded
transactions, indexes, polling, and batching first. Replacing SQLite or adding
a network queue is outside this cutover.

## 13. Test Strategy

### 13.1 Architecture gates

Required hard assertions:

- Agent Core imports no Runtime implementation;
- production entrypoints import Runtime composition and do not construct a raw
  AgentLoop;
- no legacy direct-path inventory item remains;
- only OutboxSender performs durable Channel delivery;
- only Runtime Store implementation imports `sqlite3`;
- supervised child entrypoints are top-level picklable callables;
- configuration has no legacy enabled flag.

No cutover condition is represented by `xfail`.

### 13.2 Composition tests

Exercise real factories rather than manually attaching components:

- root Config parses Runtime settings;
- CLI/env/config/default precedence;
- Gateway default constructs supervised mode with three Workers;
- one-shot CLI constructs lightweight mode with one Worker;
- API and Channel ingress obtain TaskService from the runtime application;
- health/status/metrics observe the production Store and Host automatically;
- shutdown closes every constructed dependency.

### 13.3 Golden-flow tests

Run the same scenarios through lightweight and supervised modes:

- simple final response;
- command response;
- model/tool/model turn;
- Message Tool delivery;
- same-session sequential tasks;
- three different sessions concurrently;
- final Outbox delivery;
- cancellation and steering;
- required maintenance task.

Compare durable task, session commit, journal, tool, and Outbox facts rather
than timing-sensitive stream chunks.

### 13.4 Failure-injection tests

Use real temporary SQLite databases and spawned processes. Kill a Worker:

- after claim;
- during Provider call;
- after Provider journal commit;
- during read-only tool;
- after an uncertain write tool effect;
- after session prepare;
- after session file replace;
- after Outbox enqueue.

Also restart the Control Plane, kill the Supervisor, force SQLite lock
pressure, fail Channel delivery, and verify stale-lease fencing.

### 13.5 Load gate

Before declaring the cutover complete:

- submit at least 1,000 mixed tasks over at least 100 sessions;
- demonstrate three concurrent Workers on distinct sessions;
- demonstrate zero overlap for one session;
- inject duplicate inbound messages;
- repeatedly kill and restart Workers;
- exercise maintenance and Channel failure concurrently;
- report zero missing final replies, duplicate task acceptance, session-order
  violations, and stale-lease commits;
- keep SQLite busy errors, queue age, Outbox age, memory, handles, and child
  processes within explicit test thresholds recorded by the test harness.

A 24-hour soak is a release-readiness task, not a blocking step for every
local commit. The implementation must provide the repeatable soak command and
result schema.

## 14. Delivery Sequence

The implementation plan must preserve a runnable branch at each review gate:

1. harden characterization and turn architecture xfails into explicit cutover
   gates;
2. correct Agent-owned ports and dependency direction;
3. integrate RuntimeConfig into the root configuration and CLI precedence;
4. create production Runtime composition and real child entrypoints;
5. cut one-shot CLI and direct callers to durable lightweight execution;
6. cut Gateway, API, WebSocket, and Channels to the Control Plane;
7. route Provider, tools, Message Tool, final replies, and background work
   exclusively through injected ports and durable services;
8. prove exactly three supervised Workers with golden and crash flows;
9. delete the legacy task authority and compatibility adapters;
10. split the SQLite Store without changing its public protocols;
11. complete observability, load, documentation, and clean acceptance.

Each step uses test-first changes and ends in one focused commit. A later step
must not weaken an earlier architecture gate.

## 15. Acceptance Criteria

The cutover is complete only when:

1. long-running Gateway default startup produces one ready Control Plane and
   three ready single-concurrency Workers;
2. no production composition path constructs or calls AgentLoop directly;
3. CLI, API, Channel, WebSocket, Cron, Dream, and maintenance ingress all
   submit durable tasks;
4. acknowledged work survives immediate process termination;
5. duplicate inbound messages create one task and one session sequence;
6. one session never overlaps and three different sessions can run
   concurrently;
7. stale Workers cannot mutate task, journal, session, tool, or Outbox state;
8. every model decision and terminal tool result is durable before Agent Core
   consumes it;
9. every final reply is atomically enqueued and delivered through Outbox;
10. Message Tool never publishes or sends directly;
11. required background work is never owned only by `asyncio.create_task`;
12. Agent Core imports no Runtime, SQLite, multiprocessing, Supervisor, or
    Channel implementation;
13. Runtime tasks write no legacy checkpoint authority;
14. root configuration exposes Runtime mode and Worker settings but no
    `runtime.enabled`;
15. production health/status/metrics are wired without test-only Store
    attachment;
16. architecture tests contain no legacy-path xfails;
17. lightweight and supervised durable golden facts match;
18. multiprocess failure-injection and 1,000-task load gates pass;
19. the full backend and WebUI test suites pass in the locked development
    environment;
20. documentation describes only the durable lightweight and supervised
    modes.

## 16. Final Decision

MiniUnicorn will not ship a legacy execution mode.

The implementation branch may temporarily route old method names through the
Runtime to keep intermediate commits executable, but the merge candidate
contains one durable task authority, one session content authority, one tool
execution gateway, and one final-delivery authority.

The long-running service defaults to a supervised Control Plane plus three
Worker processes. One-shot and development use cases may choose lightweight
assembly, but they do not bypass the durable Runtime.
