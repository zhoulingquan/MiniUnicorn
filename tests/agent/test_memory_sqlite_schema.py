"""SQLite schema and connection policy tests (design sections 7-8)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from miniunicorn.agent.memory_models import RepositoryDegradedError
from miniunicorn.agent.memory_sqlite_schema import (
    SCHEMA_VERSION,
    check_schema,
    connect_memory_db,
    ensure_sqlite_version,
    initialize_schema,
)

REQUIRED_TABLES = {
    "storage_meta",
    "memory_transactions",
    "memory_revisions",
    "memory_creation_keys",
    "memory_source_batches",
    "memory_tags",
    "memory_aliases",
}

EXPLICIT_INDEXES = {
    "ux_memory_current_id",
    "ux_memory_active_conflict",
    "ix_memory_recall_scope",
    "ix_memory_tags_tag",
    "ix_memory_aliases_alias",
}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _explicit_index_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex%'"
        )
    }


def test_initialize_schema_creates_required_tables_and_indexes(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with connect_memory_db(db, lock_timeout_s=0.2) as connection:
        initialize_schema(connection)
        tables = _table_names(connection)
        assert REQUIRED_TABLES <= tables
        assert EXPLICIT_INDEXES <= _explicit_index_names(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        for table in REQUIRED_TABLES:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "STRICT" in sql, f"{table} must be a STRICT table"


def test_runtime_sqlite_version_supports_strict_tables() -> None:
    assert sqlite3.sqlite_version_info >= (3, 37, 0)


def test_ensure_sqlite_version_fails_closed_below_3_37() -> None:
    with pytest.raises(RepositoryDegradedError, match="unsupported_sqlite_version"):
        ensure_sqlite_version((3, 36, 9))
    for supported in ((3, 37, 0), (3, 38, 1), sqlite3.sqlite_version_info):
        ensure_sqlite_version(supported)


def test_connect_applies_runtime_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with connect_memory_db(db, lock_timeout_s=1.5) as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1500


def test_check_schema_rejects_unknown_version_without_repair(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with connect_memory_db(db, lock_timeout_s=0.2) as connection:
        initialize_schema(connection)
        check_schema(connection)
        connection.execute("PRAGMA user_version = 999")
        with pytest.raises(RepositoryDegradedError, match="unsupported_schema_version"):
            check_schema(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_initialize_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with connect_memory_db(db, lock_timeout_s=0.2) as connection:
        initialize_schema(connection)
        initialize_schema(connection)
        check_schema(connection)
        assert REQUIRED_TABLES <= _table_names(connection)
        assert EXPLICIT_INDEXES <= _explicit_index_names(connection)


def test_parallel_initialize_produces_complete_schema(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    errors: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=10)

    def worker() -> None:
        try:
            with connect_memory_db(db, lock_timeout_s=0.2) as connection:
                barrier.wait()
                initialize_schema(connection)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not threads[0].is_alive() and not threads[1].is_alive()
    assert not errors, errors
    with connect_memory_db(db, lock_timeout_s=0.2) as connection:
        check_schema(connection)
        assert REQUIRED_TABLES <= _table_names(connection)
        assert EXPLICIT_INDEXES <= _explicit_index_names(connection)
