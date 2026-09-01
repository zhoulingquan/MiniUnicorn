"""Repository tests: replay, fail-closed corruption, atomic multi-record transactions."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    DuplicateMemoryIdempotencyKey,
    EvidenceKind,
    EvidenceRef,
    InvalidMemoryTransition,
    MemoryKind,
    MemoryLockTimeout,
    MemoryOperation,
    MemoryRecord,
    MemoryRevisionConflict,
    MemoryScope,
    MemoryStatus,
    MemoryTransaction,
    MemoryWriteError,
    ScopeKind,
    UnknownMemoryTag,
    new_memory_id,
    new_transaction_id,
    normalize_match_text,
    transaction_checksum,
)
from miniunicorn.agent.memory_repository import (
    RepositoryDegradedError,
    StructuredMemoryRepository,
)
from miniunicorn.agent.memory_sqlite_schema import (
    SQL_ORDER_BY_ID,
    SQL_RECALL_SELECT,
    SQL_RECALL_SUFFIX,
    connect_memory_db,
)

UTC = timezone.utc


def dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def make_evidence(
    kind: str = "manual",
    ref: str = "command:msg-1",
    excerpt: str = "evidence excerpt",
    sha256: str | None = None,
):
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
    return make_transaction(
        record,
        expected_revisions={record.id: expected_revision},
        actor=actor,
        reason=reason,
        source_batch=source_batch,
    )


@pytest.fixture
def replacement_tx(repository) -> MemoryTransaction:
    active = MemoryRecord.model_validate(
        record_data(status="active", revision=1, memory_id="mem_" + "a" * 32)
    )
    candidate = MemoryRecord.model_validate(
        record_data(
            statement=active.statement, status="candidate", revision=1, memory_id="mem_" + "b" * 32
        )
    )
    repository.append_transaction(make_transaction(active))
    repository.append_transaction(make_transaction(candidate))
    active_merge = active.model_copy(
        update={
            "revision": 2,
            "evidence": active.evidence
            + (EvidenceRef(kind=EvidenceKind.HISTORY, ref="history:9", excerpt="dup"),),
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


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_multi_record_transaction_survives_rebuild(repository, replacement_tx):
    repository.append_transaction(replacement_tx)
    rebuilt = StructuredMemoryRepository(repository.workspace)
    assert rebuilt.get("mem_" + "a" * 32).status == MemoryStatus.ACTIVE
    assert rebuilt.get("mem_" + "b" * 32).status == MemoryStatus.SUPERSEDED


def test_empty_log_is_healthy(repository):
    assert repository.health.state == "healthy"
    assert repository.current_records() == ()


def test_rebuild_after_commits_keeps_health_and_records(repository, transaction):
    repository.append_transaction(transaction)
    health = repository.rebuild()
    assert health.state == "healthy"
    assert len(repository.current_records()) == 1


def test_single_transaction_survives_rebuild(repository, transaction):
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
    second = MemoryRecord.model_validate(
        record_data(statement="apple", memory_id="mem_" + "b" * 32)
    )
    repository.append_transaction(make_transaction(second))
    repository.append_transaction(make_transaction(first))
    ids = [r.id for r in repository.current_records()]
    assert ids == [first.id, second.id]


# ---------------------------------------------------------------------------
# Rejected input never reaches the log; storage failure is fail-closed
# ---------------------------------------------------------------------------


def test_bad_checksum_is_rejected_and_leaves_no_trace(repository, transaction):
    bad = transaction.model_copy(update={"checksum_sha256": "0" * 64})
    with pytest.raises(MemoryWriteError, match="checksum mismatch"):
        repository.append_transaction(bad)
    assert repository.health.state == "healthy"
    assert repository.transaction_log() == ()
    assert repository.storage_stats().revision_count == 0


def test_corrupted_database_file_degrades_and_disables_writes(repository, transaction):
    repository.database_path.write_bytes(b"garbage, not a sqlite database" * 4)
    health = repository.rebuild()
    assert health.state == "degraded"
    with pytest.raises(RepositoryDegradedError):
        repository.append_transaction(transaction)


def test_unsupported_schema_disables_writes(repository, transaction):
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        connection.execute("PRAGMA user_version = 999")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.error_code == "unsupported_schema_version"
    with pytest.raises(RepositoryDegradedError):
        repository.append_transaction(transaction)


def test_skipped_revision_rejected_atomically(repository):
    tx = make_transaction(MemoryRecord.model_validate(record_data(revision=2)))
    with pytest.raises(MemoryRevisionConflict, match="expected revision 0 and revision 1"):
        repository.append_transaction(tx)
    assert repository.transaction_log() == ()
    assert repository.storage_stats().revision_count == 0


def test_illegal_status_transition_keeps_first_transaction(repository):
    active = MemoryRecord.model_validate(record_data(status="active", revision=1))
    repository.append_transaction(make_transaction(active))
    invalid = MemoryRecord.model_validate(
        record_data(status="candidate", revision=2, memory_id=active.id)
    )
    with pytest.raises(InvalidMemoryTransition):
        repository.append_transaction(make_transaction(invalid, expected_revisions={active.id: 1}))
    assert len(repository.transaction_log(limit=10)) == 1
    assert repository.get(active.id).status == MemoryStatus.ACTIVE


def test_unknown_tag_never_reaches_the_log(repository):
    tx = make_transaction(MemoryRecord.model_validate(record_data(tags=("not.registered",))))
    with pytest.raises(UnknownMemoryTag):
        repository.append_transaction(tx)
    assert repository.transaction_log() == ()
    assert repository.current_records() == ()


# ---------------------------------------------------------------------------
# Append protocol
# ---------------------------------------------------------------------------


def test_append_commit_failure_leaves_no_rows_and_degrades(repository, transaction, monkeypatch):
    import contextlib

    from miniunicorn.agent import memory_repository as repo_module

    real_connect = repo_module.connect_memory_db

    class FailingCommitConnection:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

    @contextlib.contextmanager
    def failing_commit_connect(path, lock_timeout_s):
        with real_connect(path, lock_timeout_s=lock_timeout_s) as connection:
            yield FailingCommitConnection(connection)

    monkeypatch.setattr(repo_module, "connect_memory_db", failing_commit_connect)
    with pytest.raises(MemoryWriteError, match="disk I/O error"):
        repository.append_transaction(transaction)
    assert repository.health.state == "degraded"
    assert repository.health.error_code == "sqlite_operational_error"
    with real_connect(repository.database_path, lock_timeout_s=0.1) as connection:
        transaction_count = connection.execute(
            "SELECT COUNT(*) FROM memory_transactions"
        ).fetchone()[0]
        revision_count = connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
    assert transaction_count == 0
    assert revision_count == 0


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
        repository.append_transaction(make_transaction(changed, expected_revisions={active.id: 1}))


def test_transaction_rejects_two_active_records_for_same_conflict_key(repository):
    first = MemoryRecord.model_validate(record_data(status="active", statement="Use PostgreSQL"))
    second = MemoryRecord.model_validate(record_data(status="active", statement="Use SQLite"))

    with pytest.raises(InvalidMemoryTransition, match="multiple active records"):
        repository.append_transaction(make_transaction(first, second))

    assert repository.current_records() == ()


def test_second_writer_reads_committed_state_before_validation(workspace):
    first = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    stale = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    record = MemoryRecord.model_validate(record_data())
    transaction = make_transaction(record)

    first.append_transaction(transaction)
    with pytest.raises(MemoryRevisionConflict):
        stale.append_transaction(transaction)

    assert stale.health.state == "healthy"
    assert stale.get(record.id) == record
    assert len(first.transaction_log()) == 1


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
    invalid = MemoryRecord.model_validate(
        record_data(status="candidate", revision=2, memory_id=active.id)
    )
    tx = make_transaction(invalid, expected_revisions={active.id: 1})
    with pytest.raises(InvalidMemoryTransition):
        repository.append_transaction(tx)


def test_lock_timeout_raises_memory_lock_timeout(repository, create_tx):
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(MemoryLockTimeout):
            repository.append_transaction(create_tx)


def test_canonical_line_round_trips_through_models(repository, create_tx):
    line = _canonical_line(create_tx)
    parsed = MemoryTransaction.model_validate(json.loads(line))
    assert parsed == create_tx


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_tags_and_aliases_are_written_with_each_revision(repository):
    record = MemoryRecord.model_validate(
        record_data(statement="Shared global fact", tags=("shared.fact",), aliases=("global fact",))
    )
    repository.append_transaction(make_transaction(record))
    assert repository.get(record.id).statement == "Shared global fact"
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        tags = connection.execute(
            "SELECT tag FROM memory_tags WHERE memory_id = ? AND revision = 1",
            (record.id,),
        ).fetchall()
        aliases = connection.execute(
            "SELECT alias_norm FROM memory_aliases WHERE memory_id = ? AND revision = 1",
            (record.id,),
        ).fetchall()
    assert [row["tag"] for row in tags] == ["shared.fact"]
    assert [row["alias_norm"] for row in aliases] == ["global fact"]


def test_active_for_conflict_key_tracks_only_active(repository):
    record = MemoryRecord.model_validate(record_data(status="active", revision=1))
    repository.append_transaction(make_transaction(record))
    assert repository.active_for_conflict_key(record.conflict_key) == record
    candidate = MemoryRecord.model_validate(
        record_data(status="candidate", revision=1, slot="other.slot")
    )
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
    promoted = record.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.ACTIVE,
            "updated_at": dt("2026-08-11T08:32:00Z"),
        }
    )
    repository.append_transaction(
        make_transaction(promoted, expected_revisions={record.id: 1}, source_batch="history:1-2")
    )
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


# ---------------------------------------------------------------------------
# Creation provenance (plan B: immutable creation mapping + cumulative batches)
# ---------------------------------------------------------------------------


def test_creation_batch_survives_later_revision_and_rebuild(repository):
    created = MemoryRecord.model_validate(record_data())
    repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))
    promoted = created.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.ACTIVE,
            "updated_at": dt("2026-08-11T08:32:00Z"),
        }
    )
    repository.append_transaction(
        make_transaction(promoted, expected_revisions={created.id: 1}, source_batch="")
    )

    assert repository.record_created_for("dream:batch-a", created.content_hash).id == created.id
    assert repository.record_ids_for_source("dream:batch-a") == frozenset({created.id})

    rebuilt = StructuredMemoryRepository(repository.workspace)
    assert rebuilt.record_created_for("dream:batch-a", created.content_hash).id == created.id
    assert rebuilt.record_ids_for_source("dream:batch-a") == frozenset({created.id})


def test_record_source_batches_are_cumulative(repository):
    created = MemoryRecord.model_validate(record_data())
    repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))
    revised = created.model_copy(
        update={
            "revision": 2,
            "blocked_by": ("mem_" + "a" * 32,),
            "updated_at": dt("2026-08-11T08:32:00Z"),
        }
    )
    repository.append_transaction(
        make_transaction(revised, expected_revisions={created.id: 1}, source_batch="dream:batch-b")
    )
    assert repository.record_ids_for_source("dream:batch-a") == frozenset({created.id})
    assert repository.record_ids_for_source("dream:batch-b") == frozenset({created.id})


def test_duplicate_creation_key_rejected_and_logged_once(repository):
    first = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(
        record_data(memory_id="mem_" + "b" * 32, content_hash=first.content_hash)
    )
    repository.append_transaction(make_transaction(first, source_batch="dream:stable-batch"))
    with pytest.raises(DuplicateMemoryIdempotencyKey, match="duplicate idempotency key"):
        repository.append_transaction(make_transaction(second, source_batch="dream:stable-batch"))
    assert len(repository.transaction_log(limit=10)) == 1
    assert repository.current_records() == (first,)


# ---------------------------------------------------------------------------
# Atomic conditional creation (plan B: one SQLite transaction, no external lookup)
# ---------------------------------------------------------------------------


def test_append_create_if_absent_deduplicates_stale_repository_instances(workspace):
    first_repo = StructuredMemoryRepository(workspace)
    second_repo = StructuredMemoryRepository(workspace)
    first = MemoryRecord.model_validate(record_data(memory_id=new_memory_id()))
    second = MemoryRecord.model_validate(
        record_data(memory_id=new_memory_id(), content_hash=first.content_hash)
    )

    first_current, first_created = first_repo.append_create_if_absent(
        make_transaction(first, source_batch="dream:stable-batch")
    )
    second_current, second_created = second_repo.append_create_if_absent(
        make_transaction(second, source_batch="dream:stable-batch")
    )

    assert first_created is True
    assert second_created is False
    assert second_current.id == first_current.id
    assert len(StructuredMemoryRepository(workspace).current_records()) == 1


def test_append_create_if_absent_rejects_contract_violations(repository):
    revision_two = MemoryRecord.model_validate(record_data(revision=2))
    nonzero_expected_record = MemoryRecord.model_validate(record_data(statement="expected-1"))
    cases = [
        make_transaction(MemoryRecord.model_validate(record_data(statement="empty-batch"))),
        make_transaction(
            MemoryRecord.model_validate(record_data(statement="one")),
            MemoryRecord.model_validate(record_data(statement="two")),
            source_batch="dream:stable-batch",
        ),
        make_transaction(revision_two, source_batch="dream:stable-batch"),
        make_transaction(
            nonzero_expected_record,
            source_batch="dream:stable-batch",
            expected_revisions={nonzero_expected_record.id: 1},
        ),
    ]
    for tx in cases:
        with pytest.raises(MemoryWriteError):
            repository.append_create_if_absent(tx)
    assert repository.storage_stats().transaction_count == 0
    assert repository.current_records() == ()


def _worker_create_if_absent(
    workspace: str, source_batch: str, memory_id: str, start, queue
) -> None:
    repo = StructuredMemoryRepository(Path(workspace), lock_timeout_s=15.0)
    record = MemoryRecord.model_validate(record_data(memory_id=memory_id))
    start.wait()
    current, created = repo.append_create_if_absent(
        make_transaction(record, source_batch=source_batch)
    )
    queue.put({"id": current.id, "created": created, "exit": 0})


def test_append_create_if_absent_real_multiprocess(workspace):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    first_id = "mem_" + "c" * 32
    second_id = "mem_" + "d" * 32
    procs = [
        ctx.Process(
            target=_worker_create_if_absent,
            args=(str(workspace), "dream:stable-batch", first_id, start, queue),
        ),
        ctx.Process(
            target=_worker_create_if_absent,
            args=(str(workspace), "dream:stable-batch", second_id, start, queue),
        ),
    ]
    try:
        for proc in procs:
            proc.start()
        start.set()
        for proc in procs:
            proc.join(timeout=10)
        for proc in procs:
            assert proc.exitcode == 0
        results = [queue.get(timeout=10) for _ in range(2)]
        assert [result["created"] for result in results].count(True) == 1
        ids = {result["id"] for result in results}
        assert len(ids) == 1
        assert len(StructuredMemoryRepository(workspace).current_records()) == 1
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)


class TestBlockedByMonotonicity:
    def test_candidate_revision_cannot_clear_blocked_by(self, repository):
        created = MemoryRecord.model_validate(record_data(blocked_by=("mem_" + "a" * 32,)))
        repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))

        cleared = created.model_copy(
            update={
                "revision": 2,
                "blocked_by": (),
                "updated_at": dt("2026-08-11T08:32:00Z"),
            }
        )
        with pytest.raises(InvalidMemoryTransition, match="blocked_by"):
            repository.append_transaction(
                make_transaction(cleared, expected_revisions={created.id: 1}, source_batch="")
            )
        assert len(repository.revisions(created.id)) == 1

    def test_candidate_revision_cannot_replace_blocked_by(self, repository):
        created = MemoryRecord.model_validate(record_data(blocked_by=("mem_" + "a" * 32,)))
        repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))

        replaced = created.model_copy(
            update={
                "revision": 2,
                "blocked_by": ("mem_" + "b" * 32,),
                "updated_at": dt("2026-08-11T08:32:00Z"),
            }
        )
        with pytest.raises(InvalidMemoryTransition, match="blocked_by"):
            repository.append_transaction(
                make_transaction(replaced, expected_revisions={created.id: 1}, source_batch="")
            )
        assert len(repository.revisions(created.id)) == 1

    def test_candidate_revision_may_add_blocked_by(self, repository):
        created = MemoryRecord.model_validate(record_data())
        repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))

        extended = created.model_copy(
            update={
                "revision": 2,
                "blocked_by": ("mem_" + "a" * 32, "mem_" + "b" * 32),
                "updated_at": dt("2026-08-11T08:32:00Z"),
            }
        )
        repository.append_transaction(
            make_transaction(extended, expected_revisions={created.id: 1}, source_batch="")
        )

        current = repository.current_records()[0]
        assert set(current.blocked_by) == {"mem_" + "a" * 32, "mem_" + "b" * 32}


# ---------------------------------------------------------------------------
# SQLite read path
# ---------------------------------------------------------------------------


def _seed_transaction(
    connection, transaction: MemoryTransaction, *, is_current: bool = True
) -> int:
    """Insert one transaction and its revisions directly via SQL (test seeding)."""
    payload = transaction.model_dump(mode="json")
    cursor = connection.execute(
        "INSERT INTO memory_transactions "
        "(tx_id, recorded_at, actor, reason, source_batch, checksum_sha256, transaction_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            payload["tx_id"],
            payload["recorded_at"],
            payload["actor"],
            payload["reason"],
            payload["source_batch"],
            payload["checksum_sha256"],
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        ),
    )
    tx_seq = cursor.lastrowid
    for op_index, operation in enumerate(transaction.operations):
        record = operation.record
        record_payload = record.model_dump(mode="json")
        connection.execute(
            "INSERT INTO memory_revisions "
            "(memory_id, revision, tx_seq, op_index, is_current, status, kind, scope_kind, "
            "scope_key, subject_norm, conflict_key, content_hash, source_level, importance, "
            "updated_at, expires_at, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.revision,
                tx_seq,
                op_index,
                1 if is_current else 0,
                record.status.value,
                record.kind.value,
                record.scope.kind.value,
                record.scope.key,
                normalize_match_text(record.subject),
                record.conflict_key,
                record.content_hash,
                record.source_level.value,
                record.importance,
                record_payload["updated_at"],
                record_payload["expires_at"],
                json.dumps(
                    record_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            ),
        )
        if transaction.source_batch:
            connection.execute(
                "INSERT OR IGNORE INTO memory_source_batches "
                "(memory_id, source_batch, first_tx_seq) VALUES (?, ?, ?)",
                (record.id, transaction.source_batch, tx_seq),
            )
            if record.revision == 1:
                connection.execute(
                    "INSERT INTO memory_creation_keys "
                    "(source_batch, content_hash, memory_id, created_tx_seq) VALUES (?, ?, ?, ?)",
                    (transaction.source_batch, record.content_hash, record.id, tx_seq),
                )
    return tx_seq


def test_repository_uses_canonical_sqlite_path(repository) -> None:
    assert repository.database_path == (
        repository.workspace / "memory" / "structured" / "memory.db"
    )
    assert repository.database_path.exists()
    assert repository.health.backend == "sqlite"
    assert repository.health.state == "healthy"


def test_reads_from_sqlite_return_validated_records(repository) -> None:
    record = MemoryRecord.model_validate(
        record_data(status="candidate", revision=1, memory_id="mem_" + "a" * 32)
    )
    transaction = make_transaction(record, source_batch="history:12-1")
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, transaction)

    assert isinstance(repository.get(record.id), MemoryRecord)
    assert repository.get(record.id) == record
    assert repository.get_current(record.id) == record
    assert repository.get_current(record.id, synchronize=False) == record
    assert repository.revisions(record.id) == (record,)
    assert repository.current_records() == (record,)
    assert repository.current_records(MemoryStatus.CANDIDATE) == (record,)
    assert repository.current_records(MemoryStatus.ACTIVE) == ()
    assert repository.active_for_conflict_key(record.conflict_key) is None
    assert repository.active_for_conflict_key("no-such-key") is None
    assert repository.candidate_records() == (record,)
    assert repository.candidate_ids_for_source("history:12-1") == frozenset({record.id})
    assert repository.candidate_ids_for_source("history:999") == frozenset()
    assert repository.record_created_for("history:12-1", record.content_hash) == record
    assert repository.record_ids_for_source("history:12-1") == frozenset({record.id})
    assert repository.get("mem_" + "f" * 32) is None


def test_reads_current_records_sorted_by_memory_id(repository) -> None:
    first = MemoryRecord.model_validate(record_data(statement="zebra", memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(
        record_data(statement="apple", memory_id="mem_" + "b" * 32)
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(second))
        _seed_transaction(connection, make_transaction(first))
    assert [r.id for r in repository.current_records()] == [first.id, second.id]


def test_reads_revisions_ascending_by_revision(repository) -> None:
    original = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    revised = original.model_copy(update={"revision": 2, "updated_at": dt("2026-08-11T08:32:00Z")})
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(original), is_current=False)
        _seed_transaction(connection, make_transaction(revised))
    assert [r.revision for r in repository.revisions(original.id)] == [1, 2]
    assert repository.get(original.id) == revised
    assert repository.revisions("mem_" + "f" * 32) == ()


def test_reads_candidate_source_queries(repository) -> None:
    first = MemoryRecord.model_validate(
        record_data(status="candidate", memory_id="mem_" + "a" * 32)
    )
    second = MemoryRecord.model_validate(
        record_data(status="candidate", statement="two", memory_id="mem_" + "b" * 32)
    )
    active = MemoryRecord.model_validate(
        record_data(
            status="active", statement="three", slot="other.slot", memory_id="mem_" + "c" * 32
        )
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(first, source_batch="history:1-2"))
        _seed_transaction(connection, make_transaction(second, source_batch="history:1-2"))
        _seed_transaction(connection, make_transaction(active, source_batch="history:3"))
    assert repository.candidate_records() == (first, second)
    assert repository.candidate_ids_for_source("history:1-2") == frozenset({first.id, second.id})
    assert repository.candidate_ids_for_source("history:3") == frozenset()
    assert repository.active_for_conflict_key(first.conflict_key) is None
    assert repository.record_ids_for_source("history:3") == frozenset({active.id})
    assert repository.record_ids_for_source("history:999") == frozenset()


def test_reads_source_batches_cumulative(repository) -> None:
    created = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    revised = created.model_copy(update={"revision": 2, "updated_at": dt("2026-08-11T08:32:00Z")})
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(
            connection, make_transaction(created, source_batch="dream:batch-a"), is_current=False
        )
        _seed_transaction(connection, make_transaction(revised, source_batch="dream:batch-b"))
    assert repository.record_created_for("dream:batch-a", created.content_hash) == revised
    assert repository.record_ids_for_source("dream:batch-a") == frozenset({created.id})
    assert repository.record_ids_for_source("dream:batch-b") == frozenset({created.id})


def test_reads_transaction_log_recent_first(repository) -> None:
    first = MemoryRecord.model_validate(record_data(statement="first", memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(
        record_data(statement="second", memory_id="mem_" + "b" * 32)
    )
    first_tx = make_transaction(first)
    second_tx = make_transaction(second)
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, first_tx)
        _seed_transaction(connection, second_tx)

    entries = repository.transaction_log()
    assert [t.tx_id for t in entries] == [second_tx.tx_id, first_tx.tx_id]
    assert all(isinstance(t, MemoryTransaction) for t in entries)
    assert [t.tx_id for t in repository.transaction_log(limit=1)] == [second_tx.tx_id]
    assert [t.tx_id for t in repository.transaction_log(tx_id=first_tx.tx_id)] == [first_tx.tx_id]
    assert repository.transaction_log(tx_id="mtx_" + "0" * 32) == ()


def test_reads_storage_stats_reflects_seeded_database(repository) -> None:
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(record))
    stats = repository.storage_stats()
    assert stats.backend == "sqlite"
    assert stats.schema_version == SCHEMA_VERSION
    assert stats.transaction_count == 1
    assert stats.revision_count == 1
    assert stats.current_count == 1
    assert stats.last_transaction_seq == 1
    assert stats.audit_exported_seq == 0
    assert stats.database_bytes > 0


def test_reads_storage_stats_empty_database(repository) -> None:
    stats = repository.storage_stats()
    assert stats.transaction_count == 0
    assert stats.revision_count == 0
    assert stats.current_count == 0
    assert stats.last_transaction_seq == 0
    assert stats.audit_exported_seq == 0


def test_reads_storage_stats_reads_audit_exported_seq(repository) -> None:
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        connection.execute(
            "INSERT INTO storage_meta(key, value) VALUES ('audit_exported_seq', '42')"
        )
    assert repository.storage_stats().audit_exported_seq == 42


def test_reads_recall_candidates_filters_scope_kind_expiry(repository) -> None:
    active = MemoryRecord.model_validate(record_data(status="active", memory_id="mem_" + "a" * 32))
    decision = MemoryRecord.model_validate(
        record_data(
            status="active", kind="decision", statement="use sqlite", memory_id="mem_" + "d" * 32
        )
    )
    candidate = MemoryRecord.model_validate(
        record_data(status="candidate", statement="different", memory_id="mem_" + "b" * 32)
    )
    expired = MemoryRecord.model_validate(
        record_data(
            status="active",
            statement="expired fact",
            slot="expired.slot",
            expires_at="2026-08-01T00:00:00Z",
            memory_id="mem_" + "c" * 32,
        )
    )
    other_scope = MemoryRecord.model_validate(
        record_data(
            status="active",
            statement="session thing",
            scope={"kind": "session", "key": "session:abc"},
            memory_id="mem_" + "e" * 32,
        )
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(active))
        _seed_transaction(connection, make_transaction(decision))
        _seed_transaction(connection, make_transaction(candidate))
        _seed_transaction(connection, make_transaction(expired))
        _seed_transaction(connection, make_transaction(other_scope))

    now = dt("2026-08-11T09:00:00Z")
    scope = active.scope
    hits = repository.recall_candidates(allowed_scopes=(scope,), requested_kinds=(), now=now)
    assert hits == (active, decision)
    assert repository.recall_candidates(
        allowed_scopes=(scope,), requested_kinds=(MemoryKind.DECISION,), now=now
    ) == (decision,)
    assert repository.recall_candidates(
        allowed_scopes=(other_scope.scope,), requested_kinds=(), now=now
    ) == (other_scope,)


def test_reads_recall_candidates_boundary_excludes_expiring_at_now(repository) -> None:
    expiring_now = MemoryRecord.model_validate(
        record_data(
            status="active",
            statement="expires right now",
            expires_at="2026-08-11T09:00:00Z",
            memory_id="mem_" + "f" * 32,
        )
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(expiring_now))

    now = dt("2026-08-11T09:00:00Z")
    assert (
        repository.recall_candidates(
            allowed_scopes=(expiring_now.scope,), requested_kinds=(), now=now
        )
        == ()
    )


def test_reads_recall_candidates_boundary_includes_expiring_after_now(repository) -> None:
    expiring_after = MemoryRecord.model_validate(
        record_data(
            status="active",
            statement="expires one second from now",
            expires_at="2026-08-11T09:00:01Z",
            memory_id="mem_" + "e" * 32,
        )
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(expiring_after))

    now = dt("2026-08-11T09:00:00Z")
    assert repository.recall_candidates(
        allowed_scopes=(expiring_after.scope,), requested_kinds=(), now=now
    ) == (expiring_after,)


def test_reads_recall_candidates_requires_scopes(repository) -> None:
    now = dt("2026-08-11T09:00:00Z")
    assert repository.recall_candidates(allowed_scopes=(), requested_kinds=(), now=now) == ()


def test_reads_health_degrades_on_schema_mismatch(repository) -> None:
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        connection.execute("PRAGMA user_version = 999")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.error_code == "unsupported_schema_version"
    with pytest.raises(RepositoryDegradedError):
        repository.get("mem_" + "a" * 32)
    with pytest.raises(RepositoryDegradedError):
        repository.current_records()


# ---------------------------------------------------------------------------
# SQLite atomic write path (single-transaction atomicity)
# ---------------------------------------------------------------------------


def test_atomic_create_writes_one_transaction_and_revision(repository, create_tx):
    repository.append_transaction(create_tx)
    record = create_tx.operations[0].record
    assert repository.storage_stats().transaction_count == 1
    assert repository.storage_stats().revision_count == 1
    assert [t.tx_id for t in repository.transaction_log()] == [create_tx.tx_id]
    assert repository.get(record.id) == record


def test_atomic_revision_update_preserves_history(repository, create_tx):
    record = create_tx.operations[0].record
    repository.append_transaction(create_tx)
    repository.append_transaction(update_transaction(record, 1))
    assert repository.storage_stats().transaction_count == 2
    assert repository.storage_stats().revision_count == 2
    assert repository.get(record.id).revision == 2
    assert [r.revision for r in repository.revisions(record.id)] == [1, 2]
    assert len(repository.transaction_log(limit=10)) == 2


def test_atomic_multi_operation_replacement_in_one_transaction(repository):
    active = MemoryRecord.model_validate(
        record_data(status="active", revision=1, memory_id="mem_" + "a" * 32)
    )
    candidate = MemoryRecord.model_validate(
        record_data(status="candidate", revision=1, memory_id="mem_" + "b" * 32)
    )
    repository.append_transaction(make_transaction(active))
    repository.append_transaction(make_transaction(candidate))
    active_merge = active.model_copy(
        update={"revision": 2, "updated_at": dt("2026-08-11T08:33:00Z")}
    )
    candidate_superseded = candidate.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.SUPERSEDED,
            "replacement_id": active.id,
            "updated_at": dt("2026-08-11T08:33:00Z"),
        }
    )
    repository.append_transaction(
        make_transaction(
            active_merge,
            candidate_superseded,
            expected_revisions={active.id: 1, candidate.id: 1},
            reason="dedupe identical content",
        )
    )
    assert repository.storage_stats().transaction_count == 3
    assert repository.storage_stats().revision_count == 4
    assert repository.get(active.id) == active_merge
    assert repository.get(candidate.id) == candidate_superseded
    assert len(repository.transaction_log(limit=10)) == 3


def test_atomic_append_rejects_bad_checksum_without_rows(repository, create_tx):
    bad = create_tx.model_copy(update={"checksum_sha256": "0" * 64})
    with pytest.raises(MemoryWriteError, match="checksum mismatch"):
        repository.append_transaction(bad)
    assert repository.storage_stats().transaction_count == 0
    assert repository.storage_stats().revision_count == 0


def test_atomic_append_rejects_wrong_expected_revision(repository, create_tx):
    record = create_tx.operations[0].record
    repository.append_transaction(create_tx)
    tx = update_transaction(record, expected_revision=7)
    with pytest.raises(MemoryRevisionConflict):
        repository.append_transaction(tx)
    assert repository.storage_stats().transaction_count == 1
    assert repository.get(record.id).revision == 1
    assert len(repository.transaction_log(limit=10)) == 1


def test_atomic_append_rejects_illegal_status(repository):
    active = MemoryRecord.model_validate(record_data(status="active", revision=1))
    repository.append_transaction(make_transaction(active))
    invalid = MemoryRecord.model_validate(
        record_data(status="candidate", revision=2, memory_id=active.id)
    )
    with pytest.raises(InvalidMemoryTransition):
        repository.append_transaction(make_transaction(invalid, expected_revisions={active.id: 1}))
    assert repository.storage_stats().transaction_count == 1
    assert repository.get(active.id).status == MemoryStatus.ACTIVE


def test_atomic_append_rejects_unknown_tag(repository):
    tx = make_transaction(MemoryRecord.model_validate(record_data(tags=("not.registered",))))
    with pytest.raises(UnknownMemoryTag):
        repository.append_transaction(tx)
    assert repository.storage_stats().transaction_count == 0
    assert repository.current_records() == ()


def test_atomic_append_rejects_duplicate_transaction_id(repository, create_tx):
    repository.append_transaction(create_tx)
    record = create_tx.operations[0].record
    rerun = update_transaction(record, expected_revision=1, reason="rerun")
    rerun = rerun.model_copy(update={"tx_id": create_tx.tx_id})
    rerun = rerun.model_copy(update={"checksum_sha256": transaction_checksum(rerun)})
    with pytest.raises(DuplicateMemoryIdempotencyKey, match="duplicate transaction id"):
        repository.append_transaction(rerun)
    assert repository.storage_stats().transaction_count == 1
    assert len(repository.transaction_log(limit=10)) == 1


def test_atomic_multi_operation_second_failure_rolls_back_first_op(repository, monkeypatch):
    first = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(record_data(status="active", memory_id="mem_" + "b" * 32))
    tx = make_transaction(first, second)
    real_insert = repository._insert_revision

    def fail_second_op(connection, tx_seq, op_index, transaction, record):
        if op_index == 1:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: memory_revisions.memory_id")
        return real_insert(connection, tx_seq, op_index, transaction, record)

    monkeypatch.setattr(repository, "_insert_revision", fail_second_op)
    with pytest.raises(MemoryWriteError):
        repository.append_transaction(tx)
    assert repository.storage_stats().transaction_count == 0
    assert repository.storage_stats().revision_count == 0
    assert repository.transaction_log() == ()


# ---------------------------------------------------------------------------
# SQLite database constraints
# ---------------------------------------------------------------------------


def test_db_unique_index_rejects_second_current_active_for_same_conflict_key(repository):
    first = MemoryRecord.model_validate(
        record_data(status="active", statement="Use PostgreSQL", memory_id="mem_" + "a" * 32)
    )
    second = MemoryRecord.model_validate(
        record_data(status="active", statement="Use SQLite", memory_id="mem_" + "b" * 32)
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(first))
        with pytest.raises(sqlite3.IntegrityError):
            _seed_transaction(connection, make_transaction(second))
    assert len(repository.current_records(MemoryStatus.ACTIVE)) == 1
    assert repository.active_for_conflict_key(first.conflict_key) == first


def test_db_creation_key_unique_accepts_exactly_one_mapping(repository):
    first = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(
        record_data(memory_id="mem_" + "b" * 32, content_hash=first.content_hash)
    )
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        _seed_transaction(connection, make_transaction(first, source_batch="dream:stable-batch"))
        with pytest.raises(sqlite3.IntegrityError):
            _seed_transaction(
                connection, make_transaction(second, source_batch="dream:stable-batch")
            )
    assert repository.record_created_for("dream:stable-batch", first.content_hash) == first


def test_same_creation_key_returns_same_id(repository):
    first = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    second = MemoryRecord.model_validate(
        record_data(memory_id="mem_" + "b" * 32, content_hash=first.content_hash)
    )
    first_current, first_created = repository.append_create_if_absent(
        make_transaction(first, source_batch="dream:stable-batch")
    )
    second_current, second_created = repository.append_create_if_absent(
        make_transaction(second, source_batch="dream:stable-batch")
    )
    assert first_created is True
    assert second_created is False
    assert second_current.id == first_current.id
    assert len(repository.transaction_log(limit=10)) == 1


# ---------------------------------------------------------------------------
# Four-process concurrency (spawn on Windows)
# ---------------------------------------------------------------------------


def _worker_create_distinct(
    workspace: str, source_batch: str, worker_index: int, count: int, start, queue
) -> None:
    repo = StructuredMemoryRepository(Path(workspace), lock_timeout_s=15.0)
    start.wait()
    created_ids: list[str] = []
    for i in range(count):
        record = MemoryRecord.model_validate(
            record_data(
                statement=f"parallel statement {worker_index}-{i}", memory_id=new_memory_id()
            )
        )
        current, created = repo.append_create_if_absent(
            make_transaction(record, source_batch=f"{source_batch}:{worker_index}-{i}")
        )
        assert created
        created_ids.append(current.id)
    queue.put({"created_ids": created_ids, "exit": 0})


def _worker_create_same_key(
    workspace: str, source_batch: str, memory_id: str, count: int, start, queue
) -> None:
    repo = StructuredMemoryRepository(Path(workspace), lock_timeout_s=15.0)
    record = MemoryRecord.model_validate(record_data(memory_id=memory_id))
    start.wait()
    outcomes: list[tuple[str, bool]] = []
    for _ in range(count):
        current, created = repo.append_create_if_absent(
            make_transaction(record, source_batch=source_batch)
        )
        outcomes.append((current.id, created))
    queue.put({"outcomes": outcomes, "exit": 0})


def _worker_update_expected(workspace: str, memory_id: str, start, queue) -> None:
    repo = StructuredMemoryRepository(Path(workspace), lock_timeout_s=15.0)
    start.wait()
    current = repo.get(memory_id)
    revised = current.model_copy(update={"revision": 2, "updated_at": dt("2026-08-11T08:32:00Z")})
    try:
        repo.append_transaction(make_transaction(revised, expected_revisions={memory_id: 1}))
        queue.put({"outcome": "won"})
    except MemoryRevisionConflict:
        queue.put({"outcome": "lost"})


def _worker_activate_conflict(workspace: str, slot: str, memory_id: str, start, queue) -> None:
    repo = StructuredMemoryRepository(Path(workspace), lock_timeout_s=15.0)
    record = MemoryRecord.model_validate(
        record_data(status="active", slot=slot, memory_id=memory_id)
    )
    start.wait()
    try:
        repo.append_transaction(make_transaction(record))
        queue.put({"outcome": "won"})
    except InvalidMemoryTransition:
        queue.put({"outcome": "rejected"})


def _run_workers(target, args_list, *, timeout: float = 120.0) -> list:
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    procs = [ctx.Process(target=target, args=args + (start, queue)) for args in args_list]
    try:
        for proc in procs:
            proc.start()
        start.set()
        for proc in procs:
            proc.join(timeout=timeout)
        for proc in procs:
            assert proc.exitcode == 0, f"worker {proc.pid} exited with {proc.exitcode}"
        return [queue.get(timeout=30) for _ in procs]
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)


def test_four_processes_100_distinct_creates_all_exist(workspace):
    StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    results = _run_workers(
        _worker_create_distinct, [(str(workspace), "dream:parallel", i, 25) for i in range(4)]
    )
    ids = [record_id for result in results for record_id in result["created_ids"]]
    assert len(ids) == 100
    assert len(set(ids)) == 100
    repo = StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    assert len(repo.current_records()) == 100
    assert len(repo.transaction_log(limit=200)) == 100


def test_four_processes_20_same_key_creates_one_memory_id(workspace):
    StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    memory_id = "mem_" + "c" * 32
    results = _run_workers(
        _worker_create_same_key,
        [(str(workspace), "dream:contended", memory_id, 5) for _ in range(4)],
    )
    outcomes = [item for result in results for item in result["outcomes"]]
    assert len(outcomes) == 20
    assert sum(created for _, created in outcomes) == 1
    assert {record_id for record_id, _ in outcomes} == {memory_id}
    repo = StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    assert len(repo.current_records()) == 1
    assert repo.current_records()[0].id == memory_id


def test_two_processes_racing_one_expected_revision_only_one_wins(workspace):
    repo = StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "d" * 32))
    repo.append_transaction(make_transaction(record))
    results = _run_workers(
        _worker_update_expected, [(str(workspace), record.id), (str(workspace), record.id)]
    )
    assert sorted(result["outcome"] for result in results) == ["lost", "won"]
    fresh = StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    assert fresh.get(record.id).revision == 2
    assert len(fresh.revisions(record.id)) == 2
    assert len(fresh.transaction_log(limit=10)) == 2


def test_two_processes_cannot_commit_two_current_actives(workspace):
    StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    results = _run_workers(
        _worker_activate_conflict,
        [
            (str(workspace), "db.primary", "mem_" + "e" * 32),
            (str(workspace), "db.primary", "mem_" + "f" * 32),
        ],
    )
    assert sorted(result["outcome"] for result in results) == ["rejected", "won"]
    fresh = StructuredMemoryRepository(workspace, lock_timeout_s=15.0)
    assert len(fresh.current_records(MemoryStatus.ACTIVE)) == 1
    with connect_memory_db(fresh.database_path, lock_timeout_s=15.0) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_revisions WHERE is_current = 1 AND status = 'active'"
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Startup decision matrix (design section 11: memory.db / journal / manifest)
# ---------------------------------------------------------------------------


def _canonical_line(transaction: MemoryTransaction) -> str:
    return json.dumps(
        transaction.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _write_journal(workspace: Path, line: str) -> Path:
    journal = workspace / "memory" / "structured" / "journal.jsonl"
    journal.write_text(line + "\n", encoding="utf-8")
    return journal


def test_startup_no_database_no_journal_creates_fresh_sqlite(workspace):
    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    assert repository.database_path.exists()
    assert repository.health.state == "healthy"
    assert repository.storage_stats().transaction_count == 0


def test_startup_no_database_empty_journal_creates_fresh_sqlite(workspace):
    (workspace / "memory" / "structured" / "journal.jsonl").write_text("", encoding="utf-8")
    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    assert repository.database_path.exists()
    assert repository.health.state == "healthy"
    assert repository.storage_stats().transaction_count == 0


def test_startup_migrates_non_empty_journal(workspace):
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    transactions = [make_transaction(record, source_batch="history:1-2")]
    journal = _write_journal(workspace, _canonical_line(transactions[0]))
    original_bytes = journal.read_bytes()

    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)

    assert repository.health.state == "healthy"
    assert repository.get(record.id) == record
    assert journal.read_bytes() == original_bytes
    assert not list((workspace / "memory" / "structured").glob("memory.db.importing*"))
    manifest = workspace / "memory" / "structured" / "storage-migration-v2.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "completed"


def test_startup_existing_database_never_reads_journal(workspace, monkeypatch):
    first = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    first.append_transaction(make_transaction(record))
    _write_journal(workspace, json.dumps("not-a-transaction"))

    from miniunicorn.agent import memory_jsonl_import as import_module
    from miniunicorn.agent import memory_repository as repo_module

    class ExplodingJournalPath(Path):
        def open(self, *args, **kwargs):
            if self.name == "journal.jsonl":
                raise RuntimeError("journal must never be read")
            return super().open(*args, **kwargs)

    monkeypatch.setattr(repo_module, "Path", ExplodingJournalPath)
    monkeypatch.setattr(import_module, "Path", ExplodingJournalPath)

    reopened = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    assert reopened.health.state == "healthy"
    assert reopened.get(record.id) == record


def test_startup_existing_database_ignores_completed_manifest(workspace):
    first = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    first.append_transaction(make_transaction(record))
    _write_journal(workspace, json.dumps("not-a-transaction"))
    manifest = workspace / "memory" / "structured" / "storage-migration-v2.json"
    manifest.write_text(json.dumps({"status": "completed"}), encoding="utf-8")

    reopened = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)

    assert reopened.health.state == "healthy"
    assert reopened.get(record.id) == record
    assert reopened.storage_stats().transaction_count == 1


def test_startup_completed_manifest_without_database_degrades(workspace):
    manifest = workspace / "memory" / "structured" / "storage-migration-v2.json"
    manifest.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    _write_journal(
        workspace, _canonical_line(make_transaction(MemoryRecord.model_validate(record_data())))
    )

    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)

    assert repository.health.state == "degraded"
    assert repository.health.error_code == "migration_database_lost"
    assert not repository.database_path.exists()
    with pytest.raises(RepositoryDegradedError):
        repository.current_records()


def test_startup_failed_migration_degrades_without_creating_database(workspace):
    _write_journal(workspace, '{"broken": [')

    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)

    assert repository.health.state == "degraded"
    assert repository.health.error_code == "invalid_transaction"
    assert not repository.database_path.exists()
    with pytest.raises(RepositoryDegradedError):
        repository.get("mem_" + "a" * 32)


def test_startup_removes_importing_residue_then_migrates(workspace):
    residue = workspace / "memory" / "structured" / "memory.db.importing-stale-1234"
    residue.write_bytes(b"garbage residue")
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    _write_journal(workspace, _canonical_line(make_transaction(record, source_batch="history:1-2")))

    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)

    assert not residue.exists()
    assert not list((workspace / "memory" / "structured").glob("memory.db.importing*"))
    assert repository.health.state == "healthy"
    assert repository.get(record.id) == record


# ---------------------------------------------------------------------------
# Structural scale and recovery benchmarks (task 11)
# ---------------------------------------------------------------------------


def test_startup_existing_database_journal_reader_raises_still_succeeds(workspace, monkeypatch):
    first = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    record = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    first.append_transaction(make_transaction(record))

    from miniunicorn.agent import memory_jsonl_import as import_module

    def exploding_journal_reader(path):
        raise RuntimeError("legacy journal reader must never run for an existing database")

    monkeypatch.setattr(import_module, "iter_legacy_transactions", exploding_journal_reader)

    reopened = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)

    assert reopened.health.state == "healthy"
    assert reopened.get(record.id) == record


def _seed_repository_history(workspace: Path, count: int) -> StructuredMemoryRepository:
    """Seed *count* committed transactions directly via SQL (fast, no per-tx fsync)."""
    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    with connect_memory_db(repository.database_path, lock_timeout_s=0.1) as connection:
        connection.execute("BEGIN")
        try:
            for index in range(count):
                record = MemoryRecord.model_validate(
                    record_data(
                        memory_id=f"mem_{index:032x}",
                        slot=f"db.primary.{index}",
                        statement=f"history statement {index}",
                    )
                )
                _seed_transaction(
                    connection,
                    make_transaction(record, source_batch=f"history:seed-{index}"),
                )
        finally:
            connection.commit()
    return repository


def test_append_sql_statement_count_bounded_across_history_scale(workspace, tmp_path, monkeypatch):
    from miniunicorn.agent import memory_repository as repo_module

    large_workspace = tmp_path / "large-workspace"
    structured = large_workspace / "memory" / "structured"
    structured.mkdir(parents=True)
    bundled = Path(__file__).parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    appended = MemoryRecord.model_validate(record_data(memory_id="mem_" + "e" * 32))

    def statements_for(workspace: Path, history: int) -> list[str]:
        repository = _seed_repository_history(workspace, history)
        statements: list[str] = []
        real_connect = repo_module.connect_memory_db

        @contextmanager
        def traced_connect(path, lock_timeout_s):
            with real_connect(path, lock_timeout_s=lock_timeout_s) as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with monkeypatch.context() as patched:
            patched.setattr(repo_module, "connect_memory_db", traced_connect)
            repository.append_transaction(
                make_transaction(appended, source_batch=f"history:append-{history}")
            )
        return statements

    at_100 = statements_for(workspace, 100)
    at_10_000 = statements_for(large_workspace, 10_000)

    assert len(at_100) == len(at_10_000)
    assert len(at_10_000) <= 40
    for statements in (at_100, at_10_000):
        assert not any(
            statement.lstrip().upper().startswith("SELECT") and "WHERE" not in statement.upper()
            for statement in statements
        )


def test_repository_holds_no_full_python_record_index(repository, create_tx):
    for index in range(20):
        record = MemoryRecord.model_validate(
            record_data(memory_id=f"mem_{index:032x}", slot=f"db.primary.{index}")
        )
        repository.append_transaction(make_transaction(record))
    repository.rebuild()

    assert not hasattr(repository, "_revision_history")
    assert not hasattr(repository, "_records")
    assert not hasattr(repository, "_current_records")
    record_holders = [
        key
        for key, value in vars(repository).items()
        if isinstance(value, dict)
        and any(isinstance(entry, (MemoryRecord, MemoryTransaction)) for entry in value.values())
    ]
    assert record_holders == []


def test_recall_query_plan_uses_partial_index_with_multi_scope_and_kind(repository):
    scopes = (
        MemoryScope(kind=ScopeKind.PROJECT, key="project:6b5ec7b29e32"),
        MemoryScope(kind=ScopeKind.USER, key="user:someone"),
    )
    now = dt("2026-08-11T08:30:00Z")
    params = [value for scope in scopes for value in (scope.kind.value, scope.key)]
    sql = (
        SQL_RECALL_SELECT
        + " OR ".join("(scope_kind = ? AND scope_key = ?)" for _ in scopes)
        + SQL_RECALL_SUFFIX
    )
    params.append(now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
    sql += " AND kind IN (?, ?)"
    params.extend((MemoryKind.FACT.value, MemoryKind.PREFERENCE.value))
    sql += SQL_ORDER_BY_ID

    with repository._open_read() as connection:
        rows = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    details = [row[3] for row in rows]
    assert any("ix_memory_recall_scope" in detail for detail in details)
    assert not any(detail.startswith("SCAN memory_revisions") for detail in details)


# ---------------------------------------------------------------------------
# Benchmark script smoke tests (scripts/benchmark_memory_sqlite.py)
# ---------------------------------------------------------------------------


def _load_benchmark_module():
    path = Path(__file__).parents[2] / "scripts" / "benchmark_memory_sqlite.py"
    spec = importlib.util.spec_from_file_location("benchmark_memory_sqlite", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_benchmark_parser_defaults():
    module = _load_benchmark_module()
    args = module.build_parser().parse_args([])
    assert args.workspace is None
    assert args.transactions == 100_000
    assert args.active_per_scope == 10_000
    assert args.writers == 4
    assert args.json_output is None
    assert args.keep is False


def test_benchmark_target_containment(tmp_path):
    module = _load_benchmark_module()
    created = tmp_path / "bench"
    assert module.workspace_contained(created, created / "memory" / "structured")
    assert module.workspace_contained(created, created)
    assert not module.workspace_contained(created, tmp_path / "bench-evil")
    assert not module.workspace_contained(created, tmp_path)
    assert not module.workspace_contained(tmp_path / "bench-evil", created)


def test_benchmark_cleanup_only_removes_contained_directories(tmp_path):
    module = _load_benchmark_module()
    created = tmp_path / "bench"
    (created / "memory" / "structured").mkdir(parents=True)
    marker = created / "keep.txt"
    marker.write_text("x", encoding="utf-8")

    module.cleanup_workspace(created, created / "memory")

    assert not (created / "memory").exists()
    assert marker.exists()
    sibling = tmp_path / "bench-evil"
    sibling.mkdir()
    with pytest.raises(RuntimeError):
        module.cleanup_workspace(created, sibling)
    assert sibling.exists()
    module.cleanup_workspace(created, created)
    assert not created.exists()
    assert sibling.exists()


def test_benchmark_report_structure_and_required_metrics():
    module = _load_benchmark_module()
    environment = module.collect_environment()
    assert environment["python"]
    assert environment["sqlite"]
    assert environment["os"]

    dataset = {
        "transactions": 100_000,
        "migrated": 20_000,
        "appended": 80_000,
        "active_per_scope": 10_000,
        "writers": 4,
    }
    results = {
        "database_bytes": 12_345_678,
        "migration_seconds": 12.5,
        "migration_throughput_tx_per_s": 1_600.0,
        "insert_seconds": 300.0,
        "insert_throughput_tx_per_s": 266.6,
        "startup_seconds": 0.05,
        "health_seconds": 0.03,
        "append_p50_ms": 3.1,
        "append_p95_ms": 7.2,
        "recall_sql_p50_ms": 4.0,
        "recall_sql_p95_ms": 11.0,
        "recall_full_p50_ms": 900.0,
        "recall_full_p95_ms": 1400.0,
        "audit_export_seconds": 4.2,
        "audit_export_throughput_tx_per_s": 23_000.0,
        "concurrency_writers": 4,
        "concurrency_appended": 161,
        "concurrency_lost": 0,
        "concurrency_elapsed_s": 3.0,
        "peak_rss": "n/a (platform unsupported)",
        "integrity_ok": True,
        "foreign_keys_ok": True,
    }
    report = module.build_report("--workspace override --transactions 100000", dataset, results)
    assert json.loads(json.dumps(report)) == report
    assert set(report) >= {
        "schema_version",
        "generated_at",
        "command",
        "environment",
        "dataset",
        "results",
    }
    assert report["environment"]["sqlite"]
    assert report["results"]["append_p95_ms"] == 7.2
    assert report["results"]["peak_rss"] == "n/a (platform unsupported)"
    assert report["dataset"]["transactions"] == 100_000
