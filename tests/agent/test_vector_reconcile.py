"""Tests for incremental reconcile between catalog scan and the vector index."""

from __future__ import annotations

from dataclasses import replace

import pytest

from miniunicorn.agent.memory_sources import MemorySourceRecord, SourceScan
from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding import MODEL_DIMENSION
from miniunicorn.embedding.types import EmbeddingFailure, EmbeddingResult

_BATCH_SIZE = 32


class RecordingEmbedder:
    """Fake LocalEmbeddingProvider that records embedded texts."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.calls: list[list[str]] = []
        self.fail_batches: set[int] = set()
        self.short_batches: set[int] = set()

    async def embed(self, texts: list[str], model: str | None = None) -> EmbeddingResult:
        self.texts.extend(texts)
        self.calls.append(list(texts))
        batch_index = len(self.calls) - 1
        if batch_index in self.fail_batches:
            return EmbeddingResult(
                failure=EmbeddingFailure("inference_failed", "模拟推理失败", True)
            )
        if batch_index in self.short_batches:
            return EmbeddingResult(vectors=tuple(_vector(i) for i in range(len(texts) - 1)))
        return EmbeddingResult(vectors=tuple(_vector(i) for i in range(len(texts))))


def _vector(i: int) -> tuple[float, ...]:
    vector = [0.0] * MODEL_DIMENSION
    vector[i % MODEL_DIMENSION] = 1.0
    return tuple(vector)


@pytest.fixture
def manager(tmp_path):
    pytest.importorskip("sqlite_vec")
    return VectorIndexManager(tmp_path / "memory.db")


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
    )


@pytest.mark.asyncio
async def test_reconcile_embeds_only_new_or_changed_records(manager, source_record):
    embedder = RecordingEmbedder()
    first = await manager.reconcile(SourceScan((source_record,), ()), embedder)
    second = await manager.reconcile(SourceScan((source_record,), ()), embedder)
    changed = replace(source_record, source_revision="2", content_hash="c" * 64, text="changed")
    third = await manager.reconcile(SourceScan((changed,), ()), embedder)
    assert (first.inserted, second.unchanged, third.updated) == (1, 1, 1)
    assert embedder.texts == [source_record.text, "changed"]
    assert manager.count_sources() == 1


@pytest.mark.asyncio
async def test_reconcile_failure_records_failed_without_writing(manager, source_record):
    embedder = RecordingEmbedder()
    embedder.fail_batches.add(0)
    report = await manager.reconcile(SourceScan((source_record,), ()), embedder)
    assert report.failed == 1
    assert report.inserted == 0
    assert report.failures[0]["code"] == "inference_failed"
    assert manager.count_sources() == 0


@pytest.mark.asyncio
async def test_reconcile_marks_missing_records_inactive(manager, source_record):
    embedder = RecordingEmbedder()
    first = replace(source_record, source_id="user:stale:9", text="已删除的记忆")
    await manager.reconcile(SourceScan((first,), ()), embedder)
    report = await manager.reconcile(SourceScan((source_record,), ()), embedder)
    assert report.inactive == 1
    fingerprints = manager.source_fingerprints()
    assert fingerprints[first.source_id] == (first.source_revision, first.content_hash, False)
    assert fingerprints[source_record.source_id][2] is True


@pytest.mark.asyncio
async def test_reconcile_batches_embedding(manager):
    pytest.importorskip("sqlite_vec")
    records = [
        MemorySourceRecord(
            source_id=f"user:bulk:{i}",
            source_type="user",
            source_file="USER.md",
            source_revision="1",
            content_hash=f"{i:064x}",
            text=f"第 {i} 条记忆",
            importance=0.5,
        )
        for i in range(_BATCH_SIZE + 1)
    ]
    embedder = RecordingEmbedder()
    report = await manager.reconcile(SourceScan(tuple(records), ()), embedder)
    assert report.inserted == _BATCH_SIZE + 1
    assert len(embedder.calls) == 2
    assert len(embedder.calls[0]) == _BATCH_SIZE
    assert len(embedder.calls[1]) == 1


@pytest.mark.asyncio
async def test_reconcile_partial_batch_failure_keeps_other_batches(manager):
    records = [
        MemorySourceRecord(
            source_id=f"user:bulk:{i}",
            source_type="user",
            source_file="USER.md",
            source_revision="1",
            content_hash=f"{i:064x}",
            text=f"第 {i} 条记忆",
            importance=0.5,
        )
        for i in range(_BATCH_SIZE + 1)
    ]
    embedder = RecordingEmbedder()
    embedder.fail_batches.add(1)
    report = await manager.reconcile(SourceScan(tuple(records), ()), embedder)
    assert report.inserted == _BATCH_SIZE
    assert report.failed == 1
    assert manager.count_sources() == _BATCH_SIZE


@pytest.mark.asyncio
async def test_reconcile_short_result_does_not_write_batch(manager):
    pytest.importorskip("sqlite_vec")
    records = [
        MemorySourceRecord(
            source_id=f"user:bulk:{i}",
            source_type="user",
            source_file="USER.md",
            source_revision="1",
            content_hash=f"{i:064x}",
            text=f"第 {i} 条记忆",
            importance=0.5,
        )
        for i in range(2)
    ]
    embedder = RecordingEmbedder()
    embedder.short_batches.add(0)
    report = await manager.reconcile(SourceScan(tuple(records), ()), embedder)
    assert report.inserted == 0
    assert report.failed == 2
    assert manager.count_sources() == 0
