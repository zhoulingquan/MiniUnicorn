"""WP2 — SessionCommitter and SessionCommitLedger tests (design §17.7).

Tests the prepare/apply/confirm coordinator and the SQLite SessionCommitLedger:
- prepare_session_commit inserts PREPARED row (idempotent)
- confirm_session_commit marks COMMITTED (idempotent)
- mark_session_conflict records CONFLICT
- read_session_commit retrieves rows
- SessionCommitter end-to-end: prepare → filesystem commit → confirm
- crash recovery: retry after crash at each step
- revision conflict propagated from SessionManager to SQLite
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from miniunicorn.agent.ports import (
    SafeError,
    SessionCommitRequest,
    SessionMutation,
)
from miniunicorn.runtime.contracts import SessionCommitMismatchError
from miniunicorn.runtime.models import SessionCommitWrite
from miniunicorn.runtime.session_committer import (
    SessionCommitter,
    clear_active_claim,
    set_active_claim,
)
from miniunicorn.runtime.sqlite import SqliteRuntimeStore
from miniunicorn.session.manager import SessionManager


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def session_manager(workspace: Path) -> SessionManager:
    return SessionManager(workspace)


@pytest.fixture
def committer(store: SqliteRuntimeStore, session_manager: SessionManager) -> SessionCommitter:
    return SessionCommitter(store, session_manager)


def _make_mutation(messages: list[dict], metadata: dict | None = None) -> SessionMutation:
    return SessionMutation(messages=messages, metadata_updates=metadata or {})


def _content_hash(mutation: SessionMutation) -> str:
    import json

    payload = json.dumps(
        {"messages": mutation.messages, "metadata_updates": mutation.metadata_updates},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _derive_commit_id(task_id: str, commit_kind: str) -> str:
    return hashlib.sha256(f"{task_id}:{commit_kind}".encode("utf-8")).hexdigest()


class TestSessionCommitLedger:
    """Tests for SqliteRuntimeStore SessionCommitLedger methods."""

    def test_prepare_inserts_prepared_row(
        self, store: SqliteRuntimeStore, claim_and_run
    ) -> None:
        record, claim = claim_and_run()
        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        result = store.prepare_session_commit(claim, write)
        assert result.state == "PREPARED"
        assert result.session_commit_id == "commit-1"
        assert result.base_revision == 0
        assert result.target_revision == 1

    def test_prepare_is_idempotent(
        self, store: SqliteRuntimeStore, claim_and_run
    ) -> None:
        record, claim = claim_and_run()
        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        r1 = store.prepare_session_commit(claim, write)
        r2 = store.prepare_session_commit(claim, write)
        assert r1.session_commit_id == r2.session_commit_id
        assert r2.state == "PREPARED"

    def test_prepare_rejects_mismatched_retry(
        self, store: SqliteRuntimeStore, claim_and_run
    ) -> None:
        """A retry with different immutable fields must be rejected (Task 3 Step 7)."""
        record, claim = claim_and_run()
        write1 = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        store.prepare_session_commit(claim, write1)

        write2 = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="different-hash",  # changed
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        with pytest.raises(SessionCommitMismatchError) as exc_info:
            store.prepare_session_commit(claim, write2)
        assert "content_hash" in exc_info.value.field_names

    def test_confirm_marks_committed(
        self, store: SqliteRuntimeStore, claim_and_run
    ) -> None:
        record, claim = claim_and_run()
        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        store.prepare_session_commit(claim, write)
        result = store.confirm_session_commit(claim, "commit-1", 1, 2_000_000)
        assert result.state == "COMMITTED"
        assert result.committed_at_ms == 2_000_000

    def test_confirm_is_idempotent(
        self, store: SqliteRuntimeStore, claim_and_run
    ) -> None:
        record, claim = claim_and_run()
        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        store.prepare_session_commit(claim, write)
        r1 = store.confirm_session_commit(claim, "commit-1", 1, 2_000_000)
        r2 = store.confirm_session_commit(claim, "commit-1", 1, 3_000_000)
        assert r1.state == "COMMITTED"
        assert r2.state == "COMMITTED"
        # First committed_at_ms is preserved.
        assert r2.committed_at_ms == 2_000_000

    def test_mark_conflict(self, store: SqliteRuntimeStore, claim_and_run) -> None:
        record, claim = claim_and_run()
        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        store.prepare_session_commit(claim, write)
        result = store.mark_session_conflict(
            claim, "commit-1", SafeError("SESSION_REVISION_CONFLICT", "conflict")
        )
        assert result.state == "CONFLICT"
        assert result.error_code == "SESSION_REVISION_CONFLICT"

    def test_read_session_commit(self, store: SqliteRuntimeStore, claim_and_run) -> None:
        record, claim = claim_and_run()
        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="FINAL",
            base_revision=1,
            target_revision=2,
            content_hash="hash-final",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-final",
        )
        store.prepare_session_commit(claim, write)
        result = store.read_session_commit(record.task_id, "FINAL")
        assert result is not None
        assert result.session_commit_id == "commit-final"
        assert result.commit_kind == "FINAL"

    def test_read_session_commit_not_found(self, store: SqliteRuntimeStore) -> None:
        result = store.read_session_commit("nonexistent", "INBOUND")
        assert result is None


class TestSessionCommitterEndToEnd:
    """End-to-end SessionCommitter tests: prepare → filesystem → confirm."""

    def test_successful_inbound_commit(
        self, committer: SessionCommitter, store: SqliteRuntimeStore, claim_and_run
    ) -> None:
        record, claim = claim_and_run()
        set_active_claim(record.task_id, claim)

        mutation = _make_mutation([{"role": "user", "content": "hello"}])
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=_derive_commit_id(record.task_id, "INBOUND"),
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            mutation=mutation,
            content_hash=_content_hash(mutation),
        )

        result = asyncio.run(committer.commit_turn(request))
        assert result.state == "COMMITTED"
        assert result.revision == 1

        # Verify SQLite has COMMITTED state.
        commit_record = store.read_session_commit(record.task_id, "INBOUND")
        assert commit_record is not None
        assert commit_record.state == "COMMITTED"

        clear_active_claim(record.task_id)

    def test_successful_final_commit_after_inbound(
        self,
        committer: SessionCommitter,
        store: SqliteRuntimeStore,
        session_manager: SessionManager,
        claim_and_run,
    ) -> None:
        record, claim = claim_and_run()
        set_active_claim(record.task_id, claim)

        # INBOUND commit first.
        inbound_mutation = _make_mutation([{"role": "user", "content": "hello"}])
        inbound_request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=_derive_commit_id(record.task_id, "INBOUND"),
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            mutation=inbound_mutation,
            content_hash=_content_hash(inbound_mutation),
        )
        asyncio.run(committer.commit_turn(inbound_request))

        # FINAL commit.
        final_mutation = _make_mutation([{"role": "assistant", "content": "world"}])
        final_request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=_derive_commit_id(record.task_id, "FINAL"),
            commit_kind="FINAL",
            base_revision=1,
            target_revision=2,
            mutation=final_mutation,
            content_hash=_content_hash(final_mutation),
        )
        result = asyncio.run(committer.commit_turn(final_request))
        assert result.state == "COMMITTED"
        assert result.revision == 2

        # Verify messages on disk.
        snapshot = session_manager.load_fresh(record.session_key)
        contents = [m.get("content") for m in snapshot.messages]
        assert "hello" in contents
        assert "world" in contents

        clear_active_claim(record.task_id)

    def test_crash_after_prepare_retry_completes(
        self,
        committer: SessionCommitter,
        store: SqliteRuntimeStore,
        session_manager: SessionManager,
        claim_and_run,
    ) -> None:
        """Simulate crash after SQLite prepare but before filesystem commit.

        On retry, the prepare is idempotent (returns existing PREPARED row),
        and the filesystem commit proceeds normally.
        """
        record, claim = claim_and_run()
        set_active_claim(record.task_id, claim)

        mutation = _make_mutation([{"role": "user", "content": "hello"}])
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=_derive_commit_id(record.task_id, "INBOUND"),
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            mutation=mutation,
            content_hash=_content_hash(mutation),
        )

        # Simulate: prepare succeeds, then crash before filesystem commit.
        from miniunicorn.runtime.models import SessionCommitWrite

        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash=_content_hash(mutation),
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id=_derive_commit_id(record.task_id, "INBOUND"),
        )
        store.prepare_session_commit(claim, write)

        # Now retry the full commit (simulating Worker restart).
        result = asyncio.run(committer.commit_turn(request))
        assert result.state == "COMMITTED"
        assert result.revision == 1

        clear_active_claim(record.task_id)

    def test_crash_after_filesystem_before_confirm(
        self,
        committer: SessionCommitter,
        store: SqliteRuntimeStore,
        session_manager: SessionManager,
        claim_and_run,
    ) -> None:
        """Simulate crash after filesystem commit but before SQLite confirm.

        On retry, commit_turn detects ALREADY_COMMITTED (from the sidecar),
        and the SQLite confirm is repeated.
        """
        record, claim = claim_and_run()
        set_active_claim(record.task_id, claim)

        commit_id = _derive_commit_id(record.task_id, "INBOUND")
        mutation = _make_mutation([{"role": "user", "content": "hello"}])
        content_hash = _content_hash(mutation)

        # Step 3: prepare in SQLite.
        from miniunicorn.runtime.models import SessionCommitWrite

        write = SessionCommitWrite(
            session_key=record.session_key,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash=content_hash,
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id=commit_id,
        )
        store.prepare_session_commit(claim, write)

        # Step 4: filesystem commit directly (simulating commit_turn success
        # but crash before step 5).
        session_manager.commit_turn(
            session_key=record.session_key,
            commit_id=commit_id,
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=mutation.messages,
            mutation_metadata_updates=mutation.metadata_updates,
            content_hash=content_hash,
        )

        # Now retry the full commit (simulating Worker restart).
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=commit_id,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            mutation=mutation,
            content_hash=content_hash,
        )
        result = asyncio.run(committer.commit_turn(request))
        assert result.state == "COMMITTED"
        assert result.revision == 1

        # SQLite should now be COMMITTED.
        commit_record = store.read_session_commit(record.task_id, "INBOUND")
        assert commit_record is not None
        assert commit_record.state == "COMMITTED"

        clear_active_claim(record.task_id)

    def test_revision_conflict_recorded_in_sqlite(
        self,
        committer: SessionCommitter,
        store: SqliteRuntimeStore,
        session_manager: SessionManager,
        claim_and_run,
    ) -> None:
        """Revision conflict is recorded as CONFLICT in SQLite (design §21.4)."""
        record, claim = claim_and_run()
        set_active_claim(record.task_id, claim)

        # Pre-write a commit to bump the revision.
        session_manager.commit_turn(
            session_key=record.session_key,
            commit_id="pre-existing-commit",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "existing"}],
            mutation_metadata_updates={},
            content_hash="hash-existing",
        )

        # Now try to commit with stale base_revision=0.
        mutation = _make_mutation([{"role": "user", "content": "new"}])
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=_derive_commit_id(record.task_id, "INBOUND"),
            commit_kind="INBOUND",
            base_revision=0,  # stale
            target_revision=1,
            mutation=mutation,
            content_hash=_content_hash(mutation),
        )
        result = asyncio.run(committer.commit_turn(request))
        assert result.state == "REVISION_CONFLICT"
        assert result.revision == 1  # current disk revision

        # SQLite should have CONFLICT state.
        commit_record = store.read_session_commit(record.task_id, "INBOUND")
        assert commit_record is not None
        assert commit_record.state == "CONFLICT"

        clear_active_claim(record.task_id)

    def test_no_active_claim_returns_io_failure(
        self, committer: SessionCommitter, claim_and_run
    ) -> None:
        """Without an active claim, commit_turn returns IO_FAILURE."""
        record, claim = claim_and_run()
        # Don't call set_active_claim.

        mutation = _make_mutation([{"role": "user", "content": "hello"}])
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=_derive_commit_id(record.task_id, "INBOUND"),
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            mutation=mutation,
            content_hash=_content_hash(mutation),
        )
        result = asyncio.run(committer.commit_turn(request))
        assert result.state == "IO_FAILURE"
        assert result.error is not None
        assert result.error.error_code == "NO_CLAIM_CONTEXT"

    def test_idempotent_full_commit_retry(
        self,
        committer: SessionCommitter,
        store: SqliteRuntimeStore,
        session_manager: SessionManager,
        claim_and_run,
    ) -> None:
        """Full retry of a completed commit is idempotent."""
        record, claim = claim_and_run()
        set_active_claim(record.task_id, claim)

        mutation = _make_mutation([{"role": "user", "content": "hello"}])
        content_hash = _content_hash(mutation)
        commit_id = _derive_commit_id(record.task_id, "INBOUND")
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=commit_id,
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            mutation=mutation,
            content_hash=content_hash,
        )

        # First full commit.
        r1 = asyncio.run(committer.commit_turn(request))
        assert r1.state == "COMMITTED"

        # Second full commit (idempotent retry — SQLite already COMMITTED).
        r2 = asyncio.run(committer.commit_turn(request))
        assert r2.state in ("COMMITTED", "ALREADY_COMMITTED")
        assert r2.revision == r1.revision

        # Messages not duplicated.
        snapshot = session_manager.load_fresh(record.session_key)
        assert len(snapshot.messages) == 1

        clear_active_claim(record.task_id)
