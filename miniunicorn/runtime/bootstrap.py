"""Production construction for lightweight and supervised modes (design Task 5).

``build_lightweight_runtime`` assembles the full durable kernel — Runtime
Store, SessionManager, AgentLoop, ToolGateway, SessionCommitter,
AgentExecutionCallback, LightweightHost, OutboxSender, and
RuntimeApplication — into one lifecycle-managed ``RuntimeResources``
object.

This module must not import ``sqlite3`` directly; it uses
:func:`miniunicorn.runtime.sqlite.open_connection` and
:func:`miniunicorn.runtime.sqlite.run_migrations` as opaque factories
(design Task 5 Step 3).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from miniunicorn.config.runtime import RuntimeConfig, resolve_runtime_paths
from miniunicorn.runtime.agent_adapter import AgentExecutionCallback
from miniunicorn.runtime.application import RuntimeApplication
from miniunicorn.runtime.durable_journal import DurableTurnJournalAdapter
from miniunicorn.runtime.hosts.lightweight import LightweightHost
from miniunicorn.runtime.message_delivery import DurableMessageDelivery, LocalResultSender
from miniunicorn.runtime.outbox import OutboxSender
from miniunicorn.runtime.realtime import LocalProgressPort, RealtimeSubscriptionHub
from miniunicorn.runtime.session_committer import SessionCommitter
from miniunicorn.runtime.tool_gateway import ToolGateway

# ---------------------------------------------------------------------------
# Closeable protocol (design Task 5 Step 3)
# ---------------------------------------------------------------------------


@runtime_checkable
class Closeable(Protocol):
    """Minimal closeable resource (avoids importing sqlite3 here)."""

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# RuntimeResources — lifecycle-managed lightweight runtime (Task 5 Step 3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RuntimeResources:
    """Lifecycle-managed lightweight runtime (design Task 5).

    Owns the application, host, outbox sender, store connection, and
    agent. ``start()`` brings everything online in dependency order;
    ``stop()`` tears down in reverse, continuing past individual close
    failures and re-raising the first captured exception.
    """

    application: RuntimeApplication
    host: LightweightHost
    outbox_sender: OutboxSender
    store: Any
    connection: Closeable
    agent: Any
    config: RuntimeConfig
    shutdown_grace_s: float = 60.0
    shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    channels: Any = None  # ChannelManager | None (Task 9)
    cron_service: Any = None  # CronService | None (Task 9)
    _stopped: bool = False

    async def start(self) -> None:
        """Start the outbox, host, channels, cron, and begin accepting ingress."""
        await self.outbox_sender.start()
        await self.host.start()
        if self.cron_service is not None:
            await self.cron_service.start()
        if self.channels is not None:
            await self.channels.start_all()
        self.application.start_accepting()
        logger.info("lightweight runtime resources started")

    async def stop(self) -> None:
        """Stop resources in reverse dependency order (idempotent).

        Continues closing later resources if an earlier close fails;
        re-raises the first captured exception after all cleanup.
        """
        if self._stopped:
            return
        self._stopped = True
        first_exc: BaseException | None = None

        self.application.stop_accepting()
        if self.channels is not None:
            try:
                await self.channels.stop_all()
            except BaseException as exc:  # noqa: BLE001
                first_exc = exc if first_exc is None else first_exc
        if self.cron_service is not None:
            try:
                self.cron_service.stop()
                await self.cron_service.await_stop()
            except BaseException as exc:  # noqa: BLE001
                first_exc = exc if first_exc is None else first_exc
        try:
            await self.host.stop(grace_s=self.shutdown_grace_s)
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        try:
            await self.outbox_sender.drain(timeout_s=self.shutdown_grace_s)
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        try:
            await self.outbox_sender.stop()
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        try:
            await self.agent.close_mcp()
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        try:
            self.connection.close()
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc

        self.closed.set()
        logger.info("lightweight runtime resources stopped")
        if first_exc is not None:
            raise first_exc

    def request_shutdown(self) -> None:
        """Signal the long-running launcher to shut down."""
        self.shutdown_requested.set()

    async def wait_for_shutdown(self) -> None:
        """Wait until shutdown is requested."""
        await self.shutdown_requested.wait()


# ---------------------------------------------------------------------------
# ChannelManager construction helper (Task 9)
# ---------------------------------------------------------------------------


def _build_channel_manager(
    config: Any,
    bus: Any,
    sessions: Any,
    agent: Any,
    task_service: Any,
    *,
    surface: dict[str, Any] | None = None,
) -> Any:
    """Construct a ChannelManager wired to the durable Runtime (Task 9).

    The ChannelManager's ``send_with_receipt`` is used as the Outbox
    sender, and a ``submit_inbound`` callback routes channel ingress
    to the ``TaskService`` instead of the legacy bus.
    """
    import time as _time

    from miniunicorn.channels.manager import ChannelManager
    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope

    surface = surface or {}
    scope = local_request_scope(config)

    async def submit_inbound(msg: Any) -> None:
        """Convert an InboundMessage to a durable task and submit it."""
        request = RuntimeInboundRequest(
            content=msg.content,
            media=tuple(msg.media),
            metadata=dict(msg.metadata or {}),
            session_key=msg.session_key,
            channel=msg.channel,
            channel_account=msg.sender_id,
            channel_message_id=(msg.metadata or {}).get("message_id"),
            scope=scope,
            target_key=msg.chat_id or "",
        )
        envelope = build_inbound_envelope(request, now_ms=int(_time.time() * 1000))
        await task_service.submit(envelope)

    return ChannelManager(
        config,
        bus,
        session_manager=sessions,
        webui_runtime_model_name=lambda: getattr(agent, "model", None),
        webui_static_dist=surface.get("webui_static_dist", True),
        webui_runtime_surface=surface.get("webui_runtime_surface", "browser"),
        webui_runtime_capabilities=surface.get("webui_runtime_capabilities"),
        webui_provider_loader=lambda: getattr(agent, "provider", None),
        submit_inbound=submit_inbound,
    )


# ---------------------------------------------------------------------------
# MaintenanceExecutor composition (Task 9 Step 5)
# ---------------------------------------------------------------------------


def _build_maintenance_executor(
    *,
    store: Any,
    agent: Any,
) -> Any:
    """Compose a :class:`MaintenanceExecutor` from existing Agent functions.

    Reuses the Agent's already-constructed Dream, Consolidator, and memory
    index operations — no second CronService or ChannelManager is built in
    the Worker (design §22.3, Task 9 Step 5). Retention, blob GC, WAL
    checkpoint, and backup fall back to the module-level functions in
    :mod:`miniunicorn.runtime.maintenance`, which call the same Store façade.

    Reflection and index runners are wired only when the Agent exposes
    them; an unconfigured kind fails with a stable safe error code via the
    Worker's ``MAINTENANCE_EXECUTOR_NOT_CONFIGURED`` / executor
    ``not configured`` path.
    """
    from miniunicorn.runtime.maintenance_executor import MaintenanceExecutor

    async def _dream_runner() -> bool:
        return await agent.dream.run()

    async def _consolidation_runner(session_key: str) -> str | None:
        return await agent.consolidator.compact_idle_session(session_key)

    return MaintenanceExecutor(
        store,
        dream_runner=_dream_runner,
        consolidation_runner=_consolidation_runner,
    )


# ---------------------------------------------------------------------------
# Maintenance callback wiring (design §22.3, WP7 hard cutover)
# ---------------------------------------------------------------------------


def _wire_maintenance_callbacks(
    *,
    config: Any,
    task_service: Any,
    cron_service: Any,
    agent: Any = None,
) -> None:
    """Wire Dream/Cron enqueue callbacks to submit through TaskService (§22.3).

    Wires:
    - ``CronService.system_enqueue_callback``: ``system_event`` jobs enqueue
      a durable maintenance task instead of calling ``on_job`` directly.
    - ``CronService.on_job``: ``agent_turn`` jobs enqueue a durable
      ``USER_TURN`` task so cron-triggered turns survive restarts.
    - ``DreamIdleTrigger._enqueue_callback``: the idle trigger enqueues a
      durable ``DREAM`` task instead of ``asyncio.create_task``.

    The DreamIdleTrigger is only wired when ``agent`` is provided (the
    Control Plane has no Agent).
    """
    import time as _time

    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.ingress import (
        build_inbound_envelope,
        local_request_scope,
    )
    from miniunicorn.runtime.maintenance import (
        PRIORITY_REFLECTION_DREAM,
        PRIORITY_RETENTION_CLEANUP,
        dedup_key_for_dream,
        enqueue_maintenance,
    )

    scope = local_request_scope(config)

    # --- CronService callbacks ---
    if cron_service is not None:

        async def _system_enqueue_callback(job: Any) -> str | None:
            """Enqueue a durable maintenance task for a system_event cron job."""
            name_lower = (job.name or "").lower()
            if "dream" in name_lower:
                task_kind = "DREAM"
                priority = PRIORITY_REFLECTION_DREAM
            else:
                task_kind = "MAINTENANCE"
                priority = PRIORITY_RETENTION_CLEANUP
            dedup_key = f"cron:{job.id}:{job.state.last_run_at_ms or 0}"
            return await enqueue_maintenance(
                task_service,
                task_kind=task_kind,
                scope=scope,
                dedup_key=dedup_key,
                priority=priority,
                payload={"job_id": job.id, "job_name": job.name},
            )

        cron_service.system_enqueue_callback = _system_enqueue_callback

        async def _on_job(job: Any) -> str | None:
            """Enqueue a durable USER_TURN task for an agent_turn cron job."""
            payload = job.payload
            session_key = payload.session_key or f"cron:{job.id}"
            channel = payload.channel or "cli"
            chat_id = payload.to or "cron"
            request = RuntimeInboundRequest(
                content=payload.message,
                media=(),
                metadata={
                    "cron_job_id": job.id,
                    "cron_job_name": job.name,
                    "deliver": payload.deliver,
                    "channel_meta": payload.channel_meta,
                },
                session_key=session_key,
                channel=channel,
                channel_account=chat_id,
                channel_message_id=None,
                scope=scope,
                target_key=chat_id,
            )
            envelope = build_inbound_envelope(request, now_ms=int(_time.time() * 1000))
            handle = await task_service.submit(envelope)
            return handle.task_id

        cron_service.on_job = _on_job

    # --- DreamIdleTrigger enqueue_callback ---
    if agent is not None and hasattr(agent, "dream_idle_trigger"):

        async def _dream_enqueue_callback(source_revision: str) -> str | None:
            """Enqueue a durable DREAM task (design §22.3)."""
            return await enqueue_maintenance(
                task_service,
                task_kind="DREAM",
                scope=scope,
                dedup_key=dedup_key_for_dream(source_revision=source_revision),
                priority=PRIORITY_REFLECTION_DREAM,
            )

        agent.dream_idle_trigger._enqueue_callback = _dream_enqueue_callback


# ---------------------------------------------------------------------------
# build_lightweight_runtime (Task 5 Step 4)
# ---------------------------------------------------------------------------


def build_lightweight_runtime(
    config: Any,
    *,
    provider_override: Any | None = None,
    channel_sender: Any | None = None,
    gateway: bool = False,
    surface: dict[str, Any] | None = None,
) -> RuntimeResources:
    """Construct the full lightweight runtime from a root ``Config``.

    Construction order follows design Task 5 Step 4. The same durable
    kernel is used for one-shot CLI, API ``serve``, and the gateway's
    lightweight fallback. ``gateway=True`` wires a ChannelManager-based
    sender; otherwise a :class:`LocalResultSender` is used.
    """
    # Lazy imports to keep bootstrap free of sqlite3 at module level.
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.runtime.sqlite import (
        SqliteRuntimeStore,
        open_connection,
        run_migrations,
    )
    from miniunicorn.runtime.sqlite.vector_memory_store import create_vector_store
    from miniunicorn.session.manager import SessionManager

    resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
    db_path = resolved.database_path_resolved or Path(resolved.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = open_connection(db_path)
    run_migrations(connection)
    store = SqliteRuntimeStore(connection)

    sessions = SessionManager(config.workspace_path)
    bus = MessageBus()

    from miniunicorn.runtime.ingress import local_request_scope
    from miniunicorn.runtime.maintenance import make_maintenance_enqueue_callback
    from miniunicorn.runtime.task_service import TaskService

    _task_service = TaskService(store)
    _scope = local_request_scope(config)
    _maintenance_enqueue = make_maintenance_enqueue_callback(_task_service, _scope)

    agent = AgentLoop.from_config(
        config,
        bus,
        session_manager=sessions,
        provider=provider_override,
        vector_memory_factory=create_vector_store,
        maintenance_enqueue=_maintenance_enqueue,
    )

    session_committer = SessionCommitter(store, sessions)
    tool_gateway = ToolGateway(agent.tools, store, store)
    journal = DurableTurnJournalAdapter(store, store)
    realtime = RealtimeSubscriptionHub(capacity=resolved.realtime_event_queue_capacity)

    def _outbound_factory(task_id: str) -> Any:
        return DurableMessageDelivery(task_id)

    def _progress_factory(task_id: str) -> LocalProgressPort:
        return LocalProgressPort(task_id, realtime)

    callback = AgentExecutionCallback(
        agent.core_dispatcher,
        agent.turn_coordinator,
        tool_execution_port=tool_gateway,
        turn_journal=journal,
        outbound_port_factory=_outbound_factory,
        progress_port_factory=_progress_factory,
    )

    host = LightweightHost(
        store,
        session_committer,
        callback,
        worker_count=resolved.lightweight_execution_slots,
        lease_ms=resolved.lease_timeout_s * 1000,
        heartbeat_interval_s=resolved.heartbeat_interval_s,
        max_root_attempts=resolved.task_max_attempts,
        maintenance_executor=_build_maintenance_executor(store=store, agent=agent),
    )

    if channel_sender is not None:
        sender = channel_sender
    elif gateway:
        # Gateway mode constructs ChannelManager in-process (Task 9).
        # The ChannelManager's send_with_receipt is wired as the Outbox
        # sender so durable delivery routes through real channels.
        channels = _build_channel_manager(
            config,
            bus,
            sessions,
            agent,
            host.task_service,
            surface=surface,
        )
        sender = channels
    else:
        sender = LocalResultSender()

    outbox_sender = OutboxSender(
        store,
        sender,
        lease_ms=resolved.outbox_lease_timeout_s * 1000,
        send_timeout_s=resolved.channel_send_timeout_s,
    )

    application = RuntimeApplication(
        task_service=host.task_service,
        result_store=store,
        realtime=realtime,
    )

    cron_service = None
    if gateway:
        from miniunicorn.cron.service import CronService

        cron_store_path = config.workspace_path / "cron" / "jobs.json"
        cron_service = CronService(cron_store_path)

    # Wire Dream/Cron enqueue callbacks to submit through TaskService
    # (design §22.3, WP7 hard cutover). In lightweight mode the Agent
    # owns the DreamIdleTrigger; the Control Plane has no Agent.
    if cron_service is not None:
        _wire_maintenance_callbacks(
            config=config,
            task_service=host.task_service,
            cron_service=cron_service,
            agent=agent,
        )

    return RuntimeResources(
        application=application,
        host=host,
        outbox_sender=outbox_sender,
        store=store,
        connection=connection,
        agent=agent,
        config=resolved,
        shutdown_grace_s=float(resolved.shutdown_grace_s),
        channels=channels if gateway else None,
        cron_service=cron_service,
    )


# ---------------------------------------------------------------------------
# Control Plane composition (design Task 7 Step 6)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ControlPlaneResources:
    """Lifecycle-managed Control Plane runtime (design Task 7 Step 6).

    Owns ingress (TaskService), Outbox, Channels (Task 9), API/WebUI
    (Task 9), Cron triggers (Task 9), and the realtime hub. It creates
    NO Agent and NO Worker coroutine — Workers live in their own
    processes and claim directly from the shared Store.
    """

    application: RuntimeApplication
    task_service: Any
    outbox_sender: OutboxSender
    store: Any
    connection: Closeable
    realtime: RealtimeSubscriptionHub
    config: RuntimeConfig
    shutdown_grace_s: float = 60.0
    channels: Any = None  # ChannelManager | None (Task 9)
    cron_service: Any = None  # CronService | None (Task 9)
    _started: bool = False
    # Channels run for the lifetime of the process; start_all() blocks until
    # the channels stop. Running it as a background task lets start() return
    # so the Control Plane can signal readiness to the Supervisor.
    _channels_task: Any = None

    async def start(self) -> None:
        """Start the Outbox, Channels, Cron, and begin accepting ingress."""
        await self.outbox_sender.start()
        if self.cron_service is not None:
            await self.cron_service.start()
        if self.channels is not None:
            self._channels_task = asyncio.create_task(self.channels.start_all())
        self.application.start_accepting()
        self._started = True
        logger.info("control plane resources started")

    async def stop(self) -> None:
        """Stop in reverse dependency order (idempotent)."""
        if not self._started:
            return
        self._started = False
        first_exc: BaseException | None = None
        self.application.stop_accepting()
        if self.channels is not None:
            try:
                await self.channels.stop_all()
            except BaseException as exc:  # noqa: BLE001
                first_exc = exc if first_exc is None else first_exc
        # stop_all() sets each channel's stop event, which unblocks the
        # start_all() gather; await the background task for a clean exit.
        if self._channels_task is not None:
            try:
                await self._channels_task
            except BaseException:  # noqa: BLE001
                pass
        if self.cron_service is not None:
            try:
                self.cron_service.stop()
                await self.cron_service.await_stop()
            except BaseException as exc:  # noqa: BLE001
                first_exc = exc if first_exc is None else first_exc
        try:
            await self.outbox_sender.drain(timeout_s=self.shutdown_grace_s)
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        try:
            await self.outbox_sender.stop()
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        try:
            self.connection.close()
        except BaseException as exc:  # noqa: BLE001
            first_exc = exc if first_exc is None else first_exc
        logger.info("control plane resources stopped")
        if first_exc is not None:
            raise first_exc


def build_control_plane_runtime(
    config: Any,
    connection: Any,
    *,
    surface: dict[str, Any] | None = None,
) -> ControlPlaneResources:
    """Construct the Control Plane runtime from a root ``Config``.

    Unlike :func:`build_lightweight_runtime`, this owns ingress, Outbox,
    Channels, Cron, API/WebUI, and the realtime hub but creates no Agent
    and no Worker coroutine (design Task 7 Step 6). Migrations MUST be
    run on ``connection`` by the caller before this is called.
    """
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.cron.service import CronService
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore
    from miniunicorn.runtime.task_service import TaskService
    from miniunicorn.session.manager import SessionManager

    resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
    store = SqliteRuntimeStore(connection)

    realtime = RealtimeSubscriptionHub(capacity=resolved.realtime_event_queue_capacity)
    task_service = TaskService(store)

    # Channels (Task 9): the Control Plane owns the ChannelManager so
    # inbound messages route to the TaskService and outbound delivery
    # flows through real channels via the Outbox.
    sessions = SessionManager(config.workspace_path)
    bus = MessageBus()
    channels = _build_channel_manager(
        config,
        bus,
        sessions,
        None,  # no Agent in the Control Plane
        task_service,
        surface=surface,
    )

    # Wire ChannelManager's send_with_receipt as the Outbox sender.
    outbox_sender = OutboxSender(
        store,
        channels,
        lease_ms=resolved.outbox_lease_timeout_s * 1000,
        send_timeout_s=resolved.channel_send_timeout_s,
    )

    application = RuntimeApplication(
        task_service=task_service,
        result_store=store,
        realtime=realtime,
    )

    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron_service = CronService(cron_store_path)

    # Wire Dream/Cron enqueue callbacks to submit through TaskService
    # (design §22.3, WP7 hard cutover). The Control Plane has no Agent,
    # so the DreamIdleTrigger is not wired here.
    _wire_maintenance_callbacks(
        config=config,
        task_service=task_service,
        cron_service=cron_service,
        agent=None,
    )

    return ControlPlaneResources(
        application=application,
        task_service=task_service,
        outbox_sender=outbox_sender,
        store=store,
        connection=connection,
        realtime=realtime,
        config=resolved,
        shutdown_grace_s=float(resolved.shutdown_grace_s),
        channels=channels,
        cron_service=cron_service,
    )


# ---------------------------------------------------------------------------
# Supervised runtime composition (design Task 8)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SupervisedRuntimeResources:
    """Lifecycle-managed supervised runtime (design Task 8).

    Owns a :class:`SupervisedHost` that spawns one Control Plane process
    and ``worker_count`` Worker processes. ``start()`` blocks until the
    Control Plane and all required Workers report ready; ``stop()``
    gracefully shuts down the entire child tree.
    """

    host: Any  # SupervisedHost (avoid import cycle)
    shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    _stopped: bool = False

    async def start(self) -> None:
        """Start the SupervisedHost and wait for readiness."""
        await self.host.start()

    async def stop(self) -> None:
        """Graceful shutdown (idempotent)."""
        if self._stopped:
            return
        self._stopped = True
        await self.host.stop()
        self.closed.set()

    def request_shutdown(self) -> None:
        """Signal the long-running launcher to shut down."""
        self.shutdown_requested.set()

    async def wait_for_shutdown(self) -> None:
        """Wait until shutdown is requested."""
        await self.shutdown_requested.wait()


def build_supervised_runtime(
    config: Any,
    *,
    surface: dict[str, Any] | None = None,
    bus: Any | None = None,
) -> SupervisedRuntimeResources:
    """Construct the supervised runtime from a root ``Config`` (Task 8).

    Wires the production :func:`control_plane_main` and
    :func:`worker_main` entrypoints with a frozen, picklable
    :class:`ChildBootstrapPayload`. Worker count and min_workers come
    from ``config.runtime`` (default 3). The config is serialised to
    JSON and reconstructed inside each child — no live Config, Provider,
    Agent, Store, MessageBus, callable, or open socket crosses the
    ``spawn`` boundary.
    """
    from miniunicorn.config.runtime import resolve_runtime_paths
    from miniunicorn.runtime.hosts.supervised import SupervisedHost
    from miniunicorn.runtime.process_entrypoints import (
        ChildBootstrapPayload,
        control_plane_main,
        worker_main,
    )

    resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
    worker_count = resolved.worker_count
    child_surface = dict(surface or {})
    child_surface.update(
        mode="supervised",
        worker_count=worker_count,
        database_path=str(resolved.database_path_resolved or resolved.database_path),
    )
    payload = ChildBootstrapPayload(
        config_json=config.model_dump_json(),
        surface=child_surface,
    )

    host = SupervisedHost(
        config=payload,
        control_entrypoint=control_plane_main,
        worker_entrypoint=worker_main,
        bus=bus,
        worker_count=worker_count,
        min_workers=worker_count,
        ready_timeout_s=60.0,
        shutdown_grace_s=resolved.shutdown_grace_s,
    )

    return SupervisedRuntimeResources(host=host)


__all__ = [
    "RuntimeResources",
    "ControlPlaneResources",
    "SupervisedRuntimeResources",
    "Closeable",
    "build_lightweight_runtime",
    "build_control_plane_runtime",
    "build_supervised_runtime",
]
