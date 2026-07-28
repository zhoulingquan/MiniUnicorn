"""Tests for VectorMemoryStore fingerprinting and dimension defaults.

The fingerprint prevents silently mixing vectors from different embedding
models. A mismatched database is left untouched and the store disables
itself for the run.
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


def _force_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort sqlite-vec loader for tests.

    Tests that need a real VectorMemoryStore skip when sqlite-vec is not
    installed; the fingerprint logic is still exercised on installations
    that have the extension.
    """
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

    def test_create_vector_store_default_dim_is_512(self):
        # Signature default, not a live call.
        import inspect

        sig = inspect.signature(create_vector_store)
        assert sig.parameters["embedding_dim"].default == 512


class TestFingerprint:
    def test_fresh_database_is_stamped(self, vec_enabled):
        store = VectorMemoryStore(vec_enabled)
        assert store.enabled
        conn = store._conn
        assert conn is not None
        rows = {
            r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM vec_meta").fetchall()
        }
        assert rows["schema_version"] == _VEC_SCHEMA_VERSION
        assert rows["model_id"] == _DEFAULT_MODEL_ID
        assert rows["vector_dim"] == "512"
        store.close()

    def test_matching_fingerprint_allows_init(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.enabled
        store1.close()
        # Reopen the same database — fingerprint must match.
        store2 = VectorMemoryStore(vec_enabled)
        assert store2.enabled
        store2.close()

    def test_dimension_mismatch_disables_store(self, vec_enabled):
        # Stamp with default 512.
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.enabled
        store1.close()
        # Reopen with a different dimension — must disable.
        store2 = VectorMemoryStore(vec_enabled, embedding_dim=1536)
        assert store2.enabled is False
        store2.close()

    def test_model_mismatch_disables_store(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.enabled
        store1.close()
        store2 = VectorMemoryStore(vec_enabled, model_id="text-embedding-3-small")
        assert store2.enabled is False
        store2.close()

    def test_mismatched_database_left_untouched(self, vec_enabled):
        store1 = VectorMemoryStore(vec_enabled)
        assert store1.enabled
        store1.close()
        original_size = vec_enabled.stat().st_size
        # Try to open with wrong dimension — store disables, file unchanged.
        store2 = VectorMemoryStore(vec_enabled, embedding_dim=1536)
        assert store2.enabled is False
        store2.close()
        assert vec_enabled.stat().st_size == original_size

    def test_create_vector_store_falls_back_to_noop_on_mismatch(self, vec_enabled):
        # Stamp with default.
        store1 = create_vector_store(vec_enabled)
        assert store1.enabled
        store1.close()
        # Mismatched model -> NoOp fallback.
        store2 = create_vector_store(vec_enabled, model_id="other-model")
        assert isinstance(store2, NoOpVectorStore)
        assert store2.enabled is False


class TestNoOpFallback:
    def test_noop_when_sqlite_vec_missing(self, tmp_path, monkeypatch):
        from miniunicorn.agent import vector_memory as vm

        monkeypatch.setattr(vm, "_try_load_sqlite_vec", lambda _conn: False)
        store = create_vector_store(tmp_path / "vec.db")
        assert isinstance(store, NoOpVectorStore)
        assert store.enabled is False
