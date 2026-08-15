"""SQLite fact database with fail-closed health and scoped reads.

The database at ``memory/structured/memory.db`` is the single runtime fact
store (design sections 7-8). This module owns transaction append, health
state, and all read accessors; it makes no business decisions and never
copies PRAGMA or DDL.

Normative sources:
docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md
docs/superpowers/specs/2026-08-14-sqlite-memory-storage-design.md
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from loguru import logger
from pydantic import ValidationError

from miniunicorn.agent.memory_models import (
    DuplicateMemoryIdempotencyKey,
    InvalidMemoryTransition,
    MemoryKind,
    MemoryLockTimeout,
    MemoryRecord,
    MemoryRevisionConflict,
    MemoryScope,
    MemoryStatus,
    MemoryStorageStats,
    MemoryTransaction,
    MemoryWriteError,
    RepositoryDegradedError,
    RepositoryHealth,
    TagCatalog,
    UnknownMemoryTag,
    assert_transition,
    transaction_checksum,
    validate_same_status_revision,
)
from miniunicorn.agent.memory_sqlite_schema import (
    SCHEMA_VERSION,
    check_schema,
    connect_memory_db,
    initialize_schema,
)

_SUPPORTED_TAGS_FILE = "tags.json"
_JOURNAL_FILE = "journal.jsonl"
_LOCK_FILE = "journal.lock"


class StructuredMemoryRepository:
    """SQLite-backed structured memory store with fail-closed health."""

    def __init__(self, workspace: Path, *, lock_timeout_s: float = 5.0):
        self.workspace = Path(workspace)
        self.lock_timeout_s = lock_timeout_s
        self.structured_dir = self.workspace / "memory" / "structured"
        self.database_path = self.structured_dir / "memory.db"
        self.journal_path = self.structured_dir / _JOURNAL_FILE
        self.tags_path = self.structured_dir / _SUPPORTED_TAGS_FILE
        self.lock_path = self.structured_dir / _LOCK_FILE
        self._tag_catalog = TagCatalog()
        self._health = RepositoryHealth()
        self._initialize_database()
        self.rebuild()
        logger.info("memory_repository_loaded workspace={} health={}", self.workspace, self._health.state)

    @property
    def workspace(self) -> Path:
        return self._workspace

    @workspace.setter
    def workspace(self, value: Path) -> None:
        self._workspace = Path(value)

    @property
    def health(self) -> RepositoryHealth:
        return self._health

    @property
    def tag_catalog(self) -> TagCatalog:
        return self._tag_catalog

    def _load_tag_catalog(self) -> None:
        if self.tags_path.exists():
            try:
                self._tag_catalog = TagCatalog.load(self.tags_path)
            except (OSError, ValueError, ValidationError):
                logger.warning("memory_tags_unreadable path={}", self.tags_path)
                self._tag_catalog = TagCatalog()
        else:
            self._tag_catalog = TagCatalog()

    def _initialize_database(self) -> None:
        """Create the SQLite fact database and schema when missing."""
        try:
            with connect_memory_db(
                self.database_path, lock_timeout_s=self.lock_timeout_s
            ) as connection:
                initialize_schema(connection)
        except RepositoryDegradedError as exc:
            self._degrade(_degraded_error_code(exc), str(exc))
        except (OSError, sqlite3.Error) as exc:
            self._degrade("sqlite_open_error", f"cannot open memory database: {exc}")

    # ------------------------------------------------------------------
    # Rebuild (health refresh)
    # ------------------------------------------------------------------

    def rebuild(self) -> RepositoryHealth:
        """Refresh health against the SQLite fact database; fail closed on corruption."""
        self._load_tag_catalog()
        try:
            with connect_memory_db(
                self.database_path, lock_timeout_s=self.lock_timeout_s
            ) as connection:
                self._refresh_health(connection)
        except RepositoryDegradedError as exc:
            self._degrade(_degraded_error_code(exc), str(exc))
        except (OSError, sqlite3.Error) as exc:
            self._degrade("sqlite_open_error", f"cannot open memory database: {exc}")
        return self._health

    def _refresh_health(self, connection: sqlite3.Connection) -> None:
        check_schema(connection)
        result = connection.execute("PRAGMA quick_check(1)").fetchone()
        if result is None or result[0] != "ok":
            raise RepositoryDegradedError(
                "memory database integrity check failed (code=integrity_error)"
            )
        last_seq = connection.execute(
            "SELECT COALESCE(MAX(tx_seq), 0) FROM memory_transactions"
        ).fetchone()[0]
        meta = connection.execute(
            "SELECT value FROM storage_meta WHERE key = 'audit_exported_seq'"
        ).fetchone()
        try:
            database_bytes = self.database_path.stat().st_size
        except OSError:
            database_bytes = 0
        self._health = RepositoryHealth(
            state="healthy",
            backend="sqlite",
            schema_version=SCHEMA_VERSION,
            last_transaction_seq=int(last_seq),
            audit_exported_seq=int(meta[0]) if meta else 0,
            database_bytes=database_bytes,
        )

    def _degrade(self, code: str, message: str) -> None:
        self._health = RepositoryHealth(
            state="degraded",
            backend="sqlite",
            error_code=code,
            error_message=message,
        )
        logger.error("memory_repository_degraded code={} error={}", code, message)

    def _require_healthy(self) -> None:
        if self._health.state != "healthy":
            raise RepositoryDegradedError(
                f"memory database degraded (code={self._health.error_code}); reads and writes disabled"
            )

    @contextmanager
    def _open_read(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived read connection, failing closed when degraded."""
        self._require_healthy()
        with connect_memory_db(
            self.database_path, lock_timeout_s=self.lock_timeout_s
        ) as connection:
            yield connection

    # ------------------------------------------------------------------
    # Append protocol (reimplemented on SQLite in the write migration task)
    # ------------------------------------------------------------------

    def append_transaction(self, transaction: MemoryTransaction) -> None:
        """Append one durable transaction under the journal lock (design section 12.1)."""
        try:
            with FileLock(str(self.lock_path), timeout=self.lock_timeout_s):
                self._require_healthy()
                self._synchronize_locked()
                self._append_validated_locked(transaction)
        except FileLockTimeout as exc:
            raise MemoryLockTimeout(f"journal lock timeout after {self.lock_timeout_s}s") from exc
        except MemoryError:
            raise
        except OSError as exc:
            raise MemoryWriteError(str(exc)) from exc
        logger.info("memory_transaction_committed tx={} actor={} reason={}", transaction.tx_id, transaction.actor, transaction.reason)

    def append_create_if_absent(
        self, transaction: MemoryTransaction
    ) -> tuple[MemoryRecord, bool]:
        """Atomically create a revision-1 record unless its source key exists.

        The lookup, condition, and append all happen under the same journal
        lock acquisition, so concurrent instances cannot double-create. The
        source key is ``(source_batch, content_hash)`` on the transaction.
        """
        self._validate_create_transaction_shape(transaction)
        record = transaction.operations[0].record
        try:
            with FileLock(str(self.lock_path), timeout=self.lock_timeout_s):
                self._require_healthy()
                self._synchronize_locked()
                existing = self.record_created_for(transaction.source_batch, record.content_hash)
                if existing is not None:
                    return existing, False
                self._append_validated_locked(transaction)
                return record, True
        except FileLockTimeout as exc:
            raise MemoryLockTimeout(
                f"journal lock timeout after {self.lock_timeout_s}s"
            ) from exc
        except MemoryError:
            raise
        except OSError as exc:
            raise MemoryWriteError(str(exc)) from exc

    def _append_validated_locked(self, transaction: MemoryTransaction) -> None:
        """Validate and durably append a transaction while the writer lock is held."""
        validated = self._validate_against_current(transaction)
        line = self._canonical_transaction_line(validated)
        try:
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            self._degrade(
                "write_uncertain",
                f"journal durability is uncertain: {exc}",
            )
            raise MemoryWriteError(str(exc)) from exc
        self._publish_transaction(validated)

    def _validate_create_transaction_shape(self, transaction: MemoryTransaction) -> None:
        if not transaction.source_batch:
            raise MemoryWriteError("create transaction requires a non-empty source_batch")
        if len(transaction.operations) != 1:
            raise MemoryWriteError("create transaction must contain exactly one operation")
        record = transaction.operations[0].record
        if record.revision != 1:
            raise MemoryWriteError("create transaction must create revision 1")
        if transaction.expected_revisions.get(record.id, 0) != 0:
            raise MemoryWriteError("create transaction must expect creation revision 0")

    def _synchronize_locked(self) -> None:
        """Refresh repository health while the writer lock is held.

        Repository instances are process-local. Refreshing under the shared
        lock makes expected-revision checks observe commits made by other
        instances.
        """
        health = self.rebuild()
        if health.state != "healthy":
            self._require_healthy()

    def _validate_against_current(self, transaction: MemoryTransaction) -> MemoryTransaction:
        if transaction_checksum(transaction) != transaction.checksum_sha256:
            raise MemoryWriteError("transaction checksum mismatch")
        projected = dict(self._current)
        projected_created = dict(self._created_by_source_content)
        for operation in transaction.operations:
            record = operation.record
            self._tag_catalog.validate_record(record)
            previous = projected.get(record.id)
            expected = transaction.expected_revisions[record.id]
            if previous is None:
                if expected != 0 or record.revision != 1:
                    raise MemoryRevisionConflict(
                        f"expected revision 0 and revision 1 for new record {record.id}, got expected={expected} revision={record.revision}"
                    )
            else:
                if expected != previous.revision:
                    raise MemoryRevisionConflict(
                        f"expected revision {previous.revision} for {record.id}, got {expected}"
                    )
                if record.revision != previous.revision + 1:
                    raise MemoryRevisionConflict(
                        f"revision must increment by exactly one for {record.id}: {previous.revision} -> {record.revision}"
                    )
                assert_transition(previous.status, record.status)
                validate_same_status_revision(previous, record)
            if transaction.source_batch and record.revision == 1:
                key = (transaction.source_batch, record.content_hash)
                existing_id = projected_created.get(key)
                if existing_id is not None and existing_id != record.id:
                    raise DuplicateMemoryIdempotencyKey(
                        "duplicate idempotency key "
                        f"source_batch={transaction.source_batch} content_hash={record.content_hash}: "
                        f"{existing_id}, {record.id}"
                    )
                projected_created[key] = record.id
            projected[record.id] = record
        active_by_conflict: dict[str, str] = {}
        for record in projected.values():
            if record.status is not MemoryStatus.ACTIVE:
                continue
            conflicting = active_by_conflict.get(record.conflict_key)
            if conflicting is not None and conflicting != record.id:
                raise InvalidMemoryTransition(
                    "multiple active records for conflict key "
                    f"{record.conflict_key}: {conflicting}, {record.id}"
                )
            active_by_conflict[record.conflict_key] = record.id
        return transaction

    def _canonical_transaction_line(self, transaction: MemoryTransaction) -> str:
        return json.dumps(
            transaction.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def _replay_line(self, raw: str, line_number: int) -> bool:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._degrade("invalid_json", f"line {line_number}: {exc}")
            return False
        if not isinstance(data, dict):
            self._degrade("invalid_json", f"line {line_number}: not an object")
            return False
        try:
            transaction = MemoryTransaction.model_validate(data)
        except ValidationError as exc:
            code = "unsupported_schema" if data.get("schema_version") != 1 else "invalid_transaction"
            self._degrade(code, f"line {line_number}: {exc}")
            return False
        try:
            self._validate_against_current(transaction)
        except DuplicateMemoryIdempotencyKey as exc:
            self._degrade("duplicate_idempotency_key", f"line {line_number}: {exc}")
            return False
        except MemoryRevisionConflict as exc:
            self._degrade("revision_conflict", f"line {line_number}: {exc}")
            return False
        except InvalidMemoryTransition as exc:
            self._degrade("invalid_transition", f"line {line_number}: {exc}")
            return False
        except UnknownMemoryTag as exc:
            self._degrade("unknown_tag", f"line {line_number}: {exc}")
            return False
        except MemoryWriteError as exc:
            self._degrade("checksum_mismatch", f"line {line_number}: {exc}")
            return False
        self._publish_transaction(transaction)
        return True

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    @staticmethod
    def _record_from_row(row: sqlite3.Row | None) -> MemoryRecord | None:
        if row is None:
            return None
        return MemoryRecord.model_validate_json(row["record_json"])

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._open_read() as connection:
            row = connection.execute(
                "SELECT record_json FROM memory_revisions "
                "WHERE memory_id = ? AND is_current = 1",
                (memory_id,),
            ).fetchone()
        return self._record_from_row(row)

    def get_current(
        self, memory_id: str, *, synchronize: bool = True
    ) -> MemoryRecord | None:
        """Return the committed current record for *memory_id*.

        SQLite reads already observe committed state, so ``synchronize`` is
        kept for caller compatibility and no longer triggers a rebuild.
        """
        return self.get(memory_id)

    def revisions(self, memory_id: str) -> tuple[MemoryRecord, ...]:
        with self._open_read() as connection:
            rows = connection.execute(
                "SELECT record_json FROM memory_revisions "
                "WHERE memory_id = ? ORDER BY revision ASC",
                (memory_id,),
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def current_records(self, status: MemoryStatus | None = None) -> tuple[MemoryRecord, ...]:
        sql = "SELECT record_json FROM memory_revisions WHERE is_current = 1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY memory_id ASC"
        with self._open_read() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def active_for_conflict_key(self, key: str) -> MemoryRecord | None:
        with self._open_read() as connection:
            row = connection.execute(
                "SELECT record_json FROM memory_revisions "
                "WHERE is_current = 1 AND status = 'active' AND conflict_key = ? "
                "LIMIT 1",
                (key,),
            ).fetchone()
        return self._record_from_row(row)

    def candidate_records(self) -> tuple[MemoryRecord, ...]:
        return self.current_records(MemoryStatus.CANDIDATE)

    def candidate_ids_for_source(self, source_batch: str) -> frozenset[str]:
        with self._open_read() as connection:
            rows = connection.execute(
                "SELECT r.memory_id FROM memory_revisions r "
                "JOIN memory_source_batches b ON b.memory_id = r.memory_id "
                "WHERE r.is_current = 1 AND r.status = 'candidate' AND b.source_batch = ?",
                (source_batch,),
            ).fetchall()
        return frozenset(row[0] for row in rows)

    def record_created_for(self, source_batch: str, content_hash: str) -> MemoryRecord | None:
        """Return the current record created by this source-batch/content pair.

        The creation mapping is written exactly once by the revision-1
        transaction and never moves, so this lookup is deterministic.
        """
        with self._open_read() as connection:
            row = connection.execute(
                "SELECT r.record_json FROM memory_revisions r "
                "JOIN memory_creation_keys k ON k.memory_id = r.memory_id "
                "WHERE k.source_batch = ? AND k.content_hash = ? AND r.is_current = 1",
                (source_batch, content_hash),
            ).fetchone()
        return self._record_from_row(row)

    def record_ids_for_source(self, source_batch: str) -> frozenset[str]:
        """Return every record that ever carried a non-empty source batch."""
        with self._open_read() as connection:
            rows = connection.execute(
                "SELECT memory_id FROM memory_source_batches WHERE source_batch = ?",
                (source_batch,),
            ).fetchall()
        return frozenset(row[0] for row in rows)

    def recall_candidates(
        self,
        *,
        allowed_scopes: tuple[MemoryScope, ...],
        requested_kinds: tuple[MemoryKind, ...] | frozenset[MemoryKind],
        now: datetime,
    ) -> tuple[MemoryRecord, ...]:
        """Return current active, unexpired records whose scope and kind match."""
        if not allowed_scopes:
            return ()
        scope_clause = " OR ".join("(scope_kind = ? AND scope_key = ?)" for _ in allowed_scopes)
        params: list[Any] = [
            value for scope in allowed_scopes for value in (scope.kind.value, scope.key)
        ]
        sql = (
            "SELECT record_json FROM memory_revisions "
            "WHERE is_current = 1 AND status = 'active' "
            f"AND ({scope_clause}) "
            "AND (expires_at IS NULL OR expires_at > ?)"
        )
        params.append(
            now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        if requested_kinds:
            sql += f" AND kind IN ({','.join('?' for _ in requested_kinds)})"
            params.extend(kind.value for kind in requested_kinds)
        sql += " ORDER BY memory_id ASC"
        with self._open_read() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def transaction_log(
        self, *, limit: int = 20, tx_id: str | None = None
    ) -> tuple[MemoryTransaction, ...]:
        """Return recent transactions newest first, optionally for one tx id."""
        if tx_id is None:
            sql = "SELECT transaction_json FROM memory_transactions ORDER BY tx_seq DESC LIMIT ?"
            params: list[Any] = [limit]
        else:
            sql = (
                "SELECT transaction_json FROM memory_transactions "
                "WHERE tx_id = ? ORDER BY tx_seq DESC LIMIT ?"
            )
            params = [tx_id, limit]
        with self._open_read() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(
            MemoryTransaction.model_validate_json(row["transaction_json"]) for row in rows
        )

    def storage_stats(self) -> MemoryStorageStats:
        with self._open_read() as connection:
            transaction_count = connection.execute(
                "SELECT COUNT(*) FROM memory_transactions"
            ).fetchone()[0]
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM memory_revisions"
            ).fetchone()[0]
            current_count = connection.execute(
                "SELECT COUNT(*) FROM memory_revisions WHERE is_current = 1"
            ).fetchone()[0]
            last_seq = connection.execute(
                "SELECT COALESCE(MAX(tx_seq), 0) FROM memory_transactions"
            ).fetchone()[0]
            meta = connection.execute(
                "SELECT value FROM storage_meta WHERE key = 'audit_exported_seq'"
            ).fetchone()
        try:
            database_bytes = self.database_path.stat().st_size
        except OSError:
            database_bytes = 0
        return MemoryStorageStats(
            backend="sqlite",
            schema_version=SCHEMA_VERSION,
            transaction_count=transaction_count,
            revision_count=revision_count,
            current_count=current_count,
            last_transaction_seq=int(last_seq),
            audit_exported_seq=int(meta[0]) if meta else 0,
            database_bytes=database_bytes,
        )


def _degraded_error_code(exc: RepositoryDegradedError) -> str:
    """Extract the ``(code=...)`` marker from a degradation message when present."""
    match = re.search(r"\(code=([a-z_]+)\)", str(exc))
    return match.group(1) if match else "repository_degraded"
