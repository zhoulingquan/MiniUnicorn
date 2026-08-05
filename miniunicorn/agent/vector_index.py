"""Provenance-aware vector index over ``memory/memory.db`` (version 2 schema).

Owns the sqlite-vec virtual table, the source metadata rows, and the pinned
model fingerprint. Incremental reconcile (Task 8) and atomic rebuild (Task 9)
build on this module; compatibility methods for the pre-Task-11 callers
(``enabled``, ``index``, legacy ``search``, ``count``, ``decay_importance``,
``archive_low_importance``) are marked ``COMPAT`` and removed in Task 11.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Sequence

from loguru import logger

from miniunicorn.agent.memory_sources import MemorySourceRecord
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION
from miniunicorn.embedding.types import IndexStatus

SCHEMA_VERSION = "2"

UpsertAction = Literal["inserted", "updated", "unchanged"]


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
            with self._lock:
                rows = self._conn.execute(
                    "SELECT v.rowid, v.distance, s.source_id, s.source_type, s.source_file, "
                    "s.source_revision, s.content_hash, s.text, s.importance, s.metadata_json, "
                    "s.updated_at FROM vectors v JOIN sources s ON s.id = v.rowid "
                    "WHERE v.embedding MATCH ? AND s.active = 1 AND k = ? ORDER BY v.distance",
                    (blob, max(limit * 3, k or limit)),
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
                    if kind is None or c.source_type == kind
                ][:k]
            return sorted(candidates, key=lambda c: c.similarity, reverse=True)[:limit]
        except Exception:
            logger.exception("VectorIndexManager.search failed")
            return []

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
