"""Durable maintenance enqueue and retention (design §22, §33.4, WP7).

This module is the single entry point for enqueuing background maintenance
work as durable internal tasks. Existing triggers (Cron, Dream idle
trigger, consolidation trigger, auto-compaction trigger, index trigger)
call :func:`enqueue_maintenance` instead of ``asyncio.create_task`` so
that required work survives process restarts (design §22.3, §29.16).

Priority bands (design §22.4):

- User turn: 100
- Memory consolidation and index: 50
- Reflection and Dream: 20
- Retention, backup, and cleanup: 10

The one-task maintenance quota (design §22.4, §25.2) is enforced through
the ``resource_leases`` table with ``holder_kind="MAINTENANCE"`` and
``resource_key="global:maintenance"`` (capacity 1). The Scheduler's claim
gate prevents maintenance claims while eligible user work is queued
(design §22.4, §15.2).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Callable

from miniunicorn.runtime.models import (
    InternalTaskEnvelope,
    RequestScope,
    RetentionPolicy,
    RetentionResult,
)

if TYPE_CHECKING:
    from miniunicorn.runtime.contracts import RuntimeStore
    from miniunicorn.runtime.task_service import TaskService


# ---------------------------------------------------------------------------
# Priority bands (design §22.4)
# ---------------------------------------------------------------------------

PRIORITY_USER_TURN = 100
PRIORITY_MEMORY = 50
PRIORITY_REFLECTION_DREAM = 20
PRIORITY_RETENTION_CLEANUP = 10

# Task kinds that are maintenance (non-user) work.
MAINTENANCE_TASK_KINDS = frozenset(
    {"MEMORY_CONSOLIDATION", "MEMORY_INDEX", "REFLECTION", "DREAM", "MAINTENANCE"}
)

# Maintenance task kinds that are low-priority and subject to the
# one-task global quota (design §22.4, §25.2).
LOW_PRIORITY_MAINTENANCE_KINDS = frozenset(
    {"REFLECTION", "DREAM", "MAINTENANCE"}
)

# The resource key for the global maintenance quota.
MAINTENANCE_RESOURCE_KEY = "global:maintenance"
MAINTENANCE_HOLDER_KIND = "MAINTENANCE"
MAINTENANCE_CAPACITY = 1


# ---------------------------------------------------------------------------
# Dedup key builders (design §13.1)
# ---------------------------------------------------------------------------


def dedup_key_for_dream(*, source_revision: str) -> str:
    """Build a dedup key for a Dream task (design §13.1).

    The key encodes the dream source revision (e.g. memory cursor or
    history position) so that repeated submission of the same occurrence
    deduplicates, while a later legitimate occurrence uses a different
    key.
    """
    return f"dream:{source_revision}"


def dedup_key_for_consolidation(*, source_revision: str) -> str:
    """Build a dedup key for a memory consolidation task (design §13.1)."""
    return f"consolidation:{source_revision}"


def dedup_key_for_index(*, source_revision: str) -> str:
    """Build a dedup key for a memory index task (design §13.1)."""
    return f"index:{source_revision}"


def dedup_key_for_reflection(*, source_revision: str) -> str:
    """Build a dedup key for a reflection task (design §13.1)."""
    return f"reflection:{source_revision}"


def dedup_key_for_retention(*, window_id: str) -> str:
    """Build a dedup key for a retention cleanup task (design §13.1).

    ``window_id`` should identify the cleanup window (e.g. the hour or
    day) so repeated submissions within the same window deduplicate.
    """
    return f"retention:{window_id}"


def dedup_key_for_backup(*, backup_id: str) -> str:
    """Build a dedup key for a backup task (design §13.1)."""
    return f"backup:{backup_id}"


def dedup_key_for_wal_checkpoint(*, window_id: str) -> str:
    """Build a dedup key for a WAL checkpoint task (design §13.1)."""
    return f"wal_checkpoint:{window_id}"


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------


def _build_internal_envelope(
    *,
    task_kind: str,
    scope: RequestScope,
    dedup_key: str,
    priority: int,
    payload: dict | None = None,
    session_key: str | None = None,
    available_at_ms: int | None = None,
) -> InternalTaskEnvelope:
    """Build an :class:`InternalTaskEnvelope` with a deterministic payload hash.

    Task 9 Step 3: store the serialized payload bytes inline so the Worker
    can recover them via ``read_blob_content`` without a separate artifact
    store (design §13.1, §16.15). Previously the bytes were discarded
    after hashing, leaving the blob with only an ``external_ref`` that
    ``read_blob_content`` cannot resolve.
    """
    payload_bytes = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    return InternalTaskEnvelope(
        protocol_version=1,
        task_kind=task_kind,  # type: ignore[arg-type]
        priority=priority,
        scope=scope,
        session_key=session_key or f"system:{task_kind.lower()}",
        dedup_key=dedup_key,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        received_at_ms=int(time.time() * 1000),
        available_at_ms=available_at_ms,
        payload_content=payload_bytes,
    )


async def enqueue_maintenance(
    task_service: "TaskService",
    *,
    task_kind: str,
    scope: RequestScope,
    dedup_key: str,
    priority: int,
    payload: dict | None = None,
    session_key: str | None = None,
    available_at_ms: int | None = None,
) -> str:
    """Enqueue a durable internal maintenance task (design §22.3).

    Returns the ``task_id``. Duplicate submission (same
    ``(scope, task_kind, dedup_key)``) returns the original task id
    without allocating a new session sequence (design §13.1).
    """
    envelope = _build_internal_envelope(
        task_kind=task_kind,
        scope=scope,
        dedup_key=dedup_key,
        priority=priority,
        payload=payload,
        session_key=session_key,
        available_at_ms=available_at_ms,
    )
    handle = await task_service.submit_internal(envelope)
    return handle.task_id


def make_maintenance_enqueue_callback(
    task_service: "TaskService",
    scope: RequestScope,
) -> Callable[..., Any]:
    """Create a maintenance-enqueue callback for AgentLoop (Task 13 Step 2).

    Returns an ``async def(*, task_kind, payload=None) -> str`` that
    wraps :func:`enqueue_maintenance` with a deterministic dedup key and
    priority. This lets the ``/dream`` command submit a durable DREAM
    task through TaskService instead of calling ``loop.dream.run()``
    in an untracked coroutine.
    """

    async def _enqueue(
        *,
        task_kind: str,
        payload: dict | None = None,
    ) -> str:
        import time as _time

        priority = (
            PRIORITY_REFLECTION_DREAM
            if task_kind == "DREAM"
            else PRIORITY_RETENTION_CLEANUP
        )
        dedup_key = f"cmd:{task_kind}:{int(_time.time() * 1000)}"
        return await enqueue_maintenance(
            task_service,
            task_kind=task_kind,
            scope=scope,
            dedup_key=dedup_key,
            priority=priority,
            payload=payload or {},
        )

    return _enqueue


# ---------------------------------------------------------------------------
# Retention execution (design §33.4, §16.16)
# ---------------------------------------------------------------------------


def run_retention_batch(
    store: "RuntimeStore",
    *,
    policy: RetentionPolicy | None = None,
    now_ms: int | None = None,
) -> RetentionResult:
    """Select and delete one retention batch (design §33.4, §16.16).

    This is a synchronous store operation. The caller (a maintenance
    Worker) wraps it in a durable ``MAINTENANCE`` task so that a crash
    during retention is recoverable.
    """
    policy = policy or RetentionPolicy()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    batch = store.list_retention_batch(policy, now)
    return store.delete_retention_batch(batch)


def run_blob_gc(store: "RuntimeStore", *, limit: int = 100) -> int:
    """Delete unreferenced blobs (design §16.16 step 5)."""
    blob_ids = store.list_unreferenced_blobs(limit=limit)
    return store.delete_unreferenced_blobs(blob_ids)


def run_wal_checkpoint(store: "RuntimeStore") -> None:
    """Checkpoint the WAL during quiet periods (design §33.4)."""
    store.checkpoint_wal()


def run_backup(store: "RuntimeStore", *, dest_path: str) -> None:
    """Online backup to ``dest_path`` (design §33.4)."""
    store.backup_to(dest_path)


# ---------------------------------------------------------------------------
# Maintenance claim gate (design §22.4, §15.2)
# ---------------------------------------------------------------------------


def user_work_is_queued(store: "RuntimeStore", *, now_ms: int) -> bool:
    """Check if any eligible user-turn task is waiting to be claimed.

    The Scheduler uses this to prevent maintenance claims while user
    work is queued (design §22.4: "Maintenance does not claim while an
    eligible user task waits").
    """
    row = store._conn.execute(  # type: ignore[attr-defined]
        """
        SELECT 1 FROM tasks
        WHERE state = 'QUEUED'
          AND available_at_ms <= ?
          AND priority >= ?
          AND NOT EXISTS (
              SELECT 1 FROM tasks AS earlier
              WHERE earlier.session_key = tasks.session_key
                AND earlier.session_sequence < tasks.session_sequence
                AND earlier.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
          )
        LIMIT 1
        """,
        (now_ms, PRIORITY_USER_TURN),
    ).fetchone()
    return row is not None


def is_maintenance_task_kind(task_kind: str) -> bool:
    """Return True if ``task_kind`` is a maintenance (non-user) kind."""
    return task_kind in MAINTENANCE_TASK_KINDS


def is_low_priority_maintenance(task_kind: str, priority: int) -> bool:
    """Return True if this task is subject to the one-task maintenance quota.

    A task is low-priority maintenance if:
    - it is a maintenance kind (not USER_TURN), AND
    - either its kind is in :data:`LOW_PRIORITY_MAINTENANCE_KINDS` or its
      priority is below the user-turn band (design §22.4).

    USER_TURN tasks are never maintenance regardless of priority.
    """
    if task_kind == "USER_TURN":
        return False
    if not is_maintenance_task_kind(task_kind):
        return False
    return task_kind in LOW_PRIORITY_MAINTENANCE_KINDS or priority < PRIORITY_USER_TURN


__all__ = [
    # Priorities
    "PRIORITY_USER_TURN",
    "PRIORITY_MEMORY",
    "PRIORITY_REFLECTION_DREAM",
    "PRIORITY_RETENTION_CLEANUP",
    # Constants
    "MAINTENANCE_TASK_KINDS",
    "LOW_PRIORITY_MAINTENANCE_KINDS",
    "MAINTENANCE_RESOURCE_KEY",
    "MAINTENANCE_HOLDER_KIND",
    "MAINTENANCE_CAPACITY",
    # Dedup key builders
    "dedup_key_for_dream",
    "dedup_key_for_consolidation",
    "dedup_key_for_index",
    "dedup_key_for_reflection",
    "dedup_key_for_retention",
    "dedup_key_for_backup",
    "dedup_key_for_wal_checkpoint",
    # Enqueue
    "enqueue_maintenance",
    # Retention
    "run_retention_batch",
    "run_blob_gc",
    "run_wal_checkpoint",
    "run_backup",
    # Claim gate
    "user_work_is_queued",
    "is_maintenance_task_kind",
    "is_low_priority_maintenance",
]
