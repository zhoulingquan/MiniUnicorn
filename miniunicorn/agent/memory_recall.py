"""Deterministic lexical recall: routing, exact scoring, budget, prompt rendering.

Recall never calls a model: it filters against the repository index, scores with
fixed per-attribute weights, applies a strict token budget, and renders a
stable "Why" for every hit. The same stable memory ID is shown to the model,
the user, and the audit trail.
"""

from __future__ import annotations

import re
from datetime import datetime

from miniunicorn.agent.memory_models import (
    MemoryRecord,
    RecallHit,
    RecallQuery,
    RecallResult,
    ScopeKind,
    SourceLevel,
    TagCatalog,
    normalize_match_text,
)
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

SCOPE_SCORE = {
    ScopeKind.SESSION: 12,
    ScopeKind.PROJECT: 10,
    ScopeKind.USER: 8,
    ScopeKind.SHARED: 6,
}

SOURCE_SCORE = {
    SourceLevel.EXPLICIT_CORRECTION: 25,
    SourceLevel.CONFIRMED_DECISION: 20,
    SourceLevel.VERIFIED: 15,
    SourceLevel.REPEATED_EXPERIENCE: 10,
    SourceLevel.INFERRED: 0,
}

ROUTE_EXPLICIT_ID = 100
ROUTE_SUBJECT = 60
ROUTE_CANONICAL_TAG = 45
ROUTE_CATALOG_ALIAS = 35
ROUTE_RECORD_ALIAS = 30
ROUTE_EXTRA_BONUS = 5
ROUTE_TAG_CAP = 60
ROUTE_ALIAS_CAP = 45

KIND_EXTRA = 10

PROMPT_HEADER = "# Recalled Memory (Deterministic)"

_TOKENIZER = None


def _tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        import tiktoken

        _TOKENIZER = tiktoken.get_encoding("cl100k_base")
    return _TOKENIZER


def _has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _matches(text: str, needle: str) -> bool:
    if not needle:
        return False
    if _has_cjk(needle):
        return needle in text
    return re.search(rf"\b{re.escape(needle)}\b", text) is not None


def freshness_score(updated_at: datetime, now: datetime) -> tuple[int, str]:
    days = max(0, (now - updated_at).days)
    if days <= 7:
        return 10, "freshness<=7d(+10)"
    if days <= 30:
        return 7, "freshness<=30d(+7)"
    if days <= 90:
        return 4, "freshness<=90d(+4)"
    return 0, "freshness>90d(+0)"


class StructuredMemoryRecall:
    def __init__(self, repository: StructuredMemoryRepository, tag_catalog: TagCatalog) -> None:
        self._repository = repository
        self._tag_catalog = tag_catalog
        self._aliases_by_tag = {definition.name: definition.aliases for definition in tag_catalog.tags}

    def recall(self, query: RecallQuery) -> RecallResult:
        """Run deterministic recall over SQL-preauthorized candidates.

        The repository pre-filters candidates by scope, status, kind, and
        expiry inside SQLite; ``candidates`` counts only those preauthorized
        records and ``filtered`` counts only lexical route misses, because
        scope/kind/expiry misses are never seen by this loop.
        """
        health = self._repository.health
        if health.state != "healthy":
            return RecallResult(
                degraded=True,
                error_code=health.error_code,
                error_message=health.error_message,
            )
        query_norm = normalize_match_text(query.query_text)
        explicit_ids = set(query.explicit_ids)
        explicit_tags = frozenset(normalize_match_text(tag) for tag in query.explicit_tags)
        now = query.now

        candidates = 0
        filtered = 0
        scored = []
        records = self._repository.recall_candidates(
            allowed_scopes=query.allowed_scopes,
            requested_kinds=query.requested_kinds,
            now=query.now,
        )
        for record in records:
            candidates += 1
            if record.id in explicit_ids:
                route_score, route_reasons = ROUTE_EXPLICIT_ID, [f"id={record.id}(+100)"]
            else:
                route = self._route_from_query(record, query_norm, explicit_tags)
                if route is None:
                    filtered += 1
                    continue
                route_score, route_reasons = route
            total, reasons = self._score_total(record, query, route_score, route_reasons, now)
            scored.append((total, record, reasons))

        scored.sort(
            key=lambda item: (
                -item[0],
                -SOURCE_SCORE[item[1].source_level],
                -item[1].importance,
                -item[1].updated_at.timestamp(),
                item[1].id,
            )
        )

        hits = []
        used = 0
        excluded = 0
        rendered_hits: list[str] = []
        for total, record, reasons in scored:
            if len(hits) >= query.max_hits:
                break
            rendered = self._render_hit(record, reasons, total)
            prospective = f"{PROMPT_HEADER}\n\n" + "\n".join([*rendered_hits, rendered])
            prospective_tokens = len(_tokenizer().encode(prospective))
            if prospective_tokens > query.token_budget:
                excluded += 1
                continue
            tokens = prospective_tokens - used
            hits.append(
                RecallHit(record=record, score=total, reasons=tuple(reasons), tokens=tokens)  # type: ignore[attr-defined]
            )
            rendered_hits.append(rendered)
            used = prospective_tokens
        return RecallResult(hits=tuple(hits), candidates=candidates, filtered=filtered, excluded_by_budget=excluded, tokens_used=used)

    def render_prompt(self, result: RecallResult) -> str:
        if not result.hits:
            return ""
        lines = [PROMPT_HEADER, ""]
        for hit in result.hits:
            lines.append(f"- [{hit.record.id} | {hit.record.kind.value} | {hit.record.scope.kind.value}] {hit.record.statement}")
            lines.append(f"  Why: {', '.join(hit.reasons)}, total={hit.score}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal scoring
    # ------------------------------------------------------------------

    def _route_from_query(self, record: MemoryRecord, query_norm: str, explicit_tags: frozenset[str]) -> tuple[int, list[str]] | None:
        subject_hits = _matches(query_norm, normalize_match_text(record.subject))

        tag_hits = []
        for tag in record.tags:
            if _matches(query_norm, normalize_match_text(tag)) or normalize_match_text(tag) in explicit_tags:
                tag_hits.append(tag)
        tag_score, tag_reasons = self._capped_route_reasons(
            tag_hits, prefix="tag", initial=ROUTE_CANONICAL_TAG, cap=ROUTE_TAG_CAP
        )

        catalog_hits = []
        for tag in record.tags:
            for alias in self._aliases_by_tag.get(tag, ()):
                if _matches(query_norm, normalize_match_text(alias)):
                    catalog_hits.append(alias)
        catalog_score, catalog_reasons = self._capped_route_reasons(
            catalog_hits, prefix="alias", initial=ROUTE_CATALOG_ALIAS, cap=ROUTE_ALIAS_CAP
        )

        record_hits = []
        for alias in record.aliases:
            if _matches(query_norm, normalize_match_text(alias)):
                record_hits.append(alias)
        record_score, record_reasons = self._capped_route_reasons(
            record_hits, prefix="alias", initial=ROUTE_RECORD_ALIAS, cap=ROUTE_ALIAS_CAP
        )

        routes: list[tuple[int, list[str]]] = []
        if subject_hits:
            routes.append((ROUTE_SUBJECT, ["subject(+60)"]))
        if tag_hits:
            routes.append((tag_score, tag_reasons))
        if catalog_hits:
            routes.append((catalog_score, catalog_reasons))
        if record_hits:
            routes.append((record_score, record_reasons))
        if not routes:
            return None
        routes.sort(key=lambda item: -item[0])
        return routes[0]

    @staticmethod
    def _capped_route_reasons(
        matches: list[str], *, prefix: str, initial: int, cap: int
    ) -> tuple[int, list[str]]:
        if not matches:
            return 0, []
        remaining = cap
        reasons = []
        for index, match in enumerate(matches):
            requested = initial if index == 0 else ROUTE_EXTRA_BONUS
            contribution = min(requested, remaining)
            reasons.append(f"{prefix}={match}(+{contribution})")
            remaining -= contribution
        return cap - remaining, reasons

    def _score_total(
        self,
        record: MemoryRecord,
        query: RecallQuery,
        route_score: int,
        route_reasons: list[str],
        now: datetime,
    ) -> tuple[int, list[str]]:
        reasons = list(route_reasons)
        total = route_score
        if query.requested_kinds and record.kind in query.requested_kinds:
            reasons.append(f"kind={record.kind.value}(+{KIND_EXTRA})")
            total += KIND_EXTRA
        source_score = SOURCE_SCORE[record.source_level]
        reasons.append(f"source={record.source_level.value}(+{source_score})")
        total += source_score
        scope_score = SCOPE_SCORE[record.scope.kind]
        reasons.append(f"scope={record.scope.kind.value}(+{scope_score})")
        total += scope_score
        importance_score = record.importance * 4
        reasons.append(f"importance={record.importance}(+{importance_score})")
        total += importance_score
        fresh_score, fresh_reason = freshness_score(record.updated_at, now)
        reasons.append(fresh_reason)
        total += fresh_score
        return total, reasons

    def _render_hit(self, record: MemoryRecord, reasons: list[str], total: int) -> str:
        header = f"- [{record.id} | {record.kind.value} | {record.scope.kind.value}] {record.statement}"
        return f"{header}\n  Why: {', '.join(reasons)}, total={total}"
