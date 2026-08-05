"""Backward-compatible imports for the production vector index.

The real implementation lives in :mod:`miniunicorn.agent.vector_index`
(``VectorIndexManager``, version 2 provenance-aware schema). This module
keeps the legacy names alive for pre-Task-11 callers that have not yet
migrated. Production code (AgentLoop, MemoryStore, Dream, Consolidator)
must import from ``vector_index`` directly.

Task 18 retires the prototype write paths; no second database schema
is maintained.
"""

from __future__ import annotations

import sqlite3

from loguru import logger

from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION

#: COMPAT alias; new code uses VectorIndexManager directly.
VectorMemoryStore = VectorIndexManager

#: COMPAT fingerprint schema version (mirrors vector_index.SCHEMA_VERSION).
_VEC_SCHEMA_VERSION = "2"

#: Default vector dimension for the local embedding model
#: (:data:`BAAI/bge-small-zh-v1.5 <miniunicorn.providers.local_embedding.DEFAULT_LOCAL_MODEL>`).
_DEFAULT_EMBEDDING_DIM = MODEL_DIMENSION

#: Default local model id used to fingerprint the vector database.
_DEFAULT_MODEL_ID = MODEL_ID


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


class NoOpVectorStore:
    """Fallback when sqlite-vec is unavailable or the model fingerprint mismatches.

    All operations are no-ops; ``reason`` explains why the store is disabled.
    """

    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    @property
    def enabled(self) -> bool:
        return False

    def index(self, *args, **kwargs):
        return None

    def search(self, *args, **kwargs):
        return []

    def count(self, *args, **kwargs) -> int:
        return 0

    def close(self) -> None:
        pass


def create_vector_store(
    db_path,
    embedding_dim: int = MODEL_DIMENSION,
    model_id: str = MODEL_ID,
    model_revision: str = MODEL_REVISION,
):
    """COMPAT factory: delegate to the provenance-aware VectorIndexManager.

    Returns ``NoOpVectorStore`` when the requested fingerprint does not match
    the pinned model; otherwise returns a ``VectorIndexManager``.
    """
    if embedding_dim != MODEL_DIMENSION or model_id != MODEL_ID:
        return NoOpVectorStore(reason="fingerprint_mismatch")
    return VectorIndexManager(
        db_path,
        model_id=model_id,
        model_revision=model_revision,
        vector_dimension=embedding_dim,
    )
