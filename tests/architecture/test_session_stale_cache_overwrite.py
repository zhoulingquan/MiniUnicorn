"""WP0 — Reproduce the stale two-``SessionManager`` overwrite bug.

Design §2.1 lists "two ``SessionManager`` instances can overwrite each other
from stale caches" as a failure mode the durable runtime must correct
(acceptance #11). This test reproduces the bug against the current
in-process cache so WP2 can prove it is fixed.

The bug: each ``SessionManager`` owns its own ``_cache`` and per-instance
``_save_locks``. Two managers pointing at the same sessions directory can
each hold a stale snapshot of the same session key. When both write, the
later write silently overwrites the earlier one — losing messages without
raising any error.
"""

from __future__ import annotations

from pathlib import Path

from miniunicorn.session.manager import SessionManager


def test_two_managers_with_separate_caches_lose_writes(tmp_path: Path) -> None:
    """Characterizes the stale-cache overwrite bug (design §2.1, acceptance #11).

    Today the bug is present: A's earlier write is silently lost when B
    saves from a stale cache, so this test PASSES. When WP2 adds a
    revision-aware ``commit_turn()`` that rejects the overwrite, A's write
    survives and this test FAILS — at which point it must be updated to
    assert the new contract.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager_a = SessionManager(workspace)
    manager_b = SessionManager(workspace)
    key = "websocket:test:1"

    # Both managers independently load the same (empty) session into their
    # own caches. Neither knows the other exists.
    session_a = manager_a.get_or_create(key)
    session_b = manager_b.get_or_create(key)

    assert session_a is not session_b, "managers must hold distinct Session instances"
    assert session_a.messages == [], "session A starts empty"
    assert session_b.messages == [], "session B starts empty"

    # Manager A appends a user message and saves. Disk now contains [from-A].
    session_a.add_message("user", "message-from-A")
    manager_a.save(session_a)

    # Manager B's cache is still the stale empty snapshot. It appends a
    # different message and saves, blissfully unaware of A's write.
    session_b.add_message("user", "message-from-B")
    manager_b.save(session_b)

    # Reload from disk through a third manager to see what actually persisted.
    manager_c = SessionManager(workspace)
    # Bypass the cache to force a fresh disk read.
    manager_c._cache.pop(key, None)  # noqa: SLF001 — characterization hook
    session_c = manager_c.get_or_create(key)

    persisted_contents = [m.get("content") for m in session_c.messages]

    # Bug characterization: B's later save overwrote A's earlier write
    # because B never reloaded the revision A had written.
    assert "message-from-B" in persisted_contents, "B's write must be on disk"
    assert "message-from-A" not in persisted_contents, (
        "A's earlier write survived the stale-cache overwrite — the bug has "
        "been fixed (WP2 progress). Update this characterization to assert "
        "the new revision-aware contract."
    )


def test_two_managers_have_independent_per_session_locks(tmp_path: Path) -> None:
    """Per-instance ``_save_locks`` cannot serialize cross-process writes.

    Documents why a per-instance threading.Lock is insufficient for the
    durable runtime: two managers create distinct lock objects for the same
    session key, so they cannot mutually exclude each other.
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
        "instances. WP2 must add an OS-visible per-session file lock."
    )


def test_session_has_no_revision_field_today() -> None:
    """``Session`` currently has no persisted revision counter.

    WP2 adds a revision field (design §21.1). This test pins the absence
    so the WP2 migration is detectable.
    """
    from miniunicorn.session.manager import Session

    fields = {f.name for f in Session.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert "revision" not in fields, (
        "Session now has a 'revision' field — WP2 has landed; update this "
        "characterization test to assert the new contract."
    )
