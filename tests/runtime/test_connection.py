"""WP1 — WAL/busy timeout configuration (design §16.1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from miniunicorn.runtime.sqlite import open_connection


# ---------------------------------------------------------------------------
# Required pragmas (design §16.1)
# ---------------------------------------------------------------------------


class TestWalPragma:
    def test_journal_mode_is_wal(self, tmp_path: Path) -> None:
        conn = open_connection(tmp_path / "runtime.sqlite")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()


class TestForeignKeysPragma:
    def test_foreign_keys_on(self, tmp_path: Path) -> None:
        conn = open_connection(tmp_path / "runtime.sqlite")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()


class TestSynchronousPragma:
    def test_synchronous_full(self, tmp_path: Path) -> None:
        conn = open_connection(tmp_path / "runtime.sqlite")
        # FULL = 2 in SQLite's PRAGMA synchronous mapping
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        conn.close()


class TestBusyTimeoutPragma:
    def test_default_busy_timeout_5000(self, tmp_path: Path) -> None:
        conn = open_connection(tmp_path / "runtime.sqlite")
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        conn.close()

    def test_custom_busy_timeout(self, tmp_path: Path) -> None:
        conn = open_connection(
            tmp_path / "runtime.sqlite", busy_timeout_ms=10_000
        )
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        conn.close()


class TestTempStorePragma:
    def test_temp_store_memory(self, tmp_path: Path) -> None:
        conn = open_connection(tmp_path / "runtime.sqlite")
        # MEMORY = 2 in SQLite's PRAGMA temp_store mapping
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        conn.close()


# ---------------------------------------------------------------------------
# Autocommit mode (design §16.1)
# ---------------------------------------------------------------------------


class TestAutocommitMode:
    def test_isolation_level_is_none(self, tmp_path: Path) -> None:
        """The connection must be in autocommit mode so we manage BEGIN/COMMIT
        explicitly (design §16.1)."""
        conn = open_connection(tmp_path / "runtime.sqlite")
        assert conn.isolation_level is None
        conn.close()

    def test_check_same_thread_false(self, tmp_path: Path) -> None:
        """``check_same_thread=False`` allows use from a dedicated DB executor
        thread (design §16.1)."""
        conn = open_connection(tmp_path / "runtime.sqlite")
        # No assertion needed — if this attribute exists and doesn't raise,
        # the connection was created with check_same_thread=False.
        # Verify by executing from a different thread.
        import threading

        result: list[int] = []

        def worker() -> None:
            row = conn.execute("SELECT 1").fetchone()
            result.append(row[0])

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert result == [1]
        conn.close()


# ---------------------------------------------------------------------------
# Readonly connection (design §16.1)
# ---------------------------------------------------------------------------


class TestReadonlyConnection:
    def test_readonly_cannot_write(self, tmp_path: Path) -> None:
        db_path = tmp_path / "runtime.sqlite"
        # First create the database with a write connection
        w_conn = open_connection(db_path)
        w_conn.execute("CREATE TABLE test (id INTEGER)")
        w_conn.close()

        # Open a readonly connection
        r_conn = open_connection(db_path, readonly=True)
        # Reading works
        assert r_conn.execute("SELECT COUNT(*) FROM test").fetchone()[0] == 0

        # Writing fails
        with pytest.raises(sqlite3.OperationalError):
            r_conn.execute("INSERT INTO test VALUES (1)")
        r_conn.close()

    def test_readonly_skips_wal_pragma(self, tmp_path: Path) -> None:
        """Readonly connections do not set WAL mode (it requires a write lock).
        They can still read a WAL database that was set up by a writer."""
        db_path = tmp_path / "runtime.sqlite"
        # Writer sets WAL
        w_conn = open_connection(db_path)
        assert w_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        w_conn.close()

        # Readonly reads from the WAL database
        r_conn = open_connection(db_path, readonly=True)
        # journal_mode query still returns 'wal' for a WAL database
        mode = r_conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        r_conn.close()
