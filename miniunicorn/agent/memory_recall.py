"""Bounded provenance-aware recall over the local vector index.

The recall service embeds the user query locally, over-fetches a candidate
set from the index, discards candidates below the similarity floor, de-dups
against core memory content, ranks by a deterministic weighted score, and
selects at most ``max_results`` records under a dedicated token budget.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Callable, Sequence

from miniunicorn.agent.memory_sources import _content_hash
from miniunicorn.agent.vector_index import IndexCandidate
from miniunicorn.embedding.types import EmbeddingResult

DEFAULT_SIMILARITY_FLOOR = 0.45
DEFAULT_OVERFETCH = 30
DEFAULT_MAX_RESULTS = 5
DEFAULT_TOKEN_BUDGET = 1200


@dataclass(frozen=True)
class RecallRecord:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    text: str
    content_hash: str
    similarity: float
    score: float
    token_count: int
    synchronized: bool


@dataclass(frozen=True)
class RecallOutcome:
    records: tuple[RecallRecord, ...]
    fallback_reason: str | None
    latency_ms: float


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@lru_cache(maxsize=1)
def _tokenizer() -> Callable[[str], int] | None:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(encoding.encode(text, disallowed_special=()))
    except Exception:
        return None


def _count_tokens(text: str) -> int:
    tokenizer = _tokenizer()
    if tokenizer is not None:
        try:
            return max(1, tokenizer(text))
        except Exception:
            pass
    return max(1, math.ceil(len(text) / 4))


def _parse_updated_at(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _recency(updated_at: str) -> float:
    parsed = _parse_updated_at(updated_at)
    if parsed is None:
        return 0.0
    age_days = max(0.0, (datetime.now() - parsed).total_seconds() / 86400.0)
    return max(0.0, 1.0 - age_days / 365.0)


def _score(candidate: IndexCandidate) -> float:
    recency = _recency(candidate.updated_at)
    return (
        0.85 * candidate.similarity
        + 0.10 * _clamp(candidate.importance, 0.0, 1.0)
        + 0.05 * recency
    )


def _map_failure(code: str) -> str:
    if code in ("disabled", "dependency_missing", "index_missing", "index_stale", "index_corrupt"):
        return code
    if code in ("inference_failed", "non_finite", "source_invalid", "io_error", "cancelled"):
        return "inference_failed"
    if code in ("not_downloaded", "download_failed", "hash_mismatch", "model_load_failed"):
        return "model_not_ready"
    return "model_not_ready"


class MemoryRecallService:
    """Rank and bound recall results for one turn."""

    def __init__(
        self,
        index: object,
        embedder: object,
        *,
        similarity_floor: float = DEFAULT_SIMILARITY_FLOOR,
        overfetch: int = DEFAULT_OVERFETCH,
        max_results: int = DEFAULT_MAX_RESULTS,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        token_counter: Callable[[str], int] = _count_tokens,
    ) -> None:
        self.index = index
        self.embedder = embedder
        self.similarity_floor = similarity_floor
        self.overfetch = overfetch
        self.max_results = max_results
        self.token_budget = token_budget
        self.token_counter = token_counter

    async def recall(self, query: str, *, core_texts: Sequence[str] = ()) -> RecallOutcome:
        started = time.perf_counter()
        embedded: EmbeddingResult = await self.embedder.embed([query[:2000]])
        if embedded.failure is not None:
            return self._fallback(_map_failure(embedded.failure.code), started)
        if not self.index.is_search_ready():
            return self._fallback(self.index.fallback_reason(), started)
        # Embedder may legitimately return an empty vector set (or a zero-length
        # vector) for degenerate inputs; guard before indexing into it.
        if not embedded.vectors or not embedded.vectors[0]:
            return self._fallback("inference_failed", started)
        candidates = self.index.search(list(embedded.vectors[0]), limit=self.overfetch)
        core_hashes = {_content_hash(text) for text in core_texts if text.strip()}
        selected: list[RecallRecord] = []
        seen = set(core_hashes)
        used = 0
        ranked = sorted(candidates, key=lambda c: (-_score(c), -c.similarity, c.source_id))
        for candidate in ranked:
            if candidate.similarity < self.similarity_floor or candidate.content_hash in seen:
                continue
            tokens = self.token_counter(candidate.text)
            if tokens > self.token_budget - used:
                continue
            selected.append(self._to_record(candidate, tokens))
            seen.add(candidate.content_hash)
            used += tokens
            if len(selected) == self.max_results:
                break
        return RecallOutcome(tuple(selected), None, _elapsed_ms(started))

    def _to_record(self, candidate: IndexCandidate, tokens: int) -> RecallRecord:
        return RecallRecord(
            source_id=candidate.source_id,
            source_type=candidate.source_type,
            source_file=candidate.source_file,
            source_revision=candidate.source_revision,
            text=candidate.text,
            content_hash=candidate.content_hash,
            similarity=candidate.similarity,
            score=_score(candidate),
            token_count=tokens,
            synchronized=True,
        )

    def _fallback(self, reason: str, started: float) -> RecallOutcome:
        return RecallOutcome((), reason, _elapsed_ms(started))
