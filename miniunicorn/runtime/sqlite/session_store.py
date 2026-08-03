"""Session commit ledger mixin for the SQLite Runtime Store (design §17.7).

Holds :class:`SessionStoreMixin` covering ``SessionCommitLedger``:
prepare / confirm / conflict / read of durable session commits. The
mixin shares ``self._conn`` with the other responsibility mixins via the
façade and calls shared lease-validation helpers on the base
(design §7.3, §11.2, §17.7; Task 12 Steps 3-5).
"""

from __future__ import annotations

import json
import sqlite3

from miniunicorn.agent.ports import SafeError
from miniunicorn.runtime.contracts import (
    SessionCommitMismatchError,
    TaskClaim,
)
from miniunicorn.runtime.models import (
    SessionCommitRecord,
    SessionCommitWrite,
)
from miniunicorn.runtime.sqlite.base_store import _new_uuid, _now_ms


class SessionStoreMixin:
    """Session commit prepare/confirm/conflict/read operations."""

    def _row_to_session_commit(self, row: sqlite3.Row) -> SessionCommitRecord:
        return SessionCommitRecord(
            session_commit_id=row["session_commit_id"],
            task_id=row["task_id"],
            commit_kind=row["commit_kind"],
            session_key=row["session_key"],
            base_revision=row["base_revision"],
            target_revision=row["target_revision"],
            content_hash=row["content_hash"],
            state=row["state"],
            error_code=row["error_code"],
            created_at_ms=row["created_at_ms"],
            committed_at_ms=row["committed_at_ms"],
        )

    def prepare_session_commit(
        self, claim: TaskClaim, value: SessionCommitWrite
    ) -> SessionCommitRecord:
        """Insert or verify a ``PREPARED`` session commit (design §17.7 step 3).

        Validates the task lease, then either:
        - inserts a new ``session_commits(PREPARED)`` row, or
        - returns the existing row if the same ``(task_id, commit_kind)`` is
          already prepared (idempotent retry).

        Appends ``SESSION_COMMIT_PREPARED``. Must be called inside an
        ``BEGIN IMMEDIATE`` transaction (opened by this method).
        """
        now_ms = value.created_at_ms or _now_ms()
        commit_id = value.session_commit_id or _new_uuid()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)

            # Check for an existing prepared/committed row (idempotent retry).
            existing = self._conn.execute(
                "SELECT * FROM session_commits WHERE task_id=? AND commit_kind=?",
                (claim.task_id, value.commit_kind),
            ).fetchone()
            if existing is not None:
                # A COMMITTED row means the work is already durably done.
                # The retry's request fields may legitimately differ
                # (e.g. base_revision advanced after INBOUND committed);
                # return the committed row so the caller sees
                # ALREADY_COMMITTED (design §17.7 step 5 crash recovery).
                if existing["state"] == "COMMITTED":
                    self._conn.execute("COMMIT")
                    return self._row_to_session_commit(existing)
                # For a PREPARED row, verify immutable fields match
                # (Task 3 Step 7). A retry with different fields is a
                # bug, not an idempotent retry of the same operation.
                mismatched: list[str] = []
                if existing["session_commit_id"] != commit_id:
                    mismatched.append("session_commit_id")
                if existing["session_key"] != value.session_key:
                    mismatched.append("session_key")
                if existing["base_revision"] != value.base_revision:
                    mismatched.append("base_revision")
                if existing["target_revision"] != value.target_revision:
                    mismatched.append("target_revision")
                if existing["content_hash"] != value.content_hash:
                    mismatched.append("content_hash")
                if mismatched:
                    # Let the except handler own ROLLBACK exactly once.
                    raise SessionCommitMismatchError(commit_id, mismatched)
                self._conn.execute("COMMIT")
                return self._row_to_session_commit(existing)

            self._conn.execute(
                """
                INSERT INTO session_commits (
                    session_commit_id, task_id, commit_kind, session_key,
                    base_revision, target_revision, content_hash, state,
                    error_code, created_at_ms, committed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PREPARED', NULL, ?, NULL)
                """,
                (
                    commit_id,
                    claim.task_id,
                    value.commit_kind,
                    value.session_key,
                    value.base_revision,
                    value.target_revision,
                    value.content_hash,
                    now_ms,
                ),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="SESSION_COMMIT_PREPARED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "session_commit_id": commit_id,
                        "commit_kind": value.commit_kind,
                        "session_key": value.session_key,
                        "base_revision": value.base_revision,
                        "target_revision": value.target_revision,
                    }
                ),
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE session_commit_id=?",
            (commit_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_session_commit(row)

    def confirm_session_commit(
        self, claim: TaskClaim, commit_id: str, revision: int, committed_at_ms: int
    ) -> SessionCommitRecord:
        """Mark a session commit ``COMMITTED`` (design §17.7 step 5).

        Revalidates the task lease, transitions the commit to ``COMMITTED``
        with the filesystem revision and timestamp, and appends
        ``SESSION_COMMITTED``. An already-confirmed commit is returned
        idempotently without re-validating the lease.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM session_commits WHERE session_commit_id=?",
                (commit_id,),
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise RuntimeError(f"session commit not found: {commit_id}")
            if row["state"] == "COMMITTED":
                # Idempotent: already confirmed — no lease revalidation.
                self._conn.execute("COMMIT")
                return self._row_to_session_commit(row)

            self._validate_lease(claim, now_ms=committed_at_ms)

            self._conn.execute(
                """
                UPDATE session_commits
                SET state='COMMITTED', committed_at_ms=?
                WHERE session_commit_id=? AND state='PREPARED'
                """,
                (committed_at_ms, commit_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="SESSION_COMMITTED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "session_commit_id": commit_id,
                        "revision": revision,
                    }
                ),
                now_ms=committed_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE session_commit_id=?",
            (commit_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_session_commit(row)

    def mark_session_conflict(
        self, claim: TaskClaim, commit_id: str, error: SafeError
    ) -> SessionCommitRecord:
        """Mark a session commit ``CONFLICT`` (design §17.7, §21.4; Task 3 Step 8).

        Records the conflict for operational alerting. Requires the
        current task claim so a stale Worker cannot mark a replacement
        attempt's commit conflicted. Uses ``check_deadline=False``
        because this is a voluntary release — the token/epoch check is
        sufficient fencing.
        """
        now_ms = _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms, check_deadline=False)
            self._conn.execute(
                """
                UPDATE session_commits
                SET state='CONFLICT', error_code=?, committed_at_ms=?
                WHERE session_commit_id=? AND state='PREPARED'
                """,
                (error.error_code, now_ms, commit_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE session_commit_id=?",
            (commit_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_session_commit(row)

    def read_session_commit(self, task_id: str, commit_kind: str) -> SessionCommitRecord | None:
        """Read the session commit row for ``(task_id, commit_kind)``."""
        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE task_id=? AND commit_kind=?",
            (task_id, commit_kind),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_session_commit(row)
