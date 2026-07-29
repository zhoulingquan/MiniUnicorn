"""SQLite connection factory with WAL pragmas (design §16.1).

Every process creates its own connections with the required pragmas:

.. code-block:: sql

    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    PRAGMA synchronous=FULL;
    PRAGMA busy_timeout=5000;
    PRAGMA temp_store=MEMORY;

Rules (design §16.1):

- one connection is never used concurrently by multiple threads;
- async callers use a dedicated DB executor or short synchronous calls
  outside the event loop;
- transactions are short and use parameterized SQL;
- migration takes an exclusive startup lock; Workers validate version but
  never migrate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miniunicorn.runtime.config import RuntimeConfig


def open_connection(
    database: str | Path,
    *,
    busy_timeout_ms: int = 5000,
    readonly: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite connection with the required WAL pragmas (design §16.1).

    ``readonly=True`` opens a read-only connection (Workers may use this
    for snapshot reads). The caller owns the connection and must close it.

    The connection is configured with ``check_same_thread=False`` so it
    can be used from a dedicated DB executor thread (design §16.1). The
    caller is responsible for ensuring no concurrent use from multiple
    threads.
    """
    uri = f"file:{database}"
    if readonly:
        uri += "?mode=ro"

    conn = sqlite3.connect(
        uri,
        uri=True,
        check_same_thread=False,
        isolation_level=None,  # autocommit; we manage BEGIN/COMMIT explicitly
        timeout=busy_timeout_ms / 1000.0,
    )
    # Use sqlite3.Row so callers can access columns by name (design §16.1).
    # This matches the row_factory set by ``SqliteRuntimeStore`` and keeps
    # raw connections (used in tests and migration helpers) consistent.
    conn.row_factory = sqlite3.Row

    # Apply required pragmas (design §16.1).
    # WAL mode cannot be set on a read-only connection, so skip it.
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA temp_store=MEMORY")

    return conn


def open_runtime_connection(
    config: RuntimeConfig,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    """Open a connection using a :class:`RuntimeConfig` (design §16.1).

    Uses ``config.database_path_resolved`` when available, otherwise
    ``config.database_path``. Applies ``config.sqlite_busy_timeout_ms``.
    """
    db_path = config.database_path_resolved or Path(config.database_path)
    if not readonly:
        # Ensure the parent directory exists for a read-write connection.
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return open_connection(
        db_path,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
        readonly=readonly,
    )


__all__ = ["open_connection", "open_runtime_connection"]
