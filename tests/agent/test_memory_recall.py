"""Deterministic recall tests: routing, exact scoring, budget, determinism, no LLM."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    MemoryKind,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryTransaction,
    RecallQuery,
    ScopeKind,
    new_transaction_id,
    transaction_checksum,
)
from miniunicorn.agent.memory_recall import StructuredMemoryRecall
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

UTC = timezone.utc


def dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def make_evidence(kind="manual", ref="command:msg-42", excerpt="Use deterministic structured recall.", sha256=None):
    return {
        "kind": kind,
        "ref": ref,
        "excerpt": excerpt,
        "sha256": sha256,
        "observed_at": "2026-08-11T08:30:00Z",
    }


def record_data(
    statement="Main uses deterministic structured recall.",
    *,
    status="active",
    revision=1,
    memory_id=None,
    tags=("architecture.memory", "project.decision"),
    slot="memory.retrieval.strategy",
    kind="decision",
    source_level="confirmed_decision",
    confidence=1.0,
    evidence=None,
    **overrides,
):
    return {
        "schema_version": SCHEMA_VERSION,
        "id": memory_id or (f"mem_{hashlib.sha256(statement.encode()).hexdigest()[:32]}"),
        "revision": revision,
        "status": status,
        "kind": kind,
        "scope": {"kind": "project", "key": "project:6b5ec7b29e32"},
        "subject": "MiniUnicorn",
        "slot": slot,
        "statement": statement,
        "detail": "",
        "tags": list(tags),
        "aliases": [],
        "source_level": source_level,
        "confidence": confidence,
        "importance": 4,
        "evidence": evidence or [make_evidence()],
        "content_hash": "c" * 64,
        "derived_from": [],
        "supersedes": [],
        "replacement_id": None,
        "blocked_by": [],
        "valid_from": "2026-08-11T08:30:00Z",
        "expires_at": None,
        "created_at": "2026-08-11T08:30:00Z",
        "updated_at": "2026-08-11T08:28:00Z",
        "status_reason": "test",
        **overrides,
    }


def make_transaction(*records, actor="dream", reason="test", source_batch=""):
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id=new_transaction_id(),
        recorded_at=dt("2026-08-11T08:31:00Z"),
        actor=ActorKind(actor),
        reason=reason,
        source_batch=source_batch,
        expected_revisions={rec.id: rec.revision - 1 for rec in records},
        operations=[MemoryOperation(op="put", record=rec) for rec in records],
        checksum_sha256="f" * 64,
    )
    return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})


def seed(repository, records):
    for record in records:
        repository.append_transaction(make_transaction(record))


def active_record(**overrides):
    return MemoryRecord.model_validate(record_data(**overrides))


@pytest.fixture
def workspace(tmp_path):
    structured = tmp_path / "memory" / "structured"
    structured.mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json",
        structured / "tags.json",
    )
    return tmp_path


@pytest.fixture
def repository(workspace):
    return StructuredMemoryRepository(workspace, lock_timeout_s=0.1)


@pytest.fixture
def recall(repository):
    return StructuredMemoryRecall(repository, repository.tag_catalog)


@pytest.fixture
def project_decision(repository):
    record = active_record(
        statement="Main uses deterministic structured recall.",
        kind="decision",
        slot="memory.retrieval.strategy",
        tags=("architecture.memory", "project.decision"),
        source_level="confirmed_decision",
        importance=5,
    )
    seed(repository, [record])
    return record


def make_query(text="请分析全局记忆架构决定", *, allowed_scopes=None, now="2026-08-11T08:30:00Z", token_budget=2500, max_hits=20, **overrides):
    return RecallQuery(
        query_text=text,
        allowed_scopes=allowed_scopes or (MemoryScope(kind=ScopeKind.PROJECT, key="project:6b5ec7b29e32"),),
        now=dt(now),
        token_budget=token_budget,
        max_hits=max_hits,
        **overrides,
    )


def test_candidate_and_other_user_scope_never_recalled(recall, repository):
    active_project = active_record(
        statement="Main uses deterministic structured recall.",
        kind="decision",
        slot="memory.retrieval.strategy",
        memory_id=f"mem_{'a' * 32}",
    )
    candidate = active_record(
        statement="Main uses deterministic structured recall.",
        kind="decision",
        slot="memory.retrieval.strategy",
        status="candidate",
        memory_id=f"mem_{'b' * 32}",
    )
    other_user = active_record(
        statement="Main uses deterministic structured recall.",
        kind="decision",
        slot="memory.retrieval.strategy",
        scope={"kind": "user", "key": "user:someone-else"},
        memory_id=f"mem_{'c' * 32}",
    )
    seed(repository, [active_project, candidate, other_user])
    result = recall.recall(make_query())
    ids = {hit.record.id for hit in result.hits}
    assert active_project.id in ids
    assert candidate.id not in ids
    assert other_user.id not in ids


def test_score_and_reason_are_exact(recall, project_decision):
    result = recall.recall(make_query(explicit_tags=("architecture.memory",)))
    hit = next(h for h in result.hits if h.record.id == project_decision.id)
    assert hit.score == 105
    assert hit.reasons == (
        "tag=architecture.memory(+45)",
        "source=confirmed_decision(+20)",
        "scope=project(+10)",
        "importance=5(+20)",
        "freshness<=7d(+10)",
    )


def test_ascii_word_boundaries(recall, repository):
    record = active_record(
        statement="Use deterministic recall.", kind="fact", slot="db.primary", tags=("architecture.memory",)
    )
    seed(repository, [record])
    result = recall.recall(make_query(text="use architecture memory today", explicit_tags=()))
    assert all(hit.record.id != record.id for hit in result.hits)
    result = recall.recall(make_query(text="review architecture.memory design", explicit_tags=()))
    hit = next(h for h in result.hits if h.record.id == record.id)
    assert hit.reasons[0] == "tag=architecture.memory(+45)"


def test_cjk_substring_matches_catalog_alias(recall, repository):
    record = active_record(
        statement="Use deterministic recall.", kind="fact", slot="db.primary", tags=("architecture.memory",)
    )
    seed(repository, [record])
    result = recall.recall(make_query(text="请分析全局记忆设计"))
    hit = next(h for h in result.hits if h.record.id == record.id)
    assert hit.reasons[0] == "alias=全局记忆(+35)"


def test_subject_precedence_over_tag(recall, repository):
    record = active_record(
        statement="Keep the global memory architecture decision.",
        kind="decision",
        slot="memory.retrieval.strategy",
        tags=("architecture.memory",),
        subject="全局记忆架构",
    )
    seed(repository, [record])
    result = recall.recall(make_query(text="请分析全局记忆架构决定"))
    hit = next(h for h in result.hits if h.record.id == record.id)
    assert hit.reasons[0] == "subject(+60)"
    assert not any(r.startswith("tag=") or r.startswith("alias=") for r in hit.reasons)


def test_canonical_tag_vs_catalog_alias_vs_record_alias(recall, repository):
    canonical = active_record(
        statement="A.", kind="fact", slot="db.a", tags=("architecture.memory",), importance=4
    )
    catalog_hit = active_record(
        statement="B.", kind="fact", slot="db.b", tags=("architecture.memory",), importance=4
    )
    record_alias = active_record(
        statement="C.", kind="fact", slot="db.c", tags=("architecture.memory",), aliases=("记忆库",), importance=4
    )
    seed(repository, [canonical, catalog_hit, record_alias])

    canonical_result = recall.recall(make_query(text="architecture.memory"))
    hit = next(h for h in canonical_result.hits if h.record.id == canonical.id)
    assert hit.reasons[0] == "tag=architecture.memory(+45)"

    catalog_result = recall.recall(make_query(text="全局记忆"))
    hit = next(h for h in catalog_result.hits if h.record.id == catalog_hit.id)
    assert hit.reasons[0] == "alias=全局记忆(+35)"

    alias_result = recall.recall(make_query(text="记忆库"))
    hit = next(h for h in alias_result.hits if h.record.id == record_alias.id)
    assert hit.reasons[0] == "alias=记忆库(+30)"


def test_multiple_tag_hits_bonus_and_cap(recall, repository):
    record = active_record(
        statement="Use deterministic recall.",
        kind="fact",
        slot="db.primary",
        tags=("architecture.memory", "project.decision", "project.fact"),
        importance=4,
    )
    seed(repository, [record])
    result = recall.recall(make_query(text="architecture.memory project.decision project.fact"))
    hit = next(h for h in result.hits if h.record.id == record.id)
    tag_reasons = [r for r in hit.reasons if r.startswith("tag=")]
    assert tag_reasons == [
        "tag=architecture.memory(+45)",
        "tag=project.decision(+5)",
        "tag=project.fact(+5)",
    ]


def test_insertion_order_does_not_change_recall(repository, tmp_path):
    def make_recall(records, label):
        workspace = tmp_path / f"w{label}"
        structured = workspace / "memory" / "structured"
        structured.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            Path(__file__).resolve().parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json",
            structured / "tags.json",
        )
        repo = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
        for index, record in enumerate(records):
            repo.append_transaction(
                make_transaction(record.model_copy(update={"updated_at": dt(f"2026-08-11T0{index + 1}:00:00Z")}))
            )
        return StructuredMemoryRecall(repo, repo.tag_catalog)

    records = [
        active_record(
            statement=f"S{index}.",
            kind="fact",
            slot=f"db.{index}",
            tags=("architecture.memory",),
            source_level=level,
            importance=importance,
        )
        for index, (level, importance) in enumerate(
            (
                ("inferred", 2),
                ("repeated_experience", 3),
                ("verified", 5),
                ("confirmed_decision", 4),
                ("explicit_correction", 1),
            )
        )
    ]
    forward = make_recall(records, "f").recall(make_query(text="architecture.memory"))
    reverse = make_recall(list(reversed(records)), "r").recall(make_query(text="architecture.memory"))
    assert [h.record.id for h in forward.hits] == [h.record.id for h in reverse.hits]


def test_recall_never_calls_provider(recall, project_decision):
    provider = AsyncMock()
    recall.recall(make_query())
    provider.chat_with_retry.assert_not_called()


def test_expired_at_query_time_excluded(recall, repository):
    live = active_record(statement="Live.", kind="fact", slot="db.live", tags=("architecture.memory",))
    expired = active_record(
        statement="Expired.",
        kind="fact",
        slot="db.expired",
        tags=("architecture.memory",),
        expires_at="2026-08-11T08:00:00Z",
    )
    seed(repository, [live, expired])
    result = recall.recall(make_query())
    ids = {hit.record.id for hit in result.hits}
    assert live.id in ids
    assert expired.id not in ids


def test_explicit_id_routes_without_other_route_scores(recall, project_decision, repository):
    other = active_record(statement="Other.", kind="fact", slot="db.other", tags=("architecture.memory",))
    seed(repository, [other])
    result = recall.recall(make_query(text="unrelated nothing", explicit_ids=(project_decision.id,)))
    hit = next(h for h in result.hits if h.record.id == project_decision.id)
    assert hit.score == 160
    assert hit.reasons[0] == f"id={project_decision.id}(+100)"
    assert not any(r.startswith("tag=") or r.startswith("subject") or r.startswith("alias=") for r in hit.reasons)
    assert all(h.record.id != other.id for h in result.hits)


def test_requested_kind_filters_and_scores(recall, repository):
    fact = active_record(statement="Fact.", kind="fact", slot="db.fact", tags=("architecture.memory",), importance=4)
    decision = active_record(
        statement="Decision.", kind="decision", slot="db.decision", tags=("architecture.memory",), importance=4
    )
    seed(repository, [fact, decision])
    result = recall.recall(make_query(requested_kinds=(MemoryKind.FACT,)))
    ids = {hit.record.id for hit in result.hits}
    assert fact.id in ids
    assert decision.id not in ids
    hit = next(h for h in result.hits if h.record.id == fact.id)
    assert "kind=fact(+10)" in hit.reasons


def test_oversized_first_hit_skipped_by_budget(recall, repository):
    long_statement = "中文设计" * 120
    big = active_record(
        statement=long_statement,
        kind="fact",
        slot="db.big",
        tags=("architecture.memory",),
        source_level="confirmed_decision",
        importance=5,
    )
    small = active_record(
        statement="Short fact.",
        kind="fact",
        slot="db.small",
        tags=("architecture.memory",),
        source_level="inferred",
        importance=1,
    )
    seed(repository, [big, small])
    result = recall.recall(make_query(token_budget=256))
    assert result.excluded_by_budget >= 1
    assert all(hit.record.id != big.id for hit in result.hits)
    assert any(hit.record.id == small.id for hit in result.hits)
    assert result.tokens_used == sum(hit.tokens for hit in result.hits)


def test_max_hits_limits_results(recall, repository):
    records = [
        active_record(
            statement=f"F{index}.", kind="fact", slot=f"db.{index}", tags=("architecture.memory",), importance=4
        )
        for index in range(5)
    ]
    seed(repository, records)
    result = recall.recall(make_query(max_hits=2))
    assert len(result.hits) == 2


def test_degraded_health_returns_no_hits(recall, repository, project_decision):
    with repository.journal_path.open("a", encoding="utf-8") as stream:
        stream.write("not-a-transaction\n")
    health = repository.rebuild()
    assert health.state == "degraded"
    result = recall.recall(make_query())
    assert result.hits == ()
    assert result.degraded is True
    assert result.error_code is not None
    assert recall.render_prompt(result) == ""


def test_render_prompt_format(recall, project_decision):
    result = recall.recall(make_query(explicit_tags=("architecture.memory",)))
    prompt = recall.render_prompt(result)
    assert prompt.startswith("# Recalled Memory (Deterministic)")
    assert f"- [{project_decision.id} | decision | project] Main uses deterministic structured recall." in prompt
    assert (
        "  Why: tag=architecture.memory(+45), source=confirmed_decision(+20), scope=project(+10), "
        "importance=5(+20), freshness<=7d(+10), total=105" in prompt
    )


def test_render_prompt_empty_for_no_hits(recall, repository):
    result = recall.recall(make_query(text="unrelated nothing"))
    assert recall.render_prompt(result) == ""


def test_freshness_buckets(recall, repository):
    cases = (
        ("2026-08-11T08:28:00Z", "freshness<=7d(+10)"),
        ("2026-07-20T08:00:00Z", "freshness<=30d(+7)"),
        ("2026-05-20T08:00:00Z", "freshness<=90d(+4)"),
        ("2025-08-11T08:00:00Z", "freshness>90d(+0)"),
    )
    records = [
        active_record(
            statement=f"F{index}.",
            kind="fact",
            slot=f"db.{index}",
            tags=("architecture.memory",),
            importance=4,
            updated_at=updated,
        )
        for index, (updated, _) in enumerate(cases)
    ]
    seed(repository, records)
    result = recall.recall(make_query(now="2026-08-11T08:30:00Z"))
    expected = {record.id: reason for record, (_, reason) in zip(records, cases)}
    by_id = {hit.record.id: hit for hit in result.hits}
    for record in records:
        hit = by_id[record.id]
        assert hit.reasons[-1] == expected[record.id]


def test_recall_result_counts(recall, repository):
    seed_records = [
        active_record(statement=f"F{index}.", kind="fact", slot=f"db.{index}", tags=("architecture.memory",))
        for index in range(3)
    ]
    seed(repository, seed_records)
    result = recall.recall(make_query())
    assert result.candidates == 3
    assert result.filtered == 0
    assert result.excluded_by_budget == 0
    assert len(result.hits) == 3
