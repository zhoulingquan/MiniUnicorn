"""WP1 — Shared fixtures for Runtime Store tests (design §30 WP1).

These fixtures create an isolated file-based SQLite database per test so
tests can run in parallel without conflicting on a shared database file.
The database is created under ``tmp_path`` and torn down automatically.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.models import (
    InboundTaskEnvelope,
    InternalTaskEnvelope,
    MediaRef,
    RequestScope,
)
from miniunicorn.runtime.sqlite import (
    SqliteRuntimeStore,
    open_connection,
    run_migrations,
)


@pytest.fixture
def runtime_db(tmp_path: Path) -> sqlite3.Connection:
    """File-based SQLite connection with migrations applied.

    Uses a real file (not ``:memory:``) so WAL mode and busy_timeout
    pragmas behave the same as production (design §16.1).
    """
    db_path = tmp_path / "runtime.sqlite"
    conn = open_connection(db_path)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def store(runtime_db: sqlite3.Connection) -> SqliteRuntimeStore:
    """Fresh ``SqliteRuntimeStore`` backed by an isolated database."""
    return SqliteRuntimeStore(runtime_db)


@pytest.fixture
def sample_scope() -> RequestScope:
    """Default request scope for tests (design §33.3)."""
    return RequestScope(
        tenant_id="test-tenant",
        principal_id="test-principal",
        agent_id="test-agent",
        workspace_id="test-workspace",
    )


@pytest.fixture
def make_inbound_envelope():
    """Factory for :class:`InboundTaskEnvelope` with sane defaults.

    Each call produces a fresh envelope; tests override individual fields
    via keyword arguments. ``channel_message_id`` defaults to ``None`` so
    tests must opt in to channel-message dedup explicitly.
    """

    def _make(
        scope: RequestScope,
        *,
        session_key: str = "test-session",
        channel: str | None = "websocket",
        channel_account: str | None = "test-account",
        channel_message_id: str | None = None,
        dedup_key: str | None = None,
        task_kind: str = "USER_TURN",
        priority: int = 100,
        payload_hash: str | None = None,
        normalized_payload_ref: str = "inline:test-payload",
        received_at_ms: int = 1_000_000,
        available_at_ms: int | None = None,
        turn_id: str | None = None,
        media_refs: tuple[MediaRef, ...] = (),
    ) -> InboundTaskEnvelope:
        return InboundTaskEnvelope(
            protocol_version=1,
            task_kind=task_kind,  # type: ignore[arg-type]
            priority=priority,
            scope=scope,
            session_key=session_key,
            channel=channel,
            channel_account=channel_account,
            channel_message_id=channel_message_id,
            dedup_key=dedup_key,
            normalized_payload_ref=normalized_payload_ref,
            payload_hash=payload_hash or _hash_str("default-payload"),
            media_refs=media_refs,
            received_at_ms=received_at_ms,
            available_at_ms=available_at_ms,
            turn_id=turn_id,
        )

    return _make


@pytest.fixture
def make_internal_envelope():
    """Factory for :class:`InternalTaskEnvelope` with sane defaults."""

    def _make(
        scope: RequestScope,
        *,
        session_key: str = "internal:test-agent",
        dedup_key: str = "internal-dedup-1",
        task_kind: str = "MAINTENANCE",
        priority: int = 10,
        payload_hash: str | None = None,
        normalized_payload_ref: str = "inline:internal-payload",
        received_at_ms: int = 1_000_000,
        available_at_ms: int | None = None,
    ) -> InternalTaskEnvelope:
        return InternalTaskEnvelope(
            protocol_version=1,
            task_kind=task_kind,  # type: ignore[arg-type]
            priority=priority,
            scope=scope,
            session_key=session_key,
            dedup_key=dedup_key,
            normalized_payload_ref=normalized_payload_ref,
            payload_hash=payload_hash or _hash_str("default-internal"),
            received_at_ms=received_at_ms,
            available_at_ms=available_at_ms,
        )

    return _make


@pytest.fixture
def claim_and_run(store, sample_scope, make_inbound_envelope):
    """Helper: submit, claim, and mark-running a single task.

    Returns ``(record, claim)`` for the running task. Reduces boilerplate
    in worker-ledger tests that need a task already in ``RUNNING`` state.
    """

    def _claim_and_run(
        *,
        worker_id: str = "test-worker",
        now_ms: int = 2_000_000,
        lease_ms: int = 60_000,
        **envelope_kwargs: Any,
    ) -> tuple[Any, Any]:
        from miniunicorn.runtime.contracts import ClaimRequest

        env = make_inbound_envelope(sample_scope, **envelope_kwargs)
        submit = store.submit_task(env)
        assert submit.status == "ACCEPTED"

        result = store.claim_next(
            ClaimRequest(worker_id=worker_id, now_ms=now_ms, lease_ms=lease_ms)
        )
        assert result.claimed is not None
        record = store.mark_running(result.claimed.claim, now_ms=now_ms + 1)
        return record, result.claimed.claim

    return _claim_and_run


def _hash_str(text: str) -> str:
    """Stable SHA-256 hex of a string (for test payload hashes)."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
