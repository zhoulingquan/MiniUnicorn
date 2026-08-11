"""Schema contract tests for governed structured memory (design sections 6-7)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    InvalidMemoryTransition,
    MemoryExtractionBatch,
    MemoryKind,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryTransaction,
    RecallQuery,
    RecallResult,
    RepositoryHealth,
    ScopeKind,
    TagCatalog,
    UnknownMemoryTag,
    assert_transition,
    conflict_key,
    content_hash,
    normalize_slot,
    normalize_text,
    transaction_checksum,
    validate_same_status_revision,
)


def dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


@pytest.fixture
def tag_catalog() -> TagCatalog:
    bundled = Path(__file__).parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    return TagCatalog.load(bundled)


@pytest.fixture
def record_data() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "mem_" + "a" * 32,
        "revision": 1,
        "status": "candidate",
        "kind": "decision",
        "scope": {"kind": "project", "key": "project:6b5ec7b29e32"},
        "subject": "MiniUnicorn",
        "slot": "memory.retrieval.strategy",
        "statement": "Main uses deterministic structured recall.",
        "detail": "Markdown/JSONL remain the durable source of truth.",
        "tags": ["project.decision", "architecture.memory"],
        "aliases": ["全局记忆"],
        "source_level": "inferred",
        "confidence": 0.9,
        "importance": 4,
        "evidence": [
            {
                "kind": "manual",
                "ref": "command:msg-42",
                "excerpt": "Use deterministic structured recall.",
                "sha256": None,
                "observed_at": "2026-08-11T08:30:00Z",
            }
        ],
        "content_hash": "c" * 64,
        "derived_from": ["history:42"],
        "supersedes": [],
        "replacement_id": None,
        "blocked_by": [],
        "valid_from": "2026-08-11T08:30:00Z",
        "expires_at": None,
        "created_at": "2026-08-11T08:30:00Z",
        "updated_at": "2026-08-11T08:31:00Z",
        "status_reason": "model proposal",
    }


@pytest.fixture
def transaction(record_data: dict) -> MemoryTransaction:
    record = MemoryRecord.model_validate(record_data)
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id="mtx_" + "b" * 32,
        recorded_at=dt("2026-08-11T08:31:00Z"),
        actor=ActorKind.DREAM,
        reason="promote verified candidate",
        source_batch="history:41-42",
        expected_revisions={record.id: 0},
        operations=[MemoryOperation(op="put", record=record)],
        checksum_sha256="f" * 64,
    )
    return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})


# ---------------------------------------------------------------------------
# Normalization and content hash
# ---------------------------------------------------------------------------


def test_content_hash_is_stable_after_nfkc_and_whitespace_normalization():
    left = content_hash(
        MemoryKind.FACT,
        MemoryScope(kind=ScopeKind.PROJECT, key="project:x"),
        "Ａ",
        "db.primary",
        "  PostgreSQL\n",
    )
    right = content_hash(
        MemoryKind.FACT,
        MemoryScope(kind=ScopeKind.PROJECT, key="project:x"),
        "A",
        "db.primary",
        "PostgreSQL",
    )
    assert left == right


def test_content_hash_is_64_lowercase_hex():
    digest = content_hash(
        MemoryKind.FACT,
        MemoryScope(kind=ScopeKind.PROJECT, key="project:x"),
        "subject",
        "db.primary",
        "statement",
    )
    assert len(digest) == 64
    assert digest == digest.lower()


def test_normalize_text_folds_whitespace_and_nfkc():
    assert normalize_text("  PostgreSQL\n\tis  best ") == "PostgreSQL is best"
    assert normalize_text("Ａ") == "A"


def test_normalize_slot_lowercases_and_accepts_dotted_slots():
    assert normalize_slot("DB.Primary") == "db.primary"
    assert normalize_slot("memory retrieval-strategy") == "memory.retrieval-strategy"


@pytest.mark.parametrize("bad", ["Upper!", "no spaces.", "has+plus", "has/slash", ""])
def test_normalize_slot_rejects_invalid_slots(bad):
    with pytest.raises(ValueError):
        normalize_slot(bad)


def test_conflict_key_normalizes_subject_and_slot():
    scope = MemoryScope(kind=ScopeKind.PROJECT, key="project:x")
    left = conflict_key(scope, " MiniUnicorn ", MemoryKind.DECISION, "db.primary")
    right = conflict_key(scope, "miniunicorn", MemoryKind.DECISION, "db.primary")
    assert left == right
    assert left.startswith("project|project:x|")


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("candidate", "active"),
        ("candidate", "superseded"),
        ("candidate", "revoked"),
        ("candidate", "expired"),
        ("active", "superseded"),
        ("active", "revoked"),
        ("active", "expired"),
    ],
)
def test_legal_status_transitions(old, new):
    assert_transition(old, new)


@pytest.mark.parametrize("old", ["superseded", "revoked", "expired"])
def test_terminal_status_cannot_return_active(old):
    with pytest.raises(InvalidMemoryTransition):
        assert_transition(old, "active")


@pytest.mark.parametrize("new", ["candidate"])
def test_active_cannot_return_to_candidate(new):
    with pytest.raises(InvalidMemoryTransition):
        assert_transition("active", new)


@pytest.mark.parametrize("old,new", [("superseded", "revoked"), ("superseded", "expired"), ("revoked", "expired")])
def test_terminal_statuses_cannot_move_between_terminals(old, new):
    with pytest.raises(InvalidMemoryTransition):
        assert_transition(old, new)


# ---------------------------------------------------------------------------
# Same-status revision rules (design section 7)
# ---------------------------------------------------------------------------


def test_active_same_status_revision_cannot_change_statement(record_data):
    previous = MemoryRecord.model_validate(record_data | {"status": "active", "revision": 2})
    current = previous.model_copy(update={"statement": "Different statement", "updated_at": dt("2026-08-11T08:32:00Z")})
    with pytest.raises(InvalidMemoryTransition):
        validate_same_status_revision(previous, current)


def test_active_same_status_revision_cannot_change_slot(record_data):
    previous = MemoryRecord.model_validate(record_data | {"status": "active", "revision": 2})
    current = previous.model_copy(update={"slot": "other.slot", "updated_at": dt("2026-08-11T08:32:00Z")})
    with pytest.raises(InvalidMemoryTransition):
        validate_same_status_revision(previous, current)


def test_active_same_status_revision_can_merge_evidence(record_data):
    previous = MemoryRecord.model_validate(record_data | {"status": "active", "revision": 2})
    extra = EvidenceRef(
        kind=EvidenceKind.FILE,
        ref="pyproject.toml#L1",
        excerpt="name = miniunicorn",
        sha256="d" * 64,
        observed_at=dt("2026-08-11T08:32:00Z"),
    )
    current = previous.model_copy(update={"evidence": previous.evidence + (extra,), "updated_at": dt("2026-08-11T08:32:00Z")})
    validate_same_status_revision(previous, current)


def test_candidate_same_status_revision_can_add_blocked_by(record_data):
    previous = MemoryRecord.model_validate(record_data)
    current = previous.model_copy(update={"blocked_by": ("mem_" + "e" * 32,), "updated_at": dt("2026-08-11T08:32:00Z")})
    validate_same_status_revision(previous, current)


def test_candidate_same_status_revision_cannot_remove_evidence(record_data):
    previous = MemoryRecord.model_validate(record_data)
    current = previous.model_copy(update={"evidence": (), "updated_at": dt("2026-08-11T08:32:00Z")})
    with pytest.raises(InvalidMemoryTransition):
        validate_same_status_revision(previous, current)


def test_terminal_record_cannot_revise(record_data):
    previous = MemoryRecord.model_validate(record_data | {"status": "superseded", "replacement_id": "mem_" + "e" * 32})
    current = previous.model_copy(update={"updated_at": dt("2026-08-11T08:32:00Z")})
    with pytest.raises(InvalidMemoryTransition):
        validate_same_status_revision(previous, current)


# ---------------------------------------------------------------------------
# Tag catalog
# ---------------------------------------------------------------------------


def test_bundled_catalog_contains_required_tags(tag_catalog):
    assert tag_catalog.canonical_names() >= {
        "architecture.memory",
        "project.decision",
        "project.constraint",
        "project.requirement",
        "project.fact",
        "project.outcome",
        "user.identity",
        "user.preference",
        "workflow.procedure",
        "failure.lesson",
        "tool.behavior",
        "entity.relationship",
        "session.event",
        "shared.fact",
    }
    assert "全局记忆" in tag_catalog.alias_map()


def test_record_rejects_unknown_tag(tag_catalog, record_data):
    record = MemoryRecord.model_validate(record_data | {"tags": ["not.registered"]})
    with pytest.raises(UnknownMemoryTag):
        tag_catalog.validate_record(record)


def test_catalog_matching_is_case_insensitive(tag_catalog):
    assert tag_catalog.contains("ARCHITECTURE.MEMORY")


# ---------------------------------------------------------------------------
# MemoryRecord validation
# ---------------------------------------------------------------------------


def test_record_is_frozen_and_forbids_extra(record_data):
    record = MemoryRecord.model_validate(record_data)
    with pytest.raises(ValidationError):
        record.model_validate(record_data | {"extra_field": 1})
    with pytest.raises(ValidationError):
        record.subject = "cannot mutate"


@pytest.mark.parametrize("bad_id", ["mem_abc", "mtx_" + "a" * 32, "mem_" + "g" * 32, ""])
def test_record_rejects_bad_id(record_data, bad_id):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"id": bad_id})


def test_record_computes_content_hash_from_canonical_fields(record_data):
    record = MemoryRecord.model_validate(record_data)
    expected = content_hash(
        record.kind,
        record.scope,
        record.subject,
        record.slot,
        record.statement,
    )
    assert record.content_hash == expected


def test_record_normalizes_tags_and_aliases_sorted_deduped(record_data):
    record = MemoryRecord.model_validate(record_data | {"tags": ["project.decision", "architecture.memory", "project.decision"]})
    assert record.tags == ("architecture.memory", "project.decision")


def test_record_tags_require_at_least_one_and_at_most_twelve(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"tags": []})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"tags": [f"tag.{i}" for i in range(13)]})


def test_record_aliases_limit_and_length(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"aliases": [f"alias-{i}" for i in range(21)]})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"aliases": [""]})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"aliases": ["x" * 81]})


@pytest.mark.parametrize("field", ["subject", "statement"])
def test_record_requires_nonempty_subject_and_statement(record_data, field):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {field: ""})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {field: "   "})


def test_record_subject_max_length(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"subject": "x" * 161})


def test_record_statement_max_length(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"statement": "x" * 501})


def test_record_detail_max_length(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"detail": "x" * 2001})


def test_record_confidence_and_importance_bounds(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"confidence": 1.5})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"confidence": -0.1})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"importance": 6})
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"importance": 0})


def test_record_requires_at_least_one_evidence(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"evidence": []})


def test_evidence_ref_requires_ref_and_limits_excerpt():
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.FILE, ref="", excerpt="x")
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.FILE, ref="r" * 513, excerpt="x")
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.FILE, ref="ok", excerpt="x" * 1001)


@pytest.mark.parametrize("kind", [EvidenceKind.FILE, EvidenceKind.TOOL_RESULT, EvidenceKind.GIT])
def test_evidence_requires_sha256_for_file_tool_git(kind):
    with pytest.raises(ValidationError):
        EvidenceRef(kind=kind, ref="ref", excerpt="x", sha256=None)


@pytest.mark.parametrize("kind", [EvidenceKind.MANUAL, EvidenceKind.USER_MESSAGE, EvidenceKind.HISTORY])
def test_evidence_allows_null_sha256_for_soft_kinds(kind):
    EvidenceRef(kind=kind, ref="ref", excerpt="x", sha256=None)


def test_evidence_sha256_must_be_64_hex():
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.FILE, ref="ref", excerpt="x", sha256="zz")


def test_evidence_merges_by_kind_ref_sha256_and_sorts(record_data):
    first = record_data["evidence"][0]
    record = MemoryRecord.model_validate(
        record_data
        | {
            "evidence": [
                first,
                {"kind": "history", "ref": "history:1", "excerpt": "a", "sha256": None, "observed_at": "2026-08-11T08:30:00Z"},
                first,
            ]
        }
    )
    assert len(record.evidence) == 2


def test_replacement_id_only_when_superseded(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"status": "active", "replacement_id": "mem_" + "e" * 32})


def test_blocked_by_only_when_candidate(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"status": "active", "blocked_by": ("mem_" + "e" * 32,)})


def test_datetimes_require_timezone(record_data):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(record_data | {"valid_from": "2026-08-11T08:30:00"})


def test_datetimes_are_normalized_to_utc(record_data):
    record = MemoryRecord.model_validate(record_data)
    assert record.created_at.tzinfo is timezone.utc


def test_scope_key_must_match_kind():
    with pytest.raises(ValidationError):
        MemoryScope(kind=ScopeKind.PROJECT, key="user:abc")
    with pytest.raises(ValidationError):
        MemoryScope(kind=ScopeKind.SHARED, key="shared:custom")
    assert MemoryScope(kind=ScopeKind.SHARED, key="shared:*").key == "shared:*"


# ---------------------------------------------------------------------------
# MemoryTransaction validation
# ---------------------------------------------------------------------------


def test_transaction_checksum_ignores_checksum_field(transaction):
    first = transaction_checksum(transaction)
    second = transaction_checksum(transaction.model_copy(update={"checksum_sha256": "f" * 64}))
    assert first == second


def test_transaction_forbids_duplicate_operation_id(record_data):
    record = MemoryRecord.model_validate(record_data)
    op = MemoryOperation(op="put", record=record)
    with pytest.raises(ValidationError):
        MemoryTransaction(
            tx_id="mtx_" + "b" * 32,
            recorded_at=dt("2026-08-11T08:31:00Z"),
            actor=ActorKind.DREAM,
            reason="duplicate",
            source_batch="",
            expected_revisions={record.id: 0},
            operations=(op, op),
        )


def test_transaction_requires_expected_revision_for_every_operation(record_data):
    record = MemoryRecord.model_validate(record_data)
    with pytest.raises(ValidationError):
        MemoryTransaction(
            tx_id="mtx_" + "b" * 32,
            recorded_at=dt("2026-08-11T08:31:00Z"),
            actor=ActorKind.DREAM,
            reason="missing expected revision",
            source_batch="",
            expected_revisions={},
            operations=[MemoryOperation(op="put", record=record)],
        )


def test_transaction_rejects_extra_expected_revisions(record_data):
    record = MemoryRecord.model_validate(record_data)
    with pytest.raises(ValidationError):
        MemoryTransaction(
            tx_id="mtx_" + "b" * 32,
            recorded_at=dt("2026-08-11T08:31:00Z"),
            actor=ActorKind.DREAM,
            reason="extra expected revision",
            source_batch="",
            expected_revisions={record.id: 0, "mem_" + "e" * 32: 0},
            operations=[MemoryOperation(op="put", record=record)],
        )


def test_transaction_rejects_unsupported_schema(record_data):
    record = MemoryRecord.model_validate(record_data)
    with pytest.raises(ValidationError):
        MemoryTransaction(
            schema_version=2,
            tx_id="mtx_" + "b" * 32,
            recorded_at=dt("2026-08-11T08:31:00Z"),
            actor=ActorKind.DREAM,
            reason="bad schema",
            source_batch="",
            expected_revisions={record.id: 0},
            operations=[MemoryOperation(op="put", record=record)],
        )


# ---------------------------------------------------------------------------
# Auxiliary models
# ---------------------------------------------------------------------------


def test_recall_query_defaults():
    query = RecallQuery(
        query_text="hello",
        allowed_scopes=(MemoryScope(kind=ScopeKind.PROJECT, key="project:x"),),
        now=dt("2026-08-11T08:30:00Z"),
    )
    assert query.token_budget == 2500
    assert query.max_hits == 20
    assert query.explicit_ids == ()


def test_repository_health_healthy_defaults():
    health = RepositoryHealth()
    assert health.state == "healthy"
    assert health.error_code is None


def test_extraction_batch_proposal_indices_must_be_unique():
    proposals = [
        {"proposal_index": 0, "kind": "fact", "subject": "a", "slot": "a.b", "statement": "one", "tags": ["project.fact"], "evidence_refs": ["history:1"]},
        {"proposal_index": 0, "kind": "fact", "subject": "b", "slot": "a.b", "statement": "two", "tags": ["project.fact"], "evidence_refs": ["history:1"]},
    ]
    with pytest.raises(ValidationError):
        MemoryExtractionBatch(proposals=proposals)


def test_recall_result_hits_have_score_and_reasons(record_data):
    from miniunicorn.agent.memory_models import RecallHit

    record = MemoryRecord.model_validate(record_data)
    result = RecallResult(
        hits=(RecallHit(record=record, score=105, reasons=("tag=x(+45)",), tokens=10),),
        candidates=1,
        filtered=0,
        excluded_by_budget=0,
        tokens_used=10,
    )
    assert result.hits[0].score == 105
    assert result.hits[0].reasons == ("tag=x(+45)",)
