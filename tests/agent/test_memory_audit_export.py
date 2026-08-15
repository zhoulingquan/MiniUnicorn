"""Audit export tests: segmented JSONL derivation, crash safety, rebuild, triggers."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from miniunicorn.agent.memory_audit_export import AuditExportError, MemoryAuditExporter
from miniunicorn.agent.memory_models import (
    SCHEMA_VERSION,
    ActorKind,
    MemoryOperation,
    MemoryRecord,
    MemoryTransaction,
    new_memory_id,
    new_transaction_id,
    transaction_checksum,
)
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

UTC = timezone.utc

FIXED_GENERATED_AT = "2026-08-15T00:00:00Z"
PROJECT_SCOPE = {"kind": "project", "key": "project:6b5ec7b29e32"}


def dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _record(index: int = 0) -> MemoryRecord:
    return MemoryRecord.model_validate(
        {
            "schema_version": SCHEMA_VERSION,
            "id": new_memory_id(),
            "revision": 1,
            "status": "candidate",
            "kind": "fact",
            "scope": PROJECT_SCOPE,
            "subject": "MiniUnicorn",
            "slot": f"db.primary.{index}",
            "statement": f"fact {index}",
            "detail": "",
            "tags": ["project.fact"],
            "aliases": [],
            "source_level": "inferred",
            "confidence": 0.9,
            "importance": 4,
            "evidence": [
                {
                    "kind": "manual",
                    "ref": f"command:msg-{index}",
                    "excerpt": "evidence excerpt",
                    "sha256": None,
                    "observed_at": "2026-08-11T08:30:00Z",
                }
            ],
            "content_hash": f"c{index:063d}",
            "derived_from": [],
            "supersedes": [],
            "replacement_id": None,
            "blocked_by": [],
            "valid_from": "2026-08-11T08:30:00Z",
            "expires_at": None,
            "created_at": "2026-08-11T08:30:00Z",
            "updated_at": "2026-08-11T08:31:00Z",
            "status_reason": "test",
        }
    )


def make_transaction(*records, actor: str = "dream", reason: str = "test"):
    expected = {rec.id: rec.revision - 1 for rec in records}
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id=new_transaction_id(),
        recorded_at=dt("2026-08-11T08:31:00Z"),
        actor=ActorKind(actor),
        reason=reason,
        source_batch="",
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
def workspace(tmp_path) -> Path:
    structured = tmp_path / "memory" / "structured"
    structured.mkdir(parents=True)
    bundled = Path(__file__).parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    return tmp_path


@pytest.fixture
def repository(workspace: Path) -> StructuredMemoryRepository:
    return StructuredMemoryRepository(workspace, lock_timeout_s=5.0)


def seed_transactions(repository: StructuredMemoryRepository, count: int):
    transactions = [
        make_transaction(_record(index)) for index in range(repository.storage_stats().transaction_count, repository.storage_stats().transaction_count + count)
    ]
    for transaction in transactions:
        repository.append_transaction(transaction)
    return transactions


def audit_dir(workspace: Path) -> Path:
    return workspace / "memory" / "structured" / "audit"


def snapshot_dir(path: Path) -> dict[str, bytes]:
    return {
        name: (path / name).read_bytes()
        for name in sorted(entry.name for entry in path.iterdir() if entry.is_file())
    }


def assert_audit_consistent(audit: Path) -> None:
    manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["segments"]:
        path = audit / entry["path"]
        assert path.exists(), f"missing segment {entry['path']}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert len(path.read_text(encoding="utf-8").splitlines()) == entry["rows"]


# ---------------------------------------------------------------------------
# Step 1: small-segment export shape
# ---------------------------------------------------------------------------


def test_small_segment_export_matches_exact_files(workspace, repository):
    transactions = seed_transactions(repository, 8)
    exporter = MemoryAuditExporter(repository, segment_size=3)

    result = exporter.export_pending()

    audit = audit_dir(workspace)
    assert sorted(entry.name for entry in audit.iterdir()) == [
        "journal-000000000001-000000000003.jsonl",
        "journal-000000000004-000000000006.jsonl",
        "journal-open.jsonl",
        "manifest.json",
    ]
    lines = [canonical_line(tx) for tx in transactions]
    sealed1 = (audit / "journal-000000000001-000000000003.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    sealed2 = (audit / "journal-000000000004-000000000006.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    open_tail = (audit / "journal-open.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(sealed1) == len(sealed2) == 3
    assert len(open_tail) == 2
    assert sealed1 == lines[0:3]
    assert sealed2 == lines[3:6]
    assert open_tail == lines[6:8]

    manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["database_last_tx_seq"] == 8
    assert manifest["generated_at"].endswith("Z")
    assert [entry["path"] for entry in manifest["segments"]] == [
        "journal-000000000001-000000000003.jsonl",
        "journal-000000000004-000000000006.jsonl",
        "journal-open.jsonl",
    ]
    for entry, expected_rows, expected_lines in zip(
        manifest["segments"], (3, 3, 2), (sealed1, sealed2, open_tail)
    ):
        assert entry["first_tx_seq"] + entry["rows"] - 1 == entry["last_tx_seq"]
        file_bytes = ("\n".join(expected_lines) + "\n").encode("utf-8")
        assert hashlib.sha256(file_bytes).hexdigest() == entry["sha256"]
        assert json.loads(expected_lines[0])["tx_id"] == entry["first_tx_id"]
        assert json.loads(expected_lines[-1])["tx_id"] == entry["last_tx_id"]

    stats = repository.storage_stats()
    assert stats.audit_exported_seq == 8
    assert stats.audit_lag == 0
    assert result.exported_rows == 8
    assert result.sealed_segments == 2
    assert result.first_tx_seq == 1
    assert result.last_tx_seq == 8


def test_exact_multiple_of_segment_size_has_no_open_tail(workspace, repository):
    seed_transactions(repository, 6)
    exporter = MemoryAuditExporter(repository, segment_size=3)

    exporter.export_pending()

    audit = audit_dir(workspace)
    assert sorted(entry.name for entry in audit.iterdir()) == [
        "journal-000000000001-000000000003.jsonl",
        "journal-000000000004-000000000006.jsonl",
        "manifest.json",
    ]


def test_retro_seal_replaces_open_tail_into_sealed_segment(workspace, repository):
    seed_transactions(repository, 10)
    exporter = MemoryAuditExporter(repository, segment_size=3)
    exporter.export_pending()
    audit = audit_dir(workspace)
    assert (audit / "journal-open.jsonl").exists()
    assert repository.storage_stats().audit_lag == 0

    seed_transactions(repository, 3)
    result = exporter.export_pending()

    assert [entry["path"] for entry in manifest_segments(audit)] == [
        "journal-000000000001-000000000003.jsonl",
        "journal-000000000004-000000000006.jsonl",
        "journal-000000000007-000000000009.jsonl",
        "journal-000000000010-000000000012.jsonl",
        "journal-open.jsonl",
    ]
    open_tail = (audit / "journal-open.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(open_tail) == 1
    assert repository.storage_stats().audit_exported_seq == 13
    assert repository.storage_stats().audit_lag == 0
    assert result.exported_rows == 3


def manifest_segments(audit: Path) -> list[dict]:
    return json.loads((audit / "manifest.json").read_text(encoding="utf-8"))["segments"]


def test_empty_database_is_noop_with_zero_lag(workspace, repository):
    exporter = MemoryAuditExporter(repository, segment_size=3)

    result = exporter.export_pending()

    assert result.exported_rows == 0
    assert result.lag == 0
    assert not audit_dir(workspace).exists()


def test_export_pending_is_idempotent(workspace, repository):
    seed_transactions(repository, 5)
    exporter = MemoryAuditExporter(repository, segment_size=3)
    assert exporter.export_pending().exported_rows == 5
    before = snapshot_dir(audit_dir(workspace))

    second = exporter.export_pending()

    assert second.exported_rows == 0
    assert second.lag == 0
    assert snapshot_dir(audit_dir(workspace)) == before


# ---------------------------------------------------------------------------
# Step 2: crash safety and rebuild byte-identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_export_failure_keeps_db_and_old_audit_and_rebuild_restores(
    workspace, repository, monkeypatch, tmp_path_factory, failure
):
    import miniunicorn.agent.memory_audit_export as audit_export

    transactions = seed_transactions(repository, 8)
    exporter = MemoryAuditExporter(repository, segment_size=3)
    monkeypatch.setattr(audit_export, "_utc_now", lambda: FIXED_GENERATED_AT)
    exporter.export_pending()
    audit = audit_dir(workspace)
    clean_before = snapshot_dir(audit)
    assert_audit_consistent(audit)

    extra = seed_transactions(repository, 3)

    if failure == "write":
        real_open = builtins.open

        def flaky_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and ".tmp-" in str(file):
                raise OSError("injected temp write failure")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)
    elif failure == "fsync":

        def flaky_fsync(fd):
            raise OSError("injected fsync failure")

        monkeypatch.setattr(audit_export.os, "fsync", flaky_fsync)
    else:
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected replace failure")
            real_replace(src, dst)

        monkeypatch.setattr(audit_export.os, "replace", flaky_replace)

    with pytest.raises(AuditExportError):
        exporter.export_pending()

    stats = repository.storage_stats()
    assert stats.transaction_count == 11
    assert stats.last_transaction_seq == 11
    assert stats.audit_exported_seq == 8
    assert stats.audit_lag == 3
    if failure in ("write", "fsync"):
        assert snapshot_dir(audit) == clean_before
    assert_audit_consistent(audit)

    monkeypatch.undo()
    twin = tmp_path_factory.mktemp("twin")
    structured = twin / "memory" / "structured"
    structured.mkdir(parents=True)
    bundled = Path(__file__).parents[2] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    twin_repository = StructuredMemoryRepository(twin, lock_timeout_s=5.0)
    for transaction in (*transactions, *extra):
        twin_repository.append_transaction(transaction)
    monkeypatch.setattr(audit_export, "_utc_now", lambda: FIXED_GENERATED_AT)
    MemoryAuditExporter(twin_repository, segment_size=3).export_pending()
    clean_reference = snapshot_dir(audit_dir(twin))

    exporter.rebuild()

    assert snapshot_dir(audit) == clean_reference
    assert repository.storage_stats().audit_lag == 0


def test_watermark_never_regresses_below_stored(workspace, repository):
    from miniunicorn.agent.memory_sqlite_schema import connect_memory_db

    seed_transactions(repository, 10)
    exporter = MemoryAuditExporter(repository, segment_size=3)
    with connect_memory_db(repository.database_path, lock_timeout_s=5.0) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR REPLACE INTO storage_meta(key, value) VALUES ('audit_exported_seq', ?)",
            ("10",),
        )
        connection.commit()

    result = exporter.export_pending()

    assert result.exported_rows == 0
    assert repository.storage_stats().audit_exported_seq == 10
    assert repository.storage_stats().audit_lag == 0


def test_concurrent_export_pending_does_not_corrupt_or_regress(workspace, repository):
    seed_transactions(repository, 25)
    exporter = MemoryAuditExporter(repository, segment_size=4)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        try:
            for _ in range(3):
                exporter.export_pending()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    stats = repository.storage_stats()
    assert stats.audit_exported_seq == 25
    assert stats.audit_lag == 0
    audit = audit_dir(workspace)
    manifest = json.loads((audit / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database_last_tx_seq"] == 25
    assert sum(entry["rows"] for entry in manifest["segments"]) == 25
    assert_audit_consistent(audit)


# ---------------------------------------------------------------------------
# Step 5: full rebuild
# ---------------------------------------------------------------------------


def test_rebuild_swaps_audit_and_preserves_old_in_recovery(workspace, repository):
    seed_transactions(repository, 8)
    exporter = MemoryAuditExporter(repository, segment_size=3)
    exporter.export_pending()
    old_manifest_bytes = (audit_dir(workspace) / "manifest.json").read_bytes()

    result = exporter.rebuild()

    audit = audit_dir(workspace)
    recovered = list((workspace / "memory" / "structured" / "recovery").glob("*/audit"))
    assert len(recovered) == 1
    assert (recovered[0] / "manifest.json").read_bytes() == old_manifest_bytes
    assert manifest_segments(audit)[0]["path"] == "journal-000000000001-000000000003.jsonl"
    assert repository.storage_stats().audit_lag == 0
    assert result.last_tx_seq == 8


def test_rebuild_failure_keeps_db_and_recoverable_old_dir(
    workspace, repository, monkeypatch
):
    import miniunicorn.agent.memory_audit_export as audit_export

    seed_transactions(repository, 8)
    exporter = MemoryAuditExporter(repository, segment_size=3)
    exporter.export_pending()
    audit = audit_dir(workspace)
    old_manifest_bytes = (audit / "manifest.json").read_bytes()
    seed_transactions(repository, 2)

    real_replace = os.replace

    def flaky_replace(src, dst):
        if str(dst) == str(audit):
            raise OSError("injected audit directory swap failure")
        real_replace(src, dst)

    monkeypatch.setattr(audit_export.os, "replace", flaky_replace)

    with pytest.raises(AuditExportError):
        exporter.rebuild()

    stats = repository.storage_stats()
    assert stats.transaction_count == 10
    assert stats.audit_exported_seq == 8
    assert stats.audit_lag == 2
    assert not audit.exists()
    recovered = list((workspace / "memory" / "structured" / "recovery").glob("*/audit"))
    assert len(recovered) == 1
    assert (recovered[0] / "manifest.json").read_bytes() == old_manifest_bytes

    monkeypatch.undo()
    exporter.rebuild()

    assert repository.storage_stats().audit_lag == 0
    assert manifest_segments(audit)[-1]["path"] == "journal-open.jsonl"
    assert len(list((workspace / "memory" / "structured" / "recovery").glob("*/audit"))) == 1


# ---------------------------------------------------------------------------
# Step 4: repository range query
# ---------------------------------------------------------------------------


def test_transaction_rows_in_range_returns_ordered_raw_rows(workspace, repository):
    transactions = seed_transactions(repository, 8)

    rows = repository.transaction_rows_in_range(2, 4)

    assert [seq for seq, _ in rows] == [2, 3, 4]
    assert [raw for _, raw in rows] == [
        canonical_line(transactions[1]),
        canonical_line(transactions[2]),
        canonical_line(transactions[3]),
    ]
    assert repository.transaction_rows_in_range(9, 12) == ()


# ---------------------------------------------------------------------------
# Step 6: startup trigger
# ---------------------------------------------------------------------------


def test_startup_trigger_exports_pending_lag(workspace, repository):
    from miniunicorn.agent.memory import MemoryStore
    from miniunicorn.config.schema import StructuredMemoryConfig

    seed_transactions(repository, 5)
    assert repository.storage_stats().audit_lag == 5

    store = MemoryStore(workspace, structured_config=StructuredMemoryConfig())

    assert store.structured_repository.storage_stats().audit_lag == 0
    manifest = json.loads((audit_dir(workspace) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database_last_tx_seq"] == 5
    assert_audit_consistent(audit_dir(workspace))


def test_negative_segment_size_rejected(repository):
    with pytest.raises(ValueError):
        MemoryAuditExporter(repository, segment_size=0)


def test_default_segment_size_is_ten_thousand():
    from miniunicorn.agent.memory_audit_export import SEGMENT_SIZE

    assert SEGMENT_SIZE == 10_000
