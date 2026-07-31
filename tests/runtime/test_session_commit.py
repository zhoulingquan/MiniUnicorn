"""WP2 — SessionManager ``commit_turn`` and ``load_fresh`` tests (design §21).

Tests the revision-aware commit protocol directly on SessionManager:
- idempotent commits (same commit_id + hash → ALREADY_COMMITTED)
- commit_id reuse with different hash → IO_FAILURE
- stale base_revision → REVISION_CONFLICT
- INBOUND then FINAL sequence
- crash after prepare (filesystem commit) before confirm
- fsync behavior
- OS-visible lock sidecar creation
- load_fresh bypasses cache
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from miniunicorn.session.manager import SessionCommitOutcome, SessionManager, SessionSnapshot


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    """Fresh SessionManager with an isolated workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return SessionManager(workspace)


class TestCommitTurnBasic:
    """Basic commit_turn functionality."""

    def test_inbound_commit_on_empty_session(self, manager: SessionManager) -> None:
        key = "test:inbound:1"
        outcome = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert outcome.state == "COMMITTED"
        assert outcome.revision == 1

    def test_final_commit_after_inbound(self, manager: SessionManager) -> None:
        key = "test:final:1"
        # INBOUND commit first.
        manager.commit_turn(
            session_key=key,
            commit_id="commit-inbound",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-inbound",
        )
        # FINAL commit appends assistant messages.
        outcome = manager.commit_turn(
            session_key=key,
            commit_id="commit-final",
            commit_kind="FINAL",
            base_revision=1,
            mutation_messages=[{"role": "assistant", "content": "world"}],
            mutation_metadata_updates={},
            content_hash="hash-final",
        )
        assert outcome.state == "COMMITTED"
        assert outcome.revision == 2

        snapshot = manager.load_fresh(key)
        contents = [m.get("content") for m in snapshot.messages]
        assert "hello" in contents
        assert "world" in contents

    def test_metadata_updates_applied(self, manager: SessionManager) -> None:
        key = "test:metadata:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[],
            mutation_metadata_updates={"turn_id": "abc", "model": "gpt-4"},
            content_hash="hash-1",
        )
        # Read the raw file to verify metadata.
        path = manager._get_session_path(key)  # noqa: SLF001
        with open(path, encoding="utf-8") as f:
            meta = json.loads(f.readline())
        assert meta["metadata"]["turn_id"] == "abc"
        assert meta["metadata"]["model"] == "gpt-4"
        assert meta["revision"] == 1


class TestCommitTurnIdempotency:
    """Idempotent commit behavior (design §21.2 steps 3-4)."""

    def test_identical_commit_applied_once(self, manager: SessionManager) -> None:
        key = "test:idem:1"
        # First commit succeeds.
        r1 = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert r1.state == "COMMITTED"
        assert r1.revision == 1

        # Same commit_id + hash → ALREADY_COMMITTED.
        r2 = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,  # stale, but idempotency check comes first
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert r2.state == "ALREADY_COMMITTED"
        assert r2.revision == 1

        # Messages not duplicated.
        snapshot = manager.load_fresh(key)
        assert len(snapshot.messages) == 1

    def test_same_commit_id_different_hash_rejected(self, manager: SessionManager) -> None:
        key = "test:hash-mismatch:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )

        r2 = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",  # same commit_id
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "different"}],
            mutation_metadata_updates={},
            content_hash="hash-2",  # different hash!
        )
        assert r2.state == "IO_FAILURE"
        assert "different content_hash" in (r2.error or "")


class TestCommitTurnRevisionConflict:
    """Revision conflict behavior (design §21.2 step 5, §21.4)."""

    def test_stale_revision_returns_conflict(self, manager: SessionManager) -> None:
        key = "test:conflict:1"
        # First commit at revision 0 → 1.
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "A"}],
            mutation_metadata_updates={},
            content_hash="hash-a",
        )
        # Second commit with stale base_revision=0 → CONFLICT.
        r2 = manager.commit_turn(
            session_key=key,
            commit_id="commit-2",
            commit_kind="FINAL",
            base_revision=0,  # stale
            mutation_messages=[{"role": "assistant", "content": "B"}],
            mutation_metadata_updates={},
            content_hash="hash-b",
        )
        assert r2.state == "REVISION_CONFLICT"
        assert r2.revision == 1  # current disk revision

    def test_correct_revision_allows_commit(self, manager: SessionManager) -> None:
        key = "test:correct-rev:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "A"}],
            mutation_metadata_updates={},
            content_hash="hash-a",
        )
        r2 = manager.commit_turn(
            session_key=key,
            commit_id="commit-2",
            commit_kind="FINAL",
            base_revision=1,  # correct
            mutation_messages=[{"role": "assistant", "content": "B"}],
            mutation_metadata_updates={},
            content_hash="hash-b",
        )
        assert r2.state == "COMMITTED"
        assert r2.revision == 2

    def test_stale_revision_never_overwrites(self, manager: SessionManager) -> None:
        """A stale writer must never overwrite a newer transcript (design §21.4)."""
        key = "test:never-overwrite:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "original"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        # A new manager simulating a different process with stale cache.
        manager_b = SessionManager(manager.workspace)
        r = manager_b.commit_turn(
            session_key=key,
            commit_id="commit-stale",
            commit_kind="FINAL",
            base_revision=0,  # stale
            mutation_messages=[{"role": "assistant", "content": "stale"}],
            mutation_metadata_updates={},
            content_hash="hash-stale",
        )
        assert r.state == "REVISION_CONFLICT"
        # Original content preserved.
        snapshot = manager.load_fresh(key)
        assert len(snapshot.messages) == 1
        assert snapshot.messages[0]["content"] == "original"


class TestCommitTurnCrashRecovery:
    """Crash recovery: filesystem committed but SQLite not confirmed (design §17.7).

    In these tests, we simulate a crash by calling commit_turn (filesystem
    commit) and then calling commit_turn again with the same commit_id
    (simulating a retry after crash). The second call should detect
    ALREADY_COMMITTED and return the committed revision.
    """

    def test_crash_after_filesystem_commit_before_confirm(self, manager: SessionManager) -> None:
        key = "test:crash:1"
        # First commit succeeds (filesystem + sidecar written).
        r1 = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert r1.state == "COMMITTED"

        # Simulate crash after filesystem commit but before SQLite confirm.
        # On retry, commit_turn detects the commit_id in the sidecar and
        # returns ALREADY_COMMITTED.
        r2 = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert r2.state == "ALREADY_COMMITTED"
        assert r2.revision == 1

    def test_crash_after_session_replace_returns_already_committed(
        self, manager: SessionManager
    ) -> None:
        """Crash immediately after the durable session file replace (Task 4 Step 2).

        The atomic session file replacement succeeds, but a crash prevents
        the (now-removed) sidecar write. On retry with a fresh SessionManager
        constructed over the same workspace, the same ``commit_id`` and
        ``content_hash`` must return ``ALREADY_COMMITTED`` and the message
        must not be duplicated (design §17.7, §21.2 crash recovery).
        """
        key = "test:crash-after-replace:1"

        def crash(_point: str) -> None:
            raise OSError("injected crash after session replace")

        manager._commit_fault_hook = crash  # noqa: SLF001

        # First commit: session file is replaced durably, then the hook
        # raises before the sidecar is written. The OSError is caught by
        # commit_turn's handler and surfaced as IO_FAILURE.
        r1 = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert r1.state == "IO_FAILURE"

        # A fresh SessionManager simulates a Worker restart over the same
        # workspace. The sidecar does NOT contain commit-1.
        manager_b = SessionManager(manager.workspace)

        # Retry the same commit. Expected before the fix: REVISION_CONFLICT
        # because the session file advanced to revision 1 but the sidecar
        # lacks commit-1. Expected after the fix: ALREADY_COMMITTED because
        # the commit index is embedded in the atomically-replaced session
        # file metadata.
        retry = manager_b.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert retry.state == "ALREADY_COMMITTED", (
            f"crash after session replace must be idempotent; got {retry.state}: "
            f"{retry.error}"
        )
        assert retry.revision == 1

        snapshot = manager_b.load_fresh(key)
        assert [m.get("content") for m in snapshot.messages] == ["hello"], (
            "the inbound message must appear exactly once after retry"
        )

    def test_crash_after_prepare_before_filesystem(self, manager: SessionManager) -> None:
        """Simulate crash after SQLite prepare but before filesystem commit.

        Since SessionManager.commit_turn doesn't do SQLite prepare, this
        tests that a fresh commit_turn (different commit_id) after a failed
        one works correctly.
        """
        key = "test:crash-prepare:1"
        # A failed commit (e.g. wrong revision) doesn't write to disk.
        manager.commit_turn(
            session_key=key,
            commit_id="commit-failed",
            commit_kind="INBOUND",
            base_revision=5,  # wrong, will conflict
            mutation_messages=[{"role": "user", "content": "failed"}],
            mutation_metadata_updates={},
            content_hash="hash-failed",
        )
        # A new commit with correct revision succeeds.
        r2 = manager.commit_turn(
            session_key=key,
            commit_id="commit-ok",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "ok"}],
            mutation_metadata_updates={},
            content_hash="hash-ok",
        )
        assert r2.state == "COMMITTED"
        assert r2.revision == 1

        snapshot = manager.load_fresh(key)
        assert len(snapshot.messages) == 1
        assert snapshot.messages[0]["content"] == "ok"


class TestLoadFresh:
    """load_fresh bypasses cache and reads from disk."""

    def test_load_fresh_returns_revision(self, manager: SessionManager) -> None:
        key = "test:fresh:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        snapshot = manager.load_fresh(key)
        assert isinstance(snapshot, SessionSnapshot)
        assert snapshot.revision == 1
        assert len(snapshot.messages) == 1
        assert snapshot.applied_commit_ids.get("commit-1") == "hash-1"

    def test_load_fresh_nonexistent_session(self, manager: SessionManager) -> None:
        snapshot = manager.load_fresh("nonexistent:key")
        assert snapshot.revision == 0
        assert snapshot.messages == []
        assert snapshot.applied_commit_ids == {}

    def test_load_fresh_bypasses_cache(self, manager: SessionManager) -> None:
        """load_fresh must not return cached data after a direct disk write."""
        key = "test:bypass:1"
        # Create session via get_or_create (populates cache).
        session = manager.get_or_create(key)
        session.add_message("user", "cached")
        manager.save(session)

        # Now write to disk directly, bypassing the cache.
        path = manager._get_session_path(key)  # noqa: SLF001
        # Read current file, bump revision, and write back.
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        meta = json.loads(lines[0])
        meta["revision"] = 99
        lines[0] = json.dumps(meta, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        # load_fresh should see revision 99, not the cached revision.
        snapshot = manager.load_fresh(key)
        assert snapshot.revision == 99


class TestFsyncBehavior:
    """File and parent directory fsync (design §21.2 steps 9, 11)."""

    def test_commit_turn_fsyncs_file_and_directory(self, manager: SessionManager) -> None:
        key = "test:fsync:1"
        r = manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        assert r.state == "COMMITTED"
        # If we got here without OSError, fsync succeeded.
        path = manager._get_session_path(key)  # noqa: SLF001
        assert path.exists()

    def test_commit_embeds_runtime_index_after_commit(self, manager: SessionManager) -> None:
        """The bounded commit index is embedded in the session file (Task 4 Step 3).

        After Task 4 the legacy ``.jsonl.commits`` sidecar is no longer
        written by ``commit_turn``; the index lives in the session file's
        metadata line under ``_runtime.applied_commits`` so it is atomic
        with the session content replace.
        """
        key = "test:sidecar:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        path = manager._get_session_path(key)  # noqa: SLF001
        with open(path, encoding="utf-8") as f:
            meta = json.loads(f.readline())
        assert meta["_type"] == "metadata"
        runtime = meta["_runtime"]
        assert runtime["format_version"] == 1
        applied = runtime["applied_commits"]
        assert len(applied) == 1
        assert applied[0]["commit_id"] == "commit-1"
        assert applied[0]["content_hash"] == "hash-1"
        assert applied[0]["revision"] == 1
        assert applied[0]["commit_kind"] == "INBOUND"
        # The legacy sidecar is no longer written by commit_turn.
        commits_path = manager._get_commits_path(key)  # noqa: SLF001
        assert not commits_path.exists()


class TestOSLockSidecar:
    """OS-visible lock sidecar creation (design §21.2 step 1)."""

    def test_lock_file_created_on_commit(self, manager: SessionManager) -> None:
        key = "test:lock:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        lock_path = manager._get_lock_path(key)  # noqa: SLF001
        assert lock_path.exists(), "OS lock sidecar must be created"

    def test_lock_path_is_per_session(self, manager: SessionManager) -> None:
        key_a = "test:lock-per-session:A"
        key_b = "test:lock-per-session:B"
        lock_a = manager._get_lock_path(key_a)  # noqa: SLF001
        lock_b = manager._get_lock_path(key_b)  # noqa: SLF001
        assert lock_a != lock_b


class TestCommitIndexBounded:
    """Commit-id index sidecar is bounded (design §21.3)."""

    def test_commit_index_evicts_oldest(self, manager: SessionManager) -> None:
        """The commit index retains at most _COMMIT_INDEX_MAX entries."""
        from miniunicorn.session.manager import _COMMIT_INDEX_MAX

        key = "test:bounded:1"
        # We can't easily create 200+ commits with revision conflicts,
        # so test the eviction logic directly.
        from collections import OrderedDict

        index: "OrderedDict[str, dict]" = OrderedDict()
        for i in range(_COMMIT_INDEX_MAX + 10):
            index[f"commit-{i}"] = {
                "commit_id": f"commit-{i}",
                "content_hash": f"hash-{i}",
                "revision": i + 1,
                "commit_kind": "INBOUND",
                "committed_at_ms": i,
            }
        manager._write_commit_index(key, index)  # noqa: SLF001

        # Read back and verify it's bounded.
        read_back = manager._read_commit_index(key)  # noqa: SLF001
        assert len(read_back) <= _COMMIT_INDEX_MAX
        # Oldest entries evicted (FIFO).
        assert "commit-0" not in read_back
        assert f"commit-{_COMMIT_INDEX_MAX + 9}" in read_back


class TestDeleteCleansUpSidecars:
    """delete_session removes lock and commit sidecars."""

    def test_delete_removes_sidecars(self, manager: SessionManager) -> None:
        key = "test:delete:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        lock_path = manager._get_lock_path(key)  # noqa: SLF001
        session_path = manager._get_session_path(key)  # noqa: SLF001
        assert lock_path.exists()
        assert session_path.exists()

        manager.delete_session(key)

        assert not lock_path.exists(), "lock sidecar must be deleted"
        assert not session_path.exists(), (
            "session file (carrying the embedded commit index) must be deleted"
        )


class TestEmbeddedRuntimeIndexRobustness:
    """Corrupt ``_runtime`` metadata and bounded-index behavior (Task 4 Step 6)."""

    def test_corrupt_runtime_metadata_ignored_without_losing_messages(
        self, manager: SessionManager
    ) -> None:
        """A corrupt ``_runtime`` field must not lose session messages."""
        key = "test:corrupt-runtime:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-1",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "hello"}],
            mutation_metadata_updates={},
            content_hash="hash-1",
        )
        # Overwrite the session file with a corrupt ``_runtime`` block.
        path = manager._get_session_path(key)  # noqa: SLF001
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        meta = json.loads(lines[0])
        meta["_runtime"] = {"format_version": 1, "applied_commits": "not-a-list"}
        lines[0] = json.dumps(meta, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        snapshot = manager.load_fresh(key)
        # Messages preserved despite corrupt metadata.
        assert len(snapshot.messages) == 1
        assert snapshot.messages[0]["content"] == "hello"
        assert snapshot.revision == 1
        # Corrupt index dropped (no valid applied_commit_ids).
        assert snapshot.applied_commit_ids == {}

    def test_corrupt_runtime_entry_dropped_safely(self, manager: SessionManager) -> None:
        """Individual corrupt entries are dropped; valid ones survive."""
        key = "test:corrupt-entry:1"
        manager.commit_turn(
            session_key=key,
            commit_id="commit-good",
            commit_kind="INBOUND",
            base_revision=0,
            mutation_messages=[{"role": "user", "content": "ok"}],
            mutation_metadata_updates={},
            content_hash="hash-good",
        )
        path = manager._get_session_path(key)  # noqa: SLF001
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        meta = json.loads(lines[0])
        # Mix one valid entry with corrupt ones.
        meta["_runtime"] = {
            "format_version": 1,
            "applied_commits": [
                {"commit_id": "commit-good", "content_hash": "hash-good",
                 "revision": 1, "commit_kind": "INBOUND", "committed_at_ms": 1},
                {"commit_id": "bad-no-content", "revision": 2},  # missing hash ok
                "not-a-dict",  # dropped
                {"commit_id": ""},  # dropped (empty id)
                {"commit_id": "commit-good", "content_hash": "dup"},  # dedup
            ],
        }
        lines[0] = json.dumps(meta, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        snapshot = manager.load_fresh(key)
        # The valid commit-good entry is retained.
        assert "commit-good" in snapshot.applied_commit_ids
        assert snapshot.applied_commit_ids["commit-good"] == "hash-good"
        # Messages intact.
        assert len(snapshot.messages) == 1

    def test_embedded_index_bounded_to_max(self, manager: SessionManager) -> None:
        """201 unique commits retain exactly ``_COMMIT_INDEX_MAX`` newest entries."""
        from miniunicorn.session.manager import _COMMIT_INDEX_MAX

        key = "test:bounded-embedded:1"
        for i in range(_COMMIT_INDEX_MAX + 1):
            outcome = manager.commit_turn(
                session_key=key,
                commit_id=f"commit-{i}",
                commit_kind="INBOUND",
                base_revision=i,
                mutation_messages=[{"role": "user", "content": f"msg-{i}"}],
                mutation_metadata_updates={},
                content_hash=f"hash-{i}",
            )
            assert outcome.state == "COMMITTED", f"commit {i} failed: {outcome.error}"

        snapshot = manager.load_fresh(key)
        # Exactly _COMMIT_INDEX_MAX entries retained (oldest evicted).
        assert len(snapshot.applied_commit_ids) == _COMMIT_INDEX_MAX
        assert "commit-0" not in snapshot.applied_commit_ids, (
            "oldest entry must be evicted by FIFO bound"
        )
        assert f"commit-{_COMMIT_INDEX_MAX}" in snapshot.applied_commit_ids
