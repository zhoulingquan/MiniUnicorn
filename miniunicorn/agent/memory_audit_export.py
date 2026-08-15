"""Rebuildable segmented JSONL audit export derived from the SQLite fact database.

The transaction table in ``memory.db`` is the real-time audit source; the
``audit/`` directory is a derived, rebuildable materialization (design section
12). Full segments cover fixed tx_seq ranges of :data:`SEGMENT_SIZE` rows,
``journal-open.jsonl`` always holds the partial tail (less than one full
segment) and is rewritten from SQLite on every export, and ``manifest.json``
records each file's range, row count, SHA-256, and first/last tx id.

Data is only ever read through the repository's raw ``(tx_seq,
transaction_json)`` range query; no model reconstruction happens and no field
beyond the database rows enters the audit. Every file is written via a temp
file -> ``flush`` -> ``os.fsync`` -> ``os.replace`` sequence, so a crash at any
point leaves the previous audit directory readable and the committed facts
untouched. The watermark update is atomic in one ``BEGIN IMMEDIATE``
transaction and can never move backwards, so concurrent exporters neither
corrupt segments nor regress ``audit_exported_seq``.

Normative sources:
docs/superpowers/specs/2026-08-14-sqlite-memory-storage-design.md
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel, ConfigDict

from miniunicorn.agent.memory_models import MemoryError, RepositoryDegradedError
from miniunicorn.agent.memory_sqlite_schema import connect_memory_db
from miniunicorn.utils.helpers import ensure_dir

SEGMENT_SIZE = 10_000

_MANIFEST_SCHEMA_VERSION = 1
_OPEN_SEGMENT_FILE = "journal-open.jsonl"
_MANIFEST_FILE = "manifest.json"
_MAINTENANCE_LOCK_FILE = "memory-maintenance.lock"
_RECOVERY_DIR = "recovery"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AuditExportError(MemoryError):
    """The audit export or rebuild failed; committed facts are never affected."""


class AuditExportResult(BaseModel):
    """Outcome of one ``export_pending`` / ``rebuild`` audit pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exported_rows: int = 0
    sealed_segments: int = 0
    first_tx_seq: int = 0
    last_tx_seq: int = 0
    lag: int = 0


class MemoryAuditExporter:
    """Export and rebuild the segmented JSONL audit of the SQLite fact store."""

    def __init__(self, repository, *, segment_size: int = SEGMENT_SIZE):
        if segment_size <= 0:
            raise ValueError(f"segment_size must be positive, got {segment_size}")
        self._repository = repository
        self.segment_size = segment_size
        self.audit_dir = repository.structured_dir / "audit"
        self._assert_contained(self.audit_dir)

    # -- public API ---------------------------------------------------------

    def export_pending(self) -> AuditExportResult:
        """Export every transaction above the watermark; idempotent.

        Rows already covered by ``audit_exported_seq`` are never re-exported.
        Sealed segments and the open tail are written before the manifest, and
        the manifest before the atomic watermark advance. Any failure raises
        :class:`AuditExportError` while the database and the previous audit
        directory stay intact (lag grows, facts never roll back).
        """
        try:
            return self._export_pending_inner()
        except AuditExportError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise AuditExportError(f"audit export failed: {exc}") from exc

    def rebuild(self) -> AuditExportResult:
        """Rebuild the complete audit from the database and swap it in place.

        The full export is built in a unique sibling temp directory and
        verified before ``memory-maintenance.lock`` is acquired. Under the
        lock the existing ``audit/`` is atomically moved to
        ``recovery/<UTC>/audit``, the temp directory becomes ``audit/``, and
        ``audit_exported_seq`` advances. A failure at any step leaves the
        database untouched and the previous audit recoverable.
        """
        try:
            return self._rebuild_inner()
        except AuditExportError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise AuditExportError(f"audit rebuild failed: {exc}") from exc

    # -- export -------------------------------------------------------------

    def _export_pending_inner(self) -> AuditExportResult:
        self._require_healthy()
        watermark, max_seq = self._watermark_state()
        if watermark >= max_seq:
            return AuditExportResult(last_tx_seq=watermark, lag=0)
        ensure_dir(self.audit_dir)
        generated_at = _utc_now()
        segments, rows_written, first_exported = self._build_segments(
            self.audit_dir, watermark, max_seq
        )
        full_segments = self._merge_manifest_segments(
            self._previous_sealed_entries(self.audit_dir), segments
        )
        self._write_manifest(self.audit_dir, generated_at, max_seq, full_segments)
        if segments and watermark >= segments[0]["first_tx_seq"]:
            overlap = watermark - segments[0]["first_tx_seq"] + 1
            rows_written -= min(overlap, rows_written)
        final_watermark, lag = self._advance_watermark(max_seq)
        return AuditExportResult(
            exported_rows=rows_written,
            sealed_segments=sum(
                1 for entry in segments if entry["path"] != _OPEN_SEGMENT_FILE
            ),
            first_tx_seq=first_exported,
            last_tx_seq=final_watermark,
            lag=lag,
        )

    # -- rebuild ------------------------------------------------------------

    def _rebuild_inner(self) -> AuditExportResult:
        self._require_healthy()
        structured = self._repository.structured_dir
        self._assert_contained(structured)
        token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        temp_dir = structured / f"audit.exporting-{token}"
        ensure_dir(temp_dir)
        try:
            _, max_seq = self._watermark_state()
            generated_at = _utc_now()
            segments, rows_written, first_exported = self._build_segments(
                temp_dir, 0, max_seq
            )
            self._write_manifest(temp_dir, generated_at, max_seq, segments)
            self._verify_directory(temp_dir)
            final_watermark, lag = self._swap_audit_directory(temp_dir, max_seq)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return AuditExportResult(
            exported_rows=rows_written,
            sealed_segments=sum(
                1 for entry in segments if entry["path"] != _OPEN_SEGMENT_FILE
            ),
            first_tx_seq=first_exported,
            last_tx_seq=final_watermark,
            lag=lag,
        )

    def _swap_audit_directory(self, temp_dir: Path, exported_max: int) -> tuple[int, int]:
        audit = self.audit_dir
        self._assert_contained(audit)
        lock_path = self._repository.structured_dir / _MAINTENANCE_LOCK_FILE
        try:
            with FileLock(str(lock_path), timeout=self._repository.lock_timeout_s):
                if audit.exists():
                    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
                    recovered = (
                        self._repository.structured_dir / _RECOVERY_DIR / stamp / "audit"
                    )
                    ensure_dir(recovered.parent)
                    os.replace(audit, recovered)
                os.replace(temp_dir, audit)
        except FileLockTimeout:
            raise AuditExportError("memory-maintenance.lock timeout during audit rebuild") from None
        return self._advance_watermark(exported_max)

    # -- segment building ---------------------------------------------------

    def _build_segments(
        self, target_dir: Path, watermark: int, max_seq: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Write sealed segments above the watermark plus the open tail.

        A sealed segment is one complete period of ``segment_size`` rows with
        a deterministic ``journal-<first:012d>-<last:012d>.jsonl`` name; the
        open tail always holds ``(max_seq // segment_size) * segment_size + 1
        .. max_seq`` and is rebuilt from SQLite, so a just-completed period is
        retro-sealed and the stale tail is replaced (or unlinked when empty).
        """
        size = self.segment_size
        rows_written = 0
        first_exported = 0
        segments: list[dict[str, Any]] = []
        for period in range(watermark // size + 1, max_seq // size + 1):
            first = (period - 1) * size + 1
            last = period * size
            rows = self._read_rows(first, last)
            if len(rows) != size:
                raise AuditExportError(
                    f"sealed segment {first}..{last} has {len(rows)} rows, expected {size}"
                )
            path = target_dir / f"journal-{first:012d}-{last:012d}.jsonl"
            self._write_file_atomic(path, (row[1] for row in rows))
            segments.append(self._segment_entry(path, first, rows))
            if not first_exported:
                first_exported = first
            rows_written += size
        open_first = max_seq // size * size + 1
        if open_first <= max_seq:
            rows = self._read_rows(open_first, max_seq)
            if rows:
                path = target_dir / _OPEN_SEGMENT_FILE
                self._write_file_atomic(path, (row[1] for row in rows))
                segments.append(self._segment_entry(path, open_first, rows))
                if not first_exported:
                    first_exported = open_first
                rows_written += len(rows)
        else:
            (target_dir / _OPEN_SEGMENT_FILE).unlink(missing_ok=True)
        return segments, rows_written, first_exported

    def _segment_entry(self, path: Path, first: int, rows) -> dict[str, Any]:
        return {
            "path": path.name,
            "first_tx_seq": first,
            "last_tx_seq": first + len(rows) - 1,
            "rows": len(rows),
            "sha256": _file_sha256(path),
            "first_tx_id": json.loads(rows[0][1])["tx_id"],
            "last_tx_id": json.loads(rows[-1][1])["tx_id"],
        }

    def _previous_sealed_entries(self, target_dir: Path) -> list[dict[str, Any]]:
        """Reuse entries of already-sealed segments from the previous manifest.

        Sealed segments are immutable once written; only the open tail moves,
        so the previous manifest stays authoritative for everything it lists.
        A missing manifest simply means no sealed segments were recorded yet:
        a crash that lost the manifest never advanced the watermark, so the
        next export re-lists the same deterministic segments and self-heals.
        """
        manifest_path = target_dir / _MANIFEST_FILE
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [
            entry
            for entry in previous.get("segments", [])
            if entry.get("path") != _OPEN_SEGMENT_FILE
            and (target_dir / entry["path"]).exists()
        ]

    @staticmethod
    def _merge_manifest_segments(
        prior: list[dict[str, Any]], fresh: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Combine prior and freshly written segments, deduplicated by path.

        A prior entry and a fresh entry with the same path always describe the
        identical immutable bytes, so the fresh entry wins and each path
        appears exactly once, in deterministic order, even when two exporters
        race on the same plan.
        """
        fresh_paths = {entry["path"] for entry in fresh}
        return [*[entry for entry in prior if entry["path"] not in fresh_paths], *fresh]

    def _write_manifest(
        self, target_dir: Path, generated_at: str, max_seq: int, segments: list[dict]
    ) -> None:
        manifest = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "generated_at": generated_at,
            "database_last_tx_seq": max_seq,
            "segments": segments,
        }
        self._write_file_atomic(target_dir / _MANIFEST_FILE, [_canonical_json(manifest)])

    def _verify_directory(self, directory: Path) -> None:
        manifest = json.loads((directory / _MANIFEST_FILE).read_text(encoding="utf-8"))
        for entry in manifest["segments"]:
            path = directory / entry["path"]
            if not path.exists():
                raise AuditExportError(f"audit rebuild missing segment {entry['path']}")
            if entry["last_tx_seq"] != entry["first_tx_seq"] + entry["rows"] - 1:
                raise AuditExportError(f"audit rebuild range mismatch for {entry['path']}")
            if _file_sha256(path) != entry["sha256"]:
                raise AuditExportError(f"audit rebuild checksum mismatch for {entry['path']}")
            if _count_lines(path) != entry["rows"]:
                raise AuditExportError(f"audit rebuild row count mismatch for {entry['path']}")

    # -- file durability ----------------------------------------------------

    @staticmethod
    def _write_file_atomic(destination: Path, lines: Iterable[str]) -> None:
        temp_path = destination.with_name(
            f"{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            with open(temp_path, "w", encoding="utf-8", newline="\n") as stream:
                for line in lines:
                    stream.write(line)
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, destination)
        finally:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)

    # -- database state -----------------------------------------------------

    def _require_healthy(self) -> None:
        health = self._repository.health
        if health.state != "healthy":
            raise RepositoryDegradedError(
                f"memory database degraded (code={health.error_code}); audit export disabled"
            )

    def _read_rows(self, first: int, last: int) -> tuple[tuple[int, str], ...]:
        return self._repository.transaction_rows_in_range(first, last)

    def _watermark_state(self) -> tuple[int, int]:
        """Return ``(audit_exported_seq, max(tx_seq))`` from one snapshot."""
        with connect_memory_db(
            self._repository.database_path,
            lock_timeout_s=self._repository.lock_timeout_s,
        ) as connection:
            connection.execute("BEGIN")
            try:
                watermark = self._read_stored_watermark(connection)
                max_seq = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(tx_seq), 0) FROM memory_transactions"
                    ).fetchone()[0]
                )
            finally:
                connection.commit()
        return watermark, max_seq

    @staticmethod
    def _read_stored_watermark(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM storage_meta WHERE key = 'audit_exported_seq'"
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    def _advance_watermark(self, exported_max: int) -> tuple[int, int]:
        """Advance the watermark atomically; it can never move backwards.

        Runs in one ``BEGIN IMMEDIATE`` transaction: the stored watermark and
        the current database maximum are re-read under the write lock, so a
        concurrent exporter or a write that committed during the file phase
        can never cause a regression or claim rows that were not exported.
        """
        with connect_memory_db(
            self._repository.database_path,
            lock_timeout_s=self._repository.lock_timeout_s,
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored = self._read_stored_watermark(connection)
            db_max = int(
                connection.execute(
                    "SELECT COALESCE(MAX(tx_seq), 0) FROM memory_transactions"
                ).fetchone()[0]
            )
            target = max(stored, min(exported_max, db_max))
            if target != stored:
                connection.execute(
                    "INSERT OR REPLACE INTO storage_meta(key, value) VALUES "
                    "('audit_exported_seq', ?)",
                    (str(target),),
                )
            connection.commit()
        return target, int(db_max) - target

    # -- containment --------------------------------------------------------

    def _assert_contained(self, path: Path) -> None:
        resolved = path.resolve()
        root = self._repository.structured_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise AuditExportError(f"audit export path escapes structured dir: {path}")


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as stream:
        return sum(1 for _ in stream)
