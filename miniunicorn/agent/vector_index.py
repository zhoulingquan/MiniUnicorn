"""Provenance-aware vector index over ``memory/memory.db`` (version 2 schema).

Owns the sqlite-vec virtual table, the source metadata rows, and the pinned
model fingerprint. Incremental reconcile (Task 8) and atomic rebuild (Task 9)
build on this module; compatibility methods for the pre-Task-11 callers
(``enabled``, ``index``, legacy ``search``, ``count``, ``decay_importance``,
``archive_low_importance``) are marked ``COMPAT`` and removed in Task 11.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import struct
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from filelock import FileLock, Timeout
from loguru import logger

from miniunicorn.agent.memory_sources import MemorySourceCatalog, MemorySourceRecord, SourceScan
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION
from miniunicorn.embedding.types import IndexStatus

SCHEMA_VERSION = "2"

UpsertAction = Literal["inserted", "updated", "unchanged"]

_EMBED_BATCH_SIZE = 32

_REBUILD_LOCK_TIMEOUT = 60


@dataclass(frozen=True)
class IndexFingerprint:
    schema_version: str = SCHEMA_VERSION
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    vector_dimension: int = MODEL_DIMENSION


@dataclass(frozen=True)
class IndexCandidate:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    content_hash: str
    text: str
    importance: float
    metadata: dict[str, object]
    similarity: float
    updated_at: str


@dataclass(frozen=True)
class ReconcileReport:
    discovered: int
    inserted: int
    updated: int
    unchanged: int
    inactive: int
    invalid: int
    failed: int
    failures: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    checks: tuple[dict[str, object], ...] = ()
    message: str = ""


@dataclass(frozen=True)
class RebuildReport:
    state: Literal["ready", "failed", "cancelled"]
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    inactive: int = 0
    invalid: int = 0
    failed_count: int = 0
    validated: bool = False
    backup: str | None = None
    message: str = ""

    @classmethod
    def ready(cls, report: ReconcileReport, validation: ValidationReport, backup: str | None) -> "RebuildReport":
        return cls(
            state="ready",
            discovered=report.discovered,
            inserted=report.inserted,
            updated=report.updated,
            unchanged=report.unchanged,
            inactive=report.inactive,
            invalid=report.invalid,
            failed_count=report.failed,
            validated=validation.ok,
            backup=backup,
            message=validation.message,
        )

    @classmethod
    def failed(cls, report: ReconcileReport, validation: ValidationReport) -> "RebuildReport":
        return cls(
            state="failed",
            discovered=report.discovered,
            inserted=report.inserted,
            updated=report.updated,
            unchanged=report.unchanged,
            inactive=report.inactive,
            invalid=report.invalid,
            failed_count=report.failed,
            validated=False,
            message=validation.message or "索引验证失败",
        )

    @classmethod
    def cancelled(cls) -> "RebuildReport":
        return cls(state="cancelled", message="rebuild 已取消")


def _utc_file_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _serialize_f32(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *(float(v) for v in vec))


def _utc_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class VectorIndexManager:
    """SQLite-backed vector index with a pinned model fingerprint.

    A fresh database is stamped with the fingerprint and created ready. An
    existing database whose fingerprint is missing or mismatched is left
    untouched: the manager reports ``stale`` and only allows read-only
    diagnostics. ``stale``/``failed`` states never search and never upsert.
    """

    def __init__(
        self,
        db_path: Path,
        model_id: str = MODEL_ID,
        model_revision: str = MODEL_REVISION,
        vector_dimension: int = MODEL_DIMENSION,
        *,
        create: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.model_id = model_id
        self.model_revision = model_revision
        self.vector_dimension = vector_dimension
        self._lock = threading.Lock()
        self._state: str = "failed"
        self._error_code: str | None = None
        self._message = ""
        self._conn: sqlite3.Connection | None = None
        if create:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
        self._init_db()

    # -- lifecycle ----------------------------------------------------------

    def _init_db(self) -> None:
        try:
            from miniunicorn.agent.vector_memory import _try_load_sqlite_vec

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            if not _try_load_sqlite_vec(self._conn):
                self._state = "failed"
                self._error_code = "dependency_missing"
                self._message = "sqlite-vec 扩展不可用"
                return
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if not self._fingerprint_matches():
                self._state = "stale"
                self._error_code = "model_mismatch"
                self._message = "fingerprint 与固定模型不匹配，索引只读"
                return
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    text TEXT NOT NULL,
                    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(
                    embedding float[512] distance=cosine
                );
                CREATE INDEX IF NOT EXISTS sources_active_type ON sources(active, source_type);
                """
            )
            self._conn.commit()
            self._state = "ready"
        except Exception:
            logger.exception("VectorIndexManager init failed; disabling")
            self._state = "failed"
            self._error_code = "index_corrupt"
            self._message = "索引初始化失败"

    def _fingerprint_matches(self) -> bool:
        assert self._conn is not None
        rows = {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM index_meta").fetchall()
        }
        expected = {
            "schema_version": SCHEMA_VERSION,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "vector_dimension": str(self.vector_dimension),
        }
        if not rows:
            self._conn.executemany(
                "INSERT INTO index_meta(key, value) VALUES (?, ?)",
                list(expected.items()),
            )
            self._conn.commit()
            return True
        for key, value in expected.items():
            if rows.get(key) != value:
                logger.warning(
                    "Vector index fingerprint mismatch for {}: key={!r} expected={!r} found={!r}",
                    self.db_path,
                    key,
                    value,
                    rows.get(key),
                )
                return False
        return True

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def status(self) -> IndexStatus:
        size = 0
        try:
            size = self.db_path.stat().st_size
        except OSError:
            pass
        return IndexStatus(
            state=self._state,  # type: ignore[arg-type]
            path=str(self.db_path),
            bytes=size,
            last_error_code=self._error_code,
            message=self._message,
        )

    # -- writable gate ------------------------------------------------------

    def _require_writable_ready(self) -> None:
        if self._state != "ready" or self._conn is None:
            raise RuntimeError(f"vector index not ready (state={self._state})")

    @property
    def enabled(self) -> bool:
        # COMPAT: pre-Task-11 callers (context.py / memory.py / recall.py).
        return self._state == "ready"

    def is_search_ready(self) -> bool:
        return self._state == "ready"

    def fallback_reason(self) -> str:
        if self._state == "ready":
            return ""
        if self._state == "stale":
            return "index_stale"
        if self._state == "failed":
            if self._error_code == "dependency_missing":
                return "dependency_missing"
            return "index_corrupt"
        return "index_missing"

    # -- source rows --------------------------------------------------------

    def upsert(self, record: MemorySourceRecord, embedding: Sequence[float]) -> UpsertAction:
        self._require_writable_ready()
        vector = self._validate_vector(embedding)
        assert self._conn is not None
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id, source_revision, content_hash, active FROM sources WHERE source_id=?",
                (record.source_id,),
            ).fetchone()
            if (
                existing
                and existing["source_revision"] == record.source_revision
                and existing["content_hash"] == record.content_hash
                and existing["active"]
            ):
                return "unchanged"
            row_id = self._upsert_source_row(record, existing)
            self._conn.execute("DELETE FROM vectors WHERE rowid=?", (row_id,))
            self._conn.execute(
                "INSERT INTO vectors(rowid, embedding) VALUES (?, ?)",
                (row_id, _serialize_f32(vector)),
            )
            return "updated" if existing else "inserted"

    def _upsert_source_row(self, record: MemorySourceRecord, existing: sqlite3.Row | None) -> int:
        assert self._conn is not None
        now = _utc_now()
        metadata_json = json.dumps(record.metadata, ensure_ascii=False)
        if existing:
            self._conn.execute(
                "UPDATE sources SET source_type=?, source_file=?, source_revision=?, "
                "content_hash=?, text=?, importance=?, active=?, metadata_json=?, updated_at=? "
                "WHERE id=?",
                (
                    record.source_type,
                    record.source_file,
                    record.source_revision,
                    record.content_hash,
                    record.text,
                    float(record.importance),
                    int(record.active),
                    metadata_json,
                    now,
                    existing["id"],
                ),
            )
            return int(existing["id"])
        cur = self._conn.execute(
            "INSERT INTO sources(source_id, source_type, source_file, source_revision, "
            "content_hash, text, importance, active, metadata_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.source_id,
                record.source_type,
                record.source_file,
                record.source_revision,
                record.content_hash,
                record.text,
                float(record.importance),
                int(record.active),
                metadata_json,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def _validate_vector(self, embedding: Sequence[float]) -> list[float]:
        vector = [float(v) for v in embedding]
        if len(vector) != self.vector_dimension:
            raise ValueError(
                f"dimension mismatch: got {len(vector)}, expected {self.vector_dimension}"
            )
        if not all(v == v and abs(v) != float("inf") for v in vector):  # finite check
            raise ValueError("embedding contains non-finite values")
        return vector

    def mark_inactive_except(self, source_ids: set[str]) -> int:
        self._require_writable_ready()
        assert self._conn is not None
        with self._lock, self._conn:
            if not source_ids:
                cur = self._conn.execute("UPDATE sources SET active=0")
            else:
                placeholders = ",".join("?" * len(source_ids))
                cur = self._conn.execute(
                    f"UPDATE sources SET active=0 WHERE source_id NOT IN ({placeholders})",
                    tuple(source_ids),
                )
            return int(cur.rowcount)

    async def reconcile(
        self,
        scan: SourceScan,
        embedder: Any,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ReconcileReport:
        """Diff the authoritative scan against the index, embed only what
        changed, upsert in single transactions, and mark missing rows inactive.

        Embedding runs in batches of at most 32; a failed or short batch is
        reported and skipped without blocking other batches. A set
        ``cancel_event`` aborts between batches with ``asyncio.CancelledError``.
        """
        current = self.source_fingerprints()
        changed = [
            row
            for row in scan.records
            if row.active and current.get(row.source_id) != (row.source_revision, row.content_hash, True)
        ]
        inserted = updated = 0
        failed = 0
        failures: list[dict[str, str]] = []
        total = len(changed)
        completed = 0
        for start in range(0, total, _EMBED_BATCH_SIZE):
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            batch = changed[start : start + _EMBED_BATCH_SIZE]
            result = await embedder.embed([row.text for row in batch])
            if result.failure is not None:
                failed += len(batch)
                failures.append({"code": result.failure.code, "message": result.failure.message})
            elif len(result.vectors) != len(batch):
                failed += len(batch)
                failures.append(
                    {"code": "inference_failed", "message": "embedding 数量与输入不匹配"}
                )
            else:
                for record, vector in zip(batch, result.vectors, strict=True):
                    action = self.upsert(record, vector)
                    inserted += action == "inserted"
                    updated += action == "updated"
            completed += len(batch)
            if progress is not None:
                progress(completed, total, "嵌入中")
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        active_ids = {record.source_id for record in scan.records if record.active}
        inactive = self.mark_inactive_except(active_ids)
        return ReconcileReport(
            discovered=len(scan.records),
            inserted=inserted,
            updated=updated,
            unchanged=len(scan.records) - len(changed),
            inactive=inactive,
            invalid=len(scan.errors),
            failed=failed,
            failures=tuple(failures),
        )

    def source_fingerprints(self) -> dict[str, tuple[str, str, bool]]:
        if self._state != "ready" or self._conn is None:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, source_revision, content_hash, active FROM sources"
            ).fetchall()
        return {r["source_id"]: (r["source_revision"], r["content_hash"], bool(r["active"])) for r in rows}

    def count_sources(self) -> int:
        if self._state != "ready" or self._conn is None:
            return 0
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM sources").fetchone()
        return int(row[0]) if row else 0

    def count_active_sources(self) -> int:
        if self._state != "ready" or self._conn is None:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sources WHERE active=1"
            ).fetchone()
        return int(row[0]) if row else 0

    def get_source(self, source_id: str) -> MemorySourceRecord | None:
        if self._state != "ready" or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
        if row is None:
            return None
        return MemorySourceRecord(
            source_id=row["source_id"],
            source_type=row["source_type"],
            source_file=row["source_file"],
            source_revision=row["source_revision"],
            content_hash=row["content_hash"],
            text=row["text"],
            importance=float(row["importance"]),
            active=bool(row["active"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    # -- search -------------------------------------------------------------

    def search(
        self,
        query_embedding: Sequence[float],
        limit: int = 5,
        k: int | None = None,
        kind: str | None = None,
    ) -> list[IndexCandidate] | list[dict[str, Any]]:
        """Return top-*limit* active candidates by cosine similarity.

        ``k``/``kind`` are COMPAT legacy parameters (pre-Task-11 callers);
        when ``k`` is provided the result is the legacy list of dicts.
        """
        if self._state != "ready" or self._conn is None:
            return []
        if len(query_embedding) != self.vector_dimension:
            return []
        blob = _serialize_f32(query_embedding)
        try:
            # Legacy ``kind`` filter pushed into SQL: filtering in Python after
            # a small fetch pool under-fills results when few candidates of
            # the requested kind exist (the pool is then exhausted by other
            # kinds and ``[:k]`` comes back short).
            params: list[Any] = [blob]
            kind_clause = ""
            if kind:
                kind_clause = " AND s.source_type = ?"
                params.append(kind)
            params.append(max(limit * 3, k or limit))
            with self._lock:
                rows = self._conn.execute(
                    "SELECT v.rowid, v.distance, s.source_id, s.source_type, s.source_file, "
                    "s.source_revision, s.content_hash, s.text, s.importance, s.metadata_json, "
                    "s.updated_at FROM vectors v JOIN sources s ON s.id = v.rowid "
                    "WHERE v.embedding MATCH ? AND s.active = 1 AND k = ?"
                    + kind_clause
                    + " ORDER BY v.distance",
                    params,
                ).fetchall()
            candidates = [
                IndexCandidate(
                    source_id=r["source_id"],
                    source_type=r["source_type"],
                    source_file=r["source_file"],
                    source_revision=r["source_revision"],
                    content_hash=r["content_hash"],
                    text=r["text"],
                    importance=float(r["importance"]),
                    metadata=json.loads(r["metadata_json"] or "{}"),
                    similarity=1.0 - float(r["distance"]),
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]
            if k is not None:
                return [
                    {
                        "id": 0,
                        "kind": c.source_type,
                        "text": c.text,
                        "metadata": c.metadata,
                        "importance": c.importance,
                        "created_at": c.updated_at,
                        "similarity": c.similarity,
                        "score": c.similarity * (0.5 + 0.5 * c.importance),
                    }
                    for c in candidates
                ][:k]
            return sorted(candidates, key=lambda c: c.similarity, reverse=True)[:limit]
        except Exception:
            logger.exception("VectorIndexManager.search failed")
            return []

    # -- rebuild / validation --------------------------------------------------

    async def rebuild(
        self,
        catalog: MemorySourceCatalog,
        embedder: Any,
        *,
        cancel_event: asyncio.Event | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> RebuildReport:
        """Full rebuild into ``memory.db.rebuilding``, then atomic replace.

        The existing database is never touched until the temporary index
        passes validation; a pre-existing database is copied to a timestamped
        backup only after the replacement succeeds. A cross-process lock
        serializes CLI/gateway rebuilds (timeout -> ``failed``).
        """
        target = self.db_path
        rebuilding = target.with_name(target.name + ".rebuilding")
        lock = FileLock(str(target) + ".lock", timeout=_REBUILD_LOCK_TIMEOUT)
        try:
            with lock:
                rebuilding.unlink(missing_ok=True)
                temporary = VectorIndexManager(rebuilding, create=True)
                try:
                    scan = catalog.scan()
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError
                    report = await temporary.reconcile(
                        scan, embedder, progress=progress, cancel_event=cancel_event
                    )
                    validation = await temporary.validate(
                        embedder, expected_active=len(scan.records)
                    )
                    if report.failed or not validation.ok:
                        return RebuildReport.failed(report, validation)
                    temporary.close()
                    # 换库区间必须在 _lock 内完成:close→replace→_init_db 期间
                    # 并发的 search()/count() 可能读到已关闭的连接或半初始化的
                    # 状态,原子换库保证读者要么用旧库要么用新库。
                    with self._lock:
                        self.close()
                        backup: str | None = None
                        if target.exists():
                            backup_path = target.with_name(
                                f"{target.name}.backup.{_utc_file_stamp()}"
                            )
                            shutil.copy2(target, backup_path)
                            backup = str(backup_path)
                        os.replace(rebuilding, target)
                        self._state = "failed"
                        self._error_code = None
                        self._message = ""
                        self._conn = None
                        self._init_db()
                    return RebuildReport.ready(report, validation, backup)
                finally:
                    temporary.close()
                    rebuilding.unlink(missing_ok=True)
        except asyncio.CancelledError:
            return RebuildReport.cancelled()
        except Timeout:
            return RebuildReport(
                state="failed", message="获取索引重建锁超时", validated=False
            )
        except Exception:
            logger.exception("VectorIndexManager.rebuild failed")
            return RebuildReport(
                state="failed", message="索引重建失败", validated=False
            )

    async def validate(
        self,
        embedder: Any,
        *,
        expected_active: int | None = None,
    ) -> ValidationReport:
        """Validate fingerprint, row parity, vector blobs and a real embedding."""
        checks: list[dict[str, object]] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})

        if self._state != "ready" or self._conn is None:
            check("fingerprint", False, f"索引状态为 {self._state}")
            return ValidationReport(ok=False, checks=tuple(checks), message="索引未就绪")

        expected = {
            "schema_version": SCHEMA_VERSION,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "vector_dimension": str(self.vector_dimension),
        }
        rows = {
            r["key"]: r["value"]
            for r in self._conn.execute("SELECT key, value FROM index_meta").fetchall()
        }
        check("fingerprint", rows == expected, str(rows))
        active = self.count_active_sources()
        if expected_active is not None:
            check("active_count", active == expected_active, f"{active} != {expected_active}")
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.source_id, s.text, v.embedding FROM sources s "
                "LEFT JOIN vectors v ON v.rowid = s.id WHERE s.active=1"
            ).fetchall()
        blobs = [row["embedding"] for row in rows]
        for row in rows:
            if row["embedding"] is None:
                check("vector_parity", False, f"source {row['source_id']} 没有 vector row")
        expected_blob_len = self.vector_dimension * 4
        finite = True
        for blob in blobs:
            if blob is None:
                continue
            if len(blob) != expected_blob_len:
                finite = False
                check("blob_length", False, f"{len(blob)} != {expected_blob_len}")
                break
        if finite:
            for blob in blobs:
                if blob is None:
                    continue
                values = struct.unpack(f"{len(blob) // 4}f", blob)
                if not all(v == v and abs(v) != float("inf") for v in values):
                    check("finite", False, "向量含非有限值")
                    break

        ok = all(item["ok"] for item in checks)
        if ok and blobs:
            first = rows[0]
            result = await embedder.embed([first["text"]])
            if result.failure is None and result.vectors:
                found = self.search(result.vectors[0], limit=5)
                hit = any(
                    isinstance(c, dict) and c.get("source_id") == first["source_id"]
                    or not isinstance(c, dict) and c.source_id == first["source_id"]
                    for c in found
                )
                check("embedding_roundtrip", hit, first["source_id"])
        ok = all(item["ok"] for item in checks)
        return ValidationReport(
            ok=ok, checks=tuple(checks), message="" if ok else "索引验证未通过"
        )

    # -- COMPAT: legacy maintenance/callers (removed in Task 11) -------------

    def index(
        self,
        text: str,
        embedding: list[float],
        kind: str = "history",
        metadata: dict[str, Any] | None = None,
        importance: float = 0.5,
    ) -> int | None:
        """COMPAT: index a free-form entry under a generated source id."""
        if not self.enabled or not text:
            return None
        try:
            record = MemorySourceRecord(
                source_id=f"auto:{uuid.uuid4()}",
                source_type=kind,
                source_file="memory/dynamic.jsonl",
                source_revision="1",
                content_hash=sha256(text.encode("utf-8")).hexdigest(),
                text=text,
                importance=float(importance),
                active=True,
                metadata=dict(metadata or {}),
            )
            self.upsert(record, embedding)
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT id FROM sources WHERE source_id=?", (record.source_id,)
            ).fetchone()
            return int(row["id"]) if row is not None else None
        except Exception:
            logger.exception("VectorIndexManager.index failed")
            return None

    def count(self, kind: str | None = None) -> int:
        if self._conn is None:
            return 0
        try:
            with self._lock:
                if kind:
                    cur = self._conn.execute(
                        "SELECT COUNT(*) FROM sources WHERE source_type=?", (kind,)
                    )
                else:
                    cur = self._conn.execute("SELECT COUNT(*) FROM sources")
                return int(cur.fetchone()[0])
        except Exception:
            return 0

    def decay_importance(self, days_threshold: int = 30, decay_factor: float = 0.9) -> int:
        if self._conn is None:
            return 0
        try:
            with self._lock:
                cutoff = (datetime.now().replace(day=1)).strftime("%Y-%m-%d %H:%M:%S")
                cur = self._conn.execute(
                    "UPDATE sources SET importance = importance * ? "
                    "WHERE created_at < ? AND importance > 0.1",
                    (decay_factor, cutoff),
                )
                self._conn.commit()
                return int(cur.rowcount)
        except Exception:
            logger.exception("decay_importance failed")
            return 0

    def archive_low_importance(self, threshold: float = 0.2, min_age_days: int = 60) -> int:
        if self._conn is None:
            return 0
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id FROM sources WHERE importance < ?", (threshold,)
                ).fetchall()
                ids = [int(r[0]) for r in rows]
                if not ids:
                    return 0
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"DELETE FROM vectors WHERE rowid IN ({placeholders})", ids
                )
                self._conn.execute(
                    f"DELETE FROM sources WHERE id IN ({placeholders})", ids
                )
                self._conn.commit()
                return len(ids)
        except Exception:
            logger.exception("archive_low_importance failed")
            return 0
