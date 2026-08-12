"""Repository tests: replay, fail-closed corruption, atomic multi-record transactions."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from filelock import FileLock

from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    InvalidMemoryTransition,
    MemoryLockTimeout,
    MemoryOperation,
    MemoryRecord,
    MemoryRevisionConflict,
    MemoryStatus,
    MemoryTransaction,
    MemoryWriteError,
    ScopeKind,
    UnknownMemoryTag,
    new_memory_id,
    new_transaction_id,
    transaction_checksum,
)
from miniunicorn.agent.memory_repository import (
    RepositoryDegradedError,
    StructuredMemoryRepository,
)

UTC = timezone.utc


def dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def make_evidence(kind: str = "manual", ref: str = "command:msg-1", excerpt: str = "evidence excerpt", sha256: str | None = None):
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
    subject: str = "MiniUnicorn",
    kind: str = "fact",
    scope=None,
    evidence=None,
    source_level: str = "inferred",
    confidence: float = 0.9,
    importance: int = 4,
    **overrides,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": memory_id or new_memory_id(),
        "revision": revision,
        "status": status,
        "kind": kind,
        "scope": scope or {"kind": "project", "key": "project:6b5ec7b29e32"},
        "subject": subject,
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


def make_transaction(
    *records,
    actor: str = "dream",
    reason: str = "test",
    source_batch: str = "",
    expected_revisions: dict | None = None,
    recorded_at: str = "2026-08-11T08:31:00Z",
) -> MemoryTransaction:
    expected = expected_revisions or {rec.id: rec.revision - 1 for rec in records}
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id=new_transaction_id(),
        recorded_at=dt(recorded_at),
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
def repository(workspace: Path) -> StructuredMemoryRepository:
    return StructuredMemoryRepository(workspace, lock_timeout_s=0.1)


@pytest.fixture
def create_tx() -> MemoryTransaction:
    return make_transaction(MemoryRecord.model_validate(record_data()))


@pytest.fixture
def transaction(create_tx) -> MemoryTransaction:
    return create_tx


def update_transaction(
    previous: MemoryRecord,
    expected_revision: int,
    *,
    statement: str | None = None,
    evidence: tuple[EvidenceRef, ...] | None = None,
    status: MemoryStatus | None = None,
    actor: str = "user",
    reason: str = "update",
    source_batch: str = "",
) -> MemoryTransaction:
    record = previous.model_copy(
        update={
            "revision": previous.revision + 1,
            "updated_at": dt("2026-08-11T08:32:00Z"),
            "statement": statement or previous.statement,
            "evidence": evidence or previous.evidence,
            "status": status or previous.status,
        }
    )
    return make_transaction(record, expected_revisions={record.id: expected_revision}, actor=actor, reason=reason, source_batch=source_batch)


@pytest.fixture
def replacement_tx(repository) -> MemoryTransaction:
    active = MemoryRecord.model_validate(record_data(status="active", revision=1, memory_id="mem_" + "a" * 32))
    candidate = MemoryRecord.model_validate(
        record_data(statement=active.statement, status="candidate", revision=1, memory_id="mem_" + "b" * 32)
    )
    repository.append_transaction(make_transaction(active))
    repository.append_transaction(make_transaction(candidate))
    active_merge = active.model_copy(
        update={
            "revision": 2,
            "evidence": active.evidence + (EvidenceRef(kind=EvidenceKind.HISTORY, ref="history:9", excerpt="dup"),),
            "updated_at": dt("2026-08-11T08:33:00Z"),
        }
    )
    candidate_superseded = candidate.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.SUPERSEDED,
            "replacement_id": active.id,
            "updated_at": dt("2026-08-11T08:33:00Z"),
        }
    )
    return make_transaction(
        active_merge,
        candidate_superseded,
        expected_revisions={active.id: 1, candidate.id: 1},
        actor="dream",
        reason="dedupe identical content",
    )


@pytest.fixture
def valid_line(repository, transaction) -> str:
    return repository._canonical_transaction_line(transaction)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_multi_record_transaction_replays_atomically(repository, replacement_tx):
    repository.append_transaction(replacement_tx)
    rebuilt = StructuredMemoryRepository(repository.workspace)
    assert rebuilt.get("mem_" + "a" * 32).status == MemoryStatus.ACTIVE
    assert rebuilt.get("mem_" + "b" * 32).status == MemoryStatus.SUPERSEDED


def test_empty_log_is_healthy(repository):
    assert repository.health.state == "healthy"
    assert repository.current_records() == ()


def test_valid_empty_lines_are_ignored(repository, valid_line):
    repository.journal_path.write_text("\n\n" + valid_line + "\n\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "healthy"
    assert len(repository.current_records()) == 1


def test_single_transaction_replays(repository, transaction):
    repository.append_transaction(transaction)
    rebuilt = StructuredMemoryRepository(repository.workspace)
    record = rebuilt.get(transaction.operations[0].record.id)
    assert record is not None
    assert record.status == MemoryStatus.CANDIDATE


def test_revisions_are_ordered(repository, create_tx):
    repository.append_transaction(create_tx)
    record = create_tx.operations[0].record
    repository.append_transaction(update_transaction(record, 1))
    revisions = repository.revisions(record.id)
    assert [r.revision for r in revisions] == [1, 2]
    assert revisions[0].statement == record.statement


def test_current_records_ordering_is_stable(repository):
    first = MemoryRecord.model_validate(record_data(statement="zebra", memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(record_data(statement="apple", memory_id="mem_" + "b" * 32))
    repository.append_transaction(make_transaction(second))
    repository.append_transaction(make_transaction(first))
    ids = [r.id for r in repository.current_records()]
    assert ids == [first.id, second.id]


# ---------------------------------------------------------------------------
# Corruption is fail-closed
# ---------------------------------------------------------------------------


def test_bad_checksum_stops_at_first_bad_line_and_disables_writes(repository, valid_line):
    marker = '"checksum_sha256":"'
    pos = valid_line.find(marker) + len(marker)
    corrupted = valid_line[:pos] + ("1" if valid_line[pos] != "1" else "0") + valid_line[pos + 1 :]
    repository.journal_path.write_text(valid_line + "\n" + corrupted + "\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.last_valid_line == 1
    assert health.error_code == "checksum_mismatch"
    with pytest.raises(RepositoryDegradedError):
        repository.append_transaction(make_transaction(MemoryRecord.model_validate(record_data(statement="another"))))


def test_invalid_json_tail_degrades(repository, valid_line):
    repository.journal_path.write_text(valid_line + "\n{not valid json\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.last_valid_line == 1
    assert health.error_code == "invalid_json"
    assert repository.current_records() == ()


def test_unsupported_schema_degrades(repository, valid_line):
    raw = json.loads(valid_line)
    raw["schema_version"] = SCHEMA_VERSION + 1
    repository.journal_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.error_code == "unsupported_schema"


def test_skipped_revision_degrades(repository):
    tx = make_transaction(MemoryRecord.model_validate(record_data(revision=2)))
    repository.journal_path.write_text(repository._canonical_transaction_line(tx) + "\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.error_code == "revision_conflict"


def test_illegal_status_transition_degrades(repository):
    active = MemoryRecord.model_validate(record_data(status="active", revision=1))
    invalid = MemoryRecord.model_validate(record_data(status="candidate", revision=2, memory_id=active.id))
    tx = make_transaction(invalid, expected_revisions={active.id: 1})
    journal = (
        repository._canonical_transaction_line(make_transaction(active))
        + "\n"
        + repository._canonical_transaction_line(tx)
        + "\n"
    )
    repository.journal_path.write_text(journal, encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.last_valid_line == 1
    assert health.error_code == "invalid_transition"


def test_unknown_tag_degrades(repository):
    tx = make_transaction(MemoryRecord.model_validate(record_data(tags=("not.registered",))))
    repository.journal_path.write_text(repository._canonical_transaction_line(tx) + "\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.error_code == "unknown_tag"


def test_duplicate_operation_id_degrades(repository, create_tx):
    raw = json.loads(repository._canonical_transaction_line(create_tx))
    raw["operations"] = [raw["operations"][0], raw["operations"][0]]
    repository.journal_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.error_code == "invalid_transaction"


def test_truncated_tail_is_not_skipped(repository, valid_line):
    repository.journal_path.write_text(valid_line + "\n" + valid_line[:-10] + "\n", encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.last_valid_line == 1
    with pytest.raises(RepositoryDegradedError):
        repository.append_transaction(make_transaction(MemoryRecord.model_validate(record_data(statement="another"))))


# ---------------------------------------------------------------------------
# Append protocol
# ---------------------------------------------------------------------------


def test_append_failure_does_not_update_index(repository, transaction, monkeypatch):
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("disk full")))
    with pytest.raises(MemoryWriteError, match="disk full"):
        repository.append_transaction(transaction)
    assert repository.get(transaction.operations[0].record.id) is None
    assert repository.health.state == "degraded"
    assert repository.health.error_code == "write_uncertain"


def test_same_status_active_revision_cannot_change_fact_fields(repository):
    active = MemoryRecord.model_validate(record_data(status="active"))
    repository.append_transaction(make_transaction(active))
    changed = active.model_copy(
        update={
            "revision": 2,
            "statement": "Use SQLite",
            "updated_at": dt("2026-08-11T08:32:00Z"),
        }
    )

    with pytest.raises(InvalidMemoryTransition, match="same-status revision"):
        repository.append_transaction(
            make_transaction(changed, expected_revisions={active.id: 1})
        )


def test_transaction_rejects_two_active_records_for_same_conflict_key(repository):
    first = MemoryRecord.model_validate(record_data(status="active", statement="Use PostgreSQL"))
    second = MemoryRecord.model_validate(record_data(status="active", statement="Use SQLite"))

    with pytest.raises(InvalidMemoryTransition, match="multiple active records"):
        repository.append_transaction(make_transaction(first, second))

    assert repository.current_records() == ()


def test_locked_append_synchronizes_external_writer_before_validation(workspace):
    first = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    stale = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    record = MemoryRecord.model_validate(record_data())
    transaction = make_transaction(record)

    first.append_transaction(transaction)
    with pytest.raises(MemoryRevisionConflict):
        stale.append_transaction(transaction)

    assert stale.health.state == "healthy"
    assert stale.get(record.id) == record
    assert first.journal_path.read_text(encoding="utf-8").count("\n") == 1


def test_expected_revision_conflict_rejects_second_writer(repository, create_tx):
    repository.append_transaction(create_tx)
    record = create_tx.operations[0].record
    first_evidence = record.evidence + (
        EvidenceRef(kind=EvidenceKind.MANUAL, ref="command:first", excerpt="first"),
    )
    second_evidence = record.evidence + (
        EvidenceRef(kind=EvidenceKind.MANUAL, ref="command:second", excerpt="second"),
    )
    first = update_transaction(record, expected_revision=1, evidence=first_evidence)
    second = update_transaction(record, expected_revision=1, evidence=second_evidence)
    repository.append_transaction(first)
    with pytest.raises(MemoryRevisionConflict):
        repository.append_transaction(second)


def test_new_record_requires_expected_revision_zero(repository):
    record = MemoryRecord.model_validate(record_data())
    tx = make_transaction(record, expected_revisions={record.id: 1})
    with pytest.raises(MemoryRevisionConflict):
        repository.append_transaction(tx)
    assert repository.get(record.id) is None


def test_append_rejects_bad_checksum(repository, create_tx):
    bad = create_tx.model_copy(update={"checksum_sha256": "0" * 64})
    with pytest.raises(MemoryWriteError):
        repository.append_transaction(bad)


def test_append_rejects_unknown_tag(repository):
    tx = make_transaction(MemoryRecord.model_validate(record_data(tags=("not.registered",))))
    with pytest.raises(UnknownMemoryTag):
        repository.append_transaction(tx)
    assert repository.current_records() == ()


def test_append_rejects_illegal_transition(repository):
    active = MemoryRecord.model_validate(record_data(status="active", revision=1))
    repository.append_transaction(make_transaction(active))
    invalid = MemoryRecord.model_validate(record_data(status="candidate", revision=2, memory_id=active.id))
    tx = make_transaction(invalid, expected_revisions={active.id: 1})
    with pytest.raises(InvalidMemoryTransition):
        repository.append_transaction(tx)


def test_lock_timeout_raises_memory_lock_timeout(repository, create_tx):
    lock = FileLock(str(repository.lock_path))
    lock.acquire()
    try:
        with pytest.raises(MemoryLockTimeout):
            repository.append_transaction(create_tx)
    finally:
        lock.release()


def test_canonical_line_round_trips_through_models(repository, create_tx):
    line = repository._canonical_transaction_line(create_tx)
    parsed = MemoryTransaction.model_validate(json.loads(line))
    assert parsed == create_tx


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_indexes_support_scope_subject_tag_alias_lookup(repository):
    record = MemoryRecord.model_validate(
        record_data(statement="Shared global fact", tags=("shared.fact",), aliases=("global fact",))
    )
    repository.append_transaction(make_transaction(record))
    assert repository.get(record.id).statement == "Shared global fact"
    assert repository._by_subject[record.subject.casefold()] == {record.id}
    assert repository._by_tag["shared.fact"] == {record.id}
    assert repository._by_alias["global fact"] == {record.id}
    assert repository._by_scope[(ScopeKind.PROJECT, "project:6b5ec7b29e32")] == {record.id}
    assert repository._by_status[MemoryStatus.CANDIDATE] == {record.id}


def test_active_for_conflict_key_tracks_only_active(repository):
    record = MemoryRecord.model_validate(record_data(status="active", revision=1))
    repository.append_transaction(make_transaction(record))
    assert repository.active_for_conflict_key(record.conflict_key) == record
    candidate = MemoryRecord.model_validate(record_data(status="candidate", revision=1, slot="other.slot"))
    repository.append_transaction(make_transaction(candidate))
    assert repository.active_for_conflict_key(candidate.conflict_key) is None


def test_candidate_ids_for_source(repository):
    first = MemoryRecord.model_validate(record_data(statement="one"))
    second = MemoryRecord.model_validate(record_data(statement="two"))
    repository.append_transaction(make_transaction(first, source_batch="history:1-2"))
    repository.append_transaction(make_transaction(second, source_batch="history:1-2"))
    assert repository.candidate_ids_for_source("history:1-2") == frozenset({first.id, second.id})
    assert repository.candidate_ids_for_source("history:3") == frozenset()


def test_candidate_ids_do_not_include_promoted(repository):
    record = MemoryRecord.model_validate(record_data(status="candidate", revision=1))
    repository.append_transaction(make_transaction(record, source_batch="history:1-2"))
    promoted = record.model_copy(update={"revision": 2, "status": MemoryStatus.ACTIVE, "updated_at": dt("2026-08-11T08:32:00Z")})
    repository.append_transaction(make_transaction(promoted, expected_revisions={record.id: 1}, source_batch="history:1-2"))
    assert repository.candidate_ids_for_source("history:1-2") == frozenset()


def test_current_records_filters_by_status(repository, replacement_tx):
    repository.append_transaction(replacement_tx)
    statuses = {r.status for r in repository.current_records(MemoryStatus.ACTIVE)}
    assert statuses == {MemoryStatus.ACTIVE}
    statuses = {r.status for r in repository.current_records(MemoryStatus.SUPERSEDED)}
    assert statuses == {MemoryStatus.SUPERSEDED}


def test_rebuild_repopulates_candidate_by_source(repository, create_tx):
    record = create_tx.operations[0].record
    repository.append_transaction(make_transaction(record, source_batch="history:1-2"))
    rebuilt = StructuredMemoryRepository(repository.workspace)
    assert rebuilt.candidate_ids_for_source("history:1-2") == frozenset({record.id})
