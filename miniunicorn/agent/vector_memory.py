"""Vector retrieval layer for the memory system.

Stores embeddings of memory entries (history summaries, episodic events,
semantic facts, procedural lessons) in a local SQLite database with the
sqlite-vec extension, enabling top-k similarity search at recall time.

sqlite-vec is an optional dependency. If unavailable, VectorMemoryStore
degrades to a NoOpStore that returns empty results — the rest of the
system continues to work with the legacy full-injection memory strategy.
"""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension. Returns True on success."""
    try:
        import sqlite_vec  # type: ignore
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except ImportError:
        logger.info(
            "sqlite-vec not installed; vector memory disabled. "
            "Install with: pip install sqlite-vec"
        )
        return False
    except Exception:
        logger.exception("Failed to load sqlite-vec extension")
        return False


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float32 vector to bytes for sqlite-vec storage."""
    return struct.pack(f"{len(vec)}f", *vec)


class VectorMemoryStore:
    """SQLite-backed vector store for memory entries.

    Schema:
        vec_entries(id INTEGER PK, kind TEXT, text TEXT, embedding BLOB,
                    metadata_json TEXT, created_at TEXT)
        vec0_virtual(embedding FLOAT[N] distance) — sqlite-vec virtual table

    The store is safe for concurrent reads; writes are serialized via a lock.
    """

    def __init__(self, db_path: Path, embedding_dim: int = 1536):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self._lock = threading.Lock()
        self._enabled = False
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database; set _enabled=False if sqlite-vec unavailable."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._enabled = _try_load_sqlite_vec(self._conn)
            if not self._enabled:
                return
            # Metadata table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS vec_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding BLOB,
                    metadata_json TEXT,
                    importance REAL DEFAULT 0.5,
                    created_at TEXT NOT NULL
                )
            """)
            # Backfill importance column for pre-existing databases (older schema
            # had no importance column). CREATE TABLE IF NOT EXISTS is a no-op
            # when the table exists, so we ALTER separately and swallow the
            # OperationalError that fires when the column is already there.
            try:
                self._conn.execute(
                    "ALTER TABLE vec_entries ADD COLUMN importance REAL DEFAULT 0.5"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            # sqlite-vec virtual table (cosine distance)
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec USING vec0(embedding float[{self.embedding_dim}] distance=cosine)"
            )
            self._conn.commit()
            logger.debug("VectorMemoryStore initialized at {} (dim={})", self.db_path, self.embedding_dim)
        except Exception:
            logger.exception("VectorMemoryStore init failed; disabling")
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def index(
        self,
        text: str,
        embedding: list[float],
        kind: str = "history",
        metadata: dict[str, Any] | None = None,
        importance: float = 0.5,
    ) -> int | None:
        """Insert an entry with its pre-computed embedding. Returns row id or None."""
        if not self._enabled or self._conn is None or not text:
            return None
        if len(embedding) != self.embedding_dim:
            logger.warning(
                "Embedding dim mismatch: got {}, expected {}",
                len(embedding), self.embedding_dim,
            )
            return None
        try:
            blob = _serialize_f32(embedding)
            meta_json = json.dumps(metadata or {}, ensure_ascii=False)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO vec_entries(kind, text, embedding, metadata_json, importance, created_at) VALUES (?,?,?,?,?,?)",
                    (kind, text, blob, meta_json, float(importance), ts),
                )
                rowid = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO vec(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
                self._conn.commit()
                return rowid
        except Exception:
            logger.exception("VectorMemoryStore.index failed")
            return None

    def search(
        self,
        query_embedding: list[float],
        k: int = 5,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k entries by cosine similarity. Empty list if disabled.

        Results are ranked by a weighted score that combines cosine similarity
        with the entry's importance, so high-importance memories surface ahead
        of equally-similar low-importance ones. The weighted score is stored
        under the ``score`` key (similarity is left untouched under
        ``similarity`` for callers that still want the raw value).
        """
        if not self._enabled or self._conn is None:
            return []
        if len(query_embedding) != self.embedding_dim:
            return []
        try:
            blob = _serialize_f32(query_embedding)
            with self._lock:
                # sqlite-vec KNN query
                rows = self._conn.execute(
                    "SELECT rowid, distance FROM vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, k * 3),  # over-fetch to allow kind filtering
                ).fetchall()
                results = []
                for row in rows:
                    entry = self._conn.execute(
                        "SELECT id, kind, text, metadata_json, importance, created_at FROM vec_entries WHERE id = ?",
                        (row["rowid"],),
                    ).fetchone()
                    if entry is None:
                        continue
                    if kind is not None and entry["kind"] != kind:
                        continue
                    meta = json.loads(entry["metadata_json"] or "{}")
                    importance = entry["importance"] if entry["importance"] is not None else 0.5
                    similarity = 1.0 - row["distance"]  # cosine distance -> similarity
                    # Weighted score: similarity * (0.5 + 0.5 * importance)
                    # so importance=0.5 leaves similarity unchanged, importance=1.0
                    # boosts it by up to 1.5x, and importance=0.0 halves it.
                    score = similarity * (0.5 + 0.5 * importance)
                    results.append({
                        "id": entry["id"],
                        "kind": entry["kind"],
                        "text": entry["text"],
                        "metadata": meta,
                        "importance": importance,
                        "created_at": entry["created_at"],
                        "similarity": similarity,
                        "score": score,
                    })
                    if len(results) >= k:
                        break
                # Sort by weighted score descending so the highest-scoring
                # memory comes first regardless of the raw similarity order
                # returned by sqlite-vec.
                results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
                return results
        except Exception:
            logger.exception("VectorMemoryStore.search failed")
            return []

    def count(self, kind: str | None = None) -> int:
        """Return entry count, optionally filtered by kind."""
        if not self._enabled or self._conn is None:
            return 0
        try:
            with self._lock:
                if kind:
                    cur = self._conn.execute("SELECT COUNT(*) FROM vec_entries WHERE kind = ?", (kind,))
                else:
                    cur = self._conn.execute("SELECT COUNT(*) FROM vec_entries")
                return cur.fetchone()[0]
        except Exception:
            return 0

    def decay_importance(self, days_threshold: int = 30, decay_factor: float = 0.9) -> int:
        """Decay importance for entries older than *days_threshold*.

        Multiplies ``importance`` by *decay_factor* for every entry whose
        ``created_at`` timestamp predates the cutoff. A floor of 0.1 is
        enforced so an entry never silently drops to zero (it can still be
        archived later via :meth:`archive_low_importance`). Returns the
        number of rows affected.
        """
        if not self._enabled or self._conn is None:
            return 0
        try:
            with self._lock:
                # created_at is stored as "YYYY-MM-DD HH:MM"; lexicographic
                # comparison against the cutoff string works correctly for
                # this fixed-width format.
                cutoff = (datetime.now().replace(day=1)).strftime("%Y-%m-%d %H:%M")
                cur = self._conn.execute(
                    "UPDATE vec_entries SET importance = importance * ? "
                    "WHERE created_at < ? AND importance > 0.1",
                    (decay_factor, cutoff),
                )
                self._conn.commit()
                return cur.rowcount
        except Exception:
            logger.exception("decay_importance failed")
            return 0

    def archive_low_importance(self, threshold: float = 0.2, min_age_days: int = 60) -> int:
        """Delete entries whose importance has fallen below *threshold*.

        sqlite-vec does not support UPDATE on stored vectors, so forgetting is
        implemented as a hard DELETE from both ``vec`` and ``vec_entries``.
        Only entries below *threshold* are eligible; the ``min_age_days``
        parameter is reserved for future age-aware filtering (currently every
        sub-threshold entry is archived). Returns the number of entries
        deleted.
        """
        if not self._enabled or self._conn is None:
            return 0
        try:
            with self._lock:
                # Find entries to archive
                cur = self._conn.execute(
                    "SELECT id FROM vec_entries WHERE importance < ?",
                    (threshold,),
                )
                ids = [row[0] for row in cur.fetchall()]
                if not ids:
                    return 0
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM vec WHERE rowid IN ({placeholders})", ids)
                self._conn.execute(f"DELETE FROM vec_entries WHERE id IN ({placeholders})", ids)
                self._conn.commit()
                return len(ids)
        except Exception:
            logger.exception("archive_low_importance failed")
            return 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class NoOpVectorStore:
    """Fallback when sqlite-vec is unavailable. All operations are no-ops."""

    @property
    def enabled(self) -> bool:
        return False

    def index(self, text, embedding, kind="history", metadata=None, importance=0.5):
        return None

    def search(self, query_embedding, k=5, kind=None):
        return []

    def count(self, kind=None):
        return 0

    def decay_importance(self, days_threshold=30, decay_factor=0.9):
        return 0

    def archive_low_importance(self, threshold=0.2, min_age_days=60):
        return 0

    def close(self):
        pass


def create_vector_store(db_path: Path, embedding_dim: int = 1536):
    """Factory: return a real store if sqlite-vec loads, else NoOp."""
    store = VectorMemoryStore(db_path, embedding_dim=embedding_dim)
    if store.enabled:
        return store
    store.close()
    return NoOpVectorStore()
