"""Maintenance task executor: dispatches durable maintenance tasks (WP7).

When a Worker claims a maintenance task (``DREAM``, ``MEMORY_CONSOLIDATION``,
``MEMORY_INDEX``, ``REFLECTION``, or ``MAINTENANCE``) from the Runtime Store,
the Host calls :class:`MaintenanceExecutor` to execute it.

The executor is a thin dispatch layer: it receives the task kind and payload,
calls the appropriate Agent-layer executor (Dream, Consolidator, retention
functions), and returns a result. It does not own the task lifecycle — the
Worker handles claim, completion, and fencing.

Design references:

- §22.3: durable maintenance tasks
- §22.4: priority bands
- §29.16: trigger integration
- §33.4: retention, backup, WAL checkpoint
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from miniunicorn.runtime.maintenance import (
    run_backup,
    run_blob_gc,
    run_retention_batch,
    run_wal_checkpoint,
)

if TYPE_CHECKING:
    from miniunicorn.runtime.contracts import RuntimeStore


@dataclass(frozen=True)
class MaintenanceExecutionResult:
    """Result of executing a maintenance task."""

    success: bool
    detail: str = ""
    items_processed: int = 0


class MaintenanceExecutor:
    """Dispatches durable maintenance tasks to their executors (design §22.3).

    The executor is constructed with optional callbacks for each maintenance
    task kind. A callback that is ``None`` means that task kind is not
    supported in this configuration (the task will fail with a clear error).

    Usage::

        executor = MaintenanceExecutor(
            store=store,
            dream_runner=lambda: dream.run(),
            consolidation_runner=lambda key: consolidator.compact_idle_session(key),
            retention_runner=lambda: run_retention_batch(store),
        )
        result = await executor.execute(task_kind="DREAM", payload={...})
    """

    def __init__(
        self,
        store: "RuntimeStore",
        *,
        dream_runner: Callable[[], Coroutine[Any, Any, bool]] | None = None,
        consolidation_runner: Callable[[str], Coroutine[Any, Any, str | None]] | None = None,
        index_runner: Callable[[dict], Coroutine[Any, Any, int]] | None = None,
        reflection_runner: Callable[[dict], Coroutine[Any, Any, bool]] | None = None,
        retention_runner: Callable[[], MaintenanceExecutionResult] | None = None,
        blob_gc_runner: Callable[[], int] | None = None,
        wal_checkpoint_runner: Callable[[], None] | None = None,
        backup_runner: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._dream_runner = dream_runner
        self._consolidation_runner = consolidation_runner
        self._index_runner = index_runner
        self._reflection_runner = reflection_runner
        self._retention_runner = retention_runner
        self._blob_gc_runner = blob_gc_runner
        self._wal_checkpoint_runner = wal_checkpoint_runner
        self._backup_runner = backup_runner

    async def execute(
        self,
        *,
        task_kind: str,
        payload: dict | None = None,
    ) -> MaintenanceExecutionResult:
        """Execute a maintenance task by kind.

        Returns a :class:`MaintenanceExecutionResult`. Exceptions are caught
        and returned as ``success=False`` so the Worker can record the
        failure durably.
        """
        payload = payload or {}
        try:
            if task_kind == "DREAM":
                return await self._execute_dream(payload)
            elif task_kind == "MEMORY_CONSOLIDATION":
                return await self._execute_consolidation(payload)
            elif task_kind == "MEMORY_INDEX":
                return await self._execute_index(payload)
            elif task_kind == "REFLECTION":
                return await self._execute_reflection(payload)
            elif task_kind == "MAINTENANCE":
                return await self._execute_maintenance(payload)
            else:
                return MaintenanceExecutionResult(
                    success=False,
                    detail=f"unsupported maintenance task kind: {task_kind}",
                )
        except Exception as e:
            logger.exception("MaintenanceExecutor: {} failed", task_kind)
            return MaintenanceExecutionResult(success=False, detail=str(e))

    async def _execute_dream(self, payload: dict) -> MaintenanceExecutionResult:
        """Execute a DREAM task (design §22.3)."""
        if self._dream_runner is None:
            return MaintenanceExecutionResult(
                success=False, detail="dream runner not configured"
            )
        did_work = await self._dream_runner()
        return MaintenanceExecutionResult(
            success=True,
            detail="dream completed",
            items_processed=1 if did_work else 0,
        )

    async def _execute_consolidation(self, payload: dict) -> MaintenanceExecutionResult:
        """Execute a MEMORY_CONSOLIDATION task (design §22.3)."""
        if self._consolidation_runner is None:
            return MaintenanceExecutionResult(
                success=False, detail="consolidation runner not configured"
            )
        session_key = payload.get("session_key", "")
        summary = await self._consolidation_runner(session_key)
        return MaintenanceExecutionResult(
            success=True,
            detail="consolidation completed",
            items_processed=1 if summary else 0,
        )

    async def _execute_index(self, payload: dict) -> MaintenanceExecutionResult:
        """Execute a MEMORY_INDEX task (design §22.2, §22.3)."""
        if self._index_runner is None:
            return MaintenanceExecutionResult(
                success=False, detail="index runner not configured"
            )
        count = await self._index_runner(payload)
        return MaintenanceExecutionResult(
            success=True,
            detail="index completed",
            items_processed=count,
        )

    async def _execute_reflection(self, payload: dict) -> MaintenanceExecutionResult:
        """Execute a REFLECTION task (design §22.3)."""
        if self._reflection_runner is None:
            return MaintenanceExecutionResult(
                success=False, detail="reflection runner not configured"
            )
        did_work = await self._reflection_runner(payload)
        return MaintenanceExecutionResult(
            success=True,
            detail="reflection completed",
            items_processed=1 if did_work else 0,
        )

    async def _execute_maintenance(self, payload: dict) -> MaintenanceExecutionResult:
        """Execute a MAINTENANCE task (retention, blob GC, WAL, backup).

        The ``payload["op"]`` field selects the sub-operation:
        - ``retention``: delete one retention batch (design §33.4)
        - ``blob_gc``: delete unreferenced blobs (design §16.16)
        - ``wal_checkpoint``: checkpoint the WAL (design §33.4)
        - ``backup``: online backup to ``payload["dest_path"]`` (design §33.4)
        """
        op = payload.get("op", "retention")

        if op == "retention":
            if self._retention_runner is not None:
                return self._retention_runner()
            result = run_retention_batch(self._store)
            return MaintenanceExecutionResult(
                success=True,
                detail=f"retention: deleted {result.deleted_tasks} tasks, "
                f"{result.deleted_outbox} outbox rows",
                items_processed=result.deleted_tasks + result.deleted_outbox,
            )

        elif op == "blob_gc":
            if self._blob_gc_runner is not None:
                deleted = self._blob_gc_runner()
            else:
                deleted = run_blob_gc(self._store)
            return MaintenanceExecutionResult(
                success=True,
                detail=f"blob_gc: deleted {deleted} blobs",
                items_processed=deleted,
            )

        elif op == "wal_checkpoint":
            if self._wal_checkpoint_runner is not None:
                self._wal_checkpoint_runner()
            else:
                run_wal_checkpoint(self._store)
            return MaintenanceExecutionResult(
                success=True,
                detail="wal_checkpoint: done",
            )

        elif op == "backup":
            dest_path = payload.get("dest_path", "")
            if not dest_path:
                return MaintenanceExecutionResult(
                    success=False, detail="backup: dest_path is required"
                )
            if self._backup_runner is not None:
                self._backup_runner(dest_path)
            else:
                run_backup(self._store, dest_path=dest_path)
            return MaintenanceExecutionResult(
                success=True,
                detail=f"backup: wrote to {dest_path}",
                items_processed=1,
            )

        else:
            return MaintenanceExecutionResult(
                success=False,
                detail=f"unknown maintenance op: {op}",
            )


__all__ = [
    "MaintenanceExecutionResult",
    "MaintenanceExecutor",
]
