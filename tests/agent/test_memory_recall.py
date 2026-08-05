"""Tests for bounded provenance-aware recall."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from miniunicorn.agent.memory_recall import MemoryRecallService
from miniunicorn.agent.memory_sources import _content_hash
from miniunicorn.agent.vector_index import IndexCandidate
from miniunicorn.embedding.types import EmbeddingFailure, EmbeddingResult


@dataclass(frozen=True)
class FakeIndex:
    candidates: tuple[IndexCandidate, ...] = ()
    ready: bool = True
    reason: str = "index_missing"

    def is_search_ready(self) -> bool:
        return self.ready

    def fallback_reason(self) -> str:
        return self.reason

    def search(self, query_embedding, limit: int = 5):
        return list(self.candidates[:limit])


class FakeEmbedder:
    async def embed(self, texts, model=None):
        return EmbeddingResult(vectors=tuple((0.5,) * 512 for _ in texts))


class FailingEmbedder:
    def __init__(self, code: str) -> None:
        self._code = code

    async def embed(self, texts, model=None):
        return EmbeddingResult(failure=EmbeddingFailure(self._code, "模拟失败", True))


def candidate(source_id: str, text: str, similarity: float, importance: float) -> IndexCandidate:
    return IndexCandidate(
        source_id=source_id,
        source_type="user",
        source_file="USER.md",
        source_revision="1",
        content_hash=_content_hash(text),
        text=text,
        importance=importance,
        metadata={},
        similarity=similarity,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


@pytest.mark.asyncio
async def test_recall_returns_five_unique_budgeted_records():
    index = FakeIndex(candidates=(
        candidate("a", "重复内容", 0.91, 0.5),
        candidate("b", "重复内容", 0.90, 1.0),
        *[candidate(str(i), "记忆" + str(i) * 400, 0.89 - i / 100, 0.5) for i in range(8)],
    ))
    outcome = await MemoryRecallService(index, FakeEmbedder(), token_budget=1200).recall(
        "查询", core_texts=("固定核心",)
    )
    assert outcome.fallback_reason is None
    assert len(outcome.records) <= 5
    assert sum(row.token_count for row in outcome.records) <= 1200
    assert len({row.content_hash for row in outcome.records}) == len(outcome.records)


@pytest.mark.asyncio
async def test_embedding_failure_returns_typed_fallback():
    embedder = FailingEmbedder("not_downloaded")
    outcome = await MemoryRecallService(FakeIndex(), embedder).recall("你好")
    assert outcome.records == ()
    assert outcome.fallback_reason == "model_not_ready"


@pytest.mark.asyncio
async def test_failure_codes_map_to_fallback_reasons():
    cases = {
        "dependency_missing": "dependency_missing",
        "inference_failed": "inference_failed",
        "index_missing": "index_missing",
        "index_stale": "index_stale",
        "index_corrupt": "index_corrupt",
        "disabled": "disabled",
        "model_load_failed": "model_not_ready",
        "download_failed": "model_not_ready",
    }
    for code, expected in cases.items():
        outcome = await MemoryRecallService(FakeIndex(), FailingEmbedder(code)).recall("查询")
        assert outcome.fallback_reason == expected, code


@pytest.mark.asyncio
async def test_index_not_ready_returns_index_fallback():
    index = FakeIndex(ready=False, reason="index_stale")
    outcome = await MemoryRecallService(index, FakeEmbedder()).recall("查询")
    assert outcome.records == ()
    assert outcome.fallback_reason == "index_stale"


@pytest.mark.asyncio
async def test_below_similarity_floor_is_discarded():
    index = FakeIndex(candidates=(
        candidate("high", "高相关", 0.80, 0.5),
        candidate("low", "低相关", 0.20, 0.5),
    ))
    outcome = await MemoryRecallService(index, FakeEmbedder(), similarity_floor=0.45).recall("查询")
    assert [row.source_id for row in outcome.records] == ["high"]


@pytest.mark.asyncio
async def test_core_texts_deduplicate_recall_records():
    index = FakeIndex(candidates=(
        candidate("dup", "核心内容", 0.99, 0.5),
        candidate("other", "其他记忆", 0.80, 0.5),
    ))
    outcome = await MemoryRecallService(index, FakeEmbedder()).recall(
        "查询", core_texts=("核心内容",)
    )
    assert [row.source_id for row in outcome.records] == ["other"]


@pytest.mark.asyncio
async def test_ranking_is_deterministic_and_score_based():
    candidates = (
        candidate("sim-high", "相似度高", 0.90, 0.1),
        candidate("imp-high", "重要度高", 0.85, 1.0),
        candidate("mid", "中间", 0.80, 0.5),
    )
    outcome = await MemoryRecallService(FakeIndex(candidates=candidates), FakeEmbedder()).recall("查询")
    scores = [row.score for row in outcome.records]
    assert scores == sorted(scores, reverse=True)
    first = outcome.records[0]
    assert first.source_id == "imp-high"
    assert first.score == pytest.approx(
        0.85 * 0.85 + 0.10 * 1.0 + 0.05 * 1.0, abs=0.01
    )


@pytest.mark.asyncio
async def test_recency_decays_linearly_with_updated_at():
    today = candidate("today", "今天", 0.80, 0.5)
    old = candidate("old", "很久以前", 0.80, 0.5)
    old = IndexCandidate(
        source_id=old.source_id, source_type=old.source_type, source_file=old.source_file,
        source_revision=old.source_revision, content_hash=old.content_hash, text=old.text,
        importance=old.importance, metadata={}, similarity=old.similarity,
        updated_at="2020-01-01 00:00:00",
    )
    outcome = await MemoryRecallService(FakeIndex(candidates=(today, old)), FakeEmbedder()).recall("查询")
    assert outcome.records[0].source_id == "today"
    old_row = next(row for row in outcome.records if row.source_id == "old")
    assert old_row.score == pytest.approx(
        0.85 * 0.80 + 0.10 * 0.5 + 0.05 * 0.0, abs=1e-6
    )


@pytest.mark.asyncio
async def test_token_budget_limits_total_tokens():
    index = FakeIndex(candidates=(
        *[candidate(str(i), "字" * 500, 0.90, 0.5) for i in range(10)],
    ))
    outcome = await MemoryRecallService(index, FakeEmbedder(), token_budget=600).recall("查询")
    assert len(outcome.records) <= 3
    assert sum(row.token_count for row in outcome.records) <= 600


@pytest.mark.asyncio
async def test_latency_is_recorded():
    index = FakeIndex(candidates=(candidate("a", "内容", 0.9, 0.5),))
    outcome = await MemoryRecallService(index, FakeEmbedder()).recall("查询")
    assert outcome.latency_ms >= 0.0
