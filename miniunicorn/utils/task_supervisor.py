"""Supervisor for fire-and-forget background tasks.

Provides ``TaskSupervisor`` — a small helper that:

* Tracks every spawned ``asyncio.Task`` so it cannot be garbage-collected
  before completion.
* Logs any unhandled exception exactly once via a done-callback so failures
  never disappear silently.
* Drains or cancels all outstanding tasks on shutdown via :meth:`close`.

Two supervisors are used in the agent core:

* ``AgentLoop`` owns one for archive/save/background jobs.
* ``AgentRunner`` owns one for periodic reflections.

Channel-specific keepalive/server tasks are intentionally NOT migrated here;
they already own their own shutdown path and are addressed separately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger


class TaskSupervisor:
    """Track fire-and-forget tasks and surface their exceptions.

    A supervisor owns a strong reference to every task it creates, so the
    Python garbage collector cannot collect a running task before it
    completes. When a task finishes, its done callback:

    * discards the task reference (so the set drains over time),
    * swallows :class:`asyncio.CancelledError` silently, and
    * logs any other exception exactly once via ``logger.exception``.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_count(self) -> int:
        """Number of tasks that have not yet completed."""

        return sum(not task.done() for task in self._tasks)

    def create(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Schedule ``coro`` as a tracked background task named ``name``.

        The supervisor keeps a strong reference to the returned task until
        its done callback fires, so callers do not need to hold the
        reference themselves.
        """

        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Background task '{}' failed", task.get_name())

    async def close(
        self,
        *,
        cancel: bool,
        timeout_s: float | None = None,
    ) -> None:
        """Drain (or cancel) every outstanding task.

        Parameters
        ----------
        cancel:
            When ``True``, every still-pending task is cancelled before the
            drain. When ``False``, the supervisor waits for natural
            completion (subject to ``timeout_s``).
        timeout_s:
            Optional wall-clock bound on the drain. When the deadline is
            exceeded, every still-pending task is force-cancelled and
            awaited one more time so the done callback can fire.
        """

        tasks = tuple(self._tasks)
        if cancel:
            for task in tasks:
                task.cancel()
        if not tasks:
            return
        waiter = asyncio.gather(*tasks, return_exceptions=True)
        if timeout_s is None:
            await waiter
            return
        try:
            await asyncio.wait_for(waiter, timeout=timeout_s)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
