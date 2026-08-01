"""WP1 — Migration checksum and concurrent startup (design §16.1, §16.3)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from miniunicorn.runtime.sqlite import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    open_connection,
    run_migrations,
    validate_schema_version,
)

# ---------------------------------------------------------------------------
# Migration checksum (design §16.3)
# ---------------------------------------------------------------------------


class TestMigrationChecksum:
    def test_checksum_is_stable_sha256(self) -> None:
        """Each migration has a stable SHA-256 checksum (design §16.3)."""
        import hashlib

        for migration in MIGRATIONS:
            expected = hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()
            assert migration.checksum == expected
            assert len(migration.checksum) == 64  # SHA-256 hex

    def test_checksum_stored_in_database(
        self, runtime_db: sqlite3.Connection
    ) -> None:
        """The checksum stored in ``schema_migrations`` must match the binary
        (design §16.3)."""
        row = runtime_db.execute(
            "SELECT version, checksum FROM schema_migrations WHERE version=?",
            (CURRENT_SCHEMA_VERSION,),
        ).fetchone()
        assert row is not None
        migration = MIGRATIONS[-1]
        assert row["checksum"] == migration.checksum

    def test_checksum_mismatch_raises(self, tmp_path: Path) -> None:
        """A tampered checksum in the database must raise on re-run
        (design §16.3)."""
        db_path = tmp_path / "runtime.sqlite"
        conn = open_connection(db_path)
        run_migrations(conn)

        # Tamper with the stored checksum
        conn.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=1"
        )

        with pytest.raises(RuntimeError, match="checksum mismatch"):
            run_migrations(conn)

        conn.close()


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    def test_running_migrations_twice_is_noop(
        self, runtime_db: sqlite3.Connection
    ) -> None:
        """Running ``run_migrations`` on an already-migrated database must be
        a no-op (design §16.3)."""
        # First run already happened in the fixture
        version = run_migrations(runtime_db)
        assert version == CURRENT_SCHEMA_VERSION

        # Second run should not raise
        version = run_migrations(runtime_db)
        assert version == CURRENT_SCHEMA_VERSION

    def test_validate_schema_version_passes_after_migrate(
        self, runtime_db: sqlite3.Connection
    ) -> None:
        """``validate_schema_version`` must succeed after migration (design §16.1)."""
        validate_schema_version(runtime_db)

    def test_validate_schema_version_fails_on_empty_db(self, tmp_path: Path) -> None:
        """An unmigrated database must fail validation (design §16.1)."""
        db_path = tmp_path / "runtime.sqlite"
        conn = open_connection(db_path)

        # Don't run migrations — the schema_migrations table doesn't exist
        with pytest.raises(Exception):
            validate_schema_version(conn)

        conn.close()


# ---------------------------------------------------------------------------
# Concurrent startup (design §16.1)
# ---------------------------------------------------------------------------


class TestConcurrentStartup:
    def test_two_connections_migrate_and_validate(
        self, tmp_path: Path
    ) -> None:
        """One connection migrates, the other validates (design §16.1).

        This simulates the Host/Supervisor (migrator) and Worker (validator)
        startup sequence.
        """
        db_path = tmp_path / "runtime.sqlite"

        # Host migrates
        host_conn = open_connection(db_path)
        run_migrations(host_conn)
        host_conn.close()

        # Worker validates
        worker_conn = open_connection(db_path)
        validate_schema_version(worker_conn)
        worker_conn.close()

    def test_concurrent_migrations_both_succeed(self, tmp_path: Path) -> None:
        """Two processes calling ``run_migrations`` concurrently must both
        succeed (design §16.1, §16.3).

        Migration SQL uses only ``CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS``
        and ``INSERT OR IGNORE`` into ``schema_migrations``, so concurrent
        execution is safe.
        """
        db_path = tmp_path / "runtime.sqlite"
        errors: list[Exception] = []

        def migrator() -> None:
            try:
                conn = open_connection(db_path)
                run_migrations(conn)
                validate_schema_version(conn)
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=migrator) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent migration failed: {errors}"

        # Verify the database is in a valid state
        conn = open_connection(db_path)
        validate_schema_version(conn)
        version = conn.execute(
            "SELECT version FROM schema_migrations WHERE version=1"
        ).fetchone()
        assert version is not None
        conn.close()

    def test_all_13_tables_created(self, runtime_db: sqlite3.Connection) -> None:
        """Migration 001 must create all 13 tables from design §16.2."""
        expected_tables = {
            "schema_migrations",
            "tasks",
            "session_slots",
            "task_events",
            "checkpoints",
            "model_attempts",
            "tool_calls",
            "tool_attempts",
            "task_controls",
            "session_commits",
            "outbox",
            "resource_leases",
            "runtime_blobs",
        }
        rows = runtime_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        actual_tables = {row["name"] for row in rows}
        assert expected_tables <= actual_tables, (
            f"missing tables: {expected_tables - actual_tables}"
        )

    def test_triggers_created(self, runtime_db: sqlite3.Connection) -> None:
        """The immutability trigger must exist (design §16.6)."""
        rows = runtime_db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        trigger_names = {row["name"] for row in rows}
        assert "trg_task_events_no_update" in trigger_names
