"""Tests for the provenance-aware index fingerprint and compatibility layer.

The fingerprint prevents silently mixing vectors from different embedding
models or revisions. A mismatched database is left untouched and the index
reports ``stale`` (read-only diagnostics; never search, never upsert).
"""

from __future__ import annotations

import sqlite3

import pytest

from miniunicorn.agent.vector_memory import (
    _DEFAULT_EMBEDDING_DIM,
    _DEFAULT_MODEL_ID,
    _VEC_SCHEMA_VERSION,
    NoOpVectorStore,
    VectorMemoryStore,
    create_vector_store,
)
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION


def _force_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort sqlite-vec loader for tests."""
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


@pytest.fixture
def vec_enabled(tmp_path, monkeypatch):
    """Yield ``(db_path, skip_reason)`` — skip tests when sqlite-vec is absent."""
    db_path = tmp_path / "memory.db"
    probe = sqlite3.connect(str(db_path))
    enabled = _force_load_sqlite_vec(probe)
    probe.close()
    if not enabled:
        pytest.skip("sqlite-vec not installed")
    db_path.unlink(missing_ok=True)
    return db_path


class TestDefaults:
    def test_default_dimension_is_512(self):
        assert _DEFAULT_EMBEDDING_DIM == 512

    def test_default_model_id_is_bge_small_zh(self):
        assert _DEFAULT_MODEL_ID == "BAAI/bge-small-zh-v1.5"

    def test_defaults_match_pinned_embedding_contract(self):
        assert _DEFAULT_EMBEDDING_DIM == MODEL_DIMENSION
        assert _DEFAULT_MODEL_ID == MODEL_ID
        assert _VEC_SCHEMA_VERSION == "2"

    def test_create_vector_store_default_dim_is_512(self):
        import inspect

        sig = inspect.signature(create_vector_store)
        assert sig.parameters["embedding_dim"].default == 512

    def test_vector_memory_store_is_index_manager_alias(self):
        from miniunicorn.agent.vector_index import VectorIndexManager

        assert VectorMemoryStore is VectorIndexManager


class TestFingerprint:
    def test_fresh_database_is_stamped(self, vec_enabled):
        store = VectorMemoryStore(vec_enabled)
        assert store.status().state == "ready"
        conn = store._conn
        assert conn is not None
        rows = {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM index_meta").fetchall()
        }
        assert rows["schema_version"] == _VEC_SCHEMA_VERSION
        assert rows["model_id"] == _DEFAULT_MODEL_ID
        assert rows["model_revision"] == MODEL_REVISION
        assert rows["vector_dimension"] == "512"
        store.close()

    def test_matching_fingerprint_allows_init(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.status().state == "ready"
        store1.close()
        store2 = VectorMemoryStore(vec_enabled)
        assert store2.status().state == "ready"
        store2.close()

    def test_dimension_mismatch_returns_noop(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.status().state == "ready"
        store1.close()
        store2 = create_vector_store(vec_enabled, embedding_dim=1536)
        assert isinstance(store2, NoOpVectorStore)
        assert store2.enabled is False
        assert store2.reason == "fingerprint_mismatch"
        assert store2.search([0.0] * 512, limit=5) == []
        store2.close()

    def test_model_mismatch_returns_noop(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.status().state == "ready"
        store1.close()
        store2 = create_vector_store(vec_enabled, model_id="text-embedding-3-small")
        assert isinstance(store2, NoOpVectorStore)
        assert store2.enabled is False
        assert store2.reason == "fingerprint_mismatch"
        store2.close()

    def test_revision_mismatch_yields_stale(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.status().state == "ready"
        store1.close()
        store2 = create_vector_store(vec_enabled, model_revision="wrong-revision")
        assert store2.status().state == "stale"
        assert store2.search([0.0] * 512, limit=5) == []
        store2.close()

    def test_mismatched_database_left_untouched(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.status().state == "ready"
        store1.close()
        original_size = vec_enabled.stat().st_size
        store2 = create_vector_store(vec_enabled, embedding_dim=1536)
        assert isinstance(store2, NoOpVectorStore)
        assert store2.enabled is False
        store2.close()
        assert vec_enabled.stat().st_size == original_size


class TestNoOpFallback:
    def test_failed_state_when_sqlite_vec_missing(self, tmp_path, monkeypatch):
        from miniunicorn.agent import vector_memory as vm

        monkeypatch.setattr(vm, "_try_load_sqlite_vec", lambda _conn: False)
        store = create_vector_store(tmp_path / "vec.db")
        assert store.status().state == "failed"
        assert store.count_sources() == 0
        assert store.search([0.0] * 512, limit=5) == []

    def test_noop_vector_store_still_available_for_legacy_fallback(self):
        store = NoOpVectorStore()
        assert store.enabled is False
        assert store.search([0.0] * 512) == []
        store.close()
