"""Process-wide per-path mutexes for file read-modify-write cycles.

JSONL files in the memory layer are append-only but occasionally rewritten
in place by truncation/rotation/prune (read → write temp → ``os.replace``).
A concurrent append to the same file between the read and the replace lands
in the old inode and is silently lost.  All writers of one path share a
single ``threading.Lock`` through :func:`file_path_lock`, making prune-vs-
append safe within the process (cross-process coordination still relies on
``os.replace`` atomicity, which is the existing design).
"""

from __future__ import annotations

import threading
from pathlib import Path

_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def file_path_lock(path: Path | str) -> threading.Lock:
    """Return the process-wide lock guarding *path* (resolved)."""
    key = Path(path).resolve()
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock
