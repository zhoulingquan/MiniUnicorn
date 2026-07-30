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
from miniunicorn.runtime.application import RuntimeApplication
from miniunicorn.runtime.agent_adapter import AgentExecutionCallback
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
    _stopped: bool = False

    async def start(self) -> None:
        """Start the outbox, host, and begin accepting ingress."""
        await self.outbox_sender.start()
        await self.host.start()
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

    agent = AgentLoop.from_config(
        config,
        bus,
        session_manager=sessions,
        provider=provider_override,
        vector_memory_factory=create_vector_store,
    )

    session_committer = SessionCommitter(store, sessions)
    tool_gateway = ToolGateway(agent.tools, store, store)
    journal = DurableTurnJournalAdapter(store, store)
    realtime = RealtimeSubscriptionHub(
        capacity=resolved.realtime_event_queue_capacity
    )

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
    )

    if channel_sender is not None:
        sender = channel_sender
    elif gateway:
        # Gateway mode constructs ChannelManager in-process (Task 9).
        # Until then, fall back to LocalResultSender.
        sender = LocalResultSender()
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

    return RuntimeResources(
        application=application,
        host=host,
        outbox_sender=outbox_sender,
        store=store,
        connection=connection,
        agent=agent,
        config=resolved,
        shutdown_grace_s=float(resolved.shutdown_grace_s),
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
    _started: bool = False

    async def start(self) -> None:
        """Start the Outbox and begin accepting ingress."""
        await self.outbox_sender.start()
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
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore
    from miniunicorn.runtime.task_service import TaskService

    resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
    store = SqliteRuntimeStore(connection)

    realtime = RealtimeSubscriptionHub(
        capacity=resolved.realtime_event_queue_capacity
    )
    task_service = TaskService(store)

    # Channels/API/WebUI arrive in Task 9; until then the Control Plane
    # uses a LocalResultSender so the Outbox can drain CLI/API receipts.
    sender = LocalResultSender()
    outbox_sender = OutboxSender(
        store,
        sender,
        lease_ms=resolved.outbox_lease_timeout_s * 1000,
        send_timeout_s=resolved.channel_send_timeout_s,
    )

    application = RuntimeApplication(
        task_service=task_service,
        result_store=store,
        realtime=realtime,
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
    payload = ChildBootstrapPayload(
        config_json=config.model_dump_json(),
        surface={
            "mode": "supervised",
            "worker_count": worker_count,
            "database_path": str(resolved.database_path_resolved or resolved.database_path),
        },
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
