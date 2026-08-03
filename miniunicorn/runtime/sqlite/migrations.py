"""Numbered schema migrations for the Runtime Store (design §16, §10.2).

Migrations are immutable after merge (design §16.3). Startup validates
checksums. The Lightweight Host or Supervisor performs migration before
starting execution slots or child processes; Workers validate version but
never migrate (design §16.1).

Each migration is a ``(version, name, sql, checksum)`` tuple. The
``checksum`` is a stable SHA-256 of the SQL text so a migrated database
can be verified against the binary (design §16.3, WP1 test:
"migration checksum").
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Migration 001: initial schema (design §16.2 — 13 tables)
# ---------------------------------------------------------------------------

_MIGRATION_001_SQL = """
-- 1. schema_migrations (design §16.3)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    checksum        TEXT NOT NULL,
    applied_at_ms   INTEGER NOT NULL
);

-- 2. tasks (design §16.4)
CREATE TABLE IF NOT EXISTS tasks (
    task_id                  TEXT PRIMARY KEY,
    turn_id                  TEXT UNIQUE,
    protocol_version         INTEGER NOT NULL,
    tenant_id                TEXT NOT NULL,
    principal_id             TEXT NOT NULL,
    agent_id                 TEXT NOT NULL,
    workspace_id             TEXT NOT NULL,
    session_key              TEXT NOT NULL,
    session_sequence         INTEGER NOT NULL,
    channel                  TEXT,
    channel_account          TEXT,
    channel_message_id       TEXT,
    dedup_key                TEXT,
    task_kind                TEXT NOT NULL CHECK (task_kind IN (
        'USER_TURN', 'MEMORY_CONSOLIDATION', 'MEMORY_INDEX',
        'REFLECTION', 'DREAM', 'MAINTENANCE'
    )),
    priority                 INTEGER NOT NULL,
    payload_blob_id          TEXT NOT NULL,
    payload_hash             TEXT NOT NULL,
    state                    TEXT NOT NULL CHECK (state IN (
        'QUEUED', 'LEASED', 'RUNNING', 'RETRY_WAIT',
        'WAITING_USER', 'COMPLETED', 'FAILED', 'CANCELLED'
    )),
    checkpoint_phase         TEXT NOT NULL,
    run_segment              INTEGER NOT NULL DEFAULT 0,
    root_attempt_count       INTEGER NOT NULL DEFAULT 0,
    max_root_attempts        INTEGER NOT NULL,
    recovery_pending         INTEGER NOT NULL DEFAULT 0,
    leased_by                TEXT,
    lease_token              TEXT,
    lease_epoch              INTEGER NOT NULL DEFAULT 0,
    lease_until_ms           INTEGER,
    last_heartbeat_at_ms     INTEGER,
    last_progress_at_ms      INTEGER,
    available_at_ms          INTEGER NOT NULL,
    state_version            INTEGER NOT NULL DEFAULT 0,
    control_cursor           INTEGER NOT NULL DEFAULT 0,
    cumulative_input_tokens  INTEGER NOT NULL DEFAULT 0,
    cumulative_output_tokens INTEGER NOT NULL DEFAULT 0,
    error_code               TEXT,
    error_summary            TEXT,
    waiting_reason           TEXT,
    waiting_ref              TEXT,
    wait_until_ms            INTEGER,
    created_at_ms            INTEGER NOT NULL,
    updated_at_ms            INTEGER NOT NULL,
    completed_at_ms          INTEGER,
    UNIQUE (session_key, session_sequence),
    CHECK (root_attempt_count >= 0),
    CHECK (cumulative_input_tokens >= 0),
    CHECK (cumulative_output_tokens >= 0)
);

-- Conditional uniqueness for channel messages (non-null fields only).
-- SQLite partial unique indexes enforce this (design §16.4).
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_channel_message
    ON tasks (tenant_id, channel, channel_account, channel_message_id)
    WHERE channel IS NOT NULL AND channel_message_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_dedup
    ON tasks (tenant_id, agent_id, workspace_id, task_kind, dedup_key)
    WHERE dedup_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_tasks_claim
    ON tasks (state, available_at_ms, priority, created_at_ms);

CREATE INDEX IF NOT EXISTS ix_tasks_session
    ON tasks (session_key, session_sequence, state);

CREATE INDEX IF NOT EXISTS ix_tasks_lease_until
    ON tasks (lease_until_ms)
    WHERE state IN ('LEASED', 'RUNNING');

CREATE INDEX IF NOT EXISTS ix_tasks_recovery
    ON tasks (recovery_pending, state, priority);

CREATE INDEX IF NOT EXISTS ix_tasks_kind_state
    ON tasks (task_kind, state);

CREATE INDEX IF NOT EXISTS ix_tasks_wait_until
    ON tasks (state, wait_until_ms)
    WHERE state = 'WAITING_USER';

-- 3. session_slots (design §16.5)
CREATE TABLE IF NOT EXISTS session_slots (
    session_key          TEXT PRIMARY KEY,
    next_sequence        INTEGER NOT NULL,
    active_task_id       TEXT,
    state_version        INTEGER NOT NULL DEFAULT 0,
    updated_at_ms        INTEGER NOT NULL,
    CHECK (next_sequence >= 0),
    FOREIGN KEY (active_task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
);

-- 4. task_events (design §16.6)
CREATE TABLE IF NOT EXISTS task_events (
    event_seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id             TEXT UNIQUE NOT NULL,
    task_id              TEXT NOT NULL,
    event_type           TEXT NOT NULL,
    phase                TEXT,
    safe_payload_json    TEXT,
    payload_blob_id      TEXT,
    lease_epoch          INTEGER,
    created_at_ms        INTEGER NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY (payload_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT
);

-- Immutability trigger: reject UPDATE on task_events (design §16.6).
CREATE TRIGGER IF NOT EXISTS trg_task_events_no_update
    BEFORE UPDATE ON task_events
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'task_events is immutable');
    END;

CREATE INDEX IF NOT EXISTS ix_task_events_task_seq
    ON task_events (task_id, event_seq);

CREATE INDEX IF NOT EXISTS ix_task_events_type_created
    ON task_events (event_type, created_at_ms);

-- 5. checkpoints (design §16.7)
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id        TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    format_version       INTEGER NOT NULL,
    phase                TEXT NOT NULL,
    run_segment          INTEGER NOT NULL,
    ordinal              INTEGER NOT NULL,
    payload_blob_id      TEXT NOT NULL,
    payload_hash         TEXT NOT NULL,
    lease_epoch          INTEGER NOT NULL,
    created_at_ms        INTEGER NOT NULL,
    UNIQUE (task_id, run_segment, ordinal),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY (payload_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_checkpoints_restore
    ON checkpoints (task_id, run_segment DESC, ordinal DESC);

-- 6. model_attempts (design §16.8)
CREATE TABLE IF NOT EXISTS model_attempts (
    model_attempt_id     TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    logical_call_id      TEXT NOT NULL,
    attempt_no           INTEGER NOT NULL,
    provider_name        TEXT NOT NULL,
    model_name           TEXT NOT NULL,
    request_hash         TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('STARTED', 'COMPLETED', 'FAILED')),
    response_blob_id     TEXT,
    response_hash        TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    finish_reason        TEXT,
    error_code           TEXT,
    error_summary        TEXT,
    started_at_ms        INTEGER NOT NULL,
    finished_at_ms       INTEGER,
    UNIQUE (task_id, logical_call_id, attempt_no),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY (response_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_model_attempts_call
    ON model_attempts (task_id, logical_call_id, attempt_no);

CREATE INDEX IF NOT EXISTS ix_model_attempts_state_started
    ON model_attempts (state, started_at_ms);

-- 7. tool_calls (design §16.9)
CREATE TABLE IF NOT EXISTS tool_calls (
    task_id              TEXT NOT NULL,
    tool_call_id         TEXT NOT NULL,
    tool_name            TEXT NOT NULL,
    arguments_blob_id    TEXT NOT NULL,
    arguments_hash       TEXT NOT NULL,
    effect_class         TEXT NOT NULL,
    risk_class           TEXT NOT NULL,
    idempotency_mode     TEXT NOT NULL,
    idempotency_key      TEXT NOT NULL,
    approval_policy      TEXT NOT NULL,
    recovery_policy      TEXT NOT NULL,
    concurrency_scope    TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
        'PREPARED', 'WAITING_APPROVAL', 'RUNNING',
        'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN', 'REJECTED'
    )),
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    result_blob_id       TEXT,
    result_hash          TEXT,
    effect_receipt_ref   TEXT,
    error_code           TEXT,
    error_summary        TEXT,
    created_at_ms        INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL,
    PRIMARY KEY (task_id, tool_call_id),
    CHECK (attempt_count >= 0),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY (arguments_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT,
    FOREIGN KEY (result_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_tool_calls_task_state
    ON tool_calls (task_id, state);

CREATE INDEX IF NOT EXISTS ix_tool_calls_idempotency
    ON tool_calls (idempotency_key);

CREATE INDEX IF NOT EXISTS ix_tool_calls_state_updated
    ON tool_calls (state, updated_at_ms);

-- 8. tool_attempts (design §16.10)
CREATE TABLE IF NOT EXISTS tool_attempts (
    tool_attempt_id      TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    tool_call_id         TEXT NOT NULL,
    attempt_no           INTEGER NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
        'STARTED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN'
    )),
    resource_token       TEXT,
    effect_receipt_ref   TEXT,
    error_code           TEXT,
    error_summary        TEXT,
    started_at_ms        INTEGER NOT NULL,
    finished_at_ms       INTEGER,
    UNIQUE (task_id, tool_call_id, attempt_no),
    FOREIGN KEY (task_id, tool_call_id) REFERENCES tool_calls(task_id, tool_call_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_tool_attempts_restore
    ON tool_attempts (task_id, tool_call_id, attempt_no);

-- 9. task_controls (design §16.11)
CREATE TABLE IF NOT EXISTS task_controls (
    control_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    control_id           TEXT UNIQUE NOT NULL,
    task_id              TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN (
        'CANCEL', 'APPROVE_TOOL', 'REJECT_TOOL', 'RESOLVE_EFFECT',
        'STEER', 'CONTINUE', 'STOP_AFTER_CHECKPOINT'
    )),
    dedup_key            TEXT NOT NULL,
    payload_blob_id      TEXT,
    requested_by         TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
        'PENDING', 'APPLIED', 'REJECTED', 'EXPIRED'
    )),
    outcome_code         TEXT,
    requested_at_ms      INTEGER NOT NULL,
    applied_at_ms        INTEGER,
    UNIQUE (task_id, dedup_key),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY (payload_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_task_controls_task_seq_state
    ON task_controls (task_id, control_seq, state);

-- 10. session_commits (design §16.12)
CREATE TABLE IF NOT EXISTS session_commits (
    session_commit_id    TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    commit_kind          TEXT NOT NULL CHECK (commit_kind IN ('INBOUND', 'FINAL')),
    session_key          TEXT NOT NULL,
    base_revision        INTEGER NOT NULL,
    target_revision      INTEGER NOT NULL,
    content_hash         TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('PREPARED', 'COMMITTED', 'CONFLICT')),
    error_code           TEXT,
    created_at_ms        INTEGER NOT NULL,
    committed_at_ms      INTEGER,
    UNIQUE (task_id, commit_kind),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_session_commits_session_state
    ON session_commits (session_key, state, created_at_ms);

-- 11. outbox (design §16.13)
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id              TEXT NOT NULL,
    channel              TEXT NOT NULL,
    channel_account      TEXT NOT NULL,
    target_key           TEXT NOT NULL,
    message_kind         TEXT NOT NULL,
    payload_blob_id      TEXT NOT NULL,
    payload_hash         TEXT NOT NULL,
    dedup_key            TEXT UNIQUE NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
        'PENDING', 'SENDING', 'RETRY_WAIT', 'OUTCOME_UNKNOWN', 'DELIVERED', 'FAILED'
    )),
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL,
    available_at_ms      INTEGER NOT NULL,
    leased_by            TEXT,
    lease_token          TEXT,
    lease_epoch          INTEGER NOT NULL DEFAULT 0,
    lease_until_ms       INTEGER,
    provider_receipt_ref TEXT,
    error_code           TEXT,
    error_summary        TEXT,
    created_at_ms        INTEGER NOT NULL,
    delivered_at_ms      INTEGER,
    CHECK (attempt_count >= 0),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY (payload_blob_id) REFERENCES runtime_blobs(blob_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_outbox_claim
    ON outbox (state, available_at_ms, outbox_id);

CREATE INDEX IF NOT EXISTS ix_outbox_target_order
    ON outbox (channel, channel_account, target_key, outbox_id, state);

CREATE INDEX IF NOT EXISTS ix_outbox_lease_until
    ON outbox (lease_until_ms)
    WHERE state = 'SENDING';

-- 12. resource_leases (design §16.14)
CREATE TABLE IF NOT EXISTS resource_leases (
    resource_key         TEXT NOT NULL,
    holder_kind          TEXT NOT NULL,
    holder_id            TEXT NOT NULL,
    units                INTEGER NOT NULL,
    lease_token          TEXT NOT NULL,
    lease_until_ms       INTEGER NOT NULL,
    created_at_ms        INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL,
    PRIMARY KEY (resource_key, holder_kind, holder_id),
    CHECK (units > 0)
);

CREATE INDEX IF NOT EXISTS ix_resource_leases_expiry
    ON resource_leases (resource_key, lease_until_ms);

-- 13. runtime_blobs (design §16.15)
CREATE TABLE IF NOT EXISTS runtime_blobs (
    blob_id              TEXT PRIMARY KEY,
    scope_key            TEXT NOT NULL,
    blob_kind            TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    encoding             TEXT NOT NULL,
    compression          TEXT,
    encryption_key_id    TEXT,
    inline_content       BLOB,
    external_ref         TEXT,
    size_bytes           INTEGER NOT NULL,
    created_at_ms        INTEGER NOT NULL,
    UNIQUE (scope_key, blob_kind, content_hash),
    CHECK (
        (inline_content IS NOT NULL AND external_ref IS NULL)
        OR (inline_content IS NULL AND external_ref IS NOT NULL)
    ),
    CHECK (size_bytes >= 0)
);

CREATE INDEX IF NOT EXISTS ix_runtime_blobs_created
    ON runtime_blobs (created_at_ms);
"""


# ---------------------------------------------------------------------------
# Migration 002: add immutable delivery target to tasks (Task 7 Step 2)
# ---------------------------------------------------------------------------

_MIGRATION_002_SQL = """-- Task 7 Step 2: immutable delivery target for final replies.
-- The Outbox already stores (channel, channel_account, target_key) for
-- each delivery row, but the tasks table only carried channel and
-- channel_account. Final replies therefore lost the original inbound
-- chat/thread target and fell back to empty strings (design §17.8).
-- Existing rows default to '' so the migration is backwards-compatible;
-- new production submissions must populate target_key at ingress.
ALTER TABLE tasks ADD COLUMN target_key TEXT NOT NULL DEFAULT '';
"""


@dataclass(slots=True, frozen=True)
class Migration:
    """One numbered, checksummed migration (design §16.3)."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        """Stable SHA-256 of the migration SQL text (design §16.3)."""
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        sql=_MIGRATION_001_SQL,
    ),
    Migration(
        version=2,
        name="add_tasks_target_key",
        sql=_MIGRATION_002_SQL,
    ),
)

CURRENT_SCHEMA_VERSION: int = MIGRATIONS[-1].version


def _now_ms(conn: sqlite3.Connection) -> int:
    """Return current UTC Unix milliseconds from SQLite (design §12.2)."""
    row = conn.execute("SELECT CAST(strftime('%s', 'now') AS INTEGER) * 1000").fetchone()
    return int(row[0])


def _acquire_migration_lock(
    conn: sqlite3.Connection, *, retries: int = 200, delay_s: float = 0.02
) -> None:
    """Acquire ``BEGIN IMMEDIATE`` with BUSY retry for concurrent startup.

    SQLite's ``BEGIN IMMEDIATE`` takes a write lock immediately. Under
    concurrent startup two migrators race for it; the loser receives
    ``SQLITE_BUSY`` and must retry until the winner finishes (design §16.1).
    ``PRAGMA busy_timeout`` covers most cases, but we add an explicit
    retry loop for robustness across platforms.
    """
    for _ in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                time.sleep(delay_s)
                continue
            raise
    raise sqlite3.OperationalError("migration lock acquisition timed out")


def _exec_with_lock_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    *,
    retries: int = 200,
    delay_s: float = 0.02,
) -> Any:
    """Execute a SQL statement with BUSY/LOCKED retry (design §16.1).

    Used for autocommit-mode statements that run outside the explicit
    ``BEGIN IMMEDIATE`` transaction (e.g., the bootstrap ``CREATE TABLE``
    and the initial ``SELECT``). ``PRAGMA busy_timeout`` should handle
    most contention, but on some platforms it is not reliably respected
    for DDL, so we add an explicit retry loop.
    """
    for _ in range(retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                time.sleep(delay_s)
                continue
            raise
    raise sqlite3.OperationalError(f"statement timed out after {retries} retries: {sql[:80]}")


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements (design §16.1).

    Handles ``BEGIN ... END`` compound statement blocks (used by SQLite
    trigger definitions) so that semicolons inside the block body are
    not treated as statement terminators.

    Comment-only lines (``-- ...``) are stripped before splitting.
    """
    statements: list[str] = []
    current: list[str] = []
    begin_depth = 0

    for line in sql.splitlines():
        stripped = line.strip()
        # Skip comment-only lines (design §16.1 — comments are not statements).
        if stripped.startswith("--"):
            continue

        upper = stripped.upper()

        # Track BEGIN ... END nesting for trigger bodies.
        # ``BEGIN`` as a standalone keyword (not ``BEGIN TRANSACTION`` etc.)
        # opens a compound statement block.
        if upper == "BEGIN":
            begin_depth += 1

        current.append(line)

        # ``END`` closes a compound statement block.
        if upper.startswith("END") and begin_depth > 0:
            begin_depth -= 1

        # A semicolon at depth 0 terminates a statement.
        if begin_depth == 0 and stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []

    if current:
        stmt = "\n".join(current).strip()
        if stmt:
            statements.append(stmt)

    return statements


def _execute_migration_sql(conn: sqlite3.Connection, sql: str) -> None:
    """Execute migration SQL statement-by-statement inside the current transaction.

    Unlike :meth:`executescript` (which implicitly commits any pending
    transaction before running), this method executes each statement
    individually via :meth:`execute`, preserving the caller's
    ``BEGIN IMMEDIATE`` lock (design §16.1). This ensures the entire
    migration runs within a single exclusive transaction.
    """
    for stmt in _split_sql_statements(sql):
        conn.execute(stmt)


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations inside an exclusive startup lock.

    The Lightweight Host or Supervisor calls this before starting execution
    slots or child processes (design §16.1). Workers validate version but
    never migrate.

    Returns the resulting schema version.
    """
    # Ensure schema_migrations exists even before migration 1 runs, so we
    # can record the migration itself. The CREATE IF NOT EXISTS in
    # _MIGRATION_001_SQL handles this, but we also need a bootstrap for
    # the very first run. This bootstrap runs in autocommit with retry
    # for concurrent-startup safety (design §16.1).
    _exec_with_lock_retry(
        conn,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            checksum        TEXT NOT NULL,
            applied_at_ms   INTEGER NOT NULL
        )
        """,
    )

    applied = {
        int(row[0]): str(row[1])
        for row in _exec_with_lock_retry(
            conn,
            "SELECT version, checksum FROM schema_migrations",
        ).fetchall()
    }

    # Validate checksums of already-applied migrations (design §16.3).
    for migration in MIGRATIONS:
        if migration.version in applied:
            if applied[migration.version] != migration.checksum:
                raise RuntimeError(
                    f"schema_migrations checksum mismatch for version "
                    f"{migration.version}: db={applied[migration.version]!r} "
                    f"binary={migration.checksum!r}"
                )

    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        return CURRENT_SCHEMA_VERSION

    # Serialize concurrent migrators. The entire migration (DDL + record
    # insert) runs inside a single ``BEGIN IMMEDIATE`` transaction so the
    # write lock is held throughout. Concurrent migrators wait via
    # ``busy_timeout`` + retry in ``_acquire_migration_lock`` (design §16.1).
    for migration in pending:
        # Acquire write lock with retry (design §16.1).
        _acquire_migration_lock(conn)
        try:
            # Re-read inside the lock to handle the race where another
            # migrator applied this migration while we waited.
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?",
                (migration.version,),
            ).fetchone()
            if row is not None:
                if row[0] != migration.checksum:
                    raise RuntimeError(
                        f"schema_migrations checksum mismatch for version "
                        f"{migration.version}: db={row[0]!r} "
                        f"binary={migration.checksum!r}"
                    )
                # Already applied by another migrator; release lock and skip.
                conn.execute("COMMIT")
                continue

            # Execute migration SQL statement-by-statement within the
            # transaction (NOT executescript, which would auto-commit and
            # release the lock). The SQL is idempotent (CREATE ... IF NOT
            # EXISTS) so re-runs after a partial failure are safe
            # (design §16.1, §16.3).
            _execute_migration_sql(conn, migration.sql)

            # Record the migration inside the same transaction.
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name, checksum, applied_at_ms) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, _now_ms(conn)),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    return CURRENT_SCHEMA_VERSION


def validate_schema_version(conn: sqlite3.Connection) -> None:
    """Validate that the database schema version matches the binary.

    Workers call this on startup (design §16.1). A process refuses work
    when its schema version differs from the binary.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    db_version = int(row[0]) if row[0] is not None else 0
    if db_version != CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"schema version mismatch: db={db_version} binary={CURRENT_SCHEMA_VERSION}"
        )


__all__ = [
    "Migration",
    "MIGRATIONS",
    "CURRENT_SCHEMA_VERSION",
    "run_migrations",
    "validate_schema_version",
]
