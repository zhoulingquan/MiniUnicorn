"""Production Control Plane and Worker child entrypoints (design Task 7).

These top-level functions run inside spawned child processes under
supervised mode (design §9.2, §24.6). Each entrypoint:

1. Reconstructs ``Config`` from a JSON-serialised
   :class:`ChildBootstrapPayload` — no inherited SQLite connection,
   Provider, Agent, or bus may cross the spawn boundary (design §24.6,
   §32.5).
2. Opens its own Runtime Store connection. The Control Plane runs
   migrations before signalling ready; Workers validate schema only and
   never migrate (design §23.2) — they must wait for the Control Plane
   ready signal before touching the database.
3. Constructs process-local dependencies (SessionManager, Provider,
   Agent, Tool Gateway, etc.) and runs until ``KIND_SHUTDOWN``.
4. Closes resources in ``finally`` so partial startup never leaks.

The functions are top-level (not closures) and picklable so
``multiprocessing.spawn`` works on Windows (design §32.5).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from miniunicorn.runtime.ipc import (
    KIND_AGENT_EVENT,
    KIND_SHUTDOWN,
    KIND_TASK_PROGRESS,
    IpcEnvelope,
    ProcessIpcChannel,
    agent_event,
)

# ---------------------------------------------------------------------------
# Async IPC helpers — keep blocking pipe polls off the event-loop thread
# (Task 1 Step 4)
# ---------------------------------------------------------------------------


async def recv_child_envelope(
    channel: ProcessIpcChannel,
    timeout_s: float = 0.5,
) -> IpcEnvelope | None:
    """Poll the child IPC pipe off the event-loop thread (Task 1 Step 4).

    ``child_poll`` is a blocking call (``multiprocessing.Connection.poll``).
    Running it directly on the event-loop thread starves every asyncio task
    — including the Worker's ``run()`` coroutine — for the full poll
    interval. ``asyncio.to_thread`` moves the blocking poll to a worker
    thread so the event loop stays free to run Agent turns, heartbeats, and
    Outbox delivery.
    """
    ready = await asyncio.to_thread(channel.child_poll, timeout_s)
    if not ready:
        return None
    return channel.child_recv()


async def stop_worker_gracefully(
    worker: Any,
    task: asyncio.Task[Any],
    grace_s: float,
) -> None:
    """Stop the Worker and give its active task the configured grace period
    (Task 1 Step 5).

    ``worker.stop()`` signals the Worker loop to exit. ``asyncio.shield``
    lets the Worker finish any in-flight work within ``grace_s``. If the
    grace period expires the task is cancelled so shutdown is not
    unbounded.
    """
    worker.stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=grace_s)
    except asyncio.TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# JSON-safe scalar validation for the surface dictionary
# ---------------------------------------------------------------------------

_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


def _validate_surface(surface: Any) -> None:
    """Recursively validate that ``surface`` contains only JSON scalars.

    Rejects callables, arbitrary objects, and nested containers holding
    non-scalar values. This prevents smuggling unpicklable or untrusted
    state across the spawn boundary (design §24.6).
    """
    if isinstance(surface, dict):
        for value in surface.values():
            _validate_surface(value)
        return
    if isinstance(surface, (list, tuple)):
        for item in surface:
            _validate_surface(item)
        return
    if not isinstance(surface, _JSON_SCALAR_TYPES):
        raise TypeError(f"surface must contain only JSON scalars, got {type(surface).__name__}")
    if callable(surface):
        raise TypeError("surface must not contain callables")


# ---------------------------------------------------------------------------
# ChildBootstrapPayload — frozen, JSON-safe spawn payload (Task 7 Step 5)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ChildBootstrapPayload:
    """Picklable, JSON-safe payload handed to each spawned child.

    - ``config_json`` — ``Config.model_validate_json`` input; never a
      live ``Config`` object (design §24.6).
    - ``surface`` — JSON-safe scalar dict of derived values the child
      needs without re-reading the config file (e.g. resolved paths,
      mode). Recursively validated at construction time.
    """

    config_json: str
    surface: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_surface(self.surface)
        if not isinstance(self.config_json, str):
            raise TypeError("config_json must be a string")


# ---------------------------------------------------------------------------
# RealtimeIpcEmitter — bounded Worker-side IPC emitter (Task 7 Step 4)
# ---------------------------------------------------------------------------


class RealtimeIpcEmitter:
    """Bounded emitter that forwards serialized Agent events to the parent
    Control Plane over IPC without blocking the Agent turn.

    ``emit()`` only calls ``put_nowait()`` on a bounded process-local
    queue. One reconstructable sender task performs the blocking
    ``ipc_channel.child_send()`` via ``asyncio.to_thread``. When the
    queue is full the transient event is dropped and
    ``dropped_events`` is incremented — the Agent turn never awaits free
    space (design §23.2).
    """

    def __init__(
        self,
        *,
        instance_id: str,
        ipc_channel: ProcessIpcChannel,
        capacity: int = 256,
    ) -> None:
        self._instance_id = instance_id
        self._ipc = ipc_channel
        self._queue: asyncio.Queue[IpcEnvelope | None] = asyncio.Queue(maxsize=capacity)
        self._sender_task: asyncio.Task[None] | None = None
        self.dropped_events = 0

    async def start(self) -> None:
        if self._sender_task is None:
            self._sender_task = asyncio.create_task(
                self._sender_loop(), name=f"ipc-emitter-{self._instance_id}"
            )

    async def stop(self) -> None:
        """Close IPC, cancel the sender task, and record the final drop count."""
        if self._sender_task is None:
            return
        # Signal the sender loop to drain and exit.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self._sender_task.cancel()
        try:
            await self._sender_task
        except asyncio.CancelledError:
            pass
        self._sender_task = None

    def emit(self, *, task_id: str | None, event: dict[str, Any]) -> None:
        """Non-blocking emit. Drops + increments counter on full queue."""
        env = agent_event(
            self._instance_id,
            event=event,
            task_id=task_id,
        )
        try:
            self._queue.put_nowait(env)
        except asyncio.QueueFull:
            self.dropped_events += 1

    async def _sender_loop(self) -> None:
        """Drain the queue and perform blocking IPC sends off the event loop."""
        while True:
            env = await self._queue.get()
            if env is None:
                return
            try:
                await asyncio.to_thread(self._ipc.child_send, env)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # IPC closed or broken — drop the event and keep the loop
                # alive until explicitly stopped.
                self.dropped_events += 1


# ---------------------------------------------------------------------------
# IpcProgressPort — per-task ProgressPort backed by RealtimeIpcEmitter
# ---------------------------------------------------------------------------


class IpcProgressPort:
    """Per-task ProgressPort that serializes events to the IPC emitter.

    Constructed once per claimed task. ``emit()`` only calls
    ``put_nowait()`` on the emitter — never blocks on the IPC pipe
    (design §23.2, Task 7 Step 4).
    """

    def __init__(self, task_id: str, emitter: RealtimeIpcEmitter) -> None:
        self._task_id = task_id
        self._emitter = emitter

    async def emit(self, event: Any) -> None:
        from miniunicorn.bus.agent_events import serialize_agent_event

        self._emitter.emit(task_id=self._task_id, event=serialize_agent_event(event))


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


def worker_main(**kwargs: Any) -> int:
    """Worker child entrypoint (design Task 7 Step 5).

    Reconstructs ``Config`` from the bootstrap payload, waits for the
    Control Plane ready signal, then opens its own Store connection
    (schema validate only — no migrations), constructs the Agent /
    Tool Gateway / Session Committer / Scheduler / one
    :class:`AgentTaskWorker`, binds an IPC-backed ProgressPort, and runs
    until ``KIND_SHUTDOWN``.
    """
    return asyncio.run(_worker_async(**kwargs))


def control_plane_main(**kwargs: Any) -> int:
    """Control Plane child entrypoint (design Task 7 Step 6).

    Reconstructs ``Config``, opens its own Store connection and runs
    migrations before readiness, constructs MessageBus / TaskService /
    OutboxSender / Channels / API surface / Cron triggers /
    RealtimeSubscriptionHub, sends ``KIND_WAKE_HINT`` to the parent
    after every accepted submit, consumes relayed Worker events, and
    drains/closes on ``KIND_SHUTDOWN``.
    """
    return asyncio.run(_control_plane_async(**kwargs))


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


def _reconstruct_config(config: Any) -> Any:
    """Reconstruct a root ``Config`` from a payload, JSON string, or live object."""
    from miniunicorn.config.schema import Config

    if isinstance(config, ChildBootstrapPayload):
        return Config.model_validate_json(config.config_json)
    if isinstance(config, str):
        return Config.model_validate_json(config)
    return config


async def _worker_async(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Callable[[str], None],
    control_ready_event: asyncio.Event | None = None,
    **_unused: Any,
) -> int:
    """Async Worker implementation (design Task 7 Step 5).

    The ``control_ready_event`` gate enforces startup order: the Worker
    must not open the Runtime database before the Control Plane reports
    ready after migrations (design §23.2, Task 7 Step 5). In production
    spawn the Supervisor only spawns Workers after the Control Plane is
    ready, so a ``None`` event is set immediately.
    """
    from pathlib import Path

    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.config.runtime import resolve_runtime_paths
    from miniunicorn.runtime.agent_adapter import AgentExecutionCallback
    from miniunicorn.runtime.containment import ProcessContainmentScope
    from miniunicorn.runtime.durable_journal import DurableTurnJournalAdapter
    from miniunicorn.runtime.message_delivery import DurableMessageDelivery
    from miniunicorn.runtime.scheduler import Scheduler
    from miniunicorn.runtime.session_committer import SessionCommitter
    from miniunicorn.runtime.sqlite import (
        SqliteRuntimeStore,
        open_connection,
        validate_schema_version,
    )
    from miniunicorn.runtime.sqlite.vector_memory_store import create_vector_store
    from miniunicorn.runtime.tool_gateway import ToolGateway
    from miniunicorn.runtime.worker import AgentTaskWorker
    from miniunicorn.session.manager import SessionManager

    cfg = _reconstruct_config(config)
    resolved = resolve_runtime_paths(cfg.runtime, cfg.workspace_path)

    # Gate on Control Plane readiness. In production the Supervisor
    # guarantees ordering (Workers spawn only after Control Plane ready),
    # so a ``None`` event is satisfied immediately.
    if control_ready_event is None:
        control_ready_event = asyncio.Event()
        control_ready_event.set()
    await control_ready_event.wait()

    # Open the Worker's own connection and VALIDATE schema only — never
    # migrate (design §23.2, §16.1). Migrations are the Control Plane's
    # exclusive responsibility.
    db_path = resolved.database_path_resolved or Path(resolved.database_path)
    connection = open_connection(db_path)
    validate_schema_version(connection)
    store = SqliteRuntimeStore(connection)

    sessions = SessionManager(cfg.workspace_path)
    bus = MessageBus()

    from miniunicorn.runtime.ingress import local_request_scope
    from miniunicorn.runtime.maintenance import make_maintenance_enqueue_callback
    from miniunicorn.runtime.task_service import TaskService

    _task_service = TaskService(store)
    _scope = local_request_scope(cfg)
    _maintenance_enqueue = make_maintenance_enqueue_callback(_task_service, _scope)

    agent = AgentLoop.from_config(
        cfg,
        bus,
        session_manager=sessions,
        vector_memory_factory=create_vector_store,
        maintenance_enqueue=_maintenance_enqueue,
    )

    session_committer = SessionCommitter(store, sessions)
    tool_gateway = ToolGateway(agent.tools, store, store)
    journal = DurableTurnJournalAdapter(store, store)
    scheduler = Scheduler(
        store,
        lease_ms=resolved.lease_timeout_s * 1000,
        max_root_attempts=resolved.task_max_attempts,
    )

    # Build the bounded IPC emitter for transient realtime events.
    emitter = RealtimeIpcEmitter(
        instance_id=instance_id,
        ipc_channel=ipc_channel,
        capacity=resolved.realtime_event_queue_capacity,
    )
    await emitter.start()

    def _outbound_factory(task_id: str) -> Any:
        return DurableMessageDelivery(task_id)

    def _progress_factory(task_id: str) -> IpcProgressPort:
        return IpcProgressPort(task_id, emitter)

    callback = AgentExecutionCallback(
        agent.core_dispatcher,
        agent.turn_coordinator,
        tool_execution_port=tool_gateway,
        turn_journal=journal,
        outbound_port_factory=_outbound_factory,
        progress_port_factory=_progress_factory,
    )

    # Task 9 Step 5: compose a MaintenanceExecutor from the Agent's
    # existing Dream/Consolidator functions so durable maintenance tasks
    # claimed by this Worker dispatch in-process (design §22.3). No
    # second CronService or ChannelManager is constructed here.
    from miniunicorn.runtime.bootstrap import _build_maintenance_executor

    maintenance_executor = _build_maintenance_executor(store=store, agent=agent)

    worker = AgentTaskWorker(
        worker_id=instance_id,
        scheduler=scheduler,
        worker_ledger=store,
        session_committer=session_committer,
        execution_callback=callback,
        heartbeat_interval_s=resolved.heartbeat_interval_s,
        maintenance_executor=maintenance_executor,
        containment_factory=lambda task_id: ProcessContainmentScope(task_id),
    )

    # Signal readiness to the Supervisor only after all process-local
    # dependencies are ready (design Task 7 Step 5).
    ready_signal(role)

    worker_task = asyncio.create_task(worker.run(), name=f"worker-{instance_id}")
    logger.info("worker {} ready and running", instance_id)

    try:
        # Run the Worker until KIND_SHUTDOWN. The AgentTaskWorker polls
        # the Store directly; wake hints only reduce poll latency.
        # The blocking child_poll is moved off the event-loop thread via
        # recv_child_envelope so worker.run() is not starved (Task 1 Step 4).
        while True:
            env = await recv_child_envelope(ipc_channel, timeout_s=0.5)
            if env is None:
                continue
            if env.kind == KIND_SHUTDOWN:
                break
            # Wake hints reduce poll latency; correctness does not depend
            # on them (Workers poll the Store directly — design §23.2).
        return 0
    except (EOFError, OSError):
        return 0
    finally:
        await stop_worker_gracefully(worker, worker_task, resolved.shutdown_grace_s)
        await emitter.stop()
        try:
            await agent.close_mcp()
        except Exception:  # noqa: BLE001
            logger.warning("worker {} agent.close_mcp raised", instance_id)
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            logger.warning("worker {} connection.close raised", instance_id)
        logger.info("worker {} exited", instance_id)


async def _control_plane_async(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Callable[[str], None],
    **_unused: Any,
) -> int:
    """Async Control Plane implementation (design Task 7 Step 6).

    Opens its own Store connection and runs migrations BEFORE readiness.
    Owns ingress, Outbox, Channels, Cron, API/WebUI, and the realtime
    hub but creates no Agent and no Worker coroutine.
    """
    from pathlib import Path

    from miniunicorn.config.runtime import resolve_runtime_paths
    from miniunicorn.runtime.bootstrap import build_control_plane_runtime
    from miniunicorn.runtime.sqlite import open_connection, run_migrations

    # Capture the caller-provided surface before reconstructing the
    # config so it reaches build_control_plane_runtime (Task 1 Step 6).
    # Never replace it with {} — the surface carries webui_runtime_surface,
    # webui_static_dist, and capability overrides from the launcher.
    child_surface: dict[str, Any] = {}
    if isinstance(config, ChildBootstrapPayload):
        child_surface = dict(config.surface)
    cfg = _reconstruct_config(config)
    resolved = resolve_runtime_paths(cfg.runtime, cfg.workspace_path)

    # Open the Control Plane's own connection and RUN MIGRATIONS before
    # readiness (design Task 7 Step 6). Workers must not race this.
    db_path = resolved.database_path_resolved or Path(resolved.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = open_connection(db_path)
    run_migrations(connection)

    resources = build_control_plane_runtime(cfg, connection, surface=child_surface)
    await resources.start()

    # Signal readiness to the Supervisor only after Store, Outbox, and
    # configured ingress are bound (design Task 7 Step 6).
    ready_signal(role)
    logger.info("control plane {} ready", instance_id)

    try:
        # Consume relayed Worker events from the child pipe and publish
        # them to the local hub. The Supervisor relays Worker
        # ``agent_event``/``task_progress`` envelopes here; ``KIND_SHUTDOWN``
        # terminates the loop. Wake hints on submit are sent the other
        # direction (Control Plane → Supervisor) and are wired with ingress
        # in Task 9.
        # The blocking child_poll is moved off the event-loop thread via
        # recv_child_envelope so Outbox delivery and ingress stay live
        # (Task 1 Step 4).
        while True:
            env = await recv_child_envelope(ipc_channel, timeout_s=0.5)
            if env is None:
                continue
            if env.kind == KIND_SHUTDOWN:
                break
            if env.kind in (KIND_AGENT_EVENT, KIND_TASK_PROGRESS):
                resources.realtime.publish_envelope(env)
                continue
        return 0
    except (EOFError, OSError):
        return 0
    finally:
        try:
            await resources.stop()
        except Exception:  # noqa: BLE001
            logger.warning("control plane {} resources.stop raised", instance_id)
        logger.info("control plane {} exited", instance_id)


__all__ = [
    "ChildBootstrapPayload",
    "IpcProgressPort",
    "RealtimeIpcEmitter",
    "control_plane_main",
    "worker_main",
]
