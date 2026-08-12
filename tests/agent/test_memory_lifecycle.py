"""Lifecycle tests: candidate gating, promotion matrix, conflicts, revoke, expiry."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from miniunicorn.agent.memory_lifecycle import (
    REASON_BLOCKED_LOWER_RANK,
    REASON_CORRECTION_CONFLICT,
    REASON_CREATED,
    REASON_EXISTING,
    REASON_MERGED,
    REASON_REPLACED,
    REASON_SAME_RANK,
    IngestContext,
    LifecyclePolicy,
    MemoryEvidenceUnresolved,
    MemoryLifecycleError,
    MemoryRecordNotFound,
    StructuredMemoryLifecycle,
    can_auto_promote,
)
from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryTransaction,
    MemoryWriteError,
    ScopeKind,
    SourceLevel,
    UnknownMemoryTag,
    new_memory_id,
    new_transaction_id,
    transaction_checksum,
)
from miniunicorn.agent.memory_models import (
    MemoryError as StructuredMemoryError,
)
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

UTC = timezone.utc


def dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def make_evidence(kind: str = "manual", ref: str = "command:msg-42", excerpt: str = "Use deterministic structured recall.", sha256: str | None = None):
    return {
        "kind": kind,
        "ref": ref,
        "excerpt": excerpt,
        "sha256": sha256,
        "observed_at": "2026-08-11T08:30:00Z",
    }


def record_data(
    statement: str = "Use PostgreSQL",
    *,
    status: str = "candidate",
    revision: int = 1,
    memory_id: str | None = None,
    tags=("project.fact",),
    aliases=(),
    slot: str = "db.primary",
    kind: str = "fact",
    source_level: str = "inferred",
    confidence: float = 0.9,
    importance: int = 4,
    evidence=None,
    **overrides,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": memory_id or new_memory_id(),
        "revision": revision,
        "status": status,
        "kind": kind,
        "scope": {"kind": "project", "key": "project:6b5ec7b29e32"},
        "subject": "MiniUnicorn",
        "slot": slot,
        "statement": statement,
        "detail": "",
        "tags": list(tags),
        "aliases": list(aliases),
        "source_level": source_level,
        "confidence": confidence,
        "importance": importance,
        "evidence": evidence or [make_evidence()],
        "content_hash": "c" * 64,
        "derived_from": [],
        "supersedes": [],
        "replacement_id": None,
        "blocked_by": [],
        "valid_from": "2026-08-11T08:30:00Z",
        "expires_at": None,
        "created_at": "2026-08-11T08:30:00Z",
        "updated_at": "2026-08-11T08:31:00Z",
        "status_reason": "test",
        **overrides,
    }


def make_transaction(*records, actor="dream", reason="test", source_batch="", expected_revisions=None):
    expected = expected_revisions or {rec.id: rec.revision - 1 for rec in records}
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id=new_transaction_id(),
        recorded_at=dt("2026-08-11T08:31:00Z"),
        actor=ActorKind(actor),
        reason=reason,
        source_batch=source_batch,
        expected_revisions=expected,
        operations=[MemoryOperation(op="put", record=rec) for rec in records],
        checksum_sha256="f" * 64,
    )
    return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    structured = tmp_path / "memory" / "structured"
    structured.mkdir(parents=True)
    bundled = Path(__file__).parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    return tmp_path


@pytest.fixture
def policy() -> LifecyclePolicy:
    return LifecyclePolicy(auto_promote_verified=True, min_repeated_evidence=2, candidate_ttl_days=30)


@pytest.fixture
def repository(workspace: Path) -> StructuredMemoryRepository:
    return StructuredMemoryRepository(workspace, lock_timeout_s=0.1)


@pytest.fixture
def lifecycle(repository, policy) -> StructuredMemoryLifecycle:
    return StructuredMemoryLifecycle(repository, policy)


def file_evidence(excerpt: str, ref: str = "pyproject.toml#L1") -> EvidenceRef:
    return EvidenceRef(
        kind=EvidenceKind.FILE,
        ref=ref,
        excerpt=excerpt,
        sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        observed_at=dt("2026-08-11T08:30:00Z"),
    )


@pytest.fixture
def evidence_catalog() -> dict[str, EvidenceRef]:
    tool = EvidenceRef(kind=EvidenceKind.TOOL_RESULT, ref="tool:ls", excerpt="pyproject.toml present", observed_at=dt("2026-08-11T08:29:00Z"))
    return {
        "history:1": EvidenceRef(kind=EvidenceKind.HISTORY, ref="history:1", excerpt="First observation", observed_at=dt("2026-08-11T08:29:00Z")),
        "history:2": EvidenceRef(kind=EvidenceKind.HISTORY, ref="history:2", excerpt="Second observation", observed_at=dt("2026-08-11T08:29:30Z")),
        "command:msg-42": EvidenceRef(kind=EvidenceKind.MANUAL, ref="command:msg-42", excerpt="Use deterministic structured recall.", observed_at=dt("2026-08-11T08:28:00Z")),
        "tool:ls": tool.model_copy(update={"sha256": hashlib.sha256(tool.excerpt.encode("utf-8")).hexdigest()}),
    }


@pytest.fixture
def file_catalog() -> dict[str, EvidenceRef]:
    return {"file:pyproject": file_evidence("name = miniunicorn-ai")}


@pytest.fixture
def context(evidence_catalog) -> IngestContext:
    return IngestContext(
        actor=ActorKind.DREAM,
        reason="dream batch",
        source_batch="history:41-42",
        scope=MemoryScope(kind=ScopeKind.PROJECT, key="project:6b5ec7b29e32"),
        evidence_catalog=evidence_catalog,
        now=dt("2026-08-11T08:30:00Z"),
    )


@pytest.fixture
def file_context(file_catalog) -> IngestContext:
    return IngestContext(
        actor=ActorKind.DREAM,
        reason="dream batch",
        source_batch="history:41-42",
        scope=MemoryScope(kind=ScopeKind.PROJECT, key="project:6b5ec7b29e32"),
        evidence_catalog=file_catalog,
        now=dt("2026-08-11T08:30:00Z"),
    )


def proposal(
    statement: str = "Use PostgreSQL",
    *,
    tags=("project.fact",),
    kind: str = "fact",
    speech_act: str = "inferred",
    evidence_refs=("history:1",),
    confidence: float = 0.9,
    importance: int = 4,
    slot: str = "db.primary",
    aliases=(),
    **overrides,
) -> CandidateProposal:
    return CandidateProposal.model_validate(
        {
            "proposal_index": 0,
            "kind": kind,
            "subject": "MiniUnicorn",
            "slot": slot,
            "statement": statement,
            "detail": "",
            "tags": list(tags),
            "aliases": list(aliases),
            "confidence": confidence,
            "importance": importance,
            "evidence_refs": list(evidence_refs),
            "speech_act": speech_act,
            **overrides,
        }
    )


@pytest.fixture
def inferred_proposal() -> CandidateProposal:
    return proposal()


def seed_active_decision(repository, statement="Main uses deterministic structured recall.") -> MemoryRecord:
    record = MemoryRecord.model_validate(
        record_data(
            statement=statement,
            status="active",
            revision=1,
            kind="decision",
            slot="memory.retrieval.strategy",
            tags=("architecture.memory", "project.decision"),
            source_level="confirmed_decision",
            confidence=1.0,
            importance=5,
            evidence=[make_evidence(ref="command:msg-42")],
            supersedes=[],
        )
    )
    repository.append_transaction(make_transaction(record))
    return record


@pytest.fixture
def active_decision(repository) -> MemoryRecord:
    return seed_active_decision(repository)


@pytest.fixture
def correction_proposal() -> CandidateProposal:
    return proposal(
        statement="Main uses deterministic structured recall without embeddings.",
        kind="decision",
        slot="memory.retrieval.strategy",
        tags=("architecture.memory", "project.decision"),
        speech_act="explicit_correction",
        evidence_refs=("command:msg-42",),
        confidence=1.0,
        importance=5,
    )


@pytest.fixture
def competing_decision() -> CandidateProposal:
    return proposal(
        statement="Main uses a different strategy entirely.",
        kind="decision",
        slot="memory.retrieval.strategy",
        tags=("architecture.memory", "project.decision"),
        speech_act="confirmed_decision",
        evidence_refs=("command:msg-42",),
        confidence=0.95,
        importance=5,
    )


@pytest.fixture
def replacement_chain(lifecycle, active_decision, correction_proposal, context, repository) -> dict:
    result = lifecycle.ingest(correction_proposal, context)
    current = repository.get(result.candidate_id)
    return {"current_id": current.id, "conflict_key": current.conflict_key}


# ---------------------------------------------------------------------------
# Candidate creation
# ---------------------------------------------------------------------------


def test_ingest_creates_candidate_revision_one(lifecycle, inferred_proposal, context):
    result = lifecycle.ingest(inferred_proposal, context)
    record = lifecycle.repository.get(result.candidate_id)
    assert result.final_status == MemoryStatus.CANDIDATE
    assert result.reason_code == REASON_CREATED
    assert record.revision == 1
    assert record.status == MemoryStatus.CANDIDATE
    assert record.source_level == SourceLevel.INFERRED
    assert record.detail == ""


def test_ingest_ignores_model_claimed_rank_and_calculates_source(lifecycle, context):
    verified_proposal = proposal(speech_act="explicit_correction", evidence_refs=("history:1",))
    result = lifecycle.ingest(verified_proposal, context)
    record = lifecycle.repository.get(result.candidate_id)
    assert record.source_level == SourceLevel.INFERRED


def test_ingest_uses_context_scope_not_proposal_hint(lifecycle, inferred_proposal, context):
    result = lifecycle.ingest(inferred_proposal, context)
    record = lifecycle.repository.get(result.candidate_id)
    assert record.scope == context.scope


def test_ingest_rejects_evidence_ref_missing_from_catalog(lifecycle, inferred_proposal, context):
    bad = proposal(evidence_refs=("history:999",))
    with pytest.raises(MemoryEvidenceUnresolved):
        lifecycle.ingest(bad, context)
    assert lifecycle.repository.current_records() == ()


def test_ingest_rejects_unknown_tag(lifecycle, context):
    bad = proposal(tags=("not.registered",))
    with pytest.raises(UnknownMemoryTag):
        lifecycle.ingest(bad, context)
    assert lifecycle.repository.current_records() == ()


def test_ingest_rejects_file_hash_mismatch(lifecycle, file_context):
    bad = file_context.evidence_catalog["file:pyproject"].model_copy(
        update={"sha256": hashlib.sha256(b"different content").hexdigest(), "excerpt": "name = miniunicorn-ai"}
    )
    context = IngestContext(
        actor=file_context.actor,
        reason="dream batch",
        source_batch="history:41-42",
        scope=file_context.scope,
        evidence_catalog={"file:pyproject": bad},
        now=file_context.now,
    )
    verified = proposal(evidence_refs=("file:pyproject",), tags=("project.fact",), speech_act="verified")
    with pytest.raises(MemoryEvidenceUnresolved):
        lifecycle.ingest(verified, context)
    assert lifecycle.repository.current_records() == ()


def test_ingest_rejects_excerpt_not_present_in_referenced_workspace_file(
    lifecycle, file_context
):
    source = lifecycle.repository.workspace / "evidence.txt"
    source.write_text("actual durable content", encoding="utf-8")
    claimed = file_evidence("fabricated excerpt", ref="evidence.txt#L1")
    context = IngestContext(
        actor=file_context.actor,
        reason=file_context.reason,
        source_batch=file_context.source_batch,
        scope=file_context.scope,
        evidence_catalog={"file:evidence": claimed},
        now=file_context.now,
    )
    verified = proposal(
        evidence_refs=("file:evidence",),
        tags=("project.fact",),
        speech_act="verified",
    )

    with pytest.raises(MemoryEvidenceUnresolved, match="source mismatch"):
        lifecycle.ingest(verified, context)


def test_ingest_normalizes_tags_and_aliases(lifecycle, context):
    result = lifecycle.ingest(proposal(tags=("Project.FACT",), aliases=("全局事实",)), context)
    record = lifecycle.repository.get(result.candidate_id)
    assert record.tags == ("project.fact",)


def test_ingest_rejects_non_atomic_missing_fields(lifecycle, context):
    with pytest.raises(ValidationError):
        CandidateProposal.model_validate({"proposal_index": 0, "kind": "fact", "subject": "MiniUnicorn"})


# ---------------------------------------------------------------------------
# Idempotent retry
# ---------------------------------------------------------------------------


def test_retry_same_source_batch_and_content_returns_existing_candidate(lifecycle, inferred_proposal, context):
    first = lifecycle.ingest(inferred_proposal, context)
    second = lifecycle.ingest(inferred_proposal, context)
    assert second.candidate_id == first.candidate_id
    assert second.reason_code == REASON_EXISTING
    assert len(lifecycle.repository.revisions(first.candidate_id)) == 1


def test_lifecycle_errors_use_project_memory_error_base():
    assert issubclass(MemoryLifecycleError, StructuredMemoryError)


def test_retry_after_promotion_write_failure_resumes_promotion(
    lifecycle, file_context, monkeypatch
):
    verified = proposal(
        evidence_refs=("file:pyproject",),
        speech_act="verified",
        confidence=0.9,
    )
    real_append = lifecycle.repository.append_transaction
    calls = {"count": 0}

    def fail_second_append(transaction):
        calls["count"] += 1
        if calls["count"] == 2:
            raise MemoryWriteError("promotion write failed")
        return real_append(transaction)

    monkeypatch.setattr(lifecycle.repository, "append_transaction", fail_second_append)
    with pytest.raises(MemoryWriteError, match="promotion write failed"):
        lifecycle.ingest(verified, file_context)

    monkeypatch.setattr(lifecycle.repository, "append_transaction", real_append)
    retry = lifecycle.ingest(verified, file_context)

    assert retry.final_status is MemoryStatus.ACTIVE
    assert retry.active_id == retry.candidate_id
    assert lifecycle.repository.get(retry.candidate_id).status is MemoryStatus.ACTIVE
    assert len(lifecycle.repository.current_records()) == 1


def test_retry_after_completed_auto_promotion_returns_existing_active(
    lifecycle, file_context
):
    verified = proposal(
        evidence_refs=("file:pyproject",),
        speech_act="verified",
        confidence=0.9,
    )
    first = lifecycle.ingest(verified, file_context)
    second = lifecycle.ingest(verified, file_context)

    assert first.final_status is MemoryStatus.ACTIVE
    assert second.candidate_id == first.candidate_id
    assert second.final_status is MemoryStatus.ACTIVE
    assert second.reason_code == REASON_EXISTING
    assert second.transaction_ids == ()
    assert len(lifecycle.repository.current_records()) == 1


def test_different_source_batch_creates_separate_candidate(lifecycle, inferred_proposal, context):
    first = lifecycle.ingest(inferred_proposal, context)
    later = IngestContext(
        actor=context.actor, reason=context.reason, source_batch="history:50",
        scope=context.scope, evidence_catalog=context.evidence_catalog, now=context.now,
    )
    second = lifecycle.ingest(inferred_proposal, later)
    assert second.candidate_id != first.candidate_id


# ---------------------------------------------------------------------------
# Promotion matrix (design section 8.2)
# ---------------------------------------------------------------------------


def test_inferred_candidate_never_auto_promotes(lifecycle, inferred_proposal, context):
    result = lifecycle.ingest(inferred_proposal, context)
    assert result.final_status == MemoryStatus.CANDIDATE
    assert lifecycle.repository.get(result.candidate_id).status == MemoryStatus.CANDIDATE


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.90, MemoryStatus.ACTIVE), (0.89, MemoryStatus.CANDIDATE)],
)
def test_confirmed_decision_threshold(lifecycle, context, confidence, expected):
    proposal_data = proposal(
        statement="Use PostgreSQL",
        speech_act="confirmed_decision",
        evidence_refs=("command:msg-42",),
        confidence=confidence,
    )
    result = lifecycle.ingest(proposal_data, context)
    assert result.final_status == expected


@pytest.mark.parametrize(
    ("auto_promote", "confidence", "expected"),
    [(True, 0.8, MemoryStatus.ACTIVE), (True, 0.79, MemoryStatus.CANDIDATE), (False, 0.95, MemoryStatus.CANDIDATE)],
)
def test_verified_threshold(lifecycle, file_context, auto_promote, confidence, expected):
    strict = LifecyclePolicy(auto_promote_verified=auto_promote, min_repeated_evidence=2, candidate_ttl_days=30)
    strict_lifecycle = StructuredMemoryLifecycle(lifecycle.repository, strict)
    verified = proposal(evidence_refs=("file:pyproject",), speech_act="verified", confidence=confidence)
    result = strict_lifecycle.ingest(verified, file_context)
    assert result.final_status == expected


@pytest.mark.parametrize(
    ("evidence_refs", "confidence", "expected"),
    [
        (("history:1", "history:2"), 0.85, MemoryStatus.ACTIVE),
        (("history:1",), 0.9, MemoryStatus.CANDIDATE),
        (("history:1", "history:2"), 0.84, MemoryStatus.CANDIDATE),
    ],
)
def test_repeated_experience_threshold(lifecycle, context, evidence_refs, confidence, expected):
    repeated = proposal(evidence_refs=evidence_refs, speech_act="repeated_experience", confidence=confidence)
    result = lifecycle.ingest(repeated, context)
    assert result.final_status == expected


def test_correction_promotes_immediately_with_manual_evidence(lifecycle, correction_proposal, context):
    result = lifecycle.ingest(correction_proposal, context)
    assert result.final_status == MemoryStatus.ACTIVE
    assert lifecycle.repository.get(result.candidate_id).status == MemoryStatus.ACTIVE


def test_correction_without_manual_evidence_stays_candidate(lifecycle, context):
    corrected = proposal(
        statement="Main uses deterministic recall without embeddings.",
        kind="decision",
        slot="memory.retrieval.strategy",
        tags=("architecture.memory", "project.decision"),
        speech_act="explicit_correction",
        evidence_refs=("history:1",),
        confidence=1.0,
        importance=5,
    )
    result = lifecycle.ingest(corrected, context)
    assert result.final_status == MemoryStatus.CANDIDATE
    assert lifecycle.repository.get(result.candidate_id).source_level == SourceLevel.INFERRED


# ---------------------------------------------------------------------------
# Conflict handling
# ---------------------------------------------------------------------------


def test_identical_content_merges_into_active_in_one_transaction(lifecycle, active_decision, context):
    duplicate = proposal(
        statement=active_decision.statement,
        kind="decision",
        slot=active_decision.slot,
        tags=("architecture.memory", "project.decision"),
        speech_act="confirmed_decision",
        evidence_refs=("history:1",),
        confidence=0.95,
        importance=5,
    )
    result = lifecycle.ingest(duplicate, context)
    current_active = lifecycle.repository.get(active_decision.id)
    duplicate_record = lifecycle.repository.get(result.candidate_id)
    assert result.reason_code == REASON_MERGED
    assert current_active.status == MemoryStatus.ACTIVE
    assert current_active.revision == 2
    assert current_active.statement == active_decision.statement
    assert len(current_active.evidence) == 2
    assert duplicate_record.status == MemoryStatus.SUPERSEDED
    assert duplicate_record.replacement_id == active_decision.id


def test_higher_rank_replaces_lower_rank_in_one_transaction(lifecycle, active_decision, correction_proposal, context):
    result = lifecycle.ingest(correction_proposal, context)
    old = lifecycle.repository.get(active_decision.id)
    new = lifecycle.repository.get(result.candidate_id)
    assert new.status == MemoryStatus.ACTIVE
    assert old.status == MemoryStatus.SUPERSEDED
    assert old.replacement_id == new.id
    assert new.supersedes == (old.id,)
    assert result.reason_code == REASON_REPLACED


def test_lower_rank_is_blocked(lifecycle, active_decision, context):
    weak = proposal(
        statement="A weaker claim",
        kind="decision",
        slot=active_decision.slot,
        tags=("architecture.memory", "project.decision"),
        speech_act="inferred",
        evidence_refs=("history:1",),
        confidence=0.6,
        importance=3,
    )
    result = lifecycle.ingest(weak, context)
    record = lifecycle.repository.get(result.candidate_id)
    assert record.status == MemoryStatus.CANDIDATE
    assert record.blocked_by == (active_decision.id,)
    assert result.reason_code == REASON_BLOCKED_LOWER_RANK


def test_same_rank_conflict_stays_candidate_without_replace(lifecycle, active_decision, competing_decision, context):
    result = lifecycle.ingest(competing_decision, context)
    assert result.reason_code == REASON_SAME_RANK
    assert result.final_status == MemoryStatus.CANDIDATE
    assert lifecycle.repository.get(result.candidate_id).blocked_by == (active_decision.id,)


def test_correction_conflict_requires_explicit_replace(lifecycle, active_decision, correction_proposal, context):
    first = lifecycle.ingest(correction_proposal, context)
    first_active = lifecycle.repository.get(first.candidate_id)
    later_correction = proposal(
        statement="A second user correction",
        kind="decision",
        slot=active_decision.slot,
        tags=("architecture.memory", "project.decision"),
        speech_act="explicit_correction",
        evidence_refs=("command:msg-42",),
        confidence=1.0,
        importance=5,
    )
    result = lifecycle.ingest(later_correction, context)
    second = lifecycle.repository.get(result.candidate_id)
    assert first_active.status == MemoryStatus.ACTIVE
    assert second.status == MemoryStatus.CANDIDATE
    assert second.blocked_by == (first_active.id,)
    assert result.reason_code == REASON_CORRECTION_CONFLICT


def test_explicit_promote_with_replace_overcomes_same_rank(lifecycle, active_decision, competing_decision, context):
    result = lifecycle.ingest(competing_decision, context)
    promoted = lifecycle.promote(
        result.candidate_id, actor=ActorKind.USER, reason="user confirmed", replace_id=active_decision.id
    )
    old = lifecycle.repository.get(active_decision.id)
    new = lifecycle.repository.get(promoted.candidate_id)
    assert new.status == MemoryStatus.ACTIVE
    assert old.status == MemoryStatus.SUPERSEDED
    assert old.replacement_id == new.id


def test_promote_without_candidate_id_fails(lifecycle):
    with pytest.raises(MemoryRecordNotFound):
        lifecycle.promote("mem_" + "f" * 32, actor=ActorKind.USER, reason="none")


def test_promote_on_blocked_candidate_without_replace_fails(lifecycle, active_decision, competing_decision, context):
    result = lifecycle.ingest(competing_decision, context)
    with pytest.raises(MemoryLifecycleError):
        lifecycle.promote(result.candidate_id, actor=ActorKind.USER, reason="user confirmed")


# ---------------------------------------------------------------------------
# Revoke and expiry
# ---------------------------------------------------------------------------


def test_revoke_does_not_resurrect_superseded_record(lifecycle, replacement_chain, repository):
    lifecycle.revoke(replacement_chain["current_id"], reason="user withdrew correction")
    assert lifecycle.repository.active_for_conflict_key(replacement_chain["conflict_key"]) is None


def test_revoke_creates_new_revision_and_keeps_history(lifecycle, active_decision):
    revoked = lifecycle.revoke(active_decision.id, reason="user decided")
    assert revoked.status == MemoryStatus.REVOKED
    assert revoked.revision == 2
    revisions = lifecycle.repository.revisions(active_decision.id)
    assert [r.status for r in revisions] == [MemoryStatus.ACTIVE, MemoryStatus.REVOKED]


def test_revoke_unknown_id_fails(lifecycle):
    with pytest.raises(MemoryRecordNotFound):
        lifecycle.revoke("mem_" + "f" * 32, reason="none")


def test_revoke_terminal_fails(lifecycle, replacement_chain):
    lifecycle.revoke(replacement_chain["current_id"], reason="withdraw")
    with pytest.raises(MemoryLifecycleError):
        lifecycle.revoke(replacement_chain["current_id"], reason="again")


def test_candidate_expires_after_ttl(lifecycle, inferred_proposal, context, repository):
    result = lifecycle.ingest(inferred_proposal, context)
    later = context.now + timedelta(days=31)
    expired = lifecycle.expire_due(later)
    assert result.candidate_id in expired
    assert repository.get(result.candidate_id).status == MemoryStatus.EXPIRED


def test_active_expires_at_expires_at(lifecycle, active_decision, context):
    lifecycle.repository = StructuredMemoryRepository(lifecycle.repository.workspace)
    due = MemoryRecord.model_validate(
        {
            **record_data(
                status="active",
                memory_id=new_memory_id(),
                slot="expiry.test",
                expires_at="2026-08-10T00:00:00Z",
            )
        }
    )
    lifecycle.repository.append_transaction(make_transaction(due))
    expired = lifecycle.expire_due(context.now)
    assert due.id in expired
    assert lifecycle.repository.get(due.id).status == MemoryStatus.EXPIRED


def test_active_without_expires_at_never_expires_by_age(lifecycle, active_decision, context):
    far_future = context.now + timedelta(days=400)
    assert lifecycle.expire_due(far_future) == ()


def test_expiry_does_not_touch_other_records(lifecycle, inferred_proposal, context):
    result = lifecycle.ingest(inferred_proposal, context)
    later = context.now + timedelta(days=10)
    assert lifecycle.expire_due(later) == ()
    assert lifecycle.repository.get(result.candidate_id).status == MemoryStatus.CANDIDATE


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


def test_promotion_write_failure_leaves_candidate_intact(lifecycle, context, monkeypatch):
    original = lifecycle.repository.append_transaction
    calls = {"n": 0}

    def flaky(transaction):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise MemoryWriteError("disk full")
        return original(transaction)

    monkeypatch.setattr(lifecycle.repository, "append_transaction", flaky)
    confirmed = proposal(speech_act="confirmed_decision", evidence_refs=("command:msg-42",), confidence=0.95)
    with pytest.raises(MemoryWriteError, match="disk full"):
        lifecycle.ingest(confirmed, context)
    records = lifecycle.repository.current_records(MemoryStatus.CANDIDATE)
    assert len(records) == 1
    assert records[0].revision == 1


def test_candidate_create_failure_propagates(lifecycle, context, monkeypatch):
    def boom(transaction):
        raise MemoryWriteError("journal unreadable")

    monkeypatch.setattr(lifecycle.repository, "append_transaction", boom)
    with pytest.raises(MemoryWriteError):
        lifecycle.ingest(proposal(), context)
    assert lifecycle.repository.current_records() == ()


def test_can_auto_promote_never_for_inferred():
    record = MemoryRecord.model_validate(
        record_data(source_level="inferred", confidence=1.0, importance=5)
    )
    policy = LifecyclePolicy(auto_promote_verified=True, min_repeated_evidence=2, candidate_ttl_days=30)
    assert can_auto_promote(record, policy) is False
