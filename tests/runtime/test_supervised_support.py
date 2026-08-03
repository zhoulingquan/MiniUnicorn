"""Tests for the supervised test-support helpers (Task 6).

Verifies:

- ``wait_for_task_owner`` polls until ``TaskRecord.state == "RUNNING"`` and
  ``leased_by`` is non-empty, then returns that exact owner.
- ``wait_for_task_owner`` times out with an ``AssertionError`` when the task
  stays queued.
- ``terminate_worker`` rejects an unknown worker id with ``AssertionError``.
- ``terminate_worker`` terminates a real supervised Worker and returns its pid.
- ``Supervisor.child_record`` exposes a read-only lookup without leaking the
  mutable mapping.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from miniunicorn.runtime.supervisor import (
    RestartPolicy,
    Supervisor,
)


def _ready_then_block_entrypoint(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: Any,
    ready_signal: Any,
) -> int:
    """Child stub: signal readiness then block until shutdown or parent dies."""
    ready_signal(role)
    import threading

    event = threading.Event()
    event.wait(timeout=30)
    return 0


def _make_supervisor(
    *,
    worker_count: int = 2,
    min_workers: int = 1,
    ready_timeout_s: float = 10.0,
) -> Supervisor:
    return Supervisor(
        config=None,
        control_entrypoint=_ready_then_block_entrypoint,
        worker_entrypoint=_ready_then_block_entrypoint,
        worker_count=worker_count,
        min_workers=min_workers,
        ready_timeout_s=ready_timeout_s,
        shutdown_grace_s=5,
        restart_policy=RestartPolicy(),
    )


class TestWaitForTaskOwner:
    """``wait_for_task_owner`` resolves the real lease owner (Task 6 Step 1)."""

    @pytest.mark.asyncio
    async def test_resolves_running_task_owner(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        from miniunicorn.runtime.contracts import ClaimRequest
        from tests.runtime.support.supervised import wait_for_task_owner

        env = make_inbound_envelope(sample_scope, session_key="owner-session")
        submit = store.submit_task(env)
        assert submit.status == "ACCEPTED"
        now_ms = int(time.time() * 1000)
        result = store.claim_next(
            ClaimRequest(worker_id="owner-worker", now_ms=now_ms, lease_ms=60_000)
        )
        assert result.claimed is not None
        store.mark_running(result.claimed.claim, now_ms=now_ms + 1)

        task, owner = await wait_for_task_owner(store, submit.task_id, timeout_s=2.0)
        assert task.state == "RUNNING"
        assert owner == "owner-worker"
        assert task.leased_by == owner

    @pytest.mark.asyncio
    async def test_timeout_on_queued_task(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        from tests.runtime.support.supervised import wait_for_task_owner

        env = make_inbound_envelope(sample_scope, session_key="queued-session")
        submit = store.submit_task(env)
        # Do NOT claim — the task stays QUEUED.

        with pytest.raises(AssertionError, match="lease owner was not resolved"):
            await wait_for_task_owner(store, submit.task_id, timeout_s=0.05)


class TestTerminateWorker:
    """``terminate_worker`` kills the named supervised Worker (Task 6 Step 1)."""

    def test_rejects_unknown_worker(self) -> None:
        from tests.runtime.support.supervised import terminate_worker

        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        try:
            sup.start()
            with pytest.raises(AssertionError, match="no live supervised Worker named worker-404"):
                terminate_worker(sup, "worker-404")
        finally:
            sup.shutdown(grace_s=3)

    @pytest.mark.slow
    def test_terminates_real_worker_and_returns_pid(self) -> None:
        from tests.runtime.support.supervised import terminate_worker

        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        try:
            sup.start()
            assert sup.is_ready() is True
            # The first worker id is "worker-0".
            pid = terminate_worker(sup, "worker-0", timeout_s=3.0)
            assert isinstance(pid, int)
            assert pid > 0
        finally:
            sup.shutdown(grace_s=3)


class TestSupervisorChildRecord:
    """``Supervisor.child_record`` exposes a read-only lookup (Task 6 Step 3)."""

    def test_child_record_returns_none_for_unknown(self) -> None:
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        try:
            sup.start()
            assert sup.child_record("worker-404") is None
        finally:
            sup.shutdown(grace_s=3)

    @pytest.mark.slow
    def test_child_record_returns_worker_record(self) -> None:
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        try:
            sup.start()
            record = sup.child_record("worker-0")
            assert record is not None
            assert record.role == "worker"
            assert record.process is not None
        finally:
            sup.shutdown(grace_s=3)
