"""Lightweight single-process Host (design §9.1, §30 WP3).

Runs in one process with 1–3 Worker coroutines. Uses the same durable
Runtime Store, leases, fencing, checkpoints, session commit protocol,
and recovery paths as the Supervised Host — just without process
isolation.

CLI, tests, and development launchers default to lightweight mode
(design §9.1).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from miniunicorn.runtime.contracts import RuntimeStore
from miniunicorn.runtime.models import (
    InboundTaskEnvelope,
    RequestScope,
)
from miniunicorn.runtime.scheduler import Scheduler
from miniunicorn.runtime.session_committer import SessionCommitter
from miniunicorn.runtime.task_service import TaskService
from miniunicorn.runtime.worker import (
    AgentTaskWorker,
    WorkerExecutionResult,
    WorkerTaskPayload,
)


class LightweightHost:
    """Single-process runtime host (design §9.1).

    Assembles:
    - one Runtime Store connection;
    - a TaskService for durable submission;
    - a Scheduler for claim/renew/reclaim;
    - a SessionCommitter for prepare/apply/confirm;
    - 1–3 AgentTaskWorker coroutines.

    The execution callback is pluggable; WP3 provides a default that
    wraps the existing AgentLoop / TurnDispatcher.
    """

    def __init__(
        self,
        store: RuntimeStore,
        session_committer: SessionCommitter,
        execution_callback: Any,
        *,
        worker_count: int = 1,
        lease_ms: int = 180_000,
        heartbeat_interval_s: float = 15.0,
        max_root_attempts: int = 3,
    ) -> None:
        self._store = store
        self._session_committer = session_committer
        self._execution_callback = execution_callback
        self._worker_count = max(1, min(worker_count, 3))
        self._heartbeat_interval_s = heartbeat_interval_s

        self._task_service = TaskService(store)
        self._scheduler = Scheduler(
            store,
            lease_ms=lease_ms,
            max_root_attempts=max_root_attempts,
        )

        self._workers: list[AgentTaskWorker] = []
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def task_service(self) -> TaskService:
        return self._task_service

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    @property
    def worker_count(self) -> int:
        """Number of Worker coroutines the host was configured with."""
        return self._worker_count

    async def start(self) -> None:
        """Start the host: create and launch Worker coroutines."""
        if self._running:
            return
        self._running = True

        for i in range(self._worker_count):
            worker_id = f"lightweight-{i}"
            worker = AgentTaskWorker(
                worker_id=worker_id,
                scheduler=self._scheduler,
                worker_ledger=self._store,
                session_committer=self._session_committer,
                execution_callback=self._execution_callback,
                heartbeat_interval_s=self._heartbeat_interval_s,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"worker-{worker_id}")
            self._worker_tasks.append(task)

        logger.info("lightweight host started with {} workers", self._worker_count)

    async def stop(self, *, grace_s: float | None = None) -> None:
        """Stop the host: signal Workers to stop and wait for them.

        With ``grace_s`` the host stops accepting new claims, waits up to
        the deadline for in-flight Worker tasks to finish, and only cancels
        survivors after the deadline (design Task 5 Step 3). Without
        ``grace_s`` the Workers are cancelled immediately (legacy
        behavior retained for existing tests).
        """
        if not self._running:
            return
        self._running = False

        for worker in self._workers:
            worker.stop()

        if grace_s is not None and self._worker_tasks:
            # Graceful drain: let in-flight tasks finish before cancelling.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._worker_tasks, return_exceptions=True),
                    timeout=grace_s,
                )
            except asyncio.TimeoutError:
                # Cancel survivors after the grace deadline.
                for task in self._worker_tasks:
                    task.cancel()
                await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        else:
            # Cancel worker tasks (they may be sleeping in the poll loop).
            for task in self._worker_tasks:
                task.cancel()

            if self._worker_tasks:
                await asyncio.gather(*self._worker_tasks, return_exceptions=True)

        self._worker_tasks.clear()
        self._workers.clear()
        logger.info("lightweight host stopped")

    # ------------------------------------------------------------------
    # Submit-and-await compatibility (design §11.3, WP3 task 6)
    # ------------------------------------------------------------------

    async def submit_and_wait(
        self,
        envelope: InboundTaskEnvelope,
        timeout_s: float | None = None,
    ) -> Any:
        """Submit a task and wait for it to reach a terminal state."""
        handle = await self._task_service.submit(envelope)
        scope = envelope.scope
        return await self._task_service.wait_terminal(scope, handle.task_id, timeout_s)


__all__ = ["LightweightHost"]
