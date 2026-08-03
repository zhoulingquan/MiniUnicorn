"""Integration tests for MemoryStore → VectorMemoryStore embedding pipeline.

Exercises the full seam: a fake ``LocalEmbeddingProvider`` that returns
deterministic 512-dimensional vectors is attached to a ``MemoryStore``
backed by a real ``VectorMemoryStore`` (sqlite-vec). The tests verify
scoped indexing, persistence across close/reopen, recall ranking, and
idempotent re-indexing — all with ``source_identity`` / ``source_revision``
forwarding that ``MemoryStore.index_text`` must accept and pass through.

Per Task 23 Step 1–2: these tests FAIL before the production change
because ``MemoryStore.index_text`` does not yet accept ``source_identity``,
``source_revision``, and ``scope``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from miniunicorn.agent.memory import MemoryStore
from miniunicorn.agent.vector_memory import NoOpVectorStore
from miniunicorn.providers.local_embedding import (
    DEFAULT_LOCAL_DIMENSION,
    DEFAULT_LOCAL_MODEL,
)
from miniunicorn.runtime.sqlite.vector_memory_store import (
    create_vector_store,
)

_DIM = DEFAULT_LOCAL_DIMENSION  # 512


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _force_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


@pytest.fixture
def vec_db_path(tmp_path: Path) -> Path:
    """Return a db_path that is known to support sqlite-vec, else skip."""
    db_path = tmp_path / "memory" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    probe = sqlite3.connect(str(db_path))
    enabled = _force_load_sqlite_vec(probe)
    probe.close()
    if not enabled:
        pytest.skip("sqlite-vec not installed")
    db_path.unlink(missing_ok=True)
    return db_path


class _FakeEmbeddingProvider:
    """Deterministic embedding provider that returns 512-dim L2-normalized vectors.

    Uses a bag-of-characters model: each unique character maps to a fixed
    dimension (``ord(ch) % 512``), and the vector value is the count of that
    character. L2-normalized so cosine similarity reflects character overlap.
    Texts sharing more characters produce vectors with higher cosine
    similarity, which is sufficient for the deterministic recall test.
    """

    def __init__(self) -> None:
        self.model_name = DEFAULT_LOCAL_MODEL
        self.dimension = _DIM

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            vec = [0.0] * _DIM
            for ch in text:
                vec[ord(ch) % _DIM] += 1.0
            # L2-normalize
            norm = sum(v * v for v in vec) ** 0.5
            if norm > 0:
                vec = [v / norm for v in vec]
            results.append(vec)
        return results


_SCOPE_A = {
    "tenant_id": "local",
    "principal_id": "owner",
    "agent_id": "main",
    "workspace_id": "embedding-proof",
}

_TEXT_DOC_1 = "MiniUnicorn 使用本地 CPU 嵌入保存长期记忆。"
_TEXT_DOC_2 = "三工作进程运行时负责持久任务执行。"
_TEXT_QUERY = "本地嵌入如何保存记忆？"


# ---------------------------------------------------------------------------
# Step 1 tests: scoped indexing, persistence, recall, idempotency
# ---------------------------------------------------------------------------


class TestScopedIndexPersistenceAndRecall:
    """Verify that index_text forwards source_identity/revision/scope and
    that the vector store persists, recalls correctly, and deduplicates."""

    @pytest.mark.asyncio
    async def test_scoped_index_persist_recall_idempotent(self, vec_db_path: Path) -> None:
        workspace = vec_db_path.parent.parent
        store = MemoryStore(workspace)
        provider = _FakeEmbeddingProvider()
        store.set_embed_provider(provider, model=DEFAULT_LOCAL_MODEL)

        vs = create_vector_store(vec_db_path, embedding_dim=_DIM, model_id=DEFAULT_LOCAL_MODEL)
        assert vs.enabled, "VectorMemoryStore should be enabled"
        store.attach_vector_store(vs)

        # Index two scoped Chinese texts with fixed source identity/revision.
        await store.index_text(
            _TEXT_DOC_1,
            kind="history",
            source_identity="history.jsonl",
            source_revision="1",
            scope=_SCOPE_A,
        )
        await store.index_text(
            _TEXT_DOC_2,
            kind="history",
            source_identity="history.jsonl",
            source_revision="2",
            scope=_SCOPE_A,
        )

        assert vs.count() == 2

        # Close/reopen the store to verify persistence.
        vs.close()

        vs_reopened = create_vector_store(
            vec_db_path, embedding_dim=_DIM, model_id=DEFAULT_LOCAL_MODEL
        )
        assert vs_reopened.enabled, "Reopened VectorMemoryStore should be enabled"
        assert vs_reopened.count() == 2

        store.attach_vector_store(vs_reopened)

        # Embed the query through the same provider and search with scope.
        query_vecs = await provider.embed([_TEXT_QUERY])
        assert len(query_vecs) == 1
        results = vs_reopened.search(query_vecs[0], k=2, scope=_SCOPE_A)
        assert len(results) >= 1
        # The query is about "本地嵌入保存记忆" — doc 1 should rank first.
        assert results[0]["text"] == _TEXT_DOC_1

        # Re-index the same identity/revision — row count must be unchanged.
        await store.index_text(
            _TEXT_DOC_1,
            kind="history",
            source_identity="history.jsonl",
            source_revision="1",
            scope=_SCOPE_A,
        )
        assert vs_reopened.count() == 2, "Re-indexing same identity/revision must not duplicate"

        vs_reopened.close()


class TestNoOpFallback:
    """When sqlite-vec fails to load, NoOpVectorStore must be a safe no-op
    that does not delete authoritative input text."""

    def test_noop_is_safe(self, tmp_path: Path, monkeypatch) -> None:
        # Force sqlite-vec load failure.
        from miniunicorn.runtime.sqlite import vector_memory_store as vms

        monkeypatch.setattr(vms, "_try_load_sqlite_vec", lambda _conn: False)

        store = create_vector_store(tmp_path / "noop.db")
        assert isinstance(store, NoOpVectorStore)
        assert store.enabled is False

        # index returns None (no-op), does not raise, does not delete text.
        result = store.index(
            "authoritative text",
            [0.0] * _DIM,
            source_identity="history.jsonl",
            source_revision="1",
        )
        assert result is None

        # search returns empty list.
        assert store.search([0.0] * _DIM) == []
        store.close()
