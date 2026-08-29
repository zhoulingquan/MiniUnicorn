"""SQLite online backup, verification and explicit restore (design section 13).

``MemoryBackupManager`` creates consistent, integrity-verified snapshots of
the fact database with the SQLite online backup API
(``source.backup(destination)``) — never a file copy of the running WAL
database — and restores a validated snapshot back into the live connection.

Restore protocol:

1. Parse the backup id; only canonical files under ``backups/`` are accepted.
2. Validate the snapshot read-only: ``user_version``, ``integrity_check`` and
   ``foreign_key_check``.
3. Acquire ``memory-maintenance.lock`` (same lock as migration/audit swap).
4. Backup the pre-restore database via the backup API into
   ``recovery/<UTC>/memory-before-restore.db``.
5. Copy the snapshot into the live connection via the backup API and
   checkpoint the WAL.
6. Re-run quick checks and refresh repository health.
7. Rebuild the audit export from the restored database.
8. Return the safety backup id and the restored transaction seq.

A failure at any point raises :class:`MemoryBackupError` and keeps the live
database and any safety backup in place; nothing is ever deleted on failure.

Normative sources:
docs/superpowers/specs/2026-08-14-sqlite-memory-storage-design.md
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from loguru import logger
from pydantic import BaseModel, ConfigDict

from miniunicorn.agent.memory_audit_export import MemoryAuditExporter
from miniunicorn.agent.memory_models import MemoryError
from miniunicorn.agent.memory_sqlite_schema import SCHEMA_VERSION, connect_memory_db
from miniunicorn.utils.helpers import ensure_dir

_BACKUP_DIR = "backups"
_RECOVERY_DIR = "recovery"
_MAINTENANCE_LOCK_FILE = "memory-maintenance.lock"
_SAFETY_BACKUP_FILE = "memory-before-restore.db"
_STAMP_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
_BACKUP_NAME_RE = re.compile(
    r"^memory-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-\d+(?:-[0-9a-f]{8})?\.db$"
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_uri(path: Path) -> str:
    """A ``file:`` URI that opens *path* read-only on the filesystem."""
    normalized = str(path.resolve()).replace(os.sep, "/")
    return "file:" + quote(normalized, safe="/:")


class MemoryBackupError(MemoryError):
    """Backup or restore failed; the live database and prior backups stay intact."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{message} (code={code})")


class BackupResult(BaseModel):
    """One integrity-verified database snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backup_id: str
    path: Path
    created_at: datetime
    last_transaction_seq: int
    sha256: str


class RestoreResult(BaseModel):
    """Outcome of one restore: the safety copy and the restored watermark."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    safety_backup_id: str
    safety_backup_path: Path
    restored_tx_seq: int


class MemoryBackupManager:
    """Create verified snapshots of ``memory.db`` and restore them safely."""

    def __init__(self, repository):
        self._repository = repository
        self.backups_dir = repository.structured_dir / _BACKUP_DIR
        self.recovery_dir = repository.structured_dir / _RECOVERY_DIR
        self.lock_path = repository.structured_dir / _MAINTENANCE_LOCK_FILE
        self._assert_contained(self.backups_dir)
        self._assert_contained(self.recovery_dir)

    # -- public API ---------------------------------------------------------

    def create_backup(self) -> BackupResult:
        """Create a consistent snapshot with the SQLite backup API and verify it.

        The snapshot is written through ``source.backup(destination)`` into a
        standalone ``journal_mode=DELETE`` copy, then verified with
        ``integrity_check`` / ``foreign_key_check`` / ``user_version`` before
        its SHA-256 is reported. An unverifiable copy is deleted and raises
        :class:`MemoryBackupError`; the live database is never touched.
        """
        repository = self._repository
        stats = repository.storage_stats()
        now = datetime.now(timezone.utc)
        ensure_dir(self.backups_dir)
        path = self._unique_backup_path(now, stats.last_transaction_seq)
        try:
            with connect_memory_db(
                repository.database_path, lock_timeout_s=repository.lock_timeout_s
            ) as source:
                with closing(sqlite3.connect(path)) as destination:
                    destination.execute("PRAGMA journal_mode=DELETE")
                    source.backup(destination)
            self._verify_snapshot(path)
        except MemoryBackupError:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise MemoryBackupError("backup_failed", f"backup failed: {exc}") from exc
        sha256 = _file_sha256(path)
        self._restrict_permissions(path)
        logger.info(
            "memory_backup_created path={} tx_seq={} sha256={}",
            path,
            stats.last_transaction_seq,
            sha256,
        )
        return BackupResult(
            backup_id=path.name,
            path=path,
            created_at=now,
            last_transaction_seq=stats.last_transaction_seq,
            sha256=sha256,
        )

    def restore_backup(self, backup_id: str) -> RestoreResult:
        """Restore a validated snapshot, keeping a pre-restore safety backup.

        The snapshot must be a canonical file under ``backups/`` and pass
        read-only verification before anything is touched. Under the
        maintenance lock the pre-restore database is backed up via the backup
        API into ``recovery/<UTC>/memory-before-restore.db``, the snapshot is
        copied into the live connection, quick checks run, and repository
        health refreshes. Finally the audit export is fully rebuilt.
        """
        repository = self._repository
        backup_path = self._resolve_backup_path(backup_id)
        self._verify_snapshot(backup_path)
        try:
            with FileLock(str(self.lock_path), timeout=repository.lock_timeout_s):
                safety_backup_id, safety_backup_path = self._create_safety_backup()
                self._copy_backup_into_live(backup_path)
                self._quick_check_live()
                health = repository.rebuild()
        except FileLockTimeout:
            raise MemoryBackupError(
                "lock_timeout", "memory-maintenance.lock timeout during restore"
            ) from None
        except MemoryBackupError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise MemoryBackupError("restore_failed", f"restore failed: {exc}") from exc
        self._rebuild_audit()
        logger.info(
            "memory_restore_completed backup={} safety={} tx_seq={}",
            backup_id,
            safety_backup_id,
            health.last_transaction_seq,
        )
        return RestoreResult(
            safety_backup_id=safety_backup_id,
            safety_backup_path=safety_backup_path,
            restored_tx_seq=health.last_transaction_seq,
        )

    # -- restore steps ------------------------------------------------------

    def _resolve_backup_path(self, backup_id: str) -> Path:
        """Resolve *backup_id* to a canonical snapshot file under ``backups/``."""
        if not backup_id or Path(backup_id).name != backup_id:
            raise MemoryBackupError("invalid_backup_id", f"invalid backup id: {backup_id!r}")
        if not _BACKUP_NAME_RE.fullmatch(backup_id):
            raise MemoryBackupError("invalid_backup_id", f"invalid backup id: {backup_id!r}")
        candidate = self.backups_dir / backup_id
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise MemoryBackupError(
                "invalid_backup_id", f"invalid backup id: {backup_id!r}"
            ) from exc
        root = self._repository.structured_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise MemoryBackupError(
                "invalid_backup_id", f"backup id escapes the structured dir: {backup_id!r}"
            )
        if resolved.parent != self.backups_dir.resolve():
            raise MemoryBackupError(
                "invalid_backup_id", f"backup id escapes the backups dir: {backup_id!r}"
            )
        if not resolved.is_file():
            raise MemoryBackupError("backup_not_found", f"no backup with id {backup_id}")
        return resolved

    def _create_safety_backup(self) -> tuple[str, Path]:
        """Back up the pre-restore database into ``recovery/<UTC>/`` via the backup API."""
        repository = self._repository
        stamp = _utc_stamp()
        recovery_dir = self.recovery_dir / stamp
        suffix = 2
        while recovery_dir.exists():
            recovery_dir = self.recovery_dir / f"{stamp}-{suffix}"
            suffix += 1
        ensure_dir(recovery_dir)
        safety_path = recovery_dir / _SAFETY_BACKUP_FILE
        try:
            with connect_memory_db(
                repository.database_path, lock_timeout_s=repository.lock_timeout_s
            ) as source:
                with closing(sqlite3.connect(safety_path)) as destination:
                    destination.execute("PRAGMA journal_mode=DELETE")
                    source.backup(destination)
            self._verify_snapshot(safety_path)
        except MemoryBackupError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise MemoryBackupError(
                "restore_failed", f"pre-restore safety backup failed: {exc}"
            ) from exc
        self._restrict_permissions(safety_path)
        backup_id = f"{_RECOVERY_DIR}/{recovery_dir.name}/{_SAFETY_BACKUP_FILE}"
        logger.info("memory_backup_created path={} tx_seq=pre-restore", safety_path)
        return backup_id, safety_path

    def _copy_backup_into_live(self, backup_path: Path) -> None:
        """Copy the snapshot into the live connection and checkpoint the WAL."""
        repository = self._repository
        with closing(sqlite3.connect(_read_only_uri(backup_path), uri=True)) as source:
            with closing(sqlite3.connect(repository.database_path)) as destination:
                source.backup(destination)
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _quick_check_live(self) -> None:
        """Re-verify the restored live database before health refresh."""
        repository = self._repository
        try:
            with closing(sqlite3.connect(repository.database_path)) as connection:
                self._assert_snapshot_valid(connection)
        except MemoryBackupError:
            raise
        except sqlite3.Error as exc:
            raise MemoryBackupError(
                "restore_failed", f"restored database check failed: {exc}"
            ) from exc

    def _rebuild_audit(self) -> None:
        """Rebuild the JSONL audit from the restored database."""
        try:
            MemoryAuditExporter(self._repository).rebuild()
        except MemoryError as exc:
            raise MemoryBackupError(
                "restore_failed", f"audit rebuild after restore failed: {exc}"
            ) from exc

    # -- snapshot verification ---------------------------------------------

    def _verify_snapshot(self, path: Path) -> None:
        try:
            with closing(sqlite3.connect(_read_only_uri(path), uri=True)) as connection:
                self._assert_snapshot_valid(connection)
        except MemoryBackupError:
            raise
        except sqlite3.Error as exc:
            raise MemoryBackupError(
                "invalid_backup", f"backup is not a readable sqlite database: {exc}"
            ) from exc

    @staticmethod
    def _assert_snapshot_valid(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise MemoryBackupError(
                "unsupported_schema_version",
                f"snapshot schema version {version}, expected {SCHEMA_VERSION}",
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MemoryBackupError(
                "integrity_error", f"snapshot integrity check failed: {integrity}"
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MemoryBackupError(
                "foreign_key_error",
                f"snapshot foreign key check failed with {len(violations)} violation(s)",
            )

    # -- helpers ------------------------------------------------------------

    def _unique_backup_path(self, now: datetime, tx_seq: int) -> Path:
        """Design layout ``backups/memory-<UTC>-<tx_seq>.db``; never overwrite."""
        base = f"memory-{now.strftime(_STAMP_FORMAT)}-{tx_seq}"
        candidate = self.backups_dir / f"{base}.db"
        if not candidate.exists():
            return candidate
        return self.backups_dir / f"{base}-{uuid.uuid4().hex[:8]}.db"

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        """Best effort: backups readable/writable by the current user only."""
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _assert_contained(self, path: Path) -> None:
        resolved = path.resolve()
        root = self._repository.structured_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise MemoryBackupError(
                "unsafe_backup_path", f"backup path escapes structured dir: {path}"
            )
