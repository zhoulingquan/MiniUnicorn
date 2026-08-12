"""Governed memory lifecycle: candidate ingestion, promotion, conflict, revoke, expiry.

Only this module may promote or replace records. It constructs all changed
record snapshots first and commits them through one repository transaction.

Normative source: docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from loguru import logger

from miniunicorn.agent.memory_models import (
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryTransaction,
    SourceLevel,
    UnknownMemoryTag,
    content_hash,
    new_memory_id,
    new_transaction_id,
    normalize_match_text,
    transaction_checksum,
)
from miniunicorn.agent.memory_models import (
    MemoryError as StructuredMemoryError,
)
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

REASON_CREATED = "created_candidate"
REASON_AUTO_PROMOTED = "auto_promoted"
REASON_MERGED = "merged_identical_content"
REASON_REPLACED = "replaced_higher_rank"
REASON_BLOCKED_LOWER_RANK = "blocked_by_higher_rank"
REASON_SAME_RANK = "same_rank_requires_explicit_replace"
REASON_CORRECTION_CONFLICT = "correction_conflict_requires_explicit_replace"
REASON_EXISTING = "existing_candidate_returned"

SOURCE_RANK = {
    SourceLevel.INFERRED: 1,
    SourceLevel.REPEATED_EXPERIENCE: 2,
    SourceLevel.VERIFIED: 3,
    SourceLevel.CONFIRMED_DECISION: 4,
    SourceLevel.EXPLICIT_CORRECTION: 5,
}

_HARD_EVIDENCE_KINDS = frozenset({EvidenceKind.USER_MESSAGE, EvidenceKind.MANUAL})
_VERIFIED_EVIDENCE_KINDS = frozenset({EvidenceKind.FILE, EvidenceKind.TOOL_RESULT, EvidenceKind.GIT})


class MemoryLifecycleError(StructuredMemoryError):
    """Base class for lifecycle rule violations."""


class MemoryEvidenceUnresolved(MemoryLifecycleError):  # noqa: N818
    """Raised when a proposal references evidence that cannot be located or verified."""


class MemoryRecordNotFound(MemoryLifecycleError):  # noqa: N818
    """Raised when an operation targets an unknown memory id."""


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def can_auto_promote(record: MemoryRecord, config: LifecyclePolicy) -> bool:
    distinct = {(e.kind, e.ref, e.sha256) for e in record.evidence}
    if record.source_level is SourceLevel.EXPLICIT_CORRECTION:
        return any(e.kind in _HARD_EVIDENCE_KINDS for e in record.evidence)
    if record.source_level is SourceLevel.CONFIRMED_DECISION:
        return record.confidence >= 0.90 and any(e.kind in _HARD_EVIDENCE_KINDS for e in record.evidence)
    if record.source_level is SourceLevel.VERIFIED:
        return config.auto_promote_verified and record.confidence >= 0.80 and any(e.kind in _VERIFIED_EVIDENCE_KINDS for e in record.evidence)
    if record.source_level is SourceLevel.REPEATED_EXPERIENCE:
        return record.confidence >= 0.85 and len(distinct) >= config.min_repeated_evidence
    return False


def classify_source_level(evidence: tuple[EvidenceRef, ...], speech_act: SourceLevel) -> SourceLevel:
    kinds = {e.kind for e in evidence}
    if speech_act is SourceLevel.EXPLICIT_CORRECTION and kinds & _HARD_EVIDENCE_KINDS:
        return SourceLevel.EXPLICIT_CORRECTION
    if speech_act is SourceLevel.CONFIRMED_DECISION and kinds & _HARD_EVIDENCE_KINDS:
        return SourceLevel.CONFIRMED_DECISION
    if kinds & _VERIFIED_EVIDENCE_KINDS:
        return SourceLevel.VERIFIED
    distinct = {(e.kind, e.ref, e.sha256) for e in evidence}
    if len(distinct) >= 2:
        return SourceLevel.REPEATED_EXPERIENCE
    return SourceLevel.INFERRED


@dataclass(frozen=True)
class LifecyclePolicy:
    auto_promote_verified: bool
    min_repeated_evidence: int
    candidate_ttl_days: int


@dataclass(frozen=True)
class IngestContext:
    actor: ActorKind
    reason: str
    source_batch: str
    scope: MemoryScope
    evidence_catalog: Mapping[str, EvidenceRef]
    now: datetime


@dataclass(frozen=True)
class IngestResult:
    candidate_id: str
    final_status: MemoryStatus
    active_id: str | None
    transaction_ids: tuple[str, ...]
    reason_code: str


class StructuredMemoryLifecycle:
    """Governed transitions over a StructuredMemoryRepository."""

    def __init__(self, repository: StructuredMemoryRepository, policy: LifecyclePolicy):
        self.repository = repository
        self.policy = policy

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(self, proposal: CandidateProposal, context: IngestContext) -> IngestResult:
        evidence = self._resolve_evidence(proposal, context)
        tags = self._canonical_tags(proposal.tags)
        now = _utc(context.now)
        content = content_hash(proposal.kind, context.scope, proposal.subject, proposal.slot, proposal.statement)

        existing = self._find_existing_record(context, content)
        if existing is not None:
            if existing.status is MemoryStatus.ACTIVE:
                return IngestResult(
                    candidate_id=existing.id,
                    final_status=MemoryStatus.ACTIVE,
                    active_id=existing.id,
                    transaction_ids=(),
                    reason_code=REASON_EXISTING,
                )
            if existing.status is MemoryStatus.CANDIDATE:
                return self._apply_after_ingest(existing, context, now, "")
            return IngestResult(
                candidate_id=existing.id,
                final_status=existing.status,
                active_id=None,
                transaction_ids=(),
                reason_code=REASON_EXISTING,
            )

        source_level = classify_source_level(evidence, proposal.speech_act)
        record = MemoryRecord(
            id=new_memory_id(),
            revision=1,
            status=MemoryStatus.CANDIDATE,
            kind=proposal.kind,
            scope=context.scope,
            subject=proposal.subject,
            slot=proposal.slot,
            statement=proposal.statement,
            detail=proposal.detail,
            tags=tags,
            aliases=proposal.aliases,
            source_level=source_level,
            confidence=proposal.confidence,
            importance=proposal.importance,
            evidence=evidence,
            content_hash=content,
            derived_from=tuple(sorted({f"idx:{proposal.proposal_index}"})),
            valid_from=now,
            expires_at=proposal.expires_at,
            created_at=now,
            updated_at=now,
            status_reason=context.reason,
        )
        create_tx = self._transaction(
            [record],
            actor=context.actor,
            reason=context.reason,
            source_batch=context.source_batch,
            expected_revisions={record.id: 0},
            recorded_at=now,
        )
        self.repository.append_transaction(create_tx)
        return self._apply_after_ingest(record, context, now, create_tx.tx_id)

    def _resolve_evidence(self, proposal: CandidateProposal, context: IngestContext) -> tuple[EvidenceRef, ...]:
        resolved: list[EvidenceRef] = []
        for ref in proposal.evidence_refs:
            evidence = context.evidence_catalog.get(ref)
            if evidence is None:
                raise MemoryEvidenceUnresolved(f"evidence ref not found in catalog: {ref}")
            if evidence.kind in _VERIFIED_EVIDENCE_KINDS and evidence.sha256:
                if hashlib.sha256(evidence.excerpt.encode("utf-8")).hexdigest() != evidence.sha256:
                    raise MemoryEvidenceUnresolved(f"evidence digest mismatch for {ref}")
            if evidence.kind is EvidenceKind.FILE:
                self._verify_file_evidence(evidence, ref)
            resolved.append(evidence)
        if not resolved:
            raise MemoryEvidenceUnresolved("proposal requires at least one evidence ref")
        return tuple(resolved)

    def _verify_file_evidence(self, evidence: EvidenceRef, catalog_ref: str) -> None:
        """Check a resolvable file reference against its captured excerpt."""
        locator = evidence.ref.split("#", 1)[0]
        if locator.startswith("file:"):
            locator = locator.removeprefix("file:")
        if not locator:
            return
        path = Path(locator)
        if not path.is_absolute():
            path = self.repository.workspace / path
        try:
            resolved = path.resolve()
            workspace = self.repository.workspace.resolve()
            resolved.relative_to(workspace)
        except (OSError, ValueError):
            return
        if not resolved.is_file():
            return
        try:
            source = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise MemoryEvidenceUnresolved(
                f"file evidence source unreadable for {catalog_ref}: {exc}"
            ) from exc
        if evidence.excerpt not in source:
            raise MemoryEvidenceUnresolved(
                f"file evidence source mismatch for {catalog_ref}"
            )

    def _canonical_tags(self, tags: tuple[str, ...]) -> tuple[str, ...]:
        catalog = self.repository.tag_catalog
        canonical: list[str] = []
        for tag in tags:
            matched = [entry.name for entry in catalog.tags if normalize_match_text(entry.name) == normalize_match_text(tag)]
            if not matched:
                raise UnknownMemoryTag(f"unknown tag: {tag}")
            canonical.append(matched[0])
        return tuple(sorted(set(canonical)))

    def _find_existing_record(self, context: IngestContext, content: str) -> MemoryRecord | None:
        for memory_id in self.repository.record_ids_for_source(context.source_batch):
            record = self.repository.get(memory_id)
            if record is not None and record.content_hash == content:
                return record
        return None

    # ------------------------------------------------------------------
    # Post-ingest conflict resolution
    # ------------------------------------------------------------------

    def _apply_after_ingest(self, candidate: MemoryRecord, context: IngestContext, now: datetime, create_tx_id: str) -> IngestResult:
        existing = self.repository.active_for_conflict_key(candidate.conflict_key)
        if existing is None:
            if can_auto_promote(candidate, self.policy):
                promoted = self._promote_single(candidate, context, now, reason_code=REASON_AUTO_PROMOTED)
                return dataclasses_replace(
                    promoted,
                    transaction_ids=(
                        (create_tx_id, promoted.transaction_ids[0])
                        if create_tx_id
                        else promoted.transaction_ids
                    ),
                )
            return IngestResult(
                candidate_id=candidate.id,
                final_status=MemoryStatus.CANDIDATE,
                active_id=None,
                transaction_ids=(create_tx_id,) if create_tx_id else (),
                reason_code=REASON_CREATED if create_tx_id else REASON_EXISTING,
            )
        if existing.content_hash == candidate.content_hash:
            return self._merge_identical(candidate, existing, context, now, create_tx_id)
        return self._resolve_conflict(candidate, existing, context, now, create_tx_id)

    def _resolve_conflict(
        self,
        candidate: MemoryRecord,
        existing: MemoryRecord,
        context: IngestContext,
        now: datetime,
        create_tx_id: str,
    ) -> IngestResult:
        if candidate.blocked_by == (existing.id,):
            return IngestResult(
                candidate_id=candidate.id,
                final_status=MemoryStatus.CANDIDATE,
                active_id=None,
                transaction_ids=(),
                reason_code=REASON_EXISTING,
            )
        new_rank = SOURCE_RANK[candidate.source_level]
        old_rank = SOURCE_RANK[existing.source_level]
        if new_rank > old_rank and can_auto_promote(candidate, self.policy):
            result = self._replace_with(candidate, existing, context, now)
            return IngestResult(
                candidate_id=candidate.id,
                final_status=MemoryStatus.ACTIVE,
                active_id=candidate.id,
                transaction_ids=(create_tx_id, result.transaction_ids[0]),
                reason_code=REASON_REPLACED,
            )
        if new_rank == old_rank and candidate.source_level is SourceLevel.EXPLICIT_CORRECTION:
            blocked = self._block_candidate(candidate, existing.id, context, now)
            return IngestResult(
                candidate_id=candidate.id,
                final_status=MemoryStatus.CANDIDATE,
                active_id=None,
                transaction_ids=(create_tx_id, blocked.transaction_ids[0]),
                reason_code=REASON_CORRECTION_CONFLICT,
            )
        if new_rank == old_rank:
            blocked = self._block_candidate(candidate, existing.id, context, now)
            return IngestResult(
                candidate_id=candidate.id,
                final_status=MemoryStatus.CANDIDATE,
                active_id=None,
                transaction_ids=(create_tx_id, blocked.transaction_ids[0]),
                reason_code=REASON_SAME_RANK,
            )
        blocked = self._block_candidate(candidate, existing.id, context, now)
        return IngestResult(
            candidate_id=candidate.id,
            final_status=MemoryStatus.CANDIDATE,
            active_id=None,
            transaction_ids=(create_tx_id, blocked.transaction_ids[0]),
            reason_code=REASON_BLOCKED_LOWER_RANK,
        )

    # ------------------------------------------------------------------
    # Manual promote
    # ------------------------------------------------------------------

    def promote(
        self,
        candidate_id: str,
        *,
        actor: ActorKind,
        reason: str,
        replace_id: str | None = None,
    ) -> IngestResult:
        candidate = self.repository.get(candidate_id)
        if candidate is None:
            raise MemoryRecordNotFound(f"no candidate with id {candidate_id}")
        if candidate.status is not MemoryStatus.CANDIDATE:
            raise MemoryLifecycleError(f"record {candidate_id} is not a candidate")
        now = _utc(datetime.now(timezone.utc))
        context = IngestContext(
            actor=actor,
            reason=reason,
            source_batch="",
            scope=candidate.scope,
            evidence_catalog={},
            now=now,
        )
        existing = self.repository.active_for_conflict_key(candidate.conflict_key)
        if existing is None:
            return self._promote_single(candidate, context, now, reason_code="user_promoted")
        if existing.content_hash == candidate.content_hash:
            return self._merge_identical(candidate, existing, context, now, "")
        if replace_id is None or replace_id != existing.id:
            raise MemoryLifecycleError(f"conflict with active {existing.id}; --replace {existing.id} required")
        return self._replace_with(candidate, existing, context, now)

    # ------------------------------------------------------------------
    # Revoke and expiry
    # ------------------------------------------------------------------

    def revoke(self, memory_id: str, *, reason: str) -> MemoryRecord:
        current = self.repository.get(memory_id)
        if current is None:
            raise MemoryRecordNotFound(f"no memory record with id {memory_id}")
        if current.status not in {MemoryStatus.CANDIDATE, MemoryStatus.ACTIVE}:
            raise MemoryLifecycleError(f"record {memory_id} is in terminal status {current.status.value}")
        now = _utc(datetime.now(timezone.utc))
        revoked = current.model_copy(
            update={
                "revision": current.revision + 1,
                "status": MemoryStatus.REVOKED,
                "blocked_by": (),
                "updated_at": now,
                "status_reason": reason,
            }
        )
        tx = self._transaction(
            [revoked],
            actor=ActorKind.USER,
            reason=reason,
            expected_revisions={revoked.id: current.revision},
            recorded_at=now,
        )
        self.repository.append_transaction(tx)
        logger.info("memory_record_revoked id={} reason={}", memory_id, reason)
        return revoked

    def expire_due(self, now: datetime) -> tuple[str, ...]:
        now = _utc(now)
        expired_ids: list[str] = []
        for record in self.repository.current_records():
            if record.status is MemoryStatus.CANDIDATE:
                age_days = max(0, (now - record.created_at).days)
                due = age_days >= self.policy.candidate_ttl_days
            elif record.status is MemoryStatus.ACTIVE and record.expires_at is not None:
                due = record.expires_at <= now
            else:
                due = False
            if not due:
                continue
            expired = record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "status": MemoryStatus.EXPIRED,
                    "blocked_by": (),
                    "updated_at": now,
                    "status_reason": "expired by hygiene",
                }
            )
            tx = self._transaction(
                [expired],
                actor=ActorKind.SYSTEM,
                reason="hygiene expiry",
                expected_revisions={expired.id: record.revision},
                recorded_at=now,
            )
            self.repository.append_transaction(tx)
            expired_ids.append(record.id)
        return tuple(expired_ids)

    # ------------------------------------------------------------------
    # Shared transaction builders
    # ------------------------------------------------------------------

    def _promote_single(self, candidate: MemoryRecord, context: IngestContext, now: datetime, reason_code: str) -> IngestResult:
        promoted = candidate.model_copy(
            update={
                "revision": candidate.revision + 1,
                "status": MemoryStatus.ACTIVE,
                "blocked_by": (),
                "updated_at": now,
                "status_reason": context.reason,
            }
        )
        tx = self._transaction(
            [promoted],
            actor=context.actor,
            reason=context.reason,
            source_batch=context.source_batch,
            expected_revisions={promoted.id: candidate.revision},
            recorded_at=now,
        )
        self.repository.append_transaction(tx)
        logger.info("memory_candidate_promoted id={} tx={}", candidate.id, tx.tx_id)
        return IngestResult(
            candidate_id=candidate.id,
            final_status=MemoryStatus.ACTIVE,
            active_id=candidate.id,
            transaction_ids=(tx.tx_id,),
            reason_code=reason_code,
        )

    def _merge_identical(
        self,
        candidate: MemoryRecord,
        existing: MemoryRecord,
        context: IngestContext,
        now: datetime,
        create_tx_id: str,
    ) -> IngestResult:
        merged = existing.model_copy(
            update={
                "revision": existing.revision + 1,
                "evidence": tuple({(e.kind, e.ref, e.sha256): e for e in existing.evidence + candidate.evidence}.values()),
                "derived_from": tuple(sorted(set(existing.derived_from) | set(candidate.derived_from))),
                "updated_at": now,
            }
        )
        superseded = candidate.model_copy(
            update={
                "revision": candidate.revision + 1,
                "status": MemoryStatus.SUPERSEDED,
                "replacement_id": existing.id,
                "blocked_by": (),
                "updated_at": now,
                "status_reason": "merged into active record",
            }
        )
        tx = self._transaction(
            [merged, superseded],
            actor=context.actor,
            reason=context.reason,
            source_batch=context.source_batch,
            expected_revisions={existing.id: existing.revision, candidate.id: candidate.revision},
            recorded_at=now,
        )
        self.repository.append_transaction(tx)
        logger.info("memory_record_superseded id={} replacement={} tx={}", candidate.id, existing.id, tx.tx_id)
        return IngestResult(
            candidate_id=candidate.id,
            final_status=MemoryStatus.SUPERSEDED,
            active_id=existing.id,
            transaction_ids=(create_tx_id, tx.tx_id) if create_tx_id else (tx.tx_id,),
            reason_code=REASON_MERGED,
        )

    def _replace_with(
        self,
        candidate: MemoryRecord,
        existing: MemoryRecord,
        context: IngestContext,
        now: datetime,
    ) -> IngestResult:
        promoted = candidate.model_copy(
            update={
                "revision": candidate.revision + 1,
                "status": MemoryStatus.ACTIVE,
                "supersedes": (existing.id,),
                "blocked_by": (),
                "updated_at": now,
                "status_reason": context.reason,
            }
        )
        superseded = existing.model_copy(
            update={
                "revision": existing.revision + 1,
                "status": MemoryStatus.SUPERSEDED,
                "replacement_id": candidate.id,
                "updated_at": now,
                "status_reason": context.reason,
            }
        )
        tx = self._transaction(
            [promoted, superseded],
            actor=context.actor,
            reason=context.reason,
            source_batch=context.source_batch,
            expected_revisions={candidate.id: candidate.revision, existing.id: existing.revision},
            recorded_at=now,
        )
        self.repository.append_transaction(tx)
        logger.info("memory_record_superseded id={} replacement={} tx={}", existing.id, candidate.id, tx.tx_id)
        return IngestResult(
            candidate_id=candidate.id,
            final_status=MemoryStatus.ACTIVE,
            active_id=candidate.id,
            transaction_ids=(tx.tx_id,),
            reason_code=REASON_REPLACED,
        )

    def _block_candidate(
        self,
        candidate: MemoryRecord,
        blocked_by: str,
        context: IngestContext,
        now: datetime,
    ) -> IngestResult:
        blocked = candidate.model_copy(
            update={
                "revision": candidate.revision + 1,
                "blocked_by": (blocked_by,),
                "updated_at": now,
                "status_reason": "blocked by active conflict",
            }
        )
        tx = self._transaction(
            [blocked],
            actor=context.actor,
            reason=context.reason,
            source_batch=context.source_batch,
            expected_revisions={blocked.id: candidate.revision},
            recorded_at=now,
        )
        self.repository.append_transaction(tx)
        logger.info("memory_conflict_blocked id={} blocked_by={} tx={}", candidate.id, blocked_by, tx.tx_id)
        return IngestResult(
            candidate_id=candidate.id,
            final_status=MemoryStatus.CANDIDATE,
            active_id=None,
            transaction_ids=(tx.tx_id,),
            reason_code=REASON_BLOCKED_LOWER_RANK,
        )

    def _transaction(
        self,
        records: list[MemoryRecord],
        *,
        actor: ActorKind,
        reason: str,
        source_batch: str = "",
        expected_revisions: dict[str, int],
        recorded_at: datetime,
    ) -> MemoryTransaction:
        tx = MemoryTransaction(
            tx_id=new_transaction_id(),
            recorded_at=recorded_at,
            actor=actor,
            reason=reason,
            source_batch=source_batch,
            expected_revisions=expected_revisions,
            operations=[MemoryOperation(op="put", record=record) for record in records],
            checksum_sha256="f" * 64,
        )
        return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})
