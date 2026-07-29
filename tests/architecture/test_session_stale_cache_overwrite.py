"""WP0/WP2 — Stale two-``SessionManager`` overwrite bug characterization.

Design §2.1 lists "two ``SessionManager`` instances can overwrite each other
from stale caches" as a failure mode the durable runtime must correct
(acceptance #11). This file characterizes the legacy ``save()`` bug and
verifies that WP2's ``commit_turn()`` fixes it.

The legacy bug: each ``SessionManager`` owns its own ``_cache`` and
per-instance ``_save_locks``. Two managers pointing at the same sessions
directory can each hold a stale snapshot of the same session key. When
both call ``save()``, the later write silently overwrites the earlier one.

WP2 fix: ``commit_turn()`` acquires an OS-visible per-session file lock,
reloads from disk, checks ``base_revision``, and applies the mutation
atomically. A stale writer gets ``REVISION_CONFLICT`` instead of silently
overwriting.
"""

from __future__ import annotations

from pathlib import Path

from miniunicorn.session.manager import SessionManager


def test_two_managers_with_separate_caches_lose_writes(tmp_path: Path) -> None:
    """Legacy ``save()`` still has the stale-cache overwrite bug (design §2.1).

    The legacy ``save()`` path uses per-instance ``threading.Lock`` and does
    not check revisions. Two managers can overwrite each other. This is
    intentional during the migration window — only ``commit_turn()`` is
    safe for durable runtime tasks.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager_a = SessionManager(workspace)
    manager_b = SessionManager(workspace)
    key = "websocket:test:1"

    session_a = manager_a.get_or_create(key)
    session_b = manager_b.get_or_create(key)

    assert session_a is not session_b, "managers must hold distinct Session instances"

    session_a.add_message("user", "message-from-A")
    manager_a.save(session_a)

    session_b.add_message("user", "message-from-B")
    manager_b.save(session_b)

    # Reload via a third manager, bypassing cache.
    manager_c = SessionManager(workspace)
    manager_c._cache.pop(key, None)  # noqa: SLF001 — characterization hook
    session_c = manager_c.get_or_create(key)

    persisted_contents = [m.get("content") for m in session_c.messages]
    assert "message-from-B" in persisted_contents, "B's write must be on disk"
    assert "message-from-A" not in persisted_contents, (
        "Legacy save() still loses writes — this is expected until the "
        "runtime switches all durable writes to commit_turn()."
    )


def test_commit_turn_prevents_stale_cache_overwrite(tmp_path: Path) -> None:
    """``commit_turn()`` rejects stale writes via revision conflict (WP2).

    Design §21.2, acceptance #11. Manager A commits an INBOUND message.
    Manager B (holding a stale base_revision=0) tries to commit and gets
    ``REVISION_CONFLICT``. A's write survives.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager_a = SessionManager(workspace)
    manager_b = SessionManager(workspace)
    key = "websocket:test:commit:1"

    # Both start from revision 0 (empty session).
    outcome_a = manager_a.commit_turn(
        session_key=key,
        commit_id="commit-a-inbound",
        commit_kind="INBOUND",
        base_revision=0,
        mutation_messages=[{"role": "user", "content": "message-from-A"}],
        mutation_metadata_updates={},
        content_hash="hash-a",
    )
    assert outcome_a.state == "COMMITTED", f"A should succeed: {outcome_a}"
    assert outcome_a.revision == 1

    # B tries to commit with stale base_revision=0. Disk is now at revision 1.
    outcome_b = manager_b.commit_turn(
        session_key=key,
        commit_id="commit-b-inbound",
        commit_kind="INBOUND",
        base_revision=0,
        mutation_messages=[{"role": "user", "content": "message-from-B"}],
        mutation_metadata_updates={},
        content_hash="hash-b",
    )
    assert outcome_b.state == "REVISION_CONFLICT", (
        f"B should get REVISION_CONFLICT, got {outcome_b}"
    )
    assert outcome_b.revision == 1, "B should see the current disk revision"

    # A's write survives.
    snapshot = manager_a.load_fresh(key)
    assert snapshot.revision == 1
    assert any(m.get("content") == "message-from-A" for m in snapshot.messages)
    assert not any(m.get("content") == "message-from-B" for m in snapshot.messages)


def test_two_managers_have_independent_per_session_locks(tmp_path: Path) -> None:
    """Per-instance ``_save_locks`` cannot serialize cross-process writes.

    Documents why a per-instance ``threading.Lock`` is insufficient for the
    durable runtime: two managers create distinct lock objects for the same
    session key, so they cannot mutually exclude each other. WP2 adds
    ``_OSFileLock`` for the ``commit_turn()`` path.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager_a = SessionManager(workspace)
    manager_b = SessionManager(workspace)
    key = "websocket:test:2"

    manager_a.get_or_create(key)
    manager_b.get_or_create(key)

    lock_a = manager_a._get_save_lock(key)  # noqa: SLF001 — characterization
    lock_b = manager_b._get_save_lock(key)  # noqa: SLF001 — characterization

    assert lock_a is not lock_b, (
        "Per-instance save locks are not shared across SessionManager "
        "instances. commit_turn() uses _OSFileLock for cross-process safety."
    )


def test_session_has_revision_field() -> None:
    """``Session`` has a persisted revision counter (WP2, design §21.1).

    The revision field enables optimistic concurrency control in
    ``commit_turn()``. Legacy files without this field load as revision=0.
    """
    from miniunicorn.session.manager import Session

    fields = {f.name for f in Session.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert "revision" in fields, (
        "Session must have a 'revision' field for WP2 optimistic concurrency"
    )
