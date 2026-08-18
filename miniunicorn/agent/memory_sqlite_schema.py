"""SQLite memory fact database: connection policy, schema DDL, and statements.

This module owns how the structured memory client connects to ``memory.db``,
what schema it creates, and the single home for every SQL statement the
repository executes (design sections 7-9). The Repository depends only on
:func:`connect_memory_db`, :func:`initialize_schema`, :func:`check_schema`,
:func:`record_from_row` and the statement constants; it never copies PRAGMAs,
DDL, or SQL.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from miniunicorn.agent.memory_models import MemoryRecord, RepositoryDegradedError

if TYPE_CHECKING:
    from typing import TypeAlias

    ConnectionPath: TypeAlias = str | Path

SCHEMA_VERSION = 1
_MIN_SQLITE_VERSION = (3, 37, 0)

# ---------------------------------------------------------------------------
# Connection policy (design section 8)
# ---------------------------------------------------------------------------


def ensure_sqlite_version(version_info: tuple[int, ...]) -> None:
    """Fail closed when the runtime SQLite lacks STRICT table support."""
    if version_info[:3] < _MIN_SQLITE_VERSION:
        required = ".".join(map(str, _MIN_SQLITE_VERSION))
        found = ".".join(map(str, version_info[:3]))
        raise RepositoryDegradedError(
            f"unsupported sqlite version {found}: requires >= {required} "
            "(code=unsupported_sqlite_version)"
        )


@contextmanager
def connect_memory_db(path: ConnectionPath, lock_timeout_s: float) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection with the runtime PRAGMA policy applied.

    The connection uses :data:`sqlite3.Row` rows and autocommit
    (``isolation_level=None``); callers begin transactions explicitly.
    """
    ensure_sqlite_version(sqlite3.sqlite_version_info)
    connection = sqlite3.connect(path, isolation_level=None, timeout=lock_timeout_s)
    connection.row_factory = sqlite3.Row
    try:
        _apply_pragmas(connection, lock_timeout_s)
        yield connection
    finally:
        connection.close()


def _apply_pragmas(connection: sqlite3.Connection, lock_timeout_s: float) -> None:
    connection.execute(f"PRAGMA busy_timeout={int(lock_timeout_s * 1000)}")
    _set_wal_mode(connection)
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")


def _set_wal_mode(connection: sqlite3.Connection) -> None:
    """Switch to WAL, retrying while another connection initializes the file.

    A WAL conversion requires exclusive access and fails immediately with
    ``database is locked`` if another connection holds a transaction, so the
    first two processes racing to create ``memory.db`` need a bounded retry.
    """
    deadline = time.monotonic() + 2.0
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# Schema DDL (design section 7)
# ---------------------------------------------------------------------------

_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_transactions (
        tx_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_id TEXT NOT NULL UNIQUE,
        recorded_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        source_batch TEXT NOT NULL,
        checksum_sha256 TEXT NOT NULL,
        transaction_json TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_revisions (
        memory_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        tx_seq INTEGER NOT NULL REFERENCES memory_transactions(tx_seq),
        op_index INTEGER NOT NULL,
        is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
        status TEXT NOT NULL,
        kind TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        subject_norm TEXT NOT NULL,
        conflict_key TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        source_level TEXT NOT NULL,
        importance INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        record_json TEXT NOT NULL,
        PRIMARY KEY (memory_id, revision),
        UNIQUE (tx_seq, op_index)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_creation_keys (
        source_batch TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        created_tx_seq INTEGER NOT NULL REFERENCES memory_transactions(tx_seq),
        PRIMARY KEY (source_batch, content_hash)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_source_batches (
        memory_id TEXT NOT NULL,
        source_batch TEXT NOT NULL,
        first_tx_seq INTEGER NOT NULL REFERENCES memory_transactions(tx_seq),
        PRIMARY KEY (memory_id, source_batch)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_tags (
        memory_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (memory_id, revision, tag),
        FOREIGN KEY (memory_id, revision)
            REFERENCES memory_revisions(memory_id, revision)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_aliases (
        memory_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        alias_norm TEXT NOT NULL,
        PRIMARY KEY (memory_id, revision, alias_norm),
        FOREIGN KEY (memory_id, revision)
            REFERENCES memory_revisions(memory_id, revision)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
)

_INDEX_STATEMENTS = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_current_id
    ON memory_revisions(memory_id) WHERE is_current = 1
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_active_conflict
    ON memory_revisions(conflict_key)
    WHERE is_current = 1 AND status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memory_recall_scope
    ON memory_revisions(status, scope_kind, scope_key, kind, expires_at, updated_at)
    WHERE is_current = 1
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memory_tags_tag
    ON memory_tags(tag, memory_id, revision)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memory_aliases_alias
    ON memory_aliases(alias_norm, memory_id, revision)
    """,
)


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the full schema once; no-op when it is already current.

    Idempotent and safe under concurrent initializers: DDL runs inside a
    single ``BEGIN IMMEDIATE`` transaction with ``IF NOT EXISTS`` guards, so
    any writer that wins the lock leaves a complete schema behind.
    """
    if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in (*_TABLE_STATEMENTS, *_INDEX_STATEMENTS):
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute(
            "INSERT OR IGNORE INTO storage_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def check_schema(connection: sqlite3.Connection) -> None:
    """Verify the database schema version; never repair or downgrade."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise RepositoryDegradedError(
            f"unsupported memory database schema version {version}: "
            f"expected {SCHEMA_VERSION} (code=unsupported_schema_version)"
        )


# ---------------------------------------------------------------------------
# Runtime statement constants (single SQL home; design sections 7-9)
# ---------------------------------------------------------------------------

SQL_QUICK_CHECK = "PRAGMA quick_check(1)"

SQL_MAX_TX_SEQ = "SELECT COALESCE(MAX(tx_seq), 0) FROM memory_transactions"

SQL_AUDIT_EXPORTED_SEQ = (
    "SELECT value FROM storage_meta WHERE key = 'audit_exported_seq'"
)

SQL_INSERT_TRANSACTION = (
    "INSERT INTO memory_transactions "
    "(tx_id, recorded_at, actor, reason, source_batch, checksum_sha256, "
    "transaction_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
)

SQL_UNSET_CURRENT = (
    "UPDATE memory_revisions SET is_current = 0 WHERE memory_id = ? AND is_current = 1"
)

SQL_INSERT_REVISION = (
    "INSERT INTO memory_revisions "
    "(memory_id, revision, tx_seq, op_index, is_current, status, kind, scope_kind, "
    "scope_key, subject_norm, conflict_key, content_hash, source_level, importance, "
    "updated_at, expires_at, record_json) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

SQL_INSERT_TAG = (
    "INSERT OR IGNORE INTO memory_tags (memory_id, revision, tag) VALUES (?, ?, ?)"
)

SQL_INSERT_ALIAS = (
    "INSERT OR IGNORE INTO memory_aliases "
    "(memory_id, revision, alias_norm) VALUES (?, ?, ?)"
)

SQL_INSERT_SOURCE_BATCH = (
    "INSERT OR IGNORE INTO memory_source_batches "
    "(memory_id, source_batch, first_tx_seq) VALUES (?, ?, ?)"
)

SQL_INSERT_CREATION_KEY = (
    "INSERT INTO memory_creation_keys "
    "(source_batch, content_hash, memory_id, created_tx_seq) VALUES (?, ?, ?, ?)"
)

SQL_CURRENT_RECORD_JSON_BY_ID = (
    "SELECT record_json FROM memory_revisions "
    "WHERE memory_id = ? AND is_current = 1"
)

SQL_CREATION_KEY_OWNER = (
    "SELECT memory_id FROM memory_creation_keys "
    "WHERE source_batch = ? AND content_hash = ?"
)

SQL_TX_BY_ID = "SELECT tx_id FROM memory_transactions WHERE tx_id = ?"

SQL_ACTIVE_CONFLICT_OTHER = (
    "SELECT memory_id FROM memory_revisions "
    "WHERE is_current = 1 AND status = 'active' AND conflict_key = ? AND memory_id != ?"
)

SQL_CURRENT_REVISION = (
    "SELECT revision FROM memory_revisions WHERE memory_id = ? AND is_current = 1"
)

SQL_CREATION_KEY_RECORD = (
    "SELECT r.record_json FROM memory_revisions r "
    "JOIN memory_creation_keys k ON k.memory_id = r.memory_id "
    "WHERE k.source_batch = ? AND k.content_hash = ? AND r.is_current = 1"
)

SQL_REVISIONS_BY_ID = (
    "SELECT record_json FROM memory_revisions "
    "WHERE memory_id = ? ORDER BY revision ASC"
)

SQL_CURRENT_RECORDS = "SELECT record_json FROM memory_revisions WHERE is_current = 1"

SQL_ACTIVE_BY_CONFLICT_KEY = (
    "SELECT record_json FROM memory_revisions "
    "WHERE is_current = 1 AND status = 'active' AND conflict_key = ? LIMIT 1"
)

SQL_CANDIDATE_IDS_FOR_SOURCE = (
    "SELECT r.memory_id FROM memory_revisions r "
    "JOIN memory_source_batches b ON b.memory_id = r.memory_id "
    "WHERE r.is_current = 1 AND r.status = 'candidate' AND b.source_batch = ?"
)

SQL_IDS_FOR_SOURCE = (
    "SELECT memory_id FROM memory_source_batches WHERE source_batch = ?"
)

SQL_RECALL_SELECT = (
    "SELECT record_json FROM memory_revisions "
    "WHERE is_current = 1 AND status = 'active' AND ("
)

SQL_RECALL_SUFFIX = ") AND (expires_at IS NULL OR expires_at > ?)"

SQL_TX_LOG_RECENT = (
    "SELECT transaction_json FROM memory_transactions ORDER BY tx_seq DESC LIMIT ?"
)

SQL_TX_LOG_BY_ID = (
    "SELECT transaction_json FROM memory_transactions "
    "WHERE tx_id = ? ORDER BY tx_seq DESC LIMIT ?"
)

SQL_TX_ROWS_IN_RANGE = (
    "SELECT tx_seq, transaction_json FROM memory_transactions "
    "WHERE tx_seq BETWEEN ? AND ? ORDER BY tx_seq ASC"
)

SQL_COUNT_TRANSACTIONS = "SELECT COUNT(*) FROM memory_transactions"

SQL_COUNT_REVISIONS = "SELECT COUNT(*) FROM memory_revisions"

SQL_COUNT_CURRENT = "SELECT COUNT(*) FROM memory_revisions WHERE is_current = 1"

SQL_ORDER_BY_ID = " ORDER BY memory_id ASC"


def record_from_row(row: sqlite3.Row | None) -> MemoryRecord | None:
    """Convert a ``record_json`` row (or a miss) back into a memory record."""
    if row is None:
        return None
    return MemoryRecord.model_validate_json(row["record_json"])
