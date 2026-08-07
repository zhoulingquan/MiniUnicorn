"""Maintenance ledger mixin for the SQLite Runtime Store (design §33.4, §16.16).

Holds :class:`MaintenanceStoreMixin` covering ``MaintenanceLedger``:
retention batch selection/deletion, unreferenced blob listing/deletion,
WAL checkpoint, and online backup. The mixin shares ``self._conn`` with
the other responsibility mixins via the façade (design §7.3, §11.2,
§33.4; Task 12 Steps 3-5).
"""

from __future__ import annotations

from miniunicorn.runtime.models import (
    RetentionBatch,
    RetentionPolicy,
    RetentionResult,
)


class MaintenanceStoreMixin:
    """Retention, blob GC, WAL checkpoint, and backup operations."""

    def list_retention_batch(
        self, policy: RetentionPolicy, now_ms: int
    ) -> RetentionBatch:
        """Select a bounded batch of terminal rows for retention deletion.

        Implements the deletion order from design §16.16: child rows
        (events, checkpoints, attempts) are deleted before their parent
        task, and delivered outbox rows before their parent task. Only
        terminal tasks older than the policy thresholds are selected.
        """
        success_cutoff = now_ms - policy.successful_task_age_days * 86_400_000
        failure_cutoff = now_ms - policy.failed_task_age_days * 86_400_000
        limit = policy.batch_size

        # Select terminal tasks that have NO non-terminal outbox rows
        # (design §16.16). A task with a pending/undelivered outbox row is
        # not yet retention-eligible because deleting it would violate the
        # outbox -> tasks FK constraint.
        rows = self._conn.execute(
            """
            SELECT task_id FROM tasks
            WHERE state IN ('COMPLETED', 'CANCELLED')
              AND completed_at_ms IS NOT NULL
              AND completed_at_ms < ?
              AND NOT EXISTS (
                  SELECT 1 FROM outbox o
                  WHERE o.task_id = tasks.task_id
                    AND o.state NOT IN ('DELIVERED', 'WAIVED', 'FAILED')
              )
            UNION
            SELECT task_id FROM tasks
            WHERE state = 'FAILED'
              AND completed_at_ms IS NOT NULL
              AND completed_at_ms < ?
              AND NOT EXISTS (
                  SELECT 1 FROM outbox o
                  WHERE o.task_id = tasks.task_id
                    AND o.state NOT IN ('DELIVERED', 'WAIVED', 'FAILED')
              )
            ORDER BY task_id
            LIMIT ?
            """,
            (success_cutoff, failure_cutoff, limit),
        ).fetchall()
        task_ids = tuple(r["task_id"] for r in rows)

        # Delivered or waived outbox rows for those tasks.
        if task_ids:
            outbox_ph = ",".join("?" for _ in task_ids)
            outbox_rows = self._conn.execute(
                f"""
                SELECT outbox_id FROM outbox
                WHERE task_id IN ({outbox_ph})
                AND state IN ('DELIVERED', 'WAIVED', 'FAILED')
                ORDER BY outbox_id
                LIMIT ?
                """,
                (*task_ids, limit),
            ).fetchall()
        else:
            outbox_rows = []
        outbox_ids = tuple(r["outbox_id"] for r in outbox_rows)

        # Unreferenced blobs (no remaining references from tasks,
        # outbox, checkpoints, events, or attempts).
        # tool_attempts has effect_receipt_ref (not a blob id), so it is
        # not included here; tool_calls.result_blob_id is the blob reference
        # for tool results.
        blob_rows = self._conn.execute(
            """
            SELECT b.blob_id FROM runtime_blobs b
            WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM model_attempts m WHERE m.response_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.result_blob_id = b.blob_id
                              OR tc.arguments_blob_id = b.blob_id)
            ORDER BY b.blob_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        blob_ids = tuple(r["blob_id"] for r in blob_rows)

        return RetentionBatch(
            task_ids=task_ids,
            outbox_ids=outbox_ids,
            blob_ids=blob_ids,
        )

    def delete_retention_batch(self, batch: RetentionBatch) -> RetentionResult:
        """Delete a retention batch in FK-safe order (design §16.16).

        Order: child rows first (events, checkpoints, attempts, controls),
        then outbox rows, then terminal tasks, then unreferenced blobs.
        Non-terminal tasks and non-terminal outbox rows are never deleted.
        Foreign-key failure aborts the batch and raises.
        """
        if not batch.task_ids and not batch.outbox_ids and not batch.blob_ids:
            return RetentionResult(
                deleted_tasks=0, deleted_outbox=0, deleted_blobs=0, skipped=0
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            deleted_tasks = 0
            deleted_outbox = 0
            deleted_blobs = 0
            skipped = 0

            # 1. Delete child rows for selected tasks (design §16.16).
            placeholders = ",".join("?" for _ in batch.task_ids)
            if batch.task_ids:
                # Verify all selected tasks are still terminal.
                non_terminal = self._conn.execute(
                    f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) "
                    f"AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                    batch.task_ids,
                ).fetchall()
                if non_terminal:
                    skipped += len(non_terminal)
                    safe_ids = tuple(
                        r["task_id"] for r in self._conn.execute(
                            f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) "
                            f"AND state IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                            batch.task_ids,
                        ).fetchall()
                    )
                    safe_ph = ",".join("?" for _ in safe_ids) if safe_ids else None
                else:
                    safe_ids = batch.task_ids
                    safe_ph = placeholders

                if safe_ids and safe_ph:
                    # Delete child rows in FK order. tool_attempts must be
                    # deleted before tool_calls (FK: tool_attempts -> tool_calls),
                    # and all child rows must be deleted before tasks.
                    self._conn.execute(
                        f"DELETE FROM task_events WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM checkpoints WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM model_attempts WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM tool_attempts WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM tool_calls WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM task_controls WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM session_commits WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    # Delete outbox rows for those tasks.
                    cur = self._conn.execute(
                        f"DELETE FROM outbox WHERE task_id IN ({safe_ph}) "
                        f"AND state IN ('DELIVERED', 'WAIVED', 'FAILED')",
                        safe_ids,
                    )
                    deleted_outbox += cur.rowcount
                    # Clear session slots.
                    self._conn.execute(
                        f"UPDATE session_slots SET active_task_id=NULL "
                        f"WHERE active_task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    # Delete the terminal tasks.
                    cur = self._conn.execute(
                        f"DELETE FROM tasks WHERE task_id IN ({safe_ph}) "
                        f"AND state IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                        safe_ids,
                    )
                    deleted_tasks += cur.rowcount

            # 2. Delete standalone outbox rows (if any not covered above).
            if batch.outbox_ids:
                outbox_ph = ",".join("?" for _ in batch.outbox_ids)
                cur = self._conn.execute(
                    f"DELETE FROM outbox WHERE outbox_id IN ({outbox_ph}) "
                    f"AND state IN ('DELIVERED', 'WAIVED', 'FAILED')",
                    batch.outbox_ids,
                )
                deleted_outbox += cur.rowcount

            # 3. Delete unreferenced blobs (design §16.16 step 5).
            if batch.blob_ids:
                blob_ph = ",".join("?" for _ in batch.blob_ids)
                cur = self._conn.execute(
                    f"DELETE FROM runtime_blobs WHERE blob_id IN ({blob_ph})",
                    batch.blob_ids,
                )
                deleted_blobs += cur.rowcount

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return RetentionResult(
            deleted_tasks=deleted_tasks,
            deleted_outbox=deleted_outbox,
            deleted_blobs=deleted_blobs,
            skipped=skipped,
        )

    def list_unreferenced_blobs(self, limit: int) -> list[str]:
        """List blob IDs with no remaining references (design §16.16)."""
        rows = self._conn.execute(
            """
            SELECT b.blob_id FROM runtime_blobs b
            WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM model_attempts m WHERE m.response_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.result_blob_id = b.blob_id
                              OR tc.arguments_blob_id = b.blob_id)
            ORDER BY b.blob_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [r["blob_id"] for r in rows]

    def delete_unreferenced_blobs(self, blob_ids: list[str]) -> int:
        """Delete blobs by ID. Returns the number of rows deleted."""
        if not blob_ids:
            return 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            ph = ",".join("?" for _ in blob_ids)
            cur = self._conn.execute(
                f"DELETE FROM runtime_blobs WHERE blob_id IN ({ph})",
                blob_ids,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount

    def checkpoint_wal(self) -> None:
        """Checkpoint the WAL during quiet periods (design §33.4)."""
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def backup_to(self, dest_path: str) -> None:
        """Online backup to ``dest_path`` (design §33.4).

        Uses SQLite's online backup API; never copies a WAL file on disk.
        """
        import sqlite3 as _sqlite3

        dest = _sqlite3.connect(dest_path)
        try:
            self._conn.backup(dest)
        finally:
            dest.close()
