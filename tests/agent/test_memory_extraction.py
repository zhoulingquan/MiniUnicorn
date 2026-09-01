"""Strict extraction parser tests: contract shape, evidence, tags, atomicity."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from miniunicorn.agent.memory_extraction import (
    MemoryExtractionError,
    parse_extraction_batch,
)
from miniunicorn.agent.memory_models import EvidenceKind, EvidenceRef, ScopeKind, TagCatalog

UTC = timezone.utc


@pytest.fixture
def evidence_catalog():
    return {
        "history:1": EvidenceRef(
            kind=EvidenceKind.HISTORY,
            ref="history:1",
            excerpt="Main uses deterministic structured recall.",
            observed_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        ),
        "reflection:2": EvidenceRef(
            kind=EvidenceKind.REFLECTION,
            ref="reflection:2",
            excerpt="Batch ingest must be fail-closed.",
            observed_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        ),
    }


@pytest.fixture
def tag_catalog():
    tags = [
        {"name": "architecture.memory", "aliases": ["全局记忆"]},
        {"name": "project.decision", "aliases": ["决定"]},
    ]
    return TagCatalog.model_validate({"schema_version": 1, "tags": tags})


def valid_proposal(**overrides):
    proposal = {
        "proposal_index": 0,
        "kind": "decision",
        "scope_hint": "project",
        "subject": "MiniUnicorn",
        "slot": "memory.retrieval.strategy",
        "statement": "Main uses deterministic structured recall.",
        "detail": "No embeddings are used.",
        "tags": ["architecture.memory", "project.decision"],
        "aliases": [],
        "confidence": 1.0,
        "importance": 5,
        "evidence_refs": ["history:1"],
        "speech_act": "confirmed_decision",
        "expires_at": None,
    }
    proposal.update(overrides)
    return proposal


def valid_batch(*proposals):
    return {"schema_version": 1, "proposals": list(proposals)}


@pytest.mark.parametrize(
    "raw", ["nothing new", "{}", '{"schema_version":1,"proposals":[],"extra":1}']
)
def test_extraction_rejects_non_contract_output(raw, evidence_catalog, tag_catalog):
    with pytest.raises(MemoryExtractionError):
        parse_extraction_batch(raw, evidence_catalog, tag_catalog)


def test_extraction_rejects_malformed_json(evidence_catalog, tag_catalog):
    with pytest.raises(MemoryExtractionError):
        parse_extraction_batch("{not json", evidence_catalog, tag_catalog)


def test_valid_empty_batch_is_accepted(evidence_catalog, tag_catalog):
    parsed = parse_extraction_batch(
        '{"schema_version":1,"proposals":[]}', evidence_catalog, tag_catalog
    )
    assert parsed.proposals == ()


def test_valid_single_proposal_is_accepted(evidence_catalog, tag_catalog):
    parsed = parse_extraction_batch(
        json.dumps(valid_batch(valid_proposal())), evidence_catalog, tag_catalog
    )
    assert len(parsed.proposals) == 1
    assert parsed.proposals[0].subject == "MiniUnicorn"


def test_fenced_json_is_stripped(evidence_catalog, tag_catalog):
    raw = f"```json\n{json.dumps(valid_batch(valid_proposal()))}\n```"
    parsed = parse_extraction_batch(raw, evidence_catalog, tag_catalog)
    assert len(parsed.proposals) == 1


def test_missing_evidence_ref_rejects_batch(evidence_catalog, tag_catalog):
    raw = json.dumps(valid_batch(valid_proposal(evidence_refs=["history:99"])))
    with pytest.raises(MemoryExtractionError, match="unresolved evidence ref"):
        parse_extraction_batch(raw, evidence_catalog, tag_catalog)


def test_unknown_tag_rejects_batch(evidence_catalog, tag_catalog):
    raw = json.dumps(valid_batch(valid_proposal(tags=["architecture.memory", "not.in.catalog"])))
    with pytest.raises(MemoryExtractionError, match="unknown tag"):
        parse_extraction_batch(raw, evidence_catalog, tag_catalog)


def test_non_atomic_statement_rejects_batch(evidence_catalog, tag_catalog):
    for statement in (
        "User prefers dark mode, and the project database uses PostgreSQL.",
        "用户喜欢深色模式，并且决定项目数据库使用 PostgreSQL。",
    ):
        raw = json.dumps(valid_batch(valid_proposal(statement=statement)))
        with pytest.raises(MemoryExtractionError, match="non-atomic"):
            parse_extraction_batch(raw, evidence_catalog, tag_catalog)


def test_excess_fields_reject_batch(evidence_catalog, tag_catalog):
    proposal = valid_proposal()
    proposal["status"] = "active"
    with pytest.raises(MemoryExtractionError, match="contract"):
        parse_extraction_batch(json.dumps(valid_batch(proposal)), evidence_catalog, tag_catalog)


def test_reflection_evidence_ref_is_resolved(evidence_catalog, tag_catalog):
    raw = json.dumps(valid_batch(valid_proposal(evidence_refs=["reflection:2"])))
    parsed = parse_extraction_batch(raw, evidence_catalog, tag_catalog)
    assert parsed.proposals[0].evidence_refs == ("reflection:2",)


def test_shared_scope_hint_allowed(evidence_catalog, tag_catalog):
    raw = json.dumps(valid_batch(valid_proposal(scope_hint="shared", slot="shared.fact")))
    parsed = parse_extraction_batch(raw, evidence_catalog, tag_catalog)
    assert parsed.proposals[0].scope_hint.value == "shared"


def test_user_and_session_scope_hints_rejected(evidence_catalog, tag_catalog):
    for hint in ("user", "session"):
        raw = json.dumps(valid_batch(valid_proposal(scope_hint=hint)))
        with pytest.raises(MemoryExtractionError, match="unsupported scope_hint"):
            parse_extraction_batch(raw, evidence_catalog, tag_catalog)


def test_user_and_session_scope_hints_allowed_when_batch_supplies_them(
    evidence_catalog, tag_catalog
):
    for hint in ("user", "session"):
        raw = json.dumps(valid_batch(valid_proposal(scope_hint=hint)))
        parsed = parse_extraction_batch(
            raw,
            evidence_catalog,
            tag_catalog,
            allowed_scope_hints={ScopeKind.PROJECT, ScopeKind.SHARED, ScopeKind(hint)},
        )
        assert parsed.proposals[0].scope_hint is ScopeKind(hint)


def test_duplicate_proposal_indices_rejected(evidence_catalog, tag_catalog):
    raw = json.dumps(
        valid_batch(valid_proposal(proposal_index=0), valid_proposal(proposal_index=0))
    )
    with pytest.raises(MemoryExtractionError, match="contract"):
        parse_extraction_batch(raw, evidence_catalog, tag_catalog)
