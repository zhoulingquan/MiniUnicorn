"""SQLite-backed vector memory index (design §22.2).

This module is the *only* production implementation of the derived vector
memory index. It lives under ``miniunicorn.runtime.sqlite`` because it
imports ``sqlite3`` and the optional ``sqlite-vec`` extension; Agent Core
must never import this module (design §6.17, acceptance #23).

The store is a process-local derived index. It is **not** a task
authority: tasks, leases, checkpoints, tool results, and outbox rows
live in the Runtime Store. The vector index can always be rebuilt from
authoritative memory sources via :meth:`VectorMemoryStore.rebuild`.

Public surface:

- :class:`VectorMemoryStore` — SQLite + sqlite-vec implementation.
- :func:`create_vector_store` — factory that returns a real store when
  sqlite-vec loads, else returns the Agent-owned ``NoOpVectorStore``.

The Agent package owns the matching port/protocol
(:class:`~miniunicorn.agent.ports.VectorMemoryPort`) and the
:class:`~miniunicorn.agent.vector_memory.NoOpVectorStore` fallback;
both are imported from here when production wiring is needed.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

#: Fingerprint schema version. Bump when the on-disk vector metadata layout
#: changes so older databases are rejected rather than silently misread.
#: Version 2 adds WP7 hardening columns (design §22.2).
_VEC_SCHEMA_VERSION = "2"

#: Default vector dimension for the local embedding model
#: (:data:`BAAI/bge-small-zh-v1.5 <miniunicorn.providers.local_embedding.DEFAULT_LOCAL_MODEL>`).
_DEFAULT_EMBEDDING_DIM = 512

#: Default local model id used to fingerprint the vector database.
_DEFAULT_MODEL_ID = "BAAI/bge-small-zh-v1.5"

#: Busy timeout in milliseconds for SQLite concurrent write contention
#: (design §22.2: "WAL mode and busy timeout").
_VEC_BUSY_TIMEOUT_MS = 5_000


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
            "sqlite-vec not installed; vector memory disabled. Install with: pip install sqlite-vec"
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
        vec_meta(key TEXT PK, value TEXT) — fingerprint (schema version,
            model id, vector dimension)
        vec_entries(id INTEGER PK, kind TEXT, text TEXT, embedding BLOB,
                    metadata_json TEXT, created_at TEXT)
        vec0_virtual(embedding FLOAT[N] distance) — sqlite-vec virtual table

    The store is safe for concurrent reads; writes are serialized via a lock.

    If an existing ``memory.db`` has no matching fingerprint or a different
    model/dimension, the store leaves the file untouched, disables itself
    for the run, and logs one actionable message telling the developer to
    remove the development database and restart. It never silently mixes
    vectors from different models and never deletes the database
    automatically.
    """

    def __init__(
        self,
        db_path: Path,
        embedding_dim: int = _DEFAULT_EMBEDDING_DIM,
        model_id: str = _DEFAULT_MODEL_ID,
    ):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.model_id = model_id
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
            # WP7 hardening (design §22.2): WAL mode + busy timeout for
            # concurrent write safety across processes.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={_VEC_BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._enabled = _try_load_sqlite_vec(self._conn)
            if not self._enabled:
                return
            # Fingerprint table — must exist before we check it so a fresh
            # database can be stamped and an existing one can be inspected.
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS vec_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            if not self._fingerprint_matches():
                # Leave the file untouched and disable for this run. The
                # caller (NoOp fallback) takes over so chat keeps working.
                self._enabled = False
                self._conn.close()
                self._conn = None
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
                self._conn.execute("ALTER TABLE vec_entries ADD COLUMN importance REAL DEFAULT 0.5")
            except sqlite3.OperationalError:
                pass  # column already exists
            # WP7 hardening columns (design §22.2):
            # - source_identity: where the content came from (e.g. "history.jsonl")
            # - source_revision: deterministic revision of the source (e.g. cursor)
            # - idempotency_key: unique key for dedup (kind:source_identity:source_revision)
            # - tombstone: soft-delete flag (0=active, 1=deleted)
            # - tenant_id, principal_id, agent_id, workspace_id: scope columns
            for col, col_type, default in [
                ("source_identity", "TEXT", "''"),
                ("source_revision", "TEXT", "''"),
                ("idempotency_key", "TEXT", "''"),
                ("tombstone", "INTEGER", "0"),
                ("tenant_id", "TEXT", "''"),
                ("principal_id", "TEXT", "''"),
                ("agent_id", "TEXT", "''"),
                ("workspace_id", "TEXT", "''"),
            ]:
                try:
                    self._conn.execute(
                        f"ALTER TABLE vec_entries ADD COLUMN {col} {col_type} DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
            # Unique index on idempotency_key for dedup (design §22.2).
            # Only enforce on non-empty keys to avoid conflicts with legacy
            # entries that have empty keys.
            self._conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vec_idempotency
                ON vec_entries(idempotency_key)
                WHERE idempotency_key != ''
            """)
            # Index on scope columns for filtered lookups (design §22.2).
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_vec_scope
                ON vec_entries(tenant_id, principal_id, agent_id, workspace_id)
                WHERE tombstone = 0
            """)
            # Index on kind for filtered searches.
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_vec_kind
                ON vec_entries(kind)
                WHERE tombstone = 0
            """)
            # sqlite-vec virtual table (cosine distance)
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec USING vec0(embedding float[{self.embedding_dim}] distance=cosine)"
            )
            self._conn.commit()
            logger.debug(
                "VectorMemoryStore initialized at {} (dim={}, model={}, schema=v{})",
                self.db_path,
                self.embedding_dim,
                self.model_id,
                _VEC_SCHEMA_VERSION,
            )
        except Exception:
            logger.exception("VectorMemoryStore init failed; disabling")
            self._enabled = False

    def _fingerprint_matches(self) -> bool:
        """Check/stamp the database fingerprint.

        A fresh database (no ``vec_meta`` rows) is stamped with the current
        schema version, model id, and dimension. An existing database is
        inspected: if any of those three values differ, the store refuses
        to initialize and logs an actionable message.

        Returns ``True`` when the fingerprint matches (or was just stamped),
        ``False`` when a mismatched fingerprint is detected.
        """
        assert self._conn is not None
        rows = {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM vec_meta").fetchall()
        }
        if not rows:
            # Fresh database — stamp the fingerprint.
            self._conn.executemany(
                "INSERT INTO vec_meta(key, value) VALUES (?, ?)",
                [
                    ("schema_version", _VEC_SCHEMA_VERSION),
                    ("model_id", self.model_id),
                    ("vector_dim", str(self.embedding_dim)),
                ],
            )
            self._conn.commit()
            return True

        expected = {
            "schema_version": _VEC_SCHEMA_VERSION,
            "model_id": self.model_id,
            "vector_dim": str(self.embedding_dim),
        }
        for key, value in expected.items():
            if rows.get(key) != value:
                logger.warning(
                    "Vector memory fingerprint mismatch for {}: "
                    "key={!r} expected={!r} found={!r}. "
                    "The existing database was created by a different model "
                    "or schema. Vector recall is disabled for this run to "
                    "avoid mixing vectors. To reset, remove the database "
                    "file and restart: {}",
                    self.db_path,
                    key,
                    value,
                    rows.get(key),
                    self.db_path,
                )
                return False
        return True

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
        *,
        source_identity: str = "",
        source_revision: str = "",
        scope: dict[str, str] | None = None,
    ) -> int | None:
        """Insert an entry with its pre-computed embedding. Returns row id or None.

        WP7 hardening (design §22.2):

        - ``source_identity`` and ``source_revision`` record where the content
          came from and at what revision, so a stale consolidation task cannot
          overwrite newer memory (design §13.1).
        - ``idempotency_key`` is derived from ``(kind, source_identity,
          source_revision)``; duplicate submissions are harmless (UPSERT).
        - ``scope`` carries ``tenant_id``, ``principal_id``, ``agent_id``,
          ``workspace_id`` for scoped lookups (design §22.2).
        """
        if not self._enabled or self._conn is None or not text:
            return None
        if len(embedding) != self.embedding_dim:
            logger.warning(
                "Embedding dim mismatch: got {}, expected {}",
                len(embedding),
                self.embedding_dim,
            )
            return None
        try:
            blob = _serialize_f32(embedding)
            meta_json = json.dumps(metadata or {}, ensure_ascii=False)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            idempotency_key = ""
            if source_identity or source_revision:
                idempotency_key = f"{kind}:{source_identity}:{source_revision}"
            scope = scope or {}
            tenant_id = scope.get("tenant_id", "")
            principal_id = scope.get("principal_id", "")
            agent_id = scope.get("agent_id", "")
            workspace_id = scope.get("workspace_id", "")
            with self._lock:
                # Idempotent UPSERT: if the idempotency_key already exists,
                # update the row in place (including the vector in the
                # virtual table). Otherwise insert a new row.
                existing_id = None
                if idempotency_key:
                    row = self._conn.execute(
                        "SELECT id FROM vec_entries WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    existing_id = row["id"] if row else None

                if existing_id is not None:
                    # Update existing entry (revise in place).
                    self._conn.execute(
                        "UPDATE vec_entries SET text=?, embedding=?, metadata_json=?, "
                        "importance=?, source_identity=?, source_revision=?, "
                        "tenant_id=?, principal_id=?, agent_id=?, workspace_id=?, "
                        "tombstone=0 WHERE id=?",
                        (
                            text, blob, meta_json, float(importance),
                            source_identity, source_revision,
                            tenant_id, principal_id, agent_id, workspace_id,
                            existing_id,
                        ),
                    )
                    # sqlite-vec does not support UPDATE on stored vectors;
                    # delete and re-insert to refresh the vector.
                    self._conn.execute(
                        "DELETE FROM vec WHERE rowid = ?", (existing_id,)
                    )
                    self._conn.execute(
                        "INSERT INTO vec(rowid, embedding) VALUES (?, ?)",
                        (existing_id, blob),
                    )
                    self._conn.commit()
                    return existing_id
                else:
                    cur = self._conn.execute(
                        "INSERT INTO vec_entries("
                        "kind, text, embedding, metadata_json, importance, created_at, "
                        "source_identity, source_revision, idempotency_key, tombstone, "
                        "tenant_id, principal_id, agent_id, workspace_id"
                        ") VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                        (
                            kind, text, blob, meta_json, float(importance), ts,
                            source_identity, source_revision, idempotency_key,
                            tenant_id, principal_id, agent_id, workspace_id,
                        ),
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
        *,
        scope: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k entries by cosine similarity. Empty list if disabled.

        Results are ranked by a weighted score that combines cosine similarity
        with the entry's importance, so high-importance memories surface ahead
        of equally-similar low-importance ones. The weighted score is stored
        under the ``score`` key (similarity is left untouched under
        ``similarity`` for callers that still want the raw value).

        WP7 hardening (design §22.2):

        - Tombstoned entries (``tombstone=1``) are excluded.
        - When ``scope`` is provided, results are filtered by the provided
          ``tenant_id``, ``principal_id``, ``agent_id``, ``workspace_id``
          (empty values in scope match anything).
        """
        if not self._enabled or self._conn is None:
            return []
        if len(query_embedding) != self.embedding_dim:
            return []
        try:
            blob = _serialize_f32(query_embedding)
            scope = scope or {}
            with self._lock:
                # sqlite-vec KNN query
                rows = self._conn.execute(
                    "SELECT rowid, distance FROM vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, k * 3),  # over-fetch to allow kind filtering
                ).fetchall()
                results = []
                for row in rows:
                    entry = self._conn.execute(
                        "SELECT id, kind, text, metadata_json, importance, created_at, "
                        "tombstone, tenant_id, principal_id, agent_id, workspace_id, "
                        "source_identity, source_revision "
                        "FROM vec_entries WHERE id = ?",
                        (row["rowid"],),
                    ).fetchone()
                    if entry is None:
                        continue
                    if entry["tombstone"]:
                        continue
                    if kind is not None and entry["kind"] != kind:
                        continue
                    # Scope filtering (design §22.2).
                    if scope:
                        if scope.get("tenant_id") and entry["tenant_id"] and entry["tenant_id"] != scope["tenant_id"]:
                            continue
                        if scope.get("principal_id") and entry["principal_id"] and entry["principal_id"] != scope["principal_id"]:
                            continue
                        if scope.get("agent_id") and entry["agent_id"] and entry["agent_id"] != scope["agent_id"]:
                            continue
                        if scope.get("workspace_id") and entry["workspace_id"] and entry["workspace_id"] != scope["workspace_id"]:
                            continue
                    meta = json.loads(entry["metadata_json"] or "{}")
                    importance = entry["importance"] if entry["importance"] is not None else 0.5
                    similarity = 1.0 - row["distance"]  # cosine distance -> similarity
                    # Weighted score: similarity * (0.5 + 0.5 * importance)
                    # so importance=0.5 leaves similarity unchanged, importance=1.0
                    # boosts it by up to 1.5x, and importance=0.0 halves it.
                    score = similarity * (0.5 + 0.5 * importance)
                    results.append(
                        {
                            "id": entry["id"],
                            "kind": entry["kind"],
                            "text": entry["text"],
                            "metadata": meta,
                            "importance": importance,
                            "created_at": entry["created_at"],
                            "similarity": similarity,
                            "score": score,
                            "source_identity": entry["source_identity"],
                            "source_revision": entry["source_revision"],
                        }
                    )
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
        """Return active (non-tombstoned) entry count, optionally filtered by kind."""
        if not self._enabled or self._conn is None:
            return 0
        try:
            with self._lock:
                if kind:
                    cur = self._conn.execute(
                        "SELECT COUNT(*) FROM vec_entries WHERE kind = ? AND tombstone = 0",
                        (kind,),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT COUNT(*) FROM vec_entries WHERE tombstone = 0"
                    )
                return cur.fetchone()[0]
        except Exception:
            return 0

    def tombstone_by_source_revision(
        self,
        *,
        source_identity: str,
        source_revision: str,
    ) -> int:
        """Soft-delete (tombstone) entries matching a source revision (design §22.2).

        Tombstoning is revision-aware: only entries whose
        ``source_revision`` matches are tombstoned, so a stale delete cannot
        remove newer memory. Returns the number of entries tombstoned.
        """
        if not self._enabled or self._conn is None:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE vec_entries SET tombstone = 1 "
                    "WHERE source_identity = ? AND source_revision = ? AND tombstone = 0",
                    (source_identity, source_revision),
                )
                self._conn.commit()
                return cur.rowcount
        except Exception:
            logger.exception("tombstone_by_source_revision failed")
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

    def rebuild(
        self,
        *,
        embed_fn: Callable[[str], list[float]],
        entries: list[dict[str, Any]],
        scope: dict[str, str] | None = None,
    ) -> int:
        """Rebuild the vector index from authoritative memory sources (design §22.2).

        Wipes all existing entries and vectors, then re-indexes the provided
        ``entries``. Each entry is a dict with keys: ``text``, ``kind``,
        ``metadata``, ``importance``, ``source_identity``,
        ``source_revision``.

        This is idempotent: running it twice with the same ``entries``
        produces the same final state. Used by the ``MEMORY_INDEX``
        maintenance task to recover from corruption or model swaps.

        Returns the number of entries indexed.
        """
        if not self._enabled or self._conn is None:
            return 0
        try:
            with self._lock:
                # Wipe existing data. Use DELETE (not DROP) so the schema
                # and indexes are preserved.
                self._conn.execute("DELETE FROM vec")
                self._conn.execute("DELETE FROM vec_entries")
                self._conn.commit()
            count = 0
            for entry in entries:
                text = entry.get("text", "")
                if not text:
                    continue
                kind = entry.get("kind", "history")
                metadata = entry.get("metadata")
                importance = entry.get("importance", 0.5)
                source_identity = entry.get("source_identity", "")
                source_revision = entry.get("source_revision", "")
                embedding = embed_fn(text)
                if not embedding or len(embedding) != self.embedding_dim:
                    continue
                row_id = self.index(
                    text,
                    embedding,
                    kind=kind,
                    metadata=metadata,
                    importance=importance,
                    source_identity=source_identity,
                    source_revision=source_revision,
                    scope=scope,
                )
                if row_id is not None:
                    count += 1
            logger.info(
                "VectorMemoryStore.rebuild: indexed {} entries", count
            )
            return count
        except Exception:
            logger.exception("VectorMemoryStore.rebuild failed")
            return 0


def create_vector_store(
    db_path: Path,
    embedding_dim: int = _DEFAULT_EMBEDDING_DIM,
    model_id: str = _DEFAULT_MODEL_ID,
):
    """Factory: return a real store if sqlite-vec loads, else NoOp.

    ``embedding_dim`` and ``model_id`` default to the local BGE model's
    values (512 / ``BAAI/bge-small-zh-v1.5``) and are written to the
    database fingerprint so a future model swap is detected rather than
    silently mixing vectors.

    Returns the Agent-owned ``NoOpVectorStore`` when sqlite-vec is missing
    or when the on-disk fingerprint does not match. The NoOp fallback is
    imported lazily so this Runtime module does not depend on the Agent
    package at import time.
    """
    store = VectorMemoryStore(db_path, embedding_dim=embedding_dim, model_id=model_id)
    if store.enabled:
        return store
    store.close()
    from miniunicorn.agent.vector_memory import NoOpVectorStore

    return NoOpVectorStore()


__all__ = [
    "VectorMemoryStore",
    "create_vector_store",
]
