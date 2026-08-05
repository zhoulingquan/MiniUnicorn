"""Tests for atomic, cancellable vector index rebuild."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from miniunicorn.agent.memory_sources import MemorySourceCatalog, MemorySourceRecord
from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding import MODEL_DIMENSION
from miniunicorn.embedding.types import EmbeddingResult


class RecordingEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        self.texts.extend(texts)
        vectors = [[0.0] * MODEL_DIMENSION for _ in texts]
        for i in range(len(texts)):
            vectors[i][i % MODEL_DIMENSION] = 1.0
        return EmbeddingResult(vectors=tuple(tuple(v) for v in vectors))


class BlockingEmbedder:
    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        return EmbeddingResult()


@pytest.fixture
def catalog(tmp_path) -> MemorySourceCatalog:
    (tmp_path / "USER.md").write_text("# 用户偏好\n\n喜欢深色主题\n", encoding="utf-8")
    return MemorySourceCatalog(tmp_path)


@pytest.fixture
def embedder() -> RecordingEmbedder:
    return RecordingEmbedder()


def _seeded_index(db: Path) -> VectorIndexManager:
    manager = VectorIndexManager(db)
    record = MemorySourceRecord(
        source_id="user:preferences:1",
        source_type="user",
        source_file="USER.md",
        source_revision="1",
        content_hash="a" * 64,
        text="旧内容",
        importance=0.5,
    )
    manager.upsert(record, [1.0] + [0.0] * (MODEL_DIMENSION - 1))
    return manager


@pytest.mark.asyncio
async def test_rebuild_restores_all_sources_after_db_deleted(tmp_path, catalog, embedder):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "memory" / "memory.db"
    manager = VectorIndexManager(db)
    await manager.rebuild(catalog, embedder)
    manager.close()
    db.unlink()
    report = await VectorIndexManager(db).rebuild(catalog, embedder)
    assert report.validated is True
    reopened = VectorIndexManager(db)
    assert reopened.count_active_sources() == len(catalog.scan().records)
    reopened.close()


@pytest.mark.asyncio
async def test_cancelled_rebuild_leaves_previous_database_bytes(tmp_path, catalog):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "memory" / "memory.db"
    manager = _seeded_index(db)
    manager.close()
    before = db.read_bytes()
    cancel = asyncio.Event()
    cancel.set()
    report = await VectorIndexManager(db).rebuild(
        catalog, BlockingEmbedder(), cancel_event=cancel
    )
    assert report.state == "cancelled"
    assert db.read_bytes() == before
    assert not db.with_name("memory.db.rebuilding").exists()


@pytest.mark.asyncio
async def test_rebuild_success_replaces_db_atomically(tmp_path, catalog, embedder):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "memory" / "memory.db"
    manager = _seeded_index(db)
    manager.close()
    before = db.read_bytes()
    report = await VectorIndexManager(db).rebuild(catalog, embedder)
    assert report.state == "ready"
    assert report.validated is True
    assert db.read_bytes() != before
    assert not db.with_name("memory.db.rebuilding").exists()
    assert report.backup is not None
    assert Path(report.backup).exists()


@pytest.mark.asyncio
async def test_rebuild_never_leaves_rebuilding_file_on_success(tmp_path, catalog, embedder):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "memory" / "memory.db"
    manager = VectorIndexManager(db)
    await manager.rebuild(catalog, embedder)
    manager.close()
    assert not db.with_name("memory.db.rebuilding").exists()
    assert db.exists()


@pytest.mark.asyncio
async def test_rebuild_reports_validation_failure(tmp_path, catalog):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "memory" / "memory.db"
    manager = VectorIndexManager(db)
    report = await manager.rebuild(catalog, BlockingEmbedder())
    assert report.state == "failed"
    assert report.validated is False
    assert not db.with_name("memory.db.rebuilding").exists()


@pytest.mark.asyncio
async def test_validate_roundtrip_embeds_first_source_and_finds_it(tmp_path, catalog, embedder):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "memory" / "memory.db"
    manager = VectorIndexManager(db)
    await manager.rebuild(catalog, embedder)
    validation = await manager.validate(embedder)
    assert validation.ok
    manager.close()
