"""Task child-process containment (design §20.7, §24.6, WP4 task 9).

Every spawned process belongs to the task's containment group. On Worker
termination, cancellation, or hard timeout, the entire child tree is
terminated before the task lease is released.

This module provides the containment *abstraction* and a portable
PID-tracking implementation. The full OS-level containment
(Windows Job Object with kill-on-close, POSIX process-group with
parent-death) is wired in WP6's Supervisor (design §20.7, §24.6).

Public surface:

- :class:`ContainmentScope` — Protocol every scope must implement.
- :class:`ProcessContainmentScope` — PID-tracking scope that kills
  the entire child tree on :meth:`close`.
- :class:`NullContainmentScope` — no-op scope for tests and legacy
  non-durable paths.
- :func:`current_containment_scope` / :func:`bind_containment_scope` —
  context-var binding so tools can register spawned PIDs without
  explicit plumbing.
"""

from __future__ import annotations

import contextvars
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from loguru import logger

_IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ContainmentScope(Protocol):
    """Task-level child-process containment group (design §20.7)."""

    def register(self, pid: int) -> None:
        """Register a spawned child PID into the containment group."""
        ...

    def close(self) -> None:
        """Terminate the entire child tree and release resources.

        Idempotent: calling ``close`` more than once is a no-op.
        """
        ...


# ---------------------------------------------------------------------------
# Context-var binding
# ---------------------------------------------------------------------------

_containment_var: contextvars.ContextVar[ContainmentScope | None] = (
    contextvars.ContextVar("_containment_var", default=None)
)


def current_containment_scope() -> ContainmentScope | None:
    """Return the containment scope bound to the current task, or ``None``."""
    return _containment_var.get()


def bind_containment_scope(
    scope: ContainmentScope,
) -> contextvars.Token[ContainmentScope | None]:
    """Bind *scope* as the current task's containment group."""
    return _containment_var.set(scope)


def reset_containment_scope(
    token: contextvars.Token[ContainmentScope | None],
) -> None:
    """Reset the containment scope binding to its prior state."""
    _containment_var.reset(token)


# ---------------------------------------------------------------------------
# Null scope (tests / legacy)
# ---------------------------------------------------------------------------


class NullContainmentScope:
    """No-op containment scope for tests and legacy non-durable paths."""

    def register(self, pid: int) -> None:  # noqa: D401
        """No-op."""

    def close(self) -> None:
        """No-op."""


# ---------------------------------------------------------------------------
# PID-tracking scope
# ---------------------------------------------------------------------------


@dataclass
class ProcessContainmentScope:
    """Portable PID-tracking containment scope (design §20.7).

    Tracks every PID registered by tools that spawn subprocesses. On
    :meth:`close`, terminates the entire child tree:

    - **POSIX**: sends ``SIGTERM`` to each tracked PID, waits briefly,
      then sends ``SIGKILL`` to any survivor.
    - **Windows**: invokes ``taskkill /F /T /PID`` for each tracked PID
      to recursively force-terminate the subtree.

    This is a *cooperative* implementation — tools must call
    :meth:`register` for every spawned PID. The full OS-level
    containment (Job Object kill-on-close, process-group parent-death)
    is provided by WP6's Supervisor.
    """

    task_id: str = field(default="")
    _pids: set[int] = field(default_factory=set)
    _closed: bool = field(default=False)

    def register(self, pid: int) -> None:
        """Register a spawned child PID."""
        if self._closed:
            logger.warning(
                "containment: cannot register PID {} — scope already closed (task={})",
                pid,
                self.task_id,
            )
            return
        self._pids.add(pid)

    def close(self) -> None:
        """Terminate the entire child tree (idempotent)."""
        if self._closed:
            return
        self._closed = True
        pids = sorted(self._pids)
        if not pids:
            return
        logger.info(
            "containment: terminating {} child process(es) for task={}",
            len(pids),
            self.task_id,
        )
        if _IS_WINDOWS:
            self._kill_windows(pids)
        else:
            self._kill_posix(pids)
        self._pids.clear()

    # ------------------------------------------------------------------
    # Platform-specific kill
    # ------------------------------------------------------------------

    @staticmethod
    def _kill_windows(pids: list[int]) -> None:
        """Force-terminate each PID and its subtree via ``taskkill /F /T``."""
        import subprocess

        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("containment: taskkill failed for PID {}: {}", pid, exc)

    @staticmethod
    def _kill_posix(pids: list[int]) -> None:
        """Send SIGTERM then SIGKILL to each tracked PID."""
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # Already gone.
            except Exception as exc:  # noqa: BLE001
                logger.warning("containment: SIGTERM failed for PID {}: {}", pid, exc)

        # Brief grace period, then SIGKILL survivors.
        import time

        time.sleep(0.3)

        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Already gone.
            except Exception as exc:  # noqa: BLE001
                logger.warning("containment: SIGKILL failed for PID {}: {}", pid, exc)
