"""One-time, verifiable migration of the legacy JSONL journal into SQLite.

The legacy ``journal.jsonl`` is read-only input (design section 11): it is
parsed strictly as canonical :class:`MemoryTransaction` lines, imported
through the repository's migration-only validated entry into a temporary
database, verified with ``PRAGMA integrity_check`` and
``PRAGMA foreign_key_check``, and installed atomically as ``memory.db``
together with the migration manifest ``storage-migration-v2.json``.

The journal is never modified. Any failure deletes the temporary files, leaves
no ``memory.db`` and no completed manifest, and raises
:class:`LegacyJournalImportError` carrying the failing line number and a
stable error code (never the record evidence).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ConfigDict, ValidationError

from miniunicorn.agent.memory_models import (
    DuplicateMemoryIdempotencyKey,
    InvalidMemoryTransition,
    MemoryRevisionConflict,
    MemoryTransaction,
    MemoryWriteError,
    TagCatalog,
    UnknownMemoryTag,
    transaction_checksum,
)
from miniunicorn.agent.memory_sqlite_schema import connect_memory_db, initialize_schema

_JOURNAL_FILE = "journal.jsonl"
_MANIFEST_FILE = "storage-migration-v2.json"
_MANIFEST_SCHEMA_VERSION = 1
_IMPORTING_GLOB = "memory.db.importing*"


class JsonlImportResult(BaseModel):
    """Outcome of a legacy journal migration attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    migrated: bool
    transaction_count: int = 0
    source_sha256: str = ""
    source_bytes: int = 0


class LegacyJournalImportError(MemoryError):
    """One legacy journal could not be imported.

    ``line_number`` is the 1-based journal line that caused the failure, or
    ``None`` for failures outside any single line (for example the atomic
    ``os.replace`` switch). ``code`` is a stable machine-readable error code;
    the message never echoes record evidence.
    """

    def __init__(self, line_number: int | None, code: str) -> None:
        self.line_number = line_number
        self.code = code
        if line_number is None:
            super().__init__(f"legacy journal import failed (code={code})")
        else:
            super().__init__(f"legacy journal import failed at line {line_number} (code={code})")


def iter_legacy_transactions(path: Path) -> Iterator[tuple[int, MemoryTransaction]]:
    """Yield ``(line_number, transaction)`` for every non-empty journal line.

    Strict by design: bad non-empty lines are never skipped or json-repaired —
    an invalid or checksum-mismatched line raises
    :class:`LegacyJournalImportError` immediately.
    """
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                transaction = MemoryTransaction.model_validate(value)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise LegacyJournalImportError(line_number, "invalid_transaction") from exc
            if transaction_checksum(transaction) != transaction.checksum_sha256:
                raise LegacyJournalImportError(line_number, "checksum_mismatch")
            yield line_number, transaction


def migrate_legacy_journal(workspace: Path, lock_timeout_s: float = 5.0) -> JsonlImportResult:
    """Migrate the legacy journal into a fresh ``memory.db`` exactly once.

    Idempotent: returns ``migrated=False`` when the database already exists or
    the journal is missing/empty. On success the journal bytes are unchanged,
    ``memory.db`` and a completed ``storage-migration-v2.json`` manifest are
    installed atomically via ``os.replace``, and stale
    ``memory.db.importing*`` residue from interrupted runs is removed first.
    """
    structured_dir = Path(workspace) / "memory" / "structured"
    database_path = structured_dir / "memory.db"
    journal_path = structured_dir / _JOURNAL_FILE
    manifest_path = structured_dir / _MANIFEST_FILE

    if database_path.exists():
        return JsonlImportResult(migrated=False)
    try:
        if not journal_path.exists() or journal_path.stat().st_size == 0:
            return JsonlImportResult(migrated=False)
    except OSError:
        return JsonlImportResult(migrated=False)

    for residue in structured_dir.glob(_IMPORTING_GLOB):
        try:
            residue.unlink()
        except OSError:
            pass

    original_bytes = journal_path.read_bytes()
    source_sha256 = hashlib.sha256(original_bytes).hexdigest()
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    temp_db = structured_dir / f"memory.db.importing-{token}"
    temp_manifest = structured_dir / f"{_MANIFEST_FILE}.importing-{token}"

    logger.info("memory_storage_migration_started path={}", journal_path)
    failure: LegacyJournalImportError | None = None
    try:
        transaction_count = _import_into_temp_db(
            temp_db, journal_path, structured_dir, lock_timeout_s
        )
        current_count, counts_by_status, current_state_sha256 = _verify_temp_database(
            temp_db, lock_timeout_s
        )
        _fsync_file(temp_db)
        manifest = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "status": "completed",
            "source_sha256": source_sha256,
            "source_bytes": len(original_bytes),
            "transaction_count": transaction_count,
            "current_count": current_count,
            "counts_by_status": counts_by_status,
            "current_state_sha256": current_state_sha256,
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        temp_manifest.write_text(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        _fsync_file(temp_manifest)
        os.replace(temp_db, database_path)
        os.replace(temp_manifest, manifest_path)
    except LegacyJournalImportError as exc:
        failure = exc
    except (OSError, sqlite3.Error):
        failure = LegacyJournalImportError(None, "replace_failed")
    _cleanup_temp_files(temp_db, temp_manifest)
    if failure is not None:
        raise failure
    logger.info(
        "memory_storage_migration_completed txns={} sha256={}",
        transaction_count,
        source_sha256,
    )
    return JsonlImportResult(
        migrated=True,
        transaction_count=transaction_count,
        source_sha256=source_sha256,
        source_bytes=len(original_bytes),
    )


def _import_into_temp_db(
    temp_db: Path, journal_path: Path, structured_dir: Path, lock_timeout_s: float
) -> int:
    """Import every journal line into the temporary database, one validated
    transaction per ``BEGIN IMMEDIATE``..``COMMIT``."""
    with connect_memory_db(temp_db, lock_timeout_s=lock_timeout_s) as connection:
        initialize_schema(connection)
    tags_path = structured_dir / "tags.json"
    try:
        tag_catalog = TagCatalog.load(tags_path)
    except (OSError, ValidationError):
        logger.warning("memory_tags_unreadable path={}", tags_path)
        tag_catalog = TagCatalog()
    repository = _build_import_repository(
        structured_dir.parent.parent, temp_db, lock_timeout_s, tag_catalog
    )
    count = 0
    for line_number, transaction in iter_legacy_transactions(journal_path):
        try:
            repository.append_imported_transaction(transaction)
        except (
            MemoryWriteError,
            MemoryRevisionConflict,
            DuplicateMemoryIdempotencyKey,
            InvalidMemoryTransition,
            UnknownMemoryTag,
        ) as exc:
            raise LegacyJournalImportError(line_number, _import_error_code(exc)) from exc
        except sqlite3.Error as exc:
            raise LegacyJournalImportError(line_number, "sqlite_error") from exc
        count += 1
    return count


def _build_import_repository(
    workspace: Path, temp_db_path: Path, lock_timeout_s: float, tag_catalog: TagCatalog
):
    """Migration-only repository instance bound to the temporary database.

    Bypasses the constructor on purpose: the startup path would recurse into
    migration, and the import entry touches only the attributes wired here
    (``database_path``, ``lock_timeout_s``, ``_tag_catalog``). Kept private to
    this module; never used for runtime writes.
    """
    from miniunicorn.agent.memory_repository import StructuredMemoryRepository

    repository = object.__new__(StructuredMemoryRepository)
    repository.workspace = Path(workspace)
    repository.structured_dir = repository.workspace / "memory" / "structured"
    repository.database_path = Path(temp_db_path)
    repository.lock_timeout_s = lock_timeout_s
    repository._tag_catalog = tag_catalog
    return repository


def _verify_temp_database(
    temp_db: Path, lock_timeout_s: float
) -> tuple[int, dict[str, int], str]:
    """Verify integrity, compute the canonical current-state digest and counts."""
    with connect_memory_db(temp_db, lock_timeout_s=lock_timeout_s) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise LegacyJournalImportError(None, "integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise LegacyJournalImportError(None, "foreign_key_check_failed")
        counts_by_status = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM memory_revisions "
                "WHERE is_current = 1 GROUP BY status"
            )
        }
        current_count = sum(counts_by_status.values())
        current_state_sha256 = _current_state_sha256(connection)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return current_count, counts_by_status, current_state_sha256


def _current_state_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT record_json FROM memory_revisions WHERE is_current = 1 ORDER BY memory_id ASC"
    ):
        digest.update(row["record_json"].encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _import_error_code(exc: Exception) -> str:
    if isinstance(exc, UnknownMemoryTag):
        return "unknown_tag"
    if isinstance(exc, MemoryRevisionConflict):
        return "revision_conflict"
    if isinstance(exc, DuplicateMemoryIdempotencyKey):
        return "duplicate_idempotency_key"
    if isinstance(exc, InvalidMemoryTransition):
        return "invalid_transition"
    return "write_error"


def _fsync_file(path: Path) -> None:
    """Flush a file to disk; open with write access because Windows ``fsync``
    cannot flush a read-only handle."""
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _cleanup_temp_files(temp_db: Path, temp_manifest: Path) -> None:
    for path in (
        temp_db,
        temp_manifest,
        Path(str(temp_db) + "-wal"),
        Path(str(temp_db) + "-shm"),
    ):
        try:
            path.unlink()
        except OSError:
            pass
