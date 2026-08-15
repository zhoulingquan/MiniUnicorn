"""Backup and restore safety tests for the SQLite memory database (design section 13).

A backup is a consistent, integrity-verified snapshot created with the SQLite
online backup API; a restore validates the snapshot first, keeps a
``recovery/<UTC>/memory-before-restore.db`` safety copy of the pre-restore
database, then replaces the live content and rebuilds the audit export.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from miniunicorn.agent.memory import MemoryStore
from miniunicorn.agent.memory_backup import MemoryBackupError, MemoryBackupManager
from miniunicorn.agent.memory_models import (
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryKind,
    MemoryScope,
    ScopeKind,
    SourceLevel,
)
from miniunicorn.config.schema import StructuredMemoryConfig


def _ingest_once(store: MemoryStore, statement: str, subject: str) -> None:
    """Commit exactly one transaction (a non-verified fact stays candidate)."""
    from miniunicorn.agent.memory_lifecycle import IngestContext

    now = datetime.now(timezone.utc)
    evidence = EvidenceRef(kind=EvidenceKind.MODEL_INFERENCE, ref="seed", excerpt=statement)
    proposal = CandidateProposal(
        proposal_index=0,
        kind=MemoryKind.DECISION,
        scope_hint=ScopeKind.PROJECT,
        subject=subject,
        slot="general",
        statement=statement,
        tags=("project.fact",),
        confidence=0.5,
        importance=3,
        evidence_refs=("src:0",),
        speech_act=SourceLevel.INFERRED,
    )
    store.structured_lifecycle.ingest(
        proposal,
        IngestContext(
            actor=ActorKind.SYSTEM,
            reason="seed",
            source_batch="seed:test",
            scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
            evidence_catalog={"src:0": evidence},
            now=now,
        ),
    )


def seeded_store(tmp_path: Path) -> MemoryStore:
    """A store with exactly one committed transaction (one candidate fact)."""
    store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
    _ingest_once(store, "用 SQLite 做存储", "Decision")
    return store


def structured_dir(tmp_path: Path) -> Path:
    return tmp_path / "memory" / "structured"


def test_backup_is_integrity_checked_snapshot(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    result = MemoryBackupManager(store.structured_repository).create_backup()
    assert result.path.parent == structured_dir(tmp_path) / "backups"
    assert result.backup_id == result.path.name
    assert result.last_transaction_seq == 1
    assert len(result.sha256) == 64
    assert hashlib.sha256(result.path.read_bytes()).hexdigest() == result.sha256
    with sqlite3.connect(result.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("SELECT COUNT(*) FROM memory_transactions").fetchone()[0] == 1
        )


def test_restore_roundtrip_replaces_database_and_rebuilds_audit(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    repository = store.structured_repository
    manager = MemoryBackupManager(repository)
    backup = manager.create_backup()

    _ingest_once(store, "第二次事实", "Second")
    assert repository.storage_stats().transaction_count == 2

    result = manager.restore_backup(backup.backup_id)

    recovery = structured_dir(tmp_path) / "recovery"
    safety_files = list(recovery.glob("*/memory-before-restore.db"))
    assert len(safety_files) == 1
    assert result.safety_backup_id == safety_files[0].relative_to(structured_dir(tmp_path)).as_posix()
    with sqlite3.connect(safety_files[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("SELECT COUNT(*) FROM memory_transactions").fetchone()[0] == 2
        )

    assert repository.health.state == "healthy"
    stats = repository.storage_stats()
    assert stats.transaction_count == 1
    assert stats.revision_count == 1
    assert stats.current_count == 1
    assert result.restored_tx_seq == 1
    assert stats.audit_lag == 0
    records = repository.current_records()
    assert len(records) == 1
    assert records[0].statement == "用 SQLite 做存储"
    manifest = json.loads(
        (structured_dir(tmp_path) / "audit" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["database_last_tx_seq"] == 1


def test_restore_rejects_corrupt_backup_and_keeps_database(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    repository = store.structured_repository
    manager = MemoryBackupManager(repository)
    backup = manager.create_backup()
    _ingest_once(store, "第二次事实", "Second")
    backup.path.write_bytes(b"not a sqlite database at all")

    with pytest.raises(MemoryBackupError, match="invalid_backup"):
        manager.restore_backup(backup.backup_id)

    assert repository.storage_stats().transaction_count == 2
    assert not (structured_dir(tmp_path) / "recovery").exists()


def test_restore_rejects_unsupported_schema_version_and_keeps_database(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path)
    repository = store.structured_repository
    manager = MemoryBackupManager(repository)
    backup = manager.create_backup()
    _ingest_once(store, "第二次事实", "Second")
    with sqlite3.connect(backup.path) as connection:
        connection.execute("PRAGMA user_version=99")

    with pytest.raises(MemoryBackupError, match="unsupported_schema_version"):
        manager.restore_backup(backup.backup_id)

    assert repository.storage_stats().transaction_count == 2
    assert not (structured_dir(tmp_path) / "recovery").exists()


def test_restore_rejects_paths_outside_backups_dir(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    repository = store.structured_repository
    manager = MemoryBackupManager(repository)
    backup = manager.create_backup()

    outside = tmp_path / "outside.db"
    outside.write_bytes(backup.path.read_bytes())
    for evil_id in ("../outside.db", str(outside), "memory/./outside.db"):
        with pytest.raises(MemoryBackupError, match="invalid_backup"):
            manager.restore_backup(evil_id)

    with pytest.raises(MemoryBackupError, match="backup_not_found"):
        manager.restore_backup("memory-2099-01-01T00-00-00Z-1.db")

    assert repository.storage_stats().transaction_count == 1
    assert not (structured_dir(tmp_path) / "recovery").exists()


def test_restore_failure_keeps_database_and_safety_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = seeded_store(tmp_path)
    repository = store.structured_repository
    manager = MemoryBackupManager(repository)
    backup = manager.create_backup()
    _ingest_once(store, "第二次事实", "Second")

    def boom(self, backup_path):
        raise OSError("injected copy failure")

    monkeypatch.setattr(MemoryBackupManager, "_copy_backup_into_live", boom)

    with pytest.raises(MemoryBackupError, match="restore_failed"):
        manager.restore_backup(backup.backup_id)

    assert repository.health.state == "healthy"
    assert repository.storage_stats().transaction_count == 2
    safety_files = list((structured_dir(tmp_path) / "recovery").glob("*/memory-before-restore.db"))
    assert len(safety_files) == 1
    with sqlite3.connect(safety_files[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_transactions").fetchone()[0] == 2


def test_restore_twice_same_minute_keeps_unique_recovery_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    """Two restores within the same UTC minute must not collide on the
    ``recovery/<UTC>/`` targets: neither the safety database copy nor the
    pre-restore audit swap may overwrite an existing recovery entry."""
    import miniunicorn.agent.memory_audit_export as audit_export
    import miniunicorn.agent.memory_backup as memory_backup

    store = seeded_store(tmp_path)
    repository = store.structured_repository
    manager = MemoryBackupManager(repository)
    backup = manager.create_backup()
    _ingest_once(store, "第二次事实", "Second")

    monkeypatch.setattr(memory_backup, "_utc_stamp", lambda: "2026-08-15T12-00-00Z")
    monkeypatch.setattr(audit_export, "_utc_stamp", lambda: "2026-08-15T12-00-00Z")

    manager.restore_backup(backup.backup_id)
    manager.restore_backup(backup.backup_id)

    assert repository.health.state == "healthy"
    assert repository.storage_stats().transaction_count == 1
    recovery = structured_dir(tmp_path) / "recovery"
    safeties = sorted(path.parent.name for path in recovery.glob("*/memory-before-restore.db"))
    audits = sorted(path.parent.name for path in recovery.glob("*/audit"))
    assert len(safeties) == 2
    assert len(audits) == 1
    assert safeties[0] == "2026-08-15T12-00-00Z"
    assert safeties[1].startswith("2026-08-15T12-00-00Z-")
    assert audits[0] == "2026-08-15T12-00-00Z"
