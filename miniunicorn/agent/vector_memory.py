"""Compatibility export layer for the vector retrieval subsystem.

The real implementation moved to :mod:`miniunicorn.agent.vector_index`
(``VectorIndexManager``, version 2 provenance-aware schema). This module
keeps the legacy names alive for pre-Task-11 callers:

- ``create_vector_store()`` delegates to ``VectorIndexManager``;
- ``VectorMemoryStore`` is an alias;
- ``NoOpVectorStore`` remains until Task 11 migrates all callers.

Task 11 removes these shims; no second database schema is maintained.
"""

from __future__ import annotations

import sqlite3

from loguru import logger

from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION

#: Fingerprint schema version (mirrors vector_index.SCHEMA_VERSION).
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


#: COMPAT alias; new code uses VectorIndexManager directly.
VectorMemoryStore = VectorIndexManager


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


def create_vector_store(
    db_path,
    embedding_dim: int = _DEFAULT_EMBEDDING_DIM,
    model_id: str = _DEFAULT_MODEL_ID,
    model_revision: str = MODEL_REVISION,
):
    """COMPAT factory: delegate to the provenance-aware VectorIndexManager.

    ``embedding_dim`` and ``model_id`` default to the local BGE model's
    values (512 / ``BAAI/bge-small-zh-v1.5``) and are written to the
    database fingerprint so a future model swap is detected rather than
    silently mixing vectors.
    """
    return VectorIndexManager(
        db_path,
        model_id=model_id,
        model_revision=model_revision,
        vector_dimension=embedding_dim,
    )
