"""Supervised multi-process Host (design §9.2, §23, §24, WP6).

The :class:`SupervisedHost` is the gateway-side orchestrator that owns a
:class:`~miniunicorn.runtime.supervisor.Supervisor` and bridges the
supervised child processes to the host process:

- It spawns one Control Plane process and N Worker processes through the
  Supervisor (design §9.2, §24.6).
- Workers claim directly from the shared SQLite Runtime Store (design
  §23.2, WP6 task 3). The Control Plane does not dispatch work — it only
  fans out wake hints after submit and runs the Outbox sender.
- The host runs a background :class:`RealtimeEventBridge` that reads
  typed Agent events from every Worker's IPC pipe and publishes them to
  the in-process :class:`~miniunicorn.bus.queue.MessageBus` for realtime
  UX (design §23.1, §23.2, WP6 task 4).
- :meth:`notify_submit` fans out a wake hint after a durable task is
  submitted so Workers poll immediately instead of waiting for their
  exponential idle backoff (design §23.2).
- :meth:`shutdown` coordinates graceful shutdown across the Supervisor
  and the realtime bridge (design §24.7, WP6 task 7).

The actual Control Plane and Worker entrypoints (which rebuild
process-local SQLite/Provider/Agent objects) are pluggable so the
gateway launcher can wire production entrypoints and tests can wire
minimal stubs that exercise spawn/restart/shutdown semantics without a
full Agent Core.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger

from miniunicorn.runtime.supervisor import (
    ChildEntrypoint,
    RestartPolicy,
    Supervisor,
)


# ---------------------------------------------------------------------------
# Realtime event bridge
# ---------------------------------------------------------------------------


class RealtimeEventBridge:
    """Thin façade over the Supervisor's Worker→Control Plane relay.

    Design Task 7 Step 3: the Supervisor is the only parent-side pipe
    reader. It owns a bounded relay queue and a lifecycle-owned relay
    thread that forwards Worker ``agent_event`` / ``task_progress``
    envelopes unchanged to the Control Plane child, which publishes them
    to its in-process :class:`RealtimeSubscriptionHub`.

    This class no longer polls IPC pipes — that would make it a second
    caller of ``ProcessIpcChannel.parent_recv()``. It exists only to
    preserve the SupervisedHost lifecycle/snapshot API and to surface the
    Supervisor's ``relay_dropped_events`` counter.
    """

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        bus: Any | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        self._supervisor = supervisor
        self._bus = bus
        self._running = False

    @property
    def dropped_events(self) -> int:
        """Delegate to the Supervisor's relay drop counter (Task 7 Step 3)."""
        return self._supervisor.relay_dropped_events

    def start(self) -> None:
        # The relay thread is owned and started by the Supervisor after the
        # Control Plane becomes ready; nothing to do here.
        self._running = True

    async def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# SupervisedHost
# ---------------------------------------------------------------------------


class SupervisedHost:
    """Gateway-side orchestrator for supervised mode (design §9.2, WP6).

    Owns:

    - a :class:`Supervisor` that spawns the Control Plane and Workers;
    - a :class:`RealtimeEventBridge` that forwards Worker events to the
      host :class:`MessageBus`;
    - the wake-hint fan-out entry point (:meth:`notify_submit`) called by
      :class:`~miniunicorn.runtime.task_service.TaskService` after a
      durable submit.

    The actual Control Plane / Worker entrypoints are provided by the
    caller (gateway launcher or test). They run in spawned child
    processes and rebuild all process-local dependencies from ``config``
    (design §24.6, WP6 task 2).
    """

    def __init__(
        self,
        *,
        config: Any,
        control_entrypoint: ChildEntrypoint,
        worker_entrypoint: ChildEntrypoint,
        bus: Any | None = None,
        worker_count: int = 2,
        min_workers: int = 1,
        ready_timeout_s: float = 30.0,
        shutdown_grace_s: int = 60,
        restart_policy: RestartPolicy | None = None,
        bridge_poll_interval_s: float = 0.05,
    ) -> None:
        self._supervisor = Supervisor(
            config=config,
            control_entrypoint=control_entrypoint,
            worker_entrypoint=worker_entrypoint,
            worker_count=worker_count,
            min_workers=min_workers,
            ready_timeout_s=ready_timeout_s,
            shutdown_grace_s=shutdown_grace_s,
            restart_policy=restart_policy,
        )
        self._bus = bus
        self._bridge = RealtimeEventBridge(
            self._supervisor,
            bus=bus,
            poll_interval_s=bridge_poll_interval_s,
        )
        self._maint_task: asyncio.Task[None] | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def supervisor(self) -> Supervisor:
        return self._supervisor

    @property
    def bridge(self) -> RealtimeEventBridge:
        return self._bridge

    def is_ready(self) -> bool:
        return self._supervisor.is_ready()

    def ready_workers(self) -> int:
        return self._supervisor.ready_workers()

    def snapshot(self) -> dict[str, Any]:
        snap = self._supervisor.snapshot()
        snap["bridge_dropped_events"] = self._bridge.dropped_events
        return snap

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Supervisor and the realtime event bridge."""
        if self._running:
            return
        self._running = True
        # Spawn children (blocking: waits up to ready_timeout_s).
        self._supervisor.start()
        # Start the realtime bridge in this process.
        self._bridge.start()
        # Start a maintenance coroutine that periodically reaps children
        # and restarts them per the RestartPolicy.
        self._maint_task = asyncio.create_task(
            self._maintenance_loop(), name="supervised-host-maintenance"
        )
        logger.info(
            "SupervisedHost started (ready={}, ready_workers={})",
            self.is_ready(),
            self.ready_workers(),
        )

    async def stop(self, *, grace_s: int | None = None) -> None:
        """Graceful shutdown: stop maintenance, stop bridge, shut down Supervisor."""
        if not self._running:
            return
        self._running = False
        if self._maint_task is not None:
            self._maint_task.cancel()
            try:
                await self._maint_task
            except asyncio.CancelledError:
                pass
            self._maint_task = None
        await self._bridge.stop()
        # Run the (synchronous) Supervisor shutdown in a thread so we do
        # not block the event loop while children drain.
        await asyncio.to_thread(self._supervisor.shutdown, grace_s=grace_s)
        logger.info("SupervisedHost stopped")

    def terminate(self) -> None:
        """Immediate containment close (crash-mode)."""
        self._supervisor.terminate()

    # ------------------------------------------------------------------
    # Wake hints
    # ------------------------------------------------------------------

    def notify_submit(self, *, task_id: str | None = None) -> int:
        """Fan out a wake hint after a durable submit (design §23.2).

        Returns the number of workers notified. Best-effort: a missed
        hint just means the Worker polls slightly later.
        """
        return self._supervisor.fan_out_wake(reason="submit", task_id=task_id)

    def notify_control(self, *, task_id: str | None = None) -> int:
        """Fan out a wake hint after a control is appended (design §23.2)."""
        return self._supervisor.fan_out_wake(reason="control", task_id=task_id)

    # ------------------------------------------------------------------
    # Maintenance loop
    # ------------------------------------------------------------------

    async def _maintenance_loop(self) -> None:
        """Periodically reap exited children and restart them per policy."""
        try:
            while self._running:
                # Reap+restart is synchronous (joins processes); run it in
                # a thread to avoid blocking the loop.
                await asyncio.to_thread(self._supervisor.reap, timeout_s=0.0)
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return


__all__ = ["SupervisedHost", "RealtimeEventBridge"]
