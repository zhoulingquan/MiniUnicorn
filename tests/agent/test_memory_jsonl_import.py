"""Legacy JSONL migration tests: one-time, verifiable import into sqlite.

Covers the success replay (create + promote + second candidate), strict
failure modes (invalid JSON, checksum mismatch, revision skip, unknown tag,
second-line failure, ``os.replace`` failure) and idempotent re-entry.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from erza.memory.jsonl_import import (
    LegacyJournalImportError,
    migrate_legacy_journal,
)
from erza.memory.models import (
    SCHEMA_VERSION,
    ActorKind,
    MemoryOperation,
    MemoryRecord,
    MemoryStatus,
    MemoryTransaction,
    new_memory_id,
    new_transaction_id,
    transaction_checksum,
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
    slot: str = "db.primary",
    content_hash: str = "c" * 64,
    **overrides,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": memory_id or new_memory_id(),
        "revision": revision,
        "status": status,
        "kind": "fact",
        "scope": {"kind": "project", "key": "project:6b5ec7b29e32"},
        "subject": "Erza",
        "slot": slot,
        "statement": statement,
        "detail": "",
        "tags": list(tags),
        "aliases": [],
        "source_level": "inferred",
        "confidence": 0.9,
        "importance": 4,
        "evidence": [make_evidence()],
        "content_hash": content_hash,
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


def canonical_line(transaction: MemoryTransaction) -> str:
    return json.dumps(
        transaction.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    structured = tmp_path / "memory" / "structured"
    structured.mkdir(parents=True)
    bundled = Path(__file__).parents[2] / "erza" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    return tmp_path


def journal_path(workspace: Path) -> Path:
    return workspace / "memory" / "structured" / "journal.jsonl"


def manifest_path(workspace: Path) -> Path:
    return workspace / "memory" / "structured" / "storage-migration-v2.json"


def write_journal(workspace: Path, transactions: list[MemoryTransaction]) -> bytes:
    journal = journal_path(workspace)
    payload = "".join(canonical_line(tx) + "\n" for tx in transactions)
    journal.write_text(payload, encoding="utf-8")
    return journal.read_bytes()


def make_history() -> tuple[list[MemoryTransaction], list[MemoryTransaction]]:
    """Create, promote, and second-candidate transactions replaying a history."""
    first = MemoryRecord.model_validate(record_data(memory_id="mem_" + "a" * 32))
    promoted = first.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.ACTIVE,
            "updated_at": dt("2026-08-11T08:32:00Z"),
        }
    )
    second = MemoryRecord.model_validate(
        record_data(
            statement="Use SQLite instead",
            slot="db.second",
            memory_id="mem_" + "b" * 32,
            content_hash="d" * 64,
        )
    )
    create_tx = make_transaction(first, source_batch="history:1-2")
    promote_tx = make_transaction(promoted, expected_revisions={first.id: 1})
    second_tx = make_transaction(second, source_batch="history:1-2")
    return [create_tx, promote_tx, second_tx], [first, promoted, second]


def assert_failure_no_trace(
    workspace: Path,
    error: LegacyJournalImportError,
    original_bytes: bytes,
    *,
    code: str,
    line_number: int | None,
) -> None:
    assert error.code == code
    assert error.line_number == line_number
    message = str(error)
    assert code in message
    if line_number is not None:
        assert f"line {line_number}" in message
    assert "evidence excerpt" not in message
    structured = workspace / "memory" / "structured"
    assert journal_path(workspace).read_bytes() == original_bytes
    assert not (structured / "memory.db").exists()
    assert list(structured.glob("memory.db.importing*")) == []
    assert not manifest_path(workspace).exists()


# ---------------------------------------------------------------------------
# Success migration
# ---------------------------------------------------------------------------


def test_migrate_replays_full_history_into_sqlite(workspace: Path) -> None:
    transactions, records = make_history()
    original_bytes = write_journal(workspace, transactions)

    result = migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert result.migrated is True
    assert result.transaction_count == 3
    assert result.source_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert journal_path(workspace).read_bytes() == original_bytes

    manifest = json.loads(manifest_path(workspace).read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["source_sha256"] == result.source_sha256
    assert manifest["transaction_count"] == 3

    from erza.memory.repository import StructuredMemoryRepository

    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    assert repository.current_records() == (records[1], records[2])
    assert repository.revisions(records[0].id) == (records[0], records[1])
    assert repository.revisions(records[2].id) == (records[2],)


def test_migrate_skips_blank_lines(workspace: Path) -> None:
    transactions, records = make_history()
    journal = journal_path(workspace)
    payload = (
        canonical_line(transactions[0])
        + "\n\n"
        + canonical_line(transactions[1])
        + "\n  \n"
        + canonical_line(transactions[2])
        + "\n"
    )
    journal.write_text(payload, encoding="utf-8")

    result = migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert result.migrated is True
    assert result.transaction_count == 3
    from erza.memory.repository import StructuredMemoryRepository

    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    assert repository.current_records() == (records[1], records[2])


def test_migrate_without_journal_returns_not_migrated(workspace: Path) -> None:
    result = migrate_legacy_journal(workspace, lock_timeout_s=0.1)
    assert result.migrated is False
    assert result.transaction_count == 0


def test_migrate_again_after_success_is_idempotent(workspace: Path) -> None:
    transactions, _ = make_history()
    original_bytes = write_journal(workspace, transactions)
    first = migrate_legacy_journal(workspace, lock_timeout_s=0.1)
    assert first.migrated is True

    second = migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert second.migrated is False
    assert second.transaction_count == 0
    assert journal_path(workspace).read_bytes() == original_bytes
    assert manifest_path(workspace).exists()


# ---------------------------------------------------------------------------
# Failure modes: no database, no residue, no completed manifest, journal intact
# ---------------------------------------------------------------------------


def test_invalid_json_on_second_line_fails_cleanly(workspace: Path) -> None:
    transactions, _ = make_history()
    journal = journal_path(workspace)
    journal.write_text(canonical_line(transactions[0]) + '\n{"broken": [\n', encoding="utf-8")
    original_bytes = journal.read_bytes()

    with pytest.raises(LegacyJournalImportError) as excinfo:
        migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert_failure_no_trace(
        workspace, excinfo.value, original_bytes, code="invalid_transaction", line_number=2
    )


def test_checksum_mismatch_on_second_line_fails_cleanly(workspace: Path) -> None:
    transactions, _ = make_history()
    bad = transactions[1].model_copy(update={"checksum_sha256": "0" * 64})
    lines = [canonical_line(transactions[0]), canonical_line(bad), canonical_line(transactions[2])]
    journal = journal_path(workspace)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    original_bytes = journal.read_bytes()

    with pytest.raises(LegacyJournalImportError) as excinfo:
        migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert_failure_no_trace(
        workspace, excinfo.value, original_bytes, code="checksum_mismatch", line_number=2
    )


def test_revision_skip_on_second_line_fails_cleanly(workspace: Path) -> None:
    transactions, _ = make_history()
    skipped = transactions[1].operations[0].record.model_copy(update={"revision": 3})
    skip_tx = make_transaction(skipped, expected_revisions={skipped.id: 1})
    lines = [canonical_line(transactions[0]), canonical_line(skip_tx)]
    journal = journal_path(workspace)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    original_bytes = journal.read_bytes()

    with pytest.raises(LegacyJournalImportError) as excinfo:
        migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert_failure_no_trace(
        workspace, excinfo.value, original_bytes, code="revision_conflict", line_number=2
    )


def test_unknown_tag_on_second_line_fails_cleanly(workspace: Path) -> None:
    transactions, _ = make_history()
    unknown = MemoryRecord.model_validate(
        record_data(
            statement="Use SQLite instead",
            slot="db.second",
            memory_id="mem_" + "b" * 32,
            content_hash="d" * 64,
            tags=("not.registered",),
        )
    )
    lines = [
        canonical_line(transactions[0]),
        canonical_line(make_transaction(unknown, source_batch="history:1-2")),
    ]
    journal = journal_path(workspace)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    original_bytes = journal.read_bytes()

    with pytest.raises(LegacyJournalImportError) as excinfo:
        migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert_failure_no_trace(
        workspace, excinfo.value, original_bytes, code="unknown_tag", line_number=2
    )


def test_replace_failure_cleans_temporary_files(workspace: Path, monkeypatch) -> None:
    transactions, _ = make_history()
    original_bytes = write_journal(workspace, transactions)

    def failing_replace(src, dst) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", failing_replace)
    with pytest.raises(LegacyJournalImportError) as excinfo:
        migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert_failure_no_trace(
        workspace, excinfo.value, original_bytes, code="replace_failed", line_number=None
    )


# ---------------------------------------------------------------------------
# Structural scale tests (task 11): streaming reader, bounded migration work
# ---------------------------------------------------------------------------


def test_migrate_with_existing_database_never_touches_journal_reader(
    workspace: Path, monkeypatch
) -> None:
    from erza.memory import jsonl_import as import_module

    transactions, _ = make_history()
    write_journal(workspace, transactions)
    original_bytes = journal_path(workspace).read_bytes()

    first = migrate_legacy_journal(workspace, lock_timeout_s=0.1)
    assert first.migrated is True
    assert journal_path(workspace).read_bytes() == original_bytes
    repo_bytes = (workspace / "memory" / "structured" / "memory.db").read_bytes()

    def exploding_reader(path):
        raise AssertionError("legacy journal reader must never run when memory.db exists")

    monkeypatch.setattr(import_module, "iter_legacy_transactions", exploding_reader)

    second = migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert second.migrated is False
    assert (workspace / "memory" / "structured" / "memory.db").read_bytes() == repo_bytes
    assert journal_path(workspace).read_bytes() == original_bytes


def test_iter_legacy_transactions_is_a_streaming_generator(workspace: Path) -> None:
    from erza.memory import jsonl_import as import_module

    assert inspect.isgeneratorfunction(import_module.iter_legacy_transactions)
    transactions, _ = make_history()
    write_journal(workspace, transactions)
    lines = iter(import_module.iter_legacy_transactions(journal_path(workspace)))
    first_line, first_transaction = next(lines)
    assert first_line == 1
    assert first_transaction.tx_id == transactions[0].tx_id


def test_migrate_large_journal_produces_exact_counts_at_scale(workspace: Path) -> None:
    from erza.memory.repository import StructuredMemoryRepository

    count = 1000
    transactions = [
        make_transaction(
            MemoryRecord.model_validate(
                record_data(
                    memory_id=f"mem_{index:032x}",
                    slot=f"db.primary.{index}",
                    statement=f"history statement {index}",
                )
            ),
            source_batch=f"history:scale-{index}",
        )
        for index in range(count)
    ]
    original_bytes = write_journal(workspace, transactions)

    result = migrate_legacy_journal(workspace, lock_timeout_s=0.1)

    assert result.migrated is True
    assert result.transaction_count == count
    assert journal_path(workspace).read_bytes() == original_bytes
    repository = StructuredMemoryRepository(workspace, lock_timeout_s=0.1)
    assert repository.health.state == "healthy"
    assert repository.storage_stats().transaction_count == count
    assert repository.storage_stats().current_count == count
    assert repository.get(f"mem_{0:032x}") is not None
    assert repository.get(f"mem_{count - 1:032x}") is not None
