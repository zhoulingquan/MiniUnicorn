"""Deterministic test helpers for supervised Worker fault injection (Task 6).

These helpers resolve the real lease-owning Worker from the durable
``TaskRecord.leased_by`` field and terminate that exact Worker process,
so recovery tests cannot accidentally kill an unrelated Worker.

Design references: §24.4 (supervised children), §6.10/§6.11 (lease fencing),
Task 6 Steps 3-4.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from miniunicorn.runtime.supervisor import Supervisor


async def wait_for_task_owner(
    store: Any,
    task_id: str,
    *,
    timeout_s: float,
) -> tuple[Any, str]:
    """Poll the durable store until ``task_id`` is RUNNING with a non-empty lease.

    Returns ``(task_record, leased_by)`` where ``leased_by`` is the exact
    Worker id that holds the lease at the moment of resolution. Raises
    :class:`AssertionError` when the lease owner is not resolved within
    ``timeout_s`` seconds (e.g. the task stayed QUEUED).

    The returned owner is the only Worker that may be safely killed to
    exercise lease-expiry recovery — killing any other Worker does not
    prove the task was in-flight on the terminated process (Task 6 Step 4).
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = store.read_task(task_id)
        if last is not None and last.state == "RUNNING" and last.leased_by:
            return last, last.leased_by
        await asyncio.sleep(0.05)
    raise AssertionError(f"lease owner was not resolved for {task_id}; last={last!r}")


def terminate_worker(
    supervisor: "Supervisor",
    worker_id: str,
    *,
    timeout_s: float = 2.0,
) -> int:
    """Terminate the named supervised Worker process and return its pid.

    Raises :class:`AssertionError` when ``worker_id`` is not a live
    supervised Worker (unknown id, wrong role, or already-dead process).
    The caller must have resolved ``worker_id`` from
    :func:`wait_for_task_owner` so the terminated process is the real
    lease owner (Task 6 Step 4).
    """
    record = supervisor.child_record(worker_id)
    assert record is not None and record.role == "worker" and record.process is not None, (
        f"no live supervised Worker named {worker_id}"
    )
    assert record.process.is_alive(), f"no live supervised Worker named {worker_id}"
    pid = record.process.pid
    record.process.terminate()
    record.process.join(timeout_s)
    if record.process.is_alive():
        record.process.kill()
        record.process.join(timeout_s)
    return pid
