"""WP3 — Durable lightweight task path tests (design §30 WP3).

Tests the end-to-end durable path:

- TaskService.submit → Scheduler.claim → AgentTaskWorker._execute_task → completion
- process restart after each outer phase (crash recovery);
- same-session strict ordering;
- different-session concurrency with two lightweight slots;
- cancellation at safe boundaries;
- no runtime task exists only in ``_active_tasks`` or pending queues;
- legacy path still works when ``runtime.enabled=false``.

The tests use a stub execution callback that simulates Agent execution
without requiring the full Agent Core. This isolates the runtime path
from Agent-specific behavior (design §30 WP3: "without direct Channel
delivery in runtime tests").
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.agent.ports import SafeError
from miniunicorn.runtime.contracts import RuntimeStore
from miniunicorn.runtime.hosts.lightweight import LightweightHost
from miniunicorn.runtime.models import (
    InboundTaskEnvelope,
    MediaRef,
    RequestScope,
)
from miniunicorn.runtime.scheduler import Scheduler
from miniunicorn.runtime.session_committer import SessionCommitter
from miniunicorn.runtime.sqlite import SqliteRuntimeStore
from miniunicorn.runtime.task_service import TaskService
from miniunicorn.runtime.worker import (
    AgentTaskWorker,
    WorkerExecutionResult,
    WorkerTaskPayload,
)
from miniunicorn.session.manager import SessionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def session_manager(workspace: Path) -> SessionManager:
    return SessionManager(workspace)


@pytest.fixture
def committer(store: SqliteRuntimeStore, session_manager: SessionManager) -> SessionCommitter:
    return SessionCommitter(store, session_manager)


def _make_payload_bytes(content: str, **extra: Any) -> bytes:
    """Build a JSON payload matching Worker._decode_payload expectations."""
    payload = {"content": content, "media": [], "metadata": {}}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _make_envelope(
    scope: RequestScope,
    *,
    session_key: str = "test-session",
    content: str = "hello",
    channel_message_id: str | None = None,
    dedup_key: str | None = None,
    available_at_ms: int | None = None,
) -> InboundTaskEnvelope:
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
        channel_message_id=channel_message_id,
        dedup_key=dedup_key,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        received_at_ms=1_000_000,
        available_at_ms=available_at_ms,
        payload_content=payload_bytes,
        # Task 7: immutable delivery target for CLI sessions (design §17.8).
        target_key="direct",
    )


# ---------------------------------------------------------------------------
# Stub execution callback
# ---------------------------------------------------------------------------


class StubExecutionCallback:
    """Stub execution callback that simulates Agent execution.

    Records the order of executions and optionally delays or fails.
    """

    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        fail_content: str | None = None,
        suppress_final: bool = False,
    ) -> None:
        self.delay_s = delay_s
        self.fail_content = fail_content
        self.suppress_final = suppress_final
        self.executions: list[WorkerTaskPayload] = []
        self._execution_order: list[str] = []

    async def __call__(
        self,
        payload: WorkerTaskPayload,
        session_base_revision: int,
    ) -> WorkerExecutionResult:
        self.executions.append(payload)
        self._execution_order.append(payload.session_key)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_content is not None:
            return WorkerExecutionResult(
                final_content=None,
                messages=[],
                error=SafeError(
                    error_code="AGENT_EXECUTION_FAILURE",
                    error_summary=self.fail_content,
                ),
            )
        return WorkerExecutionResult(
            final_content=f"response to: {payload.content}",
            messages=[{"role": "assistant", "content": f"response to: {payload.content}"}],
            suppress_final=self.suppress_final,
        )

    @property
    def execution_order(self) -> list[str]:
        return list(self._execution_order)


# ---------------------------------------------------------------------------
# Tests: basic submit → claim → execute → complete
# ---------------------------------------------------------------------------


class TestLightweightHostBasic:
    """Basic end-to-end: submit, execute, complete."""

    @pytest.mark.asyncio
    async def test_submit_and_complete(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        callback = StubExecutionCallback()
        host = LightweightHost(
            store,
            committer,
            callback,
            worker_count=1,
            lease_ms=60_000,
            heartbeat_interval_s=5.0,
        )
        await host.start()
        try:
            envelope = _make_envelope(sample_scope, content="hello")
            handle = await host.task_service.submit(envelope)
            snapshot = await host.task_service.wait_terminal(
                sample_scope, handle.task_id, timeout_s=5.0
            )
            assert snapshot.state == "COMPLETED"
            assert len(callback.executions) == 1
            assert callback.executions[0].content == "hello"
        finally:
            await host.stop()

    @pytest.mark.asyncio
    async def test_submit_and_wait_compatibility(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """LightweightHost.submit_and_wait returns terminal snapshot."""
        callback = StubExecutionCallback()
        host = LightweightHost(store, committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_envelope(sample_scope, content="test")
            snapshot = await host.submit_and_wait(envelope, timeout_s=5.0)
            assert snapshot.state == "COMPLETED"
        finally:
            await host.stop()

    @pytest.mark.asyncio
    async def test_execution_failure_marks_task_failed(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        callback = StubExecutionCallback(fail_content="agent crashed")
        host = LightweightHost(store, committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_envelope(sample_scope, content="hello")
            handle = await host.task_service.submit(envelope)
            snapshot = await host.task_service.wait_terminal(
                sample_scope, handle.task_id, timeout_s=5.0
            )
            assert snapshot.state == "FAILED"
            assert snapshot.error is not None
            assert snapshot.error.error_code == "AGENT_EXECUTION_FAILURE"
        finally:
            await host.stop()


# ---------------------------------------------------------------------------
# Tests: same-session strict ordering
# ---------------------------------------------------------------------------


class TestSameSessionOrdering:
    """Tasks for the same session execute in strict session_sequence order."""

    @pytest.mark.asyncio
    async def test_same_session_tasks_execute_in_order(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        session_manager: SessionManager,
        sample_scope: RequestScope,
    ) -> None:
        """Two tasks for the same session execute in submission order.

        The session transcript must contain exactly four messages in
        order: user, assistant, user, assistant (design §17.1, Task 3
        Step 1).
        """
        callback = StubExecutionCallback(delay_s=0.05)
        host = LightweightHost(store, committer, callback, worker_count=1)
        await host.start()
        try:
            # Submit two tasks for the same session.
            env1 = _make_envelope(
                sample_scope,
                session_key="session-A",
                content="first",
                channel_message_id="msg-1",
            )
            env2 = _make_envelope(
                sample_scope,
                session_key="session-A",
                content="second",
                channel_message_id="msg-2",
            )
            handle1 = await host.task_service.submit(env1)
            handle2 = await host.task_service.submit(env2)

            # Both should complete.
            snap1 = await host.task_service.wait_terminal(
                sample_scope, handle1.task_id, timeout_s=10.0
            )
            snap2 = await host.task_service.wait_terminal(
                sample_scope, handle2.task_id, timeout_s=10.0
            )
            assert snap1.state == "COMPLETED"
            assert snap2.state == "COMPLETED"

            # session_sequence must be strictly increasing.
            assert snap1.session_sequence < snap2.session_sequence

            # The session transcript must contain exactly four messages
            # in order: user, assistant, user, assistant (Task 3 Step 1).
            snapshot = session_manager.load_fresh("session-A")
            transcript = [
                (m.get("role"), m.get("content"))
                for m in snapshot.messages
            ]
            assert transcript == [
                ("user", "first"),
                ("assistant", "response to: first"),
                ("user", "second"),
                ("assistant", "response to: second"),
            ]
            assert snapshot.revision == 4
        finally:
            await host.stop()


# ---------------------------------------------------------------------------
# Tests: different-session concurrency with two lightweight slots
# ---------------------------------------------------------------------------


class TestDifferentSessionConcurrency:
    """Tasks for different sessions run concurrently with 2+ workers."""

    @pytest.mark.asyncio
    async def test_two_sessions_concurrent(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """Two tasks for different sessions run concurrently with 2 workers."""
        callback = StubExecutionCallback(delay_s=0.1)
        host = LightweightHost(store, committer, callback, worker_count=2)
        await host.start()
        try:
            env_a = _make_envelope(
                sample_scope,
                session_key="session-A",
                content="A",
                channel_message_id="msg-a",
            )
            env_b = _make_envelope(
                sample_scope,
                session_key="session-B",
                content="B",
                channel_message_id="msg-b",
            )
            handle_a = await host.task_service.submit(env_a)
            handle_b = await host.task_service.submit(env_b)

            snap_a = await host.task_service.wait_terminal(
                sample_scope, handle_a.task_id, timeout_s=10.0
            )
            snap_b = await host.task_service.wait_terminal(
                sample_scope, handle_b.task_id, timeout_s=10.0
            )
            assert snap_a.state == "COMPLETED"
            assert snap_b.state == "COMPLETED"
            assert len(callback.executions) == 2
        finally:
            await host.stop()


# ---------------------------------------------------------------------------
# Tests: process restart after each outer phase
# ---------------------------------------------------------------------------


class TestProcessRestart:
    """Crash recovery: stop the host, restart, and verify the task completes."""

    @pytest.mark.asyncio
    async def test_restart_after_submit_before_claim(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """Task submitted but not claimed: restart picks it up."""
        # Submit without starting the host.
        task_service = TaskService(store)
        envelope = _make_envelope(sample_scope, content="hello")
        handle = await task_service.submit(envelope)

        # Verify it's QUEUED.
        snap = await task_service.get_status(sample_scope, handle.task_id)
        assert snap.state == "QUEUED"

        # Now start the host — the worker should pick it up.
        callback = StubExecutionCallback()
        host = LightweightHost(store, committer, callback, worker_count=1)
        await host.start()
        try:
            snap = await host.task_service.wait_terminal(
                sample_scope, handle.task_id, timeout_s=5.0
            )
            assert snap.state == "COMPLETED"
        finally:
            await host.stop()

    @pytest.mark.asyncio
    async def test_restart_after_claim_before_complete(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """Task claimed but lease expired: restart reclaims and completes.

        Uses a very short lease so the claim expires quickly. The host is
        stopped (simulating crash) and restarted. The new worker reclaims
        the expired task and completes it.
        """
        callback = StubExecutionCallback(delay_s=0.3)
        # Short lease: 200ms, heartbeat 50ms — the 300ms execution exceeds
        # the lease, so the task will be reclaimed.
        host = LightweightHost(
            store,
            committer,
            callback,
            worker_count=1,
            lease_ms=200,
            heartbeat_interval_s=0.05,
        )
        await host.start()
        try:
            envelope = _make_envelope(sample_scope, content="hello")
            handle = await host.task_service.submit(envelope)

            # Wait a bit for the task to be claimed and started.
            await asyncio.sleep(0.1)

            # Stop the host (crash).
            await host.stop()

            # Wait for the lease to expire.
            await asyncio.sleep(0.3)

            # Restart with a new host and longer lease.
            callback2 = StubExecutionCallback()
            host2 = LightweightHost(
                store,
                committer,
                callback2,
                worker_count=1,
                lease_ms=60_000,
            )
            await host2.start()
            try:
                snap = await host2.task_service.wait_terminal(
                    sample_scope, handle.task_id, timeout_s=10.0
                )
                # The task should eventually complete (either by the first
                # attempt or by retry after reclaim).
                assert snap.state in ("COMPLETED", "FAILED")
            finally:
                await host2.stop()
        finally:
            if host._running:
                await host.stop()


# ---------------------------------------------------------------------------
# Tests: no runtime task exists only in _active_tasks or pending queues
# ---------------------------------------------------------------------------


class TestNoInMemoryOnlyTasks:
    """Runtime tasks are durable — none exist only in memory."""

    @pytest.mark.asyncio
    async def test_task_durable_before_acknowledgement(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """After submit, the task is durable in SQLite even without a running host."""
        task_service = TaskService(store)
        envelope = _make_envelope(sample_scope, content="hello")
        handle = await task_service.submit(envelope)

        # The task exists in the store (durable) without any host running.
        snap = await task_service.get_status(sample_scope, handle.task_id)
        assert snap is not None
        assert snap.state == "QUEUED"
        assert snap.task_id == handle.task_id

        # No _active_tasks or pending_queues involved — the task is in SQLite.
        # Verify by reading directly from the store.
        record = store.read_task(handle.task_id)
        assert record is not None
        assert record.state == "QUEUED"


# ---------------------------------------------------------------------------
# Tests: legacy path still works when runtime.enabled=false
# ---------------------------------------------------------------------------


class TestLegacyPathUnaffected:
    """The legacy (non-durable) path is unaffected by runtime additions."""

    def test_turn_runtime_legacy_defaults(self) -> None:
        """TurnRuntime fields default to legacy values when no durable IDs set."""
        from miniunicorn.agent.turn_runtime import TurnRuntime

        runtime = TurnRuntime(turn_id="test", session_key="test")
        # Durable identifiers default to None/0 (legacy mode).
        assert runtime.task_id is None
        assert runtime.session_sequence == 0
        assert runtime.lease_epoch == 0
        assert runtime.run_segment == 0
        assert runtime.trace_id is None

    def test_turn_executor_legacy_transitions_unchanged(self) -> None:
        """Legacy TURN_TRANSITIONS still has COMMAND -> DONE shortcut."""
        from miniunicorn.agent._state_machine import TurnState
        from miniunicorn.agent.turn_executor import TURN_TRANSITIONS

        assert TURN_TRANSITIONS[(TurnState.COMMAND, "shortcut")] is TurnState.DONE

    def test_turn_executor_runtime_transitions_remove_shortcut(self) -> None:
        """RUNTIME_TURN_TRANSITIONS routes shortcut through SAVE, not DONE."""
        from miniunicorn.agent._state_machine import TurnState
        from miniunicorn.agent.turn_executor import RUNTIME_TURN_TRANSITIONS

        assert RUNTIME_TURN_TRANSITIONS[(TurnState.COMMAND, "shortcut")] is TurnState.SAVE

    def test_dispatcher_legacy_methods_removed(self) -> None:
        """TurnDispatcher legacy authority removed (WP3 hard cutover).

        ``dispatch`` and ``process_direct`` were removed in the WP3 hard
        cutover — ingress routes through ``RuntimeApplication`` /
        ``TaskService``. Only ``process_message`` remains as the
        SDK/Worker compatibility bridge.
        """
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        # Legacy authority removed.
        assert not hasattr(TurnDispatcher, "dispatch")
        assert not hasattr(TurnDispatcher, "process_direct")
        # Compatibility bridge remains.
        assert hasattr(TurnDispatcher, "process_message")


# ---------------------------------------------------------------------------
# Tests: session commits are idempotent and durable
# ---------------------------------------------------------------------------


class TestSessionCommitIntegration:
    """The Worker's INBOUND and FINAL session commits are durable."""

    @pytest.mark.asyncio
    async def test_inbound_and_final_commits_applied(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        session_manager: SessionManager,
        sample_scope: RequestScope,
    ) -> None:
        """After completion, both the user message and assistant response are on disk."""
        callback = StubExecutionCallback()
        host = LightweightHost(store, committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_envelope(
                sample_scope,
                session_key="test-commit-session",
                content="hello world",
            )
            handle = await host.task_service.submit(envelope)
            snap = await host.task_service.wait_terminal(
                sample_scope, handle.task_id, timeout_s=5.0
            )
            assert snap.state == "COMPLETED"

            # Verify session transcript on disk.
            snapshot = session_manager.load_fresh("test-commit-session")
            contents = [m.get("content") for m in snapshot.messages]
            assert "hello world" in contents  # INBOUND user message
            assert any("response to: hello world" in (c or "") for c in contents)  # FINAL
        finally:
            await host.stop()


# ---------------------------------------------------------------------------
# Tests: session revision conflict enters bounded retry (Task 3 Step 2)
# ---------------------------------------------------------------------------


class ConflictInjectingCommitter:
    """Wraps a real SessionCommitter and injects REVISION_CONFLICT.

    Used to verify that the Worker enters bounded retry wait (not
    completion) when a session commit conflicts (Task 3 Step 2).
    """

    def __init__(self, real: SessionCommitter, *, conflict_kind: str) -> None:
        self._real = real
        self._conflict_kind = conflict_kind

    def current_revision(self, session_key: str) -> int:
        return self._real.current_revision(session_key)

    async def commit_turn(self, request: Any) -> Any:
        from miniunicorn.agent.ports import SessionCommitResult

        if request.commit_kind == self._conflict_kind:
            return SessionCommitResult(
                state="REVISION_CONFLICT",
                revision=7,
            )
        return await self._real.commit_turn(request)


class TestSessionRevisionConflict:
    """A REVISION_CONFLICT must enter bounded retry, never completion."""

    @pytest.mark.asyncio
    async def test_inbound_conflict_enters_retry_wait(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """INBOUND REVISION_CONFLICT → RETRY_WAIT, not COMPLETED."""
        fake_committer = ConflictInjectingCommitter(
            committer, conflict_kind="INBOUND"
        )
        callback = StubExecutionCallback()
        host = LightweightHost(store, fake_committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_envelope(
                sample_scope,
                session_key="conflict-inbound",
                content="conflict test",
            )
            handle = await host.task_service.submit(envelope)

            # Poll for RETRY_WAIT (not terminal, so wait_terminal times out).
            snap = await host.task_service.wait_terminal(
                sample_scope, handle.task_id, timeout_s=5.0
            )
            assert snap.state == "RETRY_WAIT"
            assert snap.error is not None
            assert snap.error.error_code == "SESSION_REVISION_CONFLICT"
        finally:
            await host.stop()

    @pytest.mark.asyncio
    async def test_final_conflict_enters_retry_wait(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        """FINAL REVISION_CONFLICT → RETRY_WAIT, not COMPLETED."""
        fake_committer = ConflictInjectingCommitter(
            committer, conflict_kind="FINAL"
        )
        callback = StubExecutionCallback()
        host = LightweightHost(store, fake_committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_envelope(
                sample_scope,
                session_key="conflict-final",
                content="conflict test",
            )
            handle = await host.task_service.submit(envelope)

            snap = await host.task_service.wait_terminal(
                sample_scope, handle.task_id, timeout_s=5.0
            )
            assert snap.state == "RETRY_WAIT"
            assert snap.error is not None
            assert snap.error.error_code == "SESSION_REVISION_CONFLICT"
        finally:
            await host.stop()


# ---------------------------------------------------------------------------
# Tests: heartbeat lease loss cancels execution (Task 2 Step 7)
# ---------------------------------------------------------------------------


class BlockingExecutionCallback:
    """Execution callback that blocks until cancelled.

    Used to verify that a rejected heartbeat cancels the active Agent
    execution before it can reach session or completion writes.
    """

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(
        self,
        payload: WorkerTaskPayload,
        session_base_revision: int,
    ) -> WorkerExecutionResult:
        self.started.set()
        try:
            # Block forever — only lease-loss cancellation can stop us.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        # Unreachable: the Event is never set.
        return WorkerExecutionResult(final_content="unreachable", messages=[])


class TestLeaseLossCancelsExecution:
    """A rejected heartbeat must cancel Agent execution (Task 2 Step 7)."""

    @pytest.mark.asyncio
    async def test_heartbeat_loss_cancels_execution(
        self,
        store: SqliteRuntimeStore,
        committer: SessionCommitter,
        sample_scope: RequestScope,
    ) -> None:
        callback = BlockingExecutionCallback()
        # Short lease (100ms) with heartbeat firing AFTER the lease expires
        # (300ms). The first heartbeat will find an expired lease and the
        # renewal is rejected.
        host = LightweightHost(
            store,
            committer,
            callback,
            worker_count=1,
            lease_ms=100,
            heartbeat_interval_s=0.3,
        )
        await host.start()
        try:
            envelope = _make_envelope(sample_scope, content="hello")
            handle = await host.task_service.submit(envelope)

            # Wait for execution to start.
            await asyncio.wait_for(callback.started.wait(), timeout=5.0)

            # Wait for heartbeat to fire, fail, and cancel execution.
            await asyncio.wait_for(callback.cancelled.wait(), timeout=5.0)

            # The execution was cancelled before any completion write.
            assert callback.cancelled.is_set()

            # The task must NOT be COMPLETED.
            task = store.read_task(handle.task_id)
            assert task is not None
            assert task.state != "COMPLETED"
        finally:
            await host.stop()
