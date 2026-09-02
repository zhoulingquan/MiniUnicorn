"""SQLite fact database with fail-closed health and scoped reads.

The database at ``memory/structured/memory.db`` is the single runtime fact
store (design sections 7-8). This module owns transaction append, health
state, and all read accessors; it makes no business decisions and never
copies PRAGMA, DDL, or SQL.

The legacy journal is read-only migration input handled by
``memory_jsonl_import``; starting the repository triggers the migrator only
when ``memory.db`` is missing and no completed migration manifest exists
(design section 11), never at runtime otherwise.

Normative sources:
docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md
docs/superpowers/specs/2026-08-14-sqlite-memory-storage-design.md
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from miniunicorn.memory.jsonl_import import (
    LegacyJournalImportError,
    migrate_legacy_journal,
)
from miniunicorn.memory.models import (
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
    assert_transition,
    normalize_match_text,
    transaction_checksum,
    validate_same_status_revision,
)
from miniunicorn.memory.sqlite_schema import (
    SCHEMA_VERSION,
    SQL_ACTIVE_BY_CONFLICT_KEY,
    SQL_ACTIVE_CONFLICT_OTHER,
    SQL_AUDIT_EXPORTED_SEQ,
    SQL_CANDIDATE_IDS_FOR_SOURCE,
    SQL_COUNT_CURRENT,
    SQL_COUNT_REVISIONS,
    SQL_COUNT_TRANSACTIONS,
    SQL_CREATION_KEY_OWNER,
    SQL_CREATION_KEY_RECORD,
    SQL_CURRENT_RECORD_JSON_BY_ID,
    SQL_CURRENT_RECORDS,
    SQL_CURRENT_REVISION,
    SQL_IDS_FOR_SOURCE,
    SQL_INSERT_ALIAS,
    SQL_INSERT_CREATION_KEY,
    SQL_INSERT_REVISION,
    SQL_INSERT_SOURCE_BATCH,
    SQL_INSERT_TAG,
    SQL_INSERT_TRANSACTION,
    SQL_MAX_TX_SEQ,
    SQL_ORDER_BY_ID,
    SQL_QUICK_CHECK,
    SQL_RECALL_SELECT,
    SQL_RECALL_SUFFIX,
    SQL_REVISIONS_BY_ID,
    SQL_TX_BY_ID,
    SQL_TX_LOG_BY_ID,
    SQL_TX_LOG_RECENT,
    SQL_TX_ROWS_IN_RANGE,
    SQL_UNSET_CURRENT,
    check_schema,
    connect_memory_db,
    initialize_schema,
    record_from_row,
)

_SUPPORTED_TAGS_FILE = "tags.json"
_MIGRATION_MANIFEST_FILE = "storage-migration-v2.json"


class StructuredMemoryRepository:
    """SQLite-backed structured memory store with fail-closed health."""

    def __init__(self, workspace: Path, *, lock_timeout_s: float = 5.0):
        self.workspace = Path(workspace)
        self.lock_timeout_s = lock_timeout_s
        self.structured_dir = self.workspace / "memory" / "structured"
        self.database_path = self.structured_dir / "memory.db"
        self.tags_path = self.structured_dir / _SUPPORTED_TAGS_FILE
        self._tag_catalog = TagCatalog()
        self._health = RepositoryHealth()
        self._initialize_database()
        if self._health.state != "degraded":
            self.rebuild()
        logger.info(
            "memory_repository_loaded workspace={} health={}", self.workspace, self._health.state
        )

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
        """Create the SQLite fact database and schema when missing (design section 11).

        Startup decision matrix: an existing database is opened directly and
        the legacy journal is never read; a missing database is migrated from
        a non-empty journal exactly once; a completed migration manifest
        without a database fails closed; a failed migration never creates a
        fresh empty database that would silently drop facts.
        """
        try:
            if not self.database_path.exists():
                self._prepare_missing_database()
            with connect_memory_db(
                self.database_path, lock_timeout_s=self.lock_timeout_s
            ) as connection:
                initialize_schema(connection)
        except LegacyJournalImportError as exc:
            self._degrade(exc.code, str(exc))
        except RepositoryDegradedError as exc:
            self._degrade(_degraded_error_code(exc), str(exc))
        except (OSError, sqlite3.Error) as exc:
            self._degrade("sqlite_open_error", f"cannot open memory database: {exc}")

    def _prepare_missing_database(self) -> None:
        """Bring up a missing database: migrate the legacy journal or fail closed.

        A completed migration manifest without a database raises
        :class:`RepositoryDegradedError` (the journal lacks post-migration
        facts, so re-importing would silently lose them). Otherwise the
        migrator runs and reads a non-empty legacy journal exactly once; a
        missing or empty journal returns ``migrated=False`` and leaves the
        fresh database creation to ``_initialize_database``, while a corrupt
        journal raises :class:`LegacyJournalImportError` that degrades health
        instead of creating an empty database over unimported facts.
        """
        if self._migration_manifest_completed():
            raise RepositoryDegradedError(
                "memory.db is missing after a completed migration; restore it from a backup "
                "(code=migration_database_lost)"
            )
        migrate_legacy_journal(self.workspace, self.lock_timeout_s)

    def _migration_manifest_completed(self) -> bool:
        try:
            manifest = json.loads(
                self.structured_dir.joinpath(_MIGRATION_MANIFEST_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return manifest.get("status") == "completed"

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
        result = connection.execute(SQL_QUICK_CHECK).fetchone()
        if result is None or result[0] != "ok":
            raise RepositoryDegradedError(
                "memory database integrity check failed (code=integrity_error)"
            )
        last_seq = connection.execute(SQL_MAX_TX_SEQ).fetchone()[0]
        meta = connection.execute(SQL_AUDIT_EXPORTED_SEQ).fetchone()
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
    # Append protocol (SQLite: atomic, idempotent, concurrent)
    # ------------------------------------------------------------------

    def append_transaction(self, transaction: MemoryTransaction) -> None:
        """Commit one governed transaction atomically (design section 12.1).

        Validation and all writes run inside one ``BEGIN IMMEDIATE``
        transaction, so concurrent writers serialize on the SQLite write
        lock and every failure leaves the database untouched.
        """
        try:
            self._require_healthy()
            with connect_memory_db(
                self.database_path, lock_timeout_s=self.lock_timeout_s
            ) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._append_in_connection(connection, transaction)
                    connection.commit()
                except sqlite3.OperationalError as exc:
                    connection.rollback()
                    if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                        raise MemoryLockTimeout(
                            f"sqlite lock timeout after {self.lock_timeout_s}s"
                        ) from exc
                    self._degrade("sqlite_operational_error", str(exc))
                    raise MemoryWriteError(str(exc)) from exc
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise self._map_integrity_error(connection, transaction) from exc
                except sqlite3.DatabaseError as exc:
                    connection.rollback()
                    self._degrade("sqlite_database_error", str(exc))
                    raise RepositoryDegradedError(
                        f"memory database failed (code=sqlite_database_error): {exc}"
                    ) from exc
        except MemoryError:
            raise
        except OSError as exc:
            raise MemoryWriteError(str(exc)) from exc
        logger.info(
            "memory_transaction_committed tx={} actor={} reason={}",
            transaction.tx_id,
            transaction.actor,
            transaction.reason,
        )

    def append_create_if_absent(self, transaction: MemoryTransaction) -> tuple[MemoryRecord, bool]:
        """Atomically create a revision-1 record unless its source key exists.

        The creation-key lookup, validation, and append all happen inside one
        ``BEGIN IMMEDIATE`` transaction, so concurrent instances cannot
        double-create. The source key is ``(source_batch, content_hash)`` on
        the transaction.
        """
        self._validate_create_transaction_shape(transaction)
        record = transaction.operations[0].record
        try:
            self._require_healthy()
            with connect_memory_db(
                self.database_path, lock_timeout_s=self.lock_timeout_s
            ) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = self._creation_key_record(
                        connection, transaction.source_batch, record.content_hash
                    )
                    if existing is not None:
                        connection.commit()
                        return existing, False
                    self._append_in_connection(connection, transaction)
                    connection.commit()
                    return record, True
                except sqlite3.OperationalError as exc:
                    connection.rollback()
                    if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                        raise MemoryLockTimeout(
                            f"sqlite lock timeout after {self.lock_timeout_s}s"
                        ) from exc
                    self._degrade("sqlite_operational_error", str(exc))
                    raise MemoryWriteError(str(exc)) from exc
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    existing = self._creation_key_record(
                        connection, transaction.source_batch, record.content_hash
                    )
                    if existing is not None:
                        return existing, False
                    raise self._map_integrity_error(connection, transaction) from exc
                except sqlite3.DatabaseError as exc:
                    connection.rollback()
                    self._degrade("sqlite_database_error", str(exc))
                    raise RepositoryDegradedError(
                        f"memory database failed (code=sqlite_database_error): {exc}"
                    ) from exc
        except MemoryError:
            raise
        except OSError as exc:
            raise MemoryWriteError(str(exc)) from exc

    def append_imported_transaction(self, transaction: MemoryTransaction) -> None:
        """Import one legacy journal transaction (migration-only, design section 11).

        Runs the exact same governed validation and writes as
        :meth:`append_transaction` (checksum, expected revisions, status
        transitions, tag catalog, creation keys, active-conflict index) inside
        its own ``BEGIN IMMEDIATE`` transaction against ``self.database_path``.

        This entry is used solely by ``memory_jsonl_import.migrate_legacy_journal``
        to replay a legacy journal into a temporary database. It never routes
        to audit export (audit export will attach only to the runtime append
        entries) and never degrades repository health: migration is
        single-process and the temporary database is discarded on any failure,
        so errors are raised to the caller instead.
        """
        with connect_memory_db(
            self.database_path, lock_timeout_s=self.lock_timeout_s
        ) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._append_in_connection(connection, transaction)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise self._map_integrity_error(connection, transaction) from exc
            except Exception:
                connection.rollback()
                raise

    def _append_in_connection(
        self,
        connection: sqlite3.Connection,
        transaction: MemoryTransaction,
    ) -> None:
        """Validate and write one transaction on a connection holding the write lock.

        Retiring revisions are written before activating ones so the new
        current row for an active record never inserts while another active
        record for the same conflict key is still current (the schema's
        partial unique index is enforced per insert, not per transaction).
        """
        validated = self._validate_against_database(connection, transaction)
        tx_seq = self._insert_transaction(connection, validated)
        for op_index, operation in sorted(
            enumerate(validated.operations),
            key=lambda pair: pair[1].record.status is MemoryStatus.ACTIVE,
        ):
            self._insert_revision(connection, tx_seq, op_index, validated, operation.record)

    def _insert_transaction(
        self, connection: sqlite3.Connection, transaction: MemoryTransaction
    ) -> int:
        payload = transaction.model_dump(mode="json")
        cursor = connection.execute(
            SQL_INSERT_TRANSACTION,
            (
                payload["tx_id"],
                payload["recorded_at"],
                payload["actor"],
                payload["reason"],
                payload["source_batch"],
                payload["checksum_sha256"],
                json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        tx_seq: int,
        op_index: int,
        transaction: MemoryTransaction,
        record: MemoryRecord,
    ) -> None:
        """Write one revision row plus tags, aliases, and provenance side tables."""
        connection.execute(SQL_UNSET_CURRENT, (record.id,))
        payload = record.model_dump(mode="json")
        connection.execute(
            SQL_INSERT_REVISION,
            (
                record.id,
                record.revision,
                tx_seq,
                op_index,
                1,
                record.status.value,
                record.kind.value,
                record.scope.kind.value,
                record.scope.key,
                normalize_match_text(record.subject),
                record.conflict_key,
                record.content_hash,
                record.source_level.value,
                record.importance,
                payload["updated_at"],
                payload["expires_at"],
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            ),
        )
        for tag in record.tags:
            connection.execute(SQL_INSERT_TAG, (record.id, record.revision, tag))
        for alias in record.aliases:
            connection.execute(
                SQL_INSERT_ALIAS, (record.id, record.revision, normalize_match_text(alias))
            )
        if transaction.source_batch:
            connection.execute(
                SQL_INSERT_SOURCE_BATCH, (record.id, transaction.source_batch, tx_seq)
            )
            if record.revision == 1:
                connection.execute(
                    SQL_INSERT_CREATION_KEY,
                    (transaction.source_batch, record.content_hash, record.id, tx_seq),
                )

    def _validate_against_database(
        self,
        connection: sqlite3.Connection,
        transaction: MemoryTransaction,
    ) -> MemoryTransaction:
        """Replicate the governed validation against committed database state.

        Runs inside the write transaction, so the observed state can only
        move forward after the write lock is acquired; expected revisions
        are compared against the current row per memory id and the creation
        key / active-conflict uniqueness checks mirror the schema indexes.
        All conflict checks use parameterized point lookups restricted to
        the records this transaction touches, so validation work never
        grows with the number of committed active records (design 16.2).
        """
        if transaction_checksum(transaction) != transaction.checksum_sha256:
            raise MemoryWriteError("transaction checksum mismatch")
        projected_actives: dict[str, str] = {}
        retired_actives: set[str] = set()
        created_owners: dict[tuple[str, str], str] = {}
        for operation in transaction.operations:
            record = operation.record
            self._tag_catalog.validate_record(record)
            previous = record_from_row(
                connection.execute(SQL_CURRENT_RECORD_JSON_BY_ID, (record.id,)).fetchone()
            )
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
                existing_id = created_owners.get(key)
                if existing_id is None:
                    row = connection.execute(SQL_CREATION_KEY_OWNER, key).fetchone()
                    existing_id = row["memory_id"] if row is not None else None
                if existing_id is not None and existing_id != record.id:
                    raise DuplicateMemoryIdempotencyKey(
                        "duplicate idempotency key "
                        f"source_batch={transaction.source_batch} content_hash={record.content_hash}: "
                        f"{existing_id}, {record.id}"
                    )
                created_owners[key] = record.id
            if record.status is MemoryStatus.ACTIVE:
                projected_actives[record.id] = record.conflict_key
            else:
                if previous is not None and previous.status is MemoryStatus.ACTIVE:
                    retired_actives.add(record.id)
                projected_actives.pop(record.id, None)
        active_by_conflict: dict[str, str] = {}
        for memory_id, conflict_key in projected_actives.items():
            conflicting = active_by_conflict.get(conflict_key)
            if conflicting is not None and conflicting != memory_id:
                raise InvalidMemoryTransition(
                    "multiple active records for conflict key "
                    f"{conflict_key}: {conflicting}, {memory_id}"
                )
            active_by_conflict[conflict_key] = memory_id
            holder = connection.execute(
                SQL_ACTIVE_CONFLICT_OTHER, (conflict_key, memory_id)
            ).fetchone()
            if holder is not None and holder["memory_id"] not in retired_actives:
                raise InvalidMemoryTransition(
                    "multiple active records for conflict key "
                    f"{conflict_key}: {holder['memory_id']}, {memory_id}"
                )
        return transaction

    def _map_integrity_error(
        self,
        connection: sqlite3.Connection,
        transaction: MemoryTransaction,
    ) -> MemoryError:
        """Translate a rolled-back IntegrityError from committed state.

        Runs after the write transaction was rolled back, so the diagnostic
        queries observe only committed rows and never the failed attempt.
        """
        duplicate = connection.execute(SQL_TX_BY_ID, (transaction.tx_id,)).fetchone()
        if duplicate is not None:
            return DuplicateMemoryIdempotencyKey(f"duplicate transaction id {transaction.tx_id}")
        if transaction.source_batch:
            for operation in transaction.operations:
                record = operation.record
                if record.revision != 1:
                    continue
                row = connection.execute(
                    SQL_CREATION_KEY_OWNER,
                    (transaction.source_batch, record.content_hash),
                ).fetchone()
                if row is not None:
                    return DuplicateMemoryIdempotencyKey(
                        "duplicate idempotency key "
                        f"source_batch={transaction.source_batch} content_hash={record.content_hash}: "
                        f"{row['memory_id']}, {record.id}"
                    )
        for operation in transaction.operations:
            record = operation.record
            if record.status is not MemoryStatus.ACTIVE:
                continue
            row = connection.execute(
                SQL_ACTIVE_CONFLICT_OTHER, (record.conflict_key, record.id)
            ).fetchone()
            if row is not None:
                return InvalidMemoryTransition(
                    "multiple active records for conflict key "
                    f"{record.conflict_key}: {row['memory_id']}, {record.id}"
                )
        for operation in transaction.operations:
            record = operation.record
            row = connection.execute(SQL_CURRENT_REVISION, (record.id,)).fetchone()
            if row is not None:
                return MemoryRevisionConflict(
                    f"expected revision {row['revision']} for {record.id}, "
                    f"got {transaction.expected_revisions[record.id]}"
                )
        return MemoryWriteError(
            f"unexpected database constraint violation for transaction {transaction.tx_id}: nothing was written"
        )

    def _creation_key_record(
        self,
        connection: sqlite3.Connection,
        source_batch: str,
        content_hash: str,
    ) -> MemoryRecord | None:
        row = connection.execute(SQL_CREATION_KEY_RECORD, (source_batch, content_hash)).fetchone()
        return record_from_row(row)

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

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._open_read() as connection:
            row = connection.execute(SQL_CURRENT_RECORD_JSON_BY_ID, (memory_id,)).fetchone()
        return record_from_row(row)

    def get_current(self, memory_id: str, *, synchronize: bool = True) -> MemoryRecord | None:
        """Return the committed current record for *memory_id*.

        SQLite reads already observe committed state, so ``synchronize`` is
        kept for caller compatibility and no longer triggers a rebuild.
        """
        return self.get(memory_id)

    def revisions(self, memory_id: str) -> tuple[MemoryRecord, ...]:
        with self._open_read() as connection:
            rows = connection.execute(SQL_REVISIONS_BY_ID, (memory_id,)).fetchall()
        return tuple(record_from_row(row) for row in rows)

    def current_records(self, status: MemoryStatus | None = None) -> tuple[MemoryRecord, ...]:
        sql = SQL_CURRENT_RECORDS
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        sql += SQL_ORDER_BY_ID
        with self._open_read() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(record_from_row(row) for row in rows)

    def active_for_conflict_key(self, key: str) -> MemoryRecord | None:
        with self._open_read() as connection:
            row = connection.execute(SQL_ACTIVE_BY_CONFLICT_KEY, (key,)).fetchone()
        return record_from_row(row)

    def candidate_records(self) -> tuple[MemoryRecord, ...]:
        return self.current_records(MemoryStatus.CANDIDATE)

    def candidate_ids_for_source(self, source_batch: str) -> frozenset[str]:
        with self._open_read() as connection:
            rows = connection.execute(SQL_CANDIDATE_IDS_FOR_SOURCE, (source_batch,)).fetchall()
        return frozenset(row[0] for row in rows)

    def record_created_for(self, source_batch: str, content_hash: str) -> MemoryRecord | None:
        """Return the current record created by this source-batch/content pair.

        The creation mapping is written exactly once by the revision-1
        transaction and never moves, so this lookup is deterministic.
        """
        with self._open_read() as connection:
            return self._creation_key_record(connection, source_batch, content_hash)

    def record_ids_for_source(self, source_batch: str) -> frozenset[str]:
        """Return every record that ever carried a non-empty source batch."""
        with self._open_read() as connection:
            rows = connection.execute(SQL_IDS_FOR_SOURCE, (source_batch,)).fetchall()
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
        sql = SQL_RECALL_SELECT + scope_clause + SQL_RECALL_SUFFIX
        params.append(now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
        if requested_kinds:
            sql += f" AND kind IN ({','.join('?' for _ in requested_kinds)})"
            params.extend(kind.value for kind in requested_kinds)
        sql += SQL_ORDER_BY_ID
        with self._open_read() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(record_from_row(row) for row in rows)

    def transaction_log(
        self, *, limit: int = 20, tx_id: str | None = None
    ) -> tuple[MemoryTransaction, ...]:
        """Return recent transactions newest first, optionally for one tx id."""
        if tx_id is None:
            sql, params = SQL_TX_LOG_RECENT, [limit]
        else:
            sql, params = SQL_TX_LOG_BY_ID, [tx_id, limit]
        with self._open_read() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(MemoryTransaction.model_validate_json(row["transaction_json"]) for row in rows)

    def transaction_rows_in_range(
        self, first_tx_seq: int, last_tx_seq: int
    ) -> tuple[tuple[int, str], ...]:
        """Return ordered ``(tx_seq, transaction_json)`` rows for a seq window.

        Consumed by the audit exporter (design section 12): raw row JSON only,
        never model reconstruction. A single SELECT observes one WAL snapshot,
        so the read never blocks concurrent writers.
        """
        with self._open_read() as connection:
            rows = connection.execute(SQL_TX_ROWS_IN_RANGE, (first_tx_seq, last_tx_seq)).fetchall()
        return tuple((row["tx_seq"], row["transaction_json"]) for row in rows)

    def storage_stats(self) -> MemoryStorageStats:
        with self._open_read() as connection:
            transaction_count = connection.execute(SQL_COUNT_TRANSACTIONS).fetchone()[0]
            revision_count = connection.execute(SQL_COUNT_REVISIONS).fetchone()[0]
            current_count = connection.execute(SQL_COUNT_CURRENT).fetchone()[0]
            last_seq = connection.execute(SQL_MAX_TX_SEQ).fetchone()[0]
            meta = connection.execute(SQL_AUDIT_EXPORTED_SEQ).fetchone()
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
