"""Tests for the provenance-aware vector index (version 2 schema).

Real sqlite-vec cases skip when the extension is missing; pure
schema/fingerprint checks must run on every installation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from miniunicorn.agent.memory_sources import MemorySourceRecord
from miniunicorn.agent.vector_index import IndexFingerprint, VectorIndexManager
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION


@pytest.fixture
def vec_db(tmp_path) -> object:
    return tmp_path / "memory.db"


@pytest.fixture
def source_record() -> MemorySourceRecord:
    return MemorySourceRecord(
        source_id="user:preferences:1",
        source_type="user",
        source_file="USER.md",
        source_revision="1",
        content_hash="a" * 64,
        text="用户喜欢浅色主题",
        importance=0.8,
        active=True,
    )


def _ensure_sqlite_vec() -> None:
    pytest.importorskip("sqlite_vec")


def _vector(i: int) -> list[float]:
    vector = [0.0] * MODEL_DIMENSION
    vector[i % MODEL_DIMENSION] = 1.0
    return vector


class TestFingerprintDefaults:
    def test_index_fingerprint_defaults_to_pinned_model(self):
        fp = IndexFingerprint()
        assert fp.schema_version == "2"
        assert fp.model_id == MODEL_ID
        assert fp.model_revision == MODEL_REVISION
        assert fp.vector_dimension == MODEL_DIMENSION


def test_mismatched_fingerprint_yields_stale_without_creating_tables(tmp_path):
        db = tmp_path / "memory.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO index_meta VALUES ('schema_version', '1')")
        conn.commit()
        conn.close()
        manager = VectorIndexManager(db)
        assert manager.status().state == "stale"
        assert manager.search([0.0] * MODEL_DIMENSION, limit=5) == []
        assert manager.count_sources() == 0
        conn = sqlite3.connect(db)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "sources" not in tables
        assert "vectors" not in tables


class TestUpsert:
    def test_upsert_is_idempotent_and_changed_content_reuses_source_row(self, vec_db, source_record):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        assert manager.upsert(source_record, _vector(0)) == "inserted"
        assert manager.upsert(source_record, _vector(0)) == "unchanged"
        changed = replace(source_record, source_revision="2", content_hash="b" * 64, text="新内容")
        assert manager.upsert(changed, _vector(1)) == "updated"
        assert manager.count_sources() == 1
        assert manager.get_source(source_record.source_id).text == "新内容"

    def test_mismatched_model_revision_cannot_search(self, vec_db, source_record):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        manager.upsert(source_record, _vector(0))
        manager.close()
        wrong = VectorIndexManager(vec_db, model_revision="wrong")
        assert wrong.status().state == "stale"
        assert wrong.search([1.0] + [0.0] * 511, limit=5) == []

    def test_wrong_dimension_vector_is_rejected(self, vec_db, source_record):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        with pytest.raises(ValueError):
            manager.upsert(source_record, [1.0, 2.0])

    def test_non_finite_vector_is_rejected(self, vec_db, source_record):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        vector = [0.0] * MODEL_DIMENSION
        vector[3] = float("nan")
        with pytest.raises(ValueError):
            manager.upsert(source_record, vector)

    def test_upsert_when_stale_raises(self, tmp_path):
        _ensure_sqlite_vec()
        db = tmp_path / "memory.db"
        manager = VectorIndexManager(db)
        manager.close()
        conn = sqlite3.connect(db)
        conn.execute("UPDATE index_meta SET value='wrong' WHERE key='model_id'")
        conn.commit()
        conn.close()
        stale = VectorIndexManager(db)
        assert stale.status().state == "stale"
        record = MemorySourceRecord(
            source_id="u:1", source_type="user", source_file="USER.md",
            source_revision="1", content_hash="a" * 64, text="文本",
            importance=0.5,
        )
        with pytest.raises(RuntimeError):
            stale.upsert(record, _vector(0))


class TestInactive:
    def test_mark_inactive_except_hides_rows_from_search(self, vec_db, source_record):
        _ensure_sqlite_vec()
        second = replace(source_record, source_id="user:other:2", text="另一条")
        manager = VectorIndexManager(vec_db)
        manager.upsert(source_record, _vector(0))
        manager.upsert(second, _vector(1))
        assert manager.mark_inactive_except({second.source_id}) == 1
        results = manager.search(_vector(0), limit=5)
        assert [row.source_id for row in results] == [second.source_id]
        fingerprints = manager.source_fingerprints()
        assert fingerprints[source_record.source_id] == (
            source_record.source_revision,
            source_record.content_hash,
            False,
        )
        assert fingerprints[second.source_id][2] is True

    def test_mark_inactive_except_empty_set_deactivates_everything(self, vec_db, source_record):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        manager.upsert(source_record, _vector(0))
        assert manager.mark_inactive_except(set()) == 1
        assert manager.search(_vector(0), limit=5) == []

    def test_inactive_rows_are_not_deleted(self, vec_db, source_record):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        manager.upsert(source_record, _vector(0))
        manager.mark_inactive_except(set())
        assert manager.count_sources() == 1
        assert manager.source_fingerprints()[source_record.source_id][2] is False


class TestStatus:
    def test_fresh_database_is_ready_and_stamped(self, vec_db):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        assert manager.status().state == "ready"
        conn = manager._conn
        assert conn is not None
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM index_meta")}
        assert rows["schema_version"] == "2"
        assert rows["model_id"] == MODEL_ID
        assert rows["model_revision"] == MODEL_REVISION
        assert rows["vector_dimension"] == str(MODEL_DIMENSION)

    def test_reopen_matching_fingerprint_is_ready(self, vec_db):
        _ensure_sqlite_vec()
        manager = VectorIndexManager(vec_db)
        manager.close()
        reopened = VectorIndexManager(vec_db)
        assert reopened.status().state == "ready"
