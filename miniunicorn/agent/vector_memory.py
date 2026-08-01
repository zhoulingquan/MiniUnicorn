"""Agent-owned vector memory contracts (design §22.2).

This module owns the Agent-facing surface of the derived vector memory
index: the ``NoOpVectorStore`` fallback used when no SQLite/vector
extension is available, and the constants that callers (including the
Runtime factory) share with the Agent package.

The concrete SQLite + sqlite-vec implementation lives in
:mod:`miniunicorn.runtime.sqlite.vector_memory_store`. Agent Core must
not import that module or ``sqlite3`` (design §6.17, acceptance #23).
Production wiring injects the Runtime factory through
``AgentLoop(vector_memory_factory=...)``; unit tests inject either
``NoOpVectorStore`` or a fake.

This file deliberately avoids importing ``sqlite3`` so the Agent package
stays dependency-pure.
"""

from __future__ import annotations

#: Fingerprint schema version. Shared with the Runtime implementation so
#: the Agent package can advertise the version it expects without
#: importing the SQLite store. The Runtime store stamps this value into
#: ``vec_meta`` on first init.
_VEC_SCHEMA_VERSION = "2"

#: Default vector dimension for the local embedding model
#: (:data:`BAAI/bge-small-zh-v1.5 <miniunicorn.providers.local_embedding.DEFAULT_LOCAL_MODEL>`).
_DEFAULT_EMBEDDING_DIM = 512

#: Default local model id used to fingerprint the vector database.
_DEFAULT_MODEL_ID = "BAAI/bge-small-zh-v1.5"


class NoOpVectorStore:
    """Fallback when sqlite-vec is unavailable. All operations are no-ops.

    Also used as the default ``vector_memory_factory`` return value in
    unit tests that do not exercise vector recall.
    """

    @property
    def enabled(self) -> bool:
        return False

    def index(self, text, embedding, kind="history", metadata=None, importance=0.5,
              *, source_identity="", source_revision="", scope=None):
        return None

    def search(self, query_embedding, k=5, kind=None, *, scope=None):
        return []

    def count(self, kind=None):
        return 0

    def decay_importance(self, days_threshold=30, decay_factor=0.9):
        return 0

    def archive_low_importance(self, threshold=0.2, min_age_days=60):
        return 0

    def tombstone_by_source_revision(self, *, source_identity, source_revision):
        return 0

    def rebuild(self, *, embed_fn, entries, scope=None):
        return 0

    def close(self):
        pass


#: Re-export the Agent-owned factory type so callers can import it from
#: either :mod:`miniunicorn.agent.ports` or here. The canonical definition
#: lives in :mod:`miniunicorn.agent.ports` (design §22.2).
from miniunicorn.agent.ports import VectorMemoryFactory  # noqa: E402,F401

__all__ = [
    "NoOpVectorStore",
    "VectorMemoryFactory",
    "_DEFAULT_EMBEDDING_DIM",
    "_DEFAULT_MODEL_ID",
    "_VEC_SCHEMA_VERSION",
]
