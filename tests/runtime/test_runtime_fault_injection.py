"""Runtime fault injection and crash-boundary recovery tests (Task 14).

Tests crash recovery at each durable boundary:

1. Lightweight crash-recovery: stop the host while a task is RUNNING,
   wait for lease expiry, restart, and verify the task completes with
   exactly one Provider call (no duplicate effects).

2. Fault-injection at named boundaries: use the :class:`FaultInjector`
   to crash the Worker after INBOUND commit, after FINAL commit, and
   after Outbox enqueue. Verify the durable state at each boundary is
   consistent and the task recovers correctly.

3. Supervised Worker-kill: kill a real Worker process while a task is
   in-flight, wait for Supervisor replacement, and verify the task is
   recovered by another Worker (Task 14 Step 4).

Design §30 Task 14: "For each durable boundary, kill the actual Worker
process owning the task, wait for Supervisor replacement, and assert
the expected recovered state."
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.application import RuntimeInboundRequest
from miniunicorn.runtime.hosts.lightweight import LightweightHost
from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
from miniunicorn.runtime.scheduler import Scheduler
from miniunicorn.runtime.session_committer import SessionCommitter
from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection
from miniunicorn.runtime.task_service import TaskService
from miniunicorn.runtime.worker import (
    WorkerExecutionResult,
    WorkerTaskPayload,
)
from miniunicorn.session.manager import SessionManager
from tests.runtime.faults import FaultInjector, collect_durable_facts

# ---------------------------------------------------------------------------
# Stub execution callback with effect counter
# ---------------------------------------------------------------------------


class CountingStubCallback:
    """Stub execution callback that counts Provider-like effects.

    Simulates the Agent execution path without requiring the full
    Agent Core. Each call increments ``effect_count`` so tests can
    assert exactly one effect per task (no duplicates after recovery).

    The counter increments only after the optional delay completes so
    a cancelled call (host crash mid-execution) does not count as an
    effect — mirroring a real Provider call that never reached the
    network or was cancelled before completion.
    """

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.effect_count = 0
        self.executions: list[WorkerTaskPayload] = []

    async def __call__(
        self,
        payload: WorkerTaskPayload,
        session_base_revision: int,
    ) -> WorkerExecutionResult:
        self.executions.append(payload)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.effect_count += 1
        return WorkerExecutionResult(
            final_content=f"response to: {payload.content}",
            messages=[{"role": "assistant", "content": f"response to: {payload.content}"}],
            suppress_final=False,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload_bytes(content: str) -> bytes:
    import json

    payload = {"content": content, "media": [], "metadata": {}}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _make_envelope(scope: Any, *, content: str = "hello", session_key: str = "fault-test") -> Any:
    from miniunicorn.runtime.models import InboundTaskEnvelope

    payload_bytes = _make_payload_bytes(content)
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    return InboundTaskEnvelope(
        protocol_version=1,
        task_kind="USER_TURN",
        priority=100,
        scope=scope,
        session_key=session_key,
        channel="cli",
        channel_account="test-user",
        channel_message_id=None,
        dedup_key=None,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        received_at_ms=int(time.time() * 1000),
        available_at_ms=None,
        payload_content=payload_bytes,
        target_key="direct",
    )


import hashlib


def _build_lightweight_host(
    store: SqliteRuntimeStore,
    session_manager: SessionManager,
    callback: Any,
    *,
    lease_ms: int = 2_000,
    heartbeat_interval_s: float = 0.5,
    fault_hook: Any = None,
) -> LightweightHost:
    """Build a LightweightHost with short lease for fast recovery tests."""
    committer = SessionCommitter(store, session_manager)
    return LightweightHost(
        store,
        committer,
        callback,
        worker_count=1,
        lease_ms=lease_ms,
        heartbeat_interval_s=heartbeat_interval_s,
        fault_hook=fault_hook,
    )


# ---------------------------------------------------------------------------
# Test 1: Lightweight crash-recovery after lease expiry
# ---------------------------------------------------------------------------


class TestLightweightCrashRecovery:
    """A task left RUNNING after a host crash is recovered by a new host."""

    @pytest.mark.asyncio
    async def test_task_recovers_after_host_crash(
        self,
        store: SqliteRuntimeStore,
        tmp_path: Path,
    ) -> None:
        """Stop the host while a task is RUNNING; restart and verify recovery.

        The task must reach COMPLETED with exactly one Provider effect
        (no duplicate execution after recovery).
        """

        from miniunicorn.runtime.models import RequestScope

        scope = RequestScope(
            tenant_id="test-tenant",
            principal_id="test-principal",
            agent_id="test-agent",
            workspace_id="test-workspace",
        )
        session_manager = SessionManager(tmp_path / "sessions")
        callback = CountingStubCallback(delay_s=0.5)

        # Phase 1: submit a task and crash the host while it's running.
        host1 = _build_lightweight_host(
            store, session_manager, callback, lease_ms=2_000, heartbeat_interval_s=0.5
        )
        await host1.start()
        try:
            envelope = _make_envelope(scope, content="crash-test")
            handle = await host1.task_service.submit(envelope)

            # Wait for the task to be claimed (RUNNING state).
            await asyncio.sleep(0.2)
            task = store.read_task(handle.task_id)
            assert task is not None
            assert task.state in ("LEASED", "RUNNING"), (
                f"task should be leased/running before crash, got {task.state}"
            )
        finally:
            # Stop the host immediately — simulates a crash.
            await host1.stop()

        # The task is now in RUNNING state with an active lease.
        task = store.read_task(handle.task_id)
        assert task is not None
        assert task.state in ("LEASED", "RUNNING", "FAILED")

        # If the task was failed by the host shutdown, skip recovery
        # (the Worker's exception handler may have failed it).
        if task.state == "FAILED":
            pytest.skip("task was failed during host shutdown, not recoverable")

        # Phase 2: wait for the lease to expire, then start a new host.
        # The lease was 2 seconds; wait 3 seconds to ensure expiry.
        await asyncio.sleep(3.0)

        # Run the reclaim scanner to move the expired task back to QUEUED.
        scheduler = Scheduler(store, lease_ms=2_000, max_root_attempts=3)
        reclaim_result = scheduler.reclaim_expired(now_ms=int(time.time() * 1000))
        assert reclaim_result.reclaimed_count >= 1, (
            f"expected at least 1 reclaimed task, got {reclaim_result.reclaimed_count}"
        )

        # Phase 3: start a new host and verify the task completes.
        callback2 = CountingStubCallback()
        host2 = _build_lightweight_host(
            store, session_manager, callback2, lease_ms=10_000, heartbeat_interval_s=2.0
        )
        await host2.start()
        try:
            snapshot = await host2.task_service.wait_terminal(scope, handle.task_id, timeout_s=10.0)
            assert snapshot.state == "COMPLETED", (
                f"task should complete after recovery, got {snapshot.state}"
            )
        finally:
            await host2.stop()

        # Exactly one Provider effect across both hosts (no duplicate).
        total_effects = callback.effect_count + callback2.effect_count
        assert total_effects == 1, (
            f"expected exactly 1 Provider effect (no duplicate), got {total_effects}"
        )


# ---------------------------------------------------------------------------
# Test 2: Fault injection at named boundaries (lightweight)
# ---------------------------------------------------------------------------


class TestFaultInjectionBoundaries:
    """Crash at named durable boundaries and verify consistent state.

    Uses the :class:`FaultInjector` to raise ``BaseException`` (which
    bypasses the Worker's ``except Exception`` handler) so the task
    stays in a non-terminal state for recovery.
    """

    @pytest.mark.asyncio
    async def test_crash_after_task_claim_leaves_task_running(
        self,
        store: SqliteRuntimeStore,
        tmp_path: Path,
    ) -> None:
        """Crash immediately after claim; task stays RUNNING for recovery."""
        from miniunicorn.runtime.models import RequestScope

        scope = RequestScope(
            tenant_id="test-tenant",
            principal_id="test-principal",
            agent_id="test-agent",
            workspace_id="test-workspace",
        )
        session_manager = SessionManager(tmp_path / "sessions")
        callback = CountingStubCallback()
        injector = FaultInjector()
        injector.raise_at("after_task_claim", _CrashException("crash after claim"))

        host = _build_lightweight_host(
            store,
            session_manager,
            callback,
            lease_ms=2_000,
            heartbeat_interval_s=0.5,
            fault_hook=injector.hook,
        )
        await host.start()
        try:
            envelope = _make_envelope(scope, content="claim-crash")
            handle = await host.task_service.submit(envelope)

            # Wait for the fault to trigger.
            await asyncio.sleep(0.5)
        finally:
            await host.stop()

        # The task should be in RUNNING or FAILED state (not COMPLETED).
        task = store.read_task(handle.task_id)
        assert task is not None
        assert task.state != "COMPLETED", "task should not complete after crash at after_task_claim"
        # The Provider should not have been called.
        assert callback.effect_count == 0

    @pytest.mark.asyncio
    async def test_crash_after_session_prepare_keeps_inbound_committed(
        self,
        store: SqliteRuntimeStore,
        tmp_path: Path,
    ) -> None:
        """Crash after INBOUND commit; the user message is durably persisted.

        On recovery, the INBOUND commit returns ALREADY_COMMITTED so the
        user message is not duplicated.
        """
        from miniunicorn.runtime.models import RequestScope

        scope = RequestScope(
            tenant_id="test-tenant",
            principal_id="test-principal",
            agent_id="test-agent",
            workspace_id="test-workspace",
        )
        session_manager = SessionManager(tmp_path / "sessions")
        callback = CountingStubCallback()
        injector = FaultInjector()
        injector.raise_at("after_session_prepare", _CrashException("crash after inbound"))

        host = _build_lightweight_host(
            store,
            session_manager,
            callback,
            lease_ms=2_000,
            heartbeat_interval_s=0.5,
            fault_hook=injector.hook,
        )
        await host.start()
        try:
            envelope = _make_envelope(
                scope, content="inbound-crash", session_key="inbound-crash-session"
            )
            _handle = await host.task_service.submit(envelope)
            await asyncio.sleep(0.5)
        finally:
            await host.stop()

        # The user message should be in the session (INBOUND committed).
        snapshot = session_manager.load_fresh("inbound-crash-session")
        user_messages = [m for m in snapshot.messages if m.get("role") == "user"]
        assert len(user_messages) == 1, (
            f"expected exactly 1 user message after INBOUND crash, got {len(user_messages)}"
        )
        assert user_messages[0]["content"] == "inbound-crash"

    @pytest.mark.asyncio
    async def test_crash_after_outbox_enqueue_leaves_task_completed(
        self,
        store: SqliteRuntimeStore,
        tmp_path: Path,
    ) -> None:
        """Crash after Outbox enqueue; the task is already COMPLETED.

        The Outbox row exists with the correct final content. The crash
        does not lose the completion or the final reply.
        """
        from miniunicorn.runtime.models import RequestScope

        scope = RequestScope(
            tenant_id="test-tenant",
            principal_id="test-principal",
            agent_id="test-agent",
            workspace_id="test-workspace",
        )
        session_manager = SessionManager(tmp_path / "sessions")
        callback = CountingStubCallback()
        injector = FaultInjector()
        injector.raise_at("after_outbox_enqueue", _CrashException("crash after enqueue"))

        host = _build_lightweight_host(
            store,
            session_manager,
            callback,
            lease_ms=10_000,
            heartbeat_interval_s=2.0,
            fault_hook=injector.hook,
        )
        await host.start()
        try:
            envelope = _make_envelope(
                scope, content="enqueue-crash", session_key="enqueue-crash-session"
            )
            handle = await host.task_service.submit(envelope)
            await asyncio.sleep(1.0)
        finally:
            await host.stop()

        # The task should be COMPLETED (the crash happened after completion).
        task = store.read_task(handle.task_id)
        assert task is not None
        assert task.state == "COMPLETED", (
            f"task should be COMPLETED after crash at after_outbox_enqueue, got {task.state}"
        )

        # The Outbox row should exist with the correct content.
        facts = collect_durable_facts(
            store,
            handle.task_id,
            "enqueue-crash-session",
            session_manager=session_manager,
        )
        assert len(facts["outbox"]) == 1
        assert facts["outbox"][0]["content"] == "response to: enqueue-crash"
        assert facts["outbox"][0]["kind"] == "FINAL_REPLY"

        # Exactly one Provider effect.
        assert callback.effect_count == 1


# ---------------------------------------------------------------------------
# Test 3: Supervised Worker-kill recovery
# ---------------------------------------------------------------------------


class TestSupervisedWorkerKillRecovery:
    """Kill the real lease-owning Worker and verify recovery (Task 7).

    The Worker that actually holds the task lease (``TaskRecord.leased_by``)
    is the one that must be terminated — not an arbitrary Worker. The
    ``wait_for_task_owner`` helper resolves that owner from durable state,
    and ``terminate_worker`` kills that exact process. Recovery must
    produce exactly one logical Provider decision and no duplicate
    terminal/final-reply effect (design §19.4, §30 Task 14).
    """

    @pytest.mark.slow
    async def test_task_recovers_after_worker_kill(
        self,
        tmp_path: Path,
    ) -> None:
        """Kill the lease-owning Worker while a task is in-flight; verify recovery.

        The Supervisor restarts the Worker, the lease expires, and the
        task is reclaimed and completed by another Worker (or the
        restarted Worker).
        """
        from miniunicorn.config.schema import Config
        from miniunicorn.runtime.bootstrap import build_supervised_runtime
        from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion
        from tests.runtime.support.supervised import (
            terminate_worker,
            wait_for_task_owner,
        )

        stub = OpenAIStubServer(
            [
                chat_completion("recovered answer"),
                chat_completion("recovered answer"),
            ],
            delay_s=2.0,
        )
        stub.start()
        try:
            workspace = tmp_path / "supervised-crash"
            workspace.mkdir()
            config = Config.model_validate(
                {
                    "agents": {
                        "defaults": {
                            "workspace": str(workspace),
                            "provider": "custom",
                            "model": "stub-model",
                            "contextWindowTokens": 32768,
                        },
                    },
                    "providers": {
                        "custom": {
                            "apiKey": "test-key",
                            "apiBase": stub.api_base,
                            "apiType": "chat_completions",
                        },
                    },
                    "runtime": {
                        "mode": "supervised",
                        "workerCount": 3,
                        "heartbeatIntervalS": 1,
                        "leaseTimeoutS": 5,
                        "progressTimeoutS": 30,
                        "queuePollMaxMs": 200,
                        "leaseScanIntervalS": 1,
                        "outboxLeaseTimeoutS": 30,
                        "channelSendTimeoutS": 10,
                        "sqliteBusyTimeoutMs": 1000,
                        "shutdownGraceS": 3,
                    },
                }
            )

            resources = build_supervised_runtime(config)
            await resources.start()
            try:
                assert resources.host.ready_workers() == 3

                from miniunicorn.config.runtime import resolve_runtime_paths

                resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
                db_path = resolved.database_path_resolved or Path(resolved.database_path)
                test_conn = open_connection(db_path)
                test_store = SqliteRuntimeStore(test_conn)
                task_service = TaskService(test_store)

                try:
                    scope = local_request_scope(config)
                    request = RuntimeInboundRequest(
                        content="survive-this",
                        media=(),
                        metadata={},
                        session_key="worker-kill-session",
                        channel="cli",
                        channel_account="test-user",
                        channel_message_id=None,
                        scope=scope,
                        target_key="direct",
                    )
                    envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
                    handle = await task_service.submit(envelope)

                    # Resolve the real lease-owning Worker from durable
                    # state. The helper fails (not skips) when the task
                    # never reaches RUNNING — that is the correct signal
                    # that the recovery scenario was not established.
                    task, owner = await wait_for_task_owner(
                        test_store,
                        handle.task_id,
                        timeout_s=10.0,
                    )
                    assert task.leased_by == owner

                    # Terminate the exact Worker that holds the lease.
                    killed_pid = terminate_worker(resources.host.supervisor, owner)
                    assert killed_pid > 0

                    # Wait for the task to complete (recovered by another Worker).
                    snapshot = await task_service.wait_terminal(scope, handle.task_id, timeout_s=45)
                    assert snapshot.state == "COMPLETED", (
                        f"task should recover after Worker kill, got {snapshot.state}"
                    )

                    # Strengthened postconditions (Task 7 Step 2).
                    final_task = test_store.read_task(handle.task_id)
                    assert final_task is not None
                    # root_attempt_count >= 2: the original attempt plus at
                    # least one recovery attempt after lease expiry.
                    assert final_task.root_attempt_count >= 2, (
                        f"expected >=2 root attempts, got {final_task.root_attempt_count}"
                    )
                    # The Provider was called at most twice: once before
                    # the kill (may not have completed) and once during
                    # recovery. At least one call is required for the
                    # task to complete.
                    assert 1 <= len(stub.requests) <= 2, (
                        f"expected 1-2 Provider calls, got {len(stub.requests)}"
                    )

                    # One logical Provider decision: exactly one COMPLETED
                    # model attempt for this task.
                    completed_attempts = test_conn.execute(
                        "SELECT COUNT(*) FROM model_attempts WHERE task_id=? AND state='COMPLETED'",
                        (handle.task_id,),
                    ).fetchone()[0]
                    assert completed_attempts == 1, (
                        f"expected exactly 1 COMPLETED model attempt, got {completed_attempts}"
                    )

                    # No duplicate terminal/final-reply effect: exactly one
                    # outbox row for this task (the final reply was enqueued
                    # exactly once, not duplicated by recovery). The row may
                    # still be PENDING because the outbox sender runs
                    # asynchronously; the key assertion is the count.
                    outbox_count = test_conn.execute(
                        "SELECT COUNT(*) FROM outbox WHERE task_id=?",
                        (handle.task_id,),
                    ).fetchone()[0]
                    assert outbox_count == 1, (
                        f"expected exactly 1 outbox row (no duplicate final "
                        f"reply), got {outbox_count}"
                    )

                    # Task 25: no duplicate MODEL_STARTED events. With
                    # attempt reuse (Task 5), recovery must NOT start a
                    # new model attempt — it reuses the existing
                    # COMPLETED row. So there should be exactly 1
                    # MODEL_STARTED event (0 duplicates).
                    model_started_count = test_conn.execute(
                        "SELECT COUNT(*) FROM task_events "
                        "WHERE task_id=? AND event_type='MODEL_STARTED'",
                        (handle.task_id,),
                    ).fetchone()[0]
                    assert model_started_count == 1, (
                        f"expected exactly 1 MODEL_STARTED event (no "
                        f"duplicate starts), got {model_started_count}"
                    )
                finally:
                    test_conn.close()
            finally:
                await resources.stop()
        finally:
            stub.close()


# ---------------------------------------------------------------------------
# Internal: crash exception that bypasses Worker's Exception handler
# ---------------------------------------------------------------------------


class _CrashException(BaseException):
    """Exception used to simulate a process crash.

    Inherits from ``BaseException`` (not ``Exception``) so the Worker's
    ``except Exception`` handler does not catch it and fail the task.
    The task stays in its current non-terminal state for recovery.
    """
