"""TurnCoordinator: per-session serialization and global concurrency gating.

The coordinator owns one weakly-held :class:`asyncio.Lock` per effective
session key and an optional global :class:`asyncio.Semaphore`. Lock
acquisition always precedes semaphore acquisition so that a task waiting
for its session lock does not consume a global concurrency permit.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from miniunicorn.agent.turn_runtime import (
    TurnRuntime,
    bind_turn_runtime,
    reset_turn_runtime,
)


@dataclass(slots=True, frozen=True)
class DurableIdentifiers:
    """Immutable durable task identifiers passed into a coordinator scope.

    Design §29.5: when the Worker Adapter binds a ``TurnRuntime`` for a
    durable task, it populates these identifiers so the state trace,
    telemetry, and logs share one durable identity. ``None``/``0`` fields
    are allowed for internal tasks that do not have a session sequence or
    lease epoch yet.
    """

    task_id: str
    session_sequence: int = 0
    lease_epoch: int = 0
    run_segment: int = 0
    trace_id: str | None = None


class TurnCoordinator:
    """Coordinate per-session locks and the global concurrency gate."""

    def __init__(self, max_concurrent_requests: int | None) -> None:
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent_requests)
            if max_concurrent_requests and max_concurrent_requests > 0
            else None
        )

    @property
    def session_locks(self) -> weakref.WeakValueDictionary[str, asyncio.Lock]:
        """Read-only compatibility alias for code that inspects session locks."""
        return self._session_locks

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    @asynccontextmanager
    async def scope(
        self,
        session_key: str,
        turn_id: str | None = None,
        *,
        durable_identifiers: "DurableIdentifiers | None" = None,
    ) -> AsyncIterator[TurnRuntime]:
        wait_started = time.monotonic()
        lock = self._lock_for(session_key)
        async with lock:
            if self._gate is not None:
                await self._gate.acquire()
            runtime = TurnRuntime(
                turn_id=turn_id or uuid.uuid4().hex,
                session_key=session_key,
                queue_wait_ms=int((time.monotonic() - wait_started) * 1000),
            )
            if durable_identifiers is not None:
                # Populate durable immutable identifiers (design §29.5).
                runtime.task_id = durable_identifiers.task_id
                runtime.session_sequence = durable_identifiers.session_sequence
                runtime.lease_epoch = durable_identifiers.lease_epoch
                runtime.run_segment = durable_identifiers.run_segment
                runtime.trace_id = durable_identifiers.trace_id
            token = bind_turn_runtime(runtime)
            try:
                yield runtime
            finally:
                reset_turn_runtime(token)
                if self._gate is not None:
                    self._gate.release()
