"""Shared primitives for the SQLite Runtime Store mixins (design §7.3, §16).

This module holds the module-level ID/time/hash helpers, the row mappers
shared by multiple responsibility mixins, and the :class:`SqliteStoreBase`
base class that owns the connection, the task-scope predicate, the durable
event log, and the internal task-transition / lease-validation helpers.

Every responsibility mixin imports the helpers it needs from this module;
no mixin imports another mixin (design §7.3, Task 12 Steps 3-5).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from typing import Any

from miniunicorn.agent.ports import SafeError
from miniunicorn.runtime.contracts import StaleLeaseError, TaskClaim
from miniunicorn.runtime.models import (
    DurableEventRecord,
    RequestScope,
    TaskControlRecord,
    TaskRecord,
    TaskSnapshot,
    is_allowed_transition,
)

# ---------------------------------------------------------------------------
# ID and time helpers (design §12.1, §12.2)
# ---------------------------------------------------------------------------


def _new_uuid() -> str:
    """Generate a UUID (design §12.1).

    Uses UUIDv4 for simplicity; UUIDv7 is recommended by the design but
    not required for correctness (design §12.1: "Correctness must not
    depend on UUID ordering").
    """
    return str(uuid.uuid4())


def _new_lease_token() -> str:
    """Cryptographically random 128-bit lease token encoded as hex (design §12.1)."""
    return secrets.token_hex(16)


def _now_ms() -> int:
    """Current UTC Unix milliseconds (design §12.2)."""
    import time

    return int(time.time() * 1000)


def _dedup_key_hash(parts: str) -> str:
    """SHA-256 hex of a dedup-key string (design §16.13)."""
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _content_hash(data: bytes | str) -> str:
    """SHA-256 hex of content (design §16.15)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _column_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    """Return ``row[name]`` if present, otherwise ``default``.

    The Runtime Store validates schema version before any query, so
    migration-added columns always exist on a migrated connection. The
    helper exists for ``_row_to_task_record`` so a row produced by an
    explicit ``SELECT`` that predates a migration column does not raise
    ``IndexError`` — it falls back to the column default instead.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_task_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        turn_id=row["turn_id"],
        protocol_version=row["protocol_version"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        agent_id=row["agent_id"],
        workspace_id=row["workspace_id"],
        session_key=row["session_key"],
        session_sequence=row["session_sequence"],
        channel=row["channel"],
        channel_account=row["channel_account"],
        channel_message_id=row["channel_message_id"],
        dedup_key=row["dedup_key"],
        target_key=_column_value(row, "target_key", ""),
        task_kind=row["task_kind"],
        priority=row["priority"],
        payload_blob_id=row["payload_blob_id"],
        payload_hash=row["payload_hash"],
        state=row["state"],
        checkpoint_phase=row["checkpoint_phase"],
        run_segment=row["run_segment"],
        root_attempt_count=row["root_attempt_count"],
        max_root_attempts=row["max_root_attempts"],
        recovery_pending=row["recovery_pending"],
        leased_by=row["leased_by"],
        lease_token=row["lease_token"],
        lease_epoch=row["lease_epoch"],
        lease_until_ms=row["lease_until_ms"],
        last_heartbeat_at_ms=row["last_heartbeat_at_ms"],
        last_progress_at_ms=row["last_progress_at_ms"],
        available_at_ms=row["available_at_ms"],
        state_version=row["state_version"],
        control_cursor=row["control_cursor"],
        cumulative_input_tokens=row["cumulative_input_tokens"],
        cumulative_output_tokens=row["cumulative_output_tokens"],
        error_code=row["error_code"],
        error_summary=row["error_summary"],
        waiting_reason=row["waiting_reason"],
        waiting_ref=row["waiting_ref"],
        wait_until_ms=row["wait_until_ms"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        completed_at_ms=row["completed_at_ms"],
    )


def _row_to_control(row: sqlite3.Row) -> TaskControlRecord:
    return TaskControlRecord(
        control_seq=row["control_seq"],
        control_id=row["control_id"],
        task_id=row["task_id"],
        kind=row["kind"],
        dedup_key=row["dedup_key"],
        payload_blob_id=row["payload_blob_id"],
        requested_by=row["requested_by"],
        state=row["state"],
        outcome_code=row["outcome_code"],
        requested_at_ms=row["requested_at_ms"],
        applied_at_ms=row["applied_at_ms"],
    )


def _row_to_event(row: sqlite3.Row) -> DurableEventRecord:
    return DurableEventRecord(
        event_seq=row["event_seq"],
        event_id=row["event_id"],
        task_id=row["task_id"],
        event_type=row["event_type"],
        phase=row["phase"],
        safe_payload_json=row["safe_payload_json"],
        payload_blob_id=row["payload_blob_id"],
        lease_epoch=row["lease_epoch"],
        created_at_ms=row["created_at_ms"],
    )


def _task_snapshot(record: TaskRecord) -> TaskSnapshot:
    error: SafeError | None = None
    if record.error_code:
        error = SafeError(
            error_code=record.error_code or "",
            error_summary=record.error_summary or "",
        )
    return TaskSnapshot(
        task_id=record.task_id,
        state=record.state,
        checkpoint_phase=record.checkpoint_phase,
        run_segment=record.run_segment,
        root_attempt_count=record.root_attempt_count,
        max_root_attempts=record.max_root_attempts,
        recovery_pending=record.recovery_pending,
        session_sequence=record.session_sequence,
        waiting_reason=record.waiting_reason,
        waiting_ref=record.waiting_ref,
        error=error,
        created_at_ms=record.created_at_ms,
        updated_at_ms=record.updated_at_ms,
        completed_at_ms=record.completed_at_ms,
    )


# ---------------------------------------------------------------------------
# SqliteStoreBase
# ---------------------------------------------------------------------------


class SqliteStoreBase:
    """Base class holding the connection and shared internal helpers.

    The façade :class:`SqliteRuntimeStore` inherits from this base plus
    the responsibility mixins; all share one ``self._conn`` connection
    (design §7.3, §16.1).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying SQLite connection (for migration and tests)."""
        return self._conn

    def _task_scope_predicate(
        self, scope: RequestScope, *, alias: str = ""
    ) -> tuple[str, tuple[str, str, str, str]]:
        """Four-field task-scope authorization predicate (design §11.3, Task 11).

        Returns a SQL ``AND``-joined predicate and bound parameters
        enforcing tenant, principal, agent, and workspace scope on the
        ``tasks`` table. Shared by ``read_task_snapshot`` and
        ``read_final_reply`` so the two authorization checks cannot drift
        apart (Task 11 Step 2).

        ``alias`` is the optional table alias prefix (e.g. ``"t"``); an
        empty string addresses the un-aliased ``tasks`` table.
        """
        prefix = f"{alias}." if alias else ""
        predicate = (
            f"{prefix}tenant_id = ? AND {prefix}principal_id = ? "
            f"AND {prefix}agent_id = ? AND {prefix}workspace_id = ?"
        )
        params = (
            scope.tenant_id,
            scope.principal_id,
            scope.agent_id,
            scope.workspace_id,
        )
        return predicate, params

    def list_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[DurableEventRecord]:
        rows = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND event_seq>? "
            "ORDER BY event_seq ASC LIMIT ?",
            (task_id, after_seq, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def _append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        phase: str | None,
        lease_epoch: int | None,
        safe_payload: str | None,
        now_ms: int,
        payload_blob_id: str | None = None,
    ) -> None:
        """Append an immutable task event (design §16.6).

        Must be called inside an active ``BEGIN IMMEDIATE`` transaction.
        """
        event_id = _new_uuid()
        self._conn.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, event_type, phase, safe_payload_json,
                payload_blob_id, lease_epoch, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                event_type,
                phase,
                safe_payload,
                payload_blob_id,
                lease_epoch,
                now_ms,
            ),
        )

    def _validate_lease(
        self,
        claim: TaskClaim,
        *,
        now_ms: int | None = None,
        check_deadline: bool = True,
    ) -> None:
        """Validate that ``claim`` matches the current task lease.

        Raises :class:`StaleLeaseError` when the token/epoch is stale, the
        task is no longer ``LEASED``/``RUNNING``, or (when
        ``check_deadline`` is true) the lease deadline has passed
        (design §6.10, §6.11, §11.2; Task 2 Step 5).

        Use one ``now_ms`` value per transaction so a transaction cannot
        observe two different deadlines. Pass ``check_deadline=False`` for
        voluntary state transitions (enter_retry_wait, enter_waiting_user,
        cancel_task) where the Worker is releasing the task — the
        token/epoch check is sufficient fencing for these.
        """
        if now_ms is None:
            now_ms = _now_ms()
        row = self._conn.execute(
            "SELECT state, lease_token, lease_epoch, lease_until_ms FROM tasks WHERE task_id=?",
            (claim.task_id,),
        ).fetchone()
        if row is None:
            from miniunicorn.runtime.observability import get_runtime_metrics

            get_runtime_metrics().inc("stale_mutations_rejected_total")
            raise StaleLeaseError(claim.task_id, claim.lease_epoch, "task not found")
        if check_deadline:
            deadline_ok = row["lease_until_ms"] is not None and row["lease_until_ms"] >= now_ms
        else:
            deadline_ok = True
        if (
            row["state"] not in ("LEASED", "RUNNING")
            or row["lease_token"] != claim.lease_token
            or row["lease_epoch"] != claim.lease_epoch
            or not deadline_ok
        ):
            from miniunicorn.runtime.observability import get_runtime_metrics

            get_runtime_metrics().inc("stale_mutations_rejected_total")
            raise StaleLeaseError(
                claim.task_id,
                claim.lease_epoch,
                f"lease mismatch: db_epoch={row['lease_epoch']}",
            )

    def _transition_task(
        self,
        task_id: str,
        *,
        from_state: str | None,
        to_state: str,
        now_ms: int,
        error: SafeError | None = None,
        clear_lease: bool = False,
    ) -> None:
        """Transition a task to ``to_state`` with validation (design §14.2).

        ``from_state=None`` means "any non-terminal state". The transition
        is validated against :data:`TRANSITIONS`. ``state_version`` is
        incremented (design §14.4). Must be called inside an active
        ``BEGIN IMMEDIATE`` transaction.
        """
        row = self._conn.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"task not found: {task_id}")
        current = row["state"]
        if from_state is not None and current != from_state:
            raise RuntimeError(
                f"task {task_id} state mismatch: expected {from_state}, got {current}"
            )
        if not is_allowed_transition(current, to_state):
            raise RuntimeError(f"forbidden transition: {current} -> {to_state} for task {task_id}")

        if clear_lease:
            self._conn.execute(
                """
                UPDATE tasks SET
                    state=?,
                    leased_by=NULL,
                    lease_token=NULL,
                    lease_epoch=lease_epoch,
                    lease_until_ms=NULL,
                    last_heartbeat_at_ms=NULL,
                    error_code=?,
                    error_summary=?,
                    completed_at_ms=CASE WHEN ? IN ('COMPLETED','FAILED','CANCELLED') THEN ? ELSE completed_at_ms END,
                    state_version=state_version+1,
                    updated_at_ms=?
                WHERE task_id=?
                """,
                (
                    to_state,
                    error.error_code if error else None,
                    error.error_summary if error else None,
                    to_state,
                    now_ms,
                    now_ms,
                    task_id,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE tasks SET
                    state=?,
                    error_code=?,
                    error_summary=?,
                    state_version=state_version+1,
                    updated_at_ms=?
                WHERE task_id=?
                """,
                (
                    to_state,
                    error.error_code if error else None,
                    error.error_summary if error else None,
                    now_ms,
                    task_id,
                ),
            )

    def _release_session_slot(self, task_id: str, now_ms: int) -> None:
        """Clear ``session_slots.active_task_id`` if it points to ``task_id``."""
        self._conn.execute(
            "UPDATE session_slots SET active_task_id=NULL, updated_at_ms=? WHERE active_task_id=?",
            (now_ms, task_id),
        )

    def _promote_due_retries_impl(self, now_ms: int, limit: int) -> int:
        """Move elapsed ``RETRY_WAIT`` tasks back to ``QUEUED`` (design §8.2).

        Must be called inside an active ``BEGIN IMMEDIATE`` transaction.
        """
        rows = self._conn.execute(
            """
            SELECT task_id FROM tasks
            WHERE state='RETRY_WAIT' AND wait_until_ms <= ?
            LIMIT ?
            """,
            (now_ms, limit),
        ).fetchall()
        for row in rows:
            self._conn.execute(
                """
                UPDATE tasks SET state='QUEUED', available_at_ms=?, wait_until_ms=NULL,
                    state_version=state_version+1, updated_at_ms=?
                WHERE task_id=?
                """,
                (now_ms, now_ms, row["task_id"]),
            )
            self._append_event(
                task_id=row["task_id"],
                event_type="TASK_LEASED",
                phase=None,
                lease_epoch=None,
                safe_payload=json.dumps({"promoted_from": "RETRY_WAIT"}),
                now_ms=now_ms,
            )
        return len(rows)
