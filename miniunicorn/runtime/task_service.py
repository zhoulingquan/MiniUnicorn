"""Durable Task Service — the only normal ingress for durable work (design §8.1, §11.3).

The Task Service normalizes Channel, CLI, SDK, cron, and maintenance
requests into durable task envelopes, validates scope, deduplicates,
allocates session sequences, and creates tasks in the Runtime Store.

It does NOT execute Agent turns, own an in-memory pending-work queue,
select a Worker, publish final replies, or edit session files directly
(design §8.1).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from miniunicorn.runtime.contracts import (
    ControlResult,
    TaskHandle,
    TaskIngressStore,
)
from miniunicorn.runtime.models import (
    InboundTaskEnvelope,
    InternalTaskEnvelope,
    RequestScope,
    TaskControlRequest,
    TaskSnapshot,
)

if TYPE_CHECKING:
    from miniunicorn.runtime.contracts import RuntimeStore


TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class TaskService:
    """Durable task ingress service (design §8.1, §11.3).

    Wraps :class:`TaskIngressStore` with async-friendly methods and
    adds ``wait_terminal`` for submit-and-await compatibility (WP3 task 6).
    """

    def __init__(self, store: TaskIngressStore | RuntimeStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(self, envelope: InboundTaskEnvelope) -> TaskHandle:
        """Submit a user task durably (design §17.1).

        Returns a :class:`TaskHandle` with the task id and initial snapshot.
        ``status`` is ``ACCEPTED`` for new tasks or ``DUPLICATE`` for
        successful dedup hits (the original ``task_id`` is returned).
        """
        result = self._store.submit_task(envelope)
        if result.status not in ("ACCEPTED", "DUPLICATE"):
            raise RuntimeError(f"unexpected submit status: {result.status}")

        snapshot = self._store.read_task_snapshot(envelope.scope, result.task_id)
        if snapshot is None:
            raise RuntimeError(f"task {result.task_id} disappeared immediately after submit")
        return TaskHandle(task_id=result.task_id, snapshot=snapshot)

    async def submit_internal(self, envelope: InternalTaskEnvelope) -> TaskHandle:
        """Submit an internal (non-user) task durably (design §8.1)."""
        result = self._store.submit_internal(envelope)
        if result.status not in ("ACCEPTED", "DUPLICATE"):
            raise RuntimeError(f"unexpected submit status: {result.status}")

        snapshot = self._store.read_task_snapshot(envelope.scope, result.task_id)
        if snapshot is None:
            raise RuntimeError(
                f"internal task {result.task_id} disappeared immediately after submit"
            )
        return TaskHandle(task_id=result.task_id, snapshot=snapshot)

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def control(self, request: TaskControlRequest) -> ControlResult:
        """Append a control request to a task (design §17.12)."""
        return self._store.append_control(request)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self, scope: RequestScope, task_id: str) -> TaskSnapshot:
        """Read the current snapshot of a task."""
        snapshot = self._store.read_task_snapshot(scope, task_id)
        if snapshot is None:
            raise KeyError(f"task not found: {task_id}")
        return snapshot

    async def wait_terminal(
        self,
        scope: RequestScope,
        task_id: str,
        timeout_s: float | None = None,
        *,
        poll_interval_s: float = 0.1,
    ) -> TaskSnapshot:
        """Wait until ``task_id`` reaches a terminal state (design §11.3).

        Uses process-local polling to reduce latency. Always rereads the
        Runtime Store and remains correct if a notification is lost or
        the waiting process restarts.
        """
        deadline = time.monotonic() + timeout_s if timeout_s else None
        while True:
            snapshot = self._store.read_task_snapshot(scope, task_id)
            if snapshot is None:
                raise KeyError(f"task not found: {task_id}")
            if snapshot.state in TERMINAL_STATES:
                return snapshot
            if deadline and time.monotonic() >= deadline:
                return snapshot
            await asyncio.sleep(poll_interval_s)


__all__ = ["TaskService"]
