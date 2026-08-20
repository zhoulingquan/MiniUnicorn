"""Narrow write facade over ``SessionManager`` (Phase 6 write-point convergence).

All external session write points (command/tools/webui/gateway) route their
``SessionManager.save`` calls through :class:`SessionWriteService` so session
persistence is reached only via ``SessionManager`` internals plus this narrow
interface.  Declared exception: ``agent.memory`` Consolidator writes directly
(see ``docs/architecture/module-boundaries.md``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from miniunicorn.session.manager import Session, SessionManager


class SessionWriteService:
    """Narrow session write-point facade.

    Methods mirror the small set of save-with-mutation patterns used by the
    production call sites, keeping the underlying ``SessionManager`` API intact.
    """

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager

    def persist(self, session: Session, *, fsync: bool = False) -> None:
        """Persist an already-mutated session."""
        self._manager.save(session, fsync=fsync)

    def record_message(
        self,
        session: Session,
        role: str,
        content: str,
        **extra: Any,
    ) -> None:
        """Append a message to a session and persist it."""
        session.add_message(role, content, **extra)
        self._manager.save(session)

    def update_metadata(
        self,
        session: Session,
        updates: dict[str, Any],
        *,
        discard: set[str] | None = None,
    ) -> None:
        """Apply metadata updates, optionally drop legacy keys, and persist."""
        session.metadata.update(updates)
        if discard:
            for key in discard:
                session.metadata.pop(key, None)
        self._manager.save(session)

    def trim_to_recent(self, session: Session, keep: int) -> None:
        """Trim a session to its recent legal suffix and persist."""
        session.retain_recent_legal_suffix(keep)
        self._manager.save(session)

    def reset(self, session: Session) -> None:
        """Clear a session, persist the empty state, and invalidate the cache entry."""
        session.clear()
        self._manager.save(session)
        self._manager.invalidate(session.key)
