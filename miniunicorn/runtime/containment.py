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
from typing import Any, Protocol, runtime_checkable

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

    def register(self, pid: int, *, pgid: int | None = None) -> None:  # noqa: D401
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
    _pgids: set[int] = field(default_factory=set)
    _closed: bool = field(default=False)
    _term_grace_s: float = field(default=0.3)

    def register(self, pid: int, *, pgid: int | None = None) -> None:
        """Register a spawned child PID (and optional POSIX process-group id)."""
        if self._closed:
            logger.warning(
                "containment: cannot register PID {} — scope already closed (task={})",
                pid,
                self.task_id,
            )
            return
        self._pids.add(pid)
        # Task 10 Step 5: track PGIDs so close() can kill the entire
        # process group on POSIX, not just individual PIDs.
        if pgid is not None and not _IS_WINDOWS:
            self._pgids.add(pgid)

    def close(self) -> None:
        """Terminate the entire child tree (idempotent)."""
        if self._closed:
            return
        self._closed = True
        pids = sorted(self._pids)
        pgids = sorted(self._pgids)
        if not pids and not pgids:
            return
        logger.info(
            "containment: terminating {} child process(es) for task={}",
            len(pids),
            self.task_id,
        )
        if _IS_WINDOWS:
            self._kill_windows(pids)
        else:
            self._kill_posix(pids, pgids, self._term_grace_s)
        self._pids.clear()
        self._pgids.clear()

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

    @classmethod
    def _kill_posix(
        cls, pids: list[int], pgids: list[int], term_grace_s: float
    ) -> None:
        """Send SIGTERM to process groups, wait grace, then SIGKILL survivors.

        Task 10 Step 5: kill POSIX process groups when a PGID is available,
        falling back to individual PIDs only when no PGID exists.
        """
        import time

        # SIGTERM each registered process group.
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "containment: killpg SIGTERM failed for PGID {}: {}", pgid, exc
                )

        # Fall back to SIGTERM on individual PIDs that have no PGID.
        pgid_pids = set(pgids)
        for pid in pids:
            if pid in pgid_pids:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # Already gone.
            except Exception as exc:  # noqa: BLE001
                logger.warning("containment: SIGTERM failed for PID {}: {}", pid, exc)

        # Wait the grace period for processes to exit cleanly.
        deadline = time.monotonic() + term_grace_s
        while time.monotonic() < deadline and _any_process_group_exists(pgids):
            time.sleep(0.05)

        # SIGKILL any surviving process groups.
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "containment: killpg SIGKILL failed for PGID {}: {}", pgid, exc
                )

        # SIGKILL any surviving individual PIDs.
        for pid in pids:
            if pid in pgid_pids:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Already gone.
            except Exception as exc:  # noqa: BLE001
                logger.warning("containment: SIGKILL failed for PID {}: {}", pid, exc)


# ---------------------------------------------------------------------------
# Supervisor-level OS containment (design §20.7, §24.6, WP6 task 6)
# ---------------------------------------------------------------------------


def _process_group_exists(pgid: int) -> bool:
    """Return ``True`` if any process in the POSIX process group *pgid* exists.

    Uses ``os.killpg(pgid, 0)`` (signal 0 = existence probe). On Windows
    or when the call fails for any reason, returns ``False``.
    """
    if _IS_WINDOWS:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _any_process_group_exists(pgids: list[int]) -> bool:
    """Return ``True`` if any of the given POSIX process groups still has members."""
    return any(_process_group_exists(pgid) for pgid in pgids)


class SupervisorContainment:
    """OS-level containment for the Supervisor's entire child tree.

    The Supervisor must ensure that **all** descendants terminate when it
    dies (design §24.6):

    - **Windows**: assign every spawned child to a Job Object configured
      with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. When the Supervisor
      process exits (cleanly or via crash), the OS closes the Job handle
      and terminates the whole tree.
    - **POSIX**: spawn each child in a new process group via
      ``os.setsid`` in the child entrypoint, then track each PGID. On
      ``close``, signal the whole group. Additionally set
      ``PR_SET_PDEATHSIG`` (Linux only) so the OS sends a signal to a
      child whose parent dies.

    This complements :class:`ProcessContainmentScope`, which is the
    *per-task* cooperative PID tracker used by the Worker. This class is
    the *process-tree* OS-level containment used by the Supervisor.

    The implementation degrades gracefully when optional OS bindings
    (``pywin32``) are unavailable: PID-tracking fallback still terminates
    the known direct children, just not arbitrary grandchildren spawned
    outside the registry.
    """

    def __init__(self) -> None:
        self._pids: set[int] = set()
        self._pgids: set[int] = set()
        self._closed: bool = False
        self._job_handle: Any = None  # Windows Job Object handle
        if _IS_WINDOWS:
            self._job_handle = _create_kill_on_close_job()

    def register(self, pid: int, *, pgid: int | None = None) -> None:
        """Register a spawned child PID (and optional POSIX process-group id)."""
        if self._closed:
            return
        self._pids.add(pid)
        if pgid is not None and not _IS_WINDOWS:
            self._pgids.add(pgid)
        if _IS_WINDOWS and self._job_handle is not None:
            _assign_pid_to_job(self._job_handle, pid)

    def close(self) -> None:
        """Terminate the entire supervised child tree (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if _IS_WINDOWS:
            # Closing the Job handle kills every assigned process.
            self._close_job_handle()
        else:
            self._kill_posix_groups()
        self._pids.clear()
        self._pgids.clear()

    def _close_job_handle(self) -> None:
        if self._job_handle is None:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._job_handle)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning("supervisor-containment: CloseHandle failed: {}", exc)
        finally:
            self._job_handle = None

    def _kill_posix_groups(self) -> None:
        # Signal each registered process group, then each stray PID.
        for pgid in sorted(self._pgids):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("supervisor-containment: killpg SIGTERM failed for PGID {}: {}", pgid, exc)

        import time

        time.sleep(0.3)

        for pgid in sorted(self._pgids):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("supervisor-containment: killpg SIGKILL failed for PGID {}: {}", pgid, exc)

        # Catch any direct PIDs that were not part of a registered group.
        for pid in sorted(self._pids):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("supervisor-containment: SIGKILL failed for PID {}: {}", pid, exc)


def _create_kill_on_close_job() -> Any:
    """Create a Windows Job Object with kill-on-close, or ``None`` if unavailable."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION layout (abbreviated).
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        size = ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
        ok = kernel32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation, ctypes.byref(info), size
        )
        if not ok:
            try:
                kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
            return None
        return handle
    except Exception as exc:  # noqa: BLE001
        logger.warning("supervisor-containment: could not create Job Object: {}", exc)
        return None


def _assign_pid_to_job(job_handle: Any, pid: int) -> None:
    """Assign a PID to the Windows Job Object."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        # Open the target process with PROCESS_SET_QUOTA | PROCESS_TERMINATE.
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        inh = False
        proc_handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, inh, pid
        )
        if not proc_handle:
            return
        try:
            kernel32.AssignProcessToJobObject(job_handle, proc_handle)
        finally:
            kernel32.CloseHandle(proc_handle)
    except Exception as exc:  # noqa: BLE001
        logger.warning("supervisor-containment: AssignProcessToJobObject failed for PID {}: {}", pid, exc)


def posix_set_child_death_signal() -> None:
    """Set ``PR_SET_PDEATHSIG`` so the OS signals this process if its parent dies.

    Linux-only helper called from the Worker / Control Plane child
    entrypoint after ``os.setsid()``. On non-Linux POSIX it is a no-op;
    the Supervisor still relies on PGID tracking plus Job Object on
    Windows (design §24.6).
    """
    if _IS_WINDOWS:
        return
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        SIGHUP = signal.SIGHUP
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, SIGHUP, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("posix_set_child_death_signal unavailable: {}", exc)


def posix_start_new_session() -> int | None:
    """Start a new POSIX session/process-group in the calling child.

    Returns the new PGID (== caller PID) on POSIX, or ``None`` on Windows.
    Called from the child entrypoint so the Supervisor can later signal
    the whole group via :func:`os.killpg`.
    """
    if _IS_WINDOWS:
        return None
    try:
        os.setsid()
        return os.getpid()
    except Exception as exc:  # noqa: BLE001
        logger.warning("posix_start_new_session failed: {}", exc)
        return None
