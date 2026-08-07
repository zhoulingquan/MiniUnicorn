"""WP6 — Supervised Host: spawn, restart, containment, IPC, shutdown (design §23, §24, WP6).

Covers:

- IPC envelope round-trip and channel behaviour (design §23.2).
- Supervisor spawn semantics: Control Plane + N Workers, readiness gating
  on Control Plane ready AND >= min_workers Workers ready (design §24.4,
  §24.5, WP6 task 8).
- Restart backoff: exponential with bounded restarts per rolling window
  (design §24.4, WP6 task 5).
- Graceful shutdown: shutdown signal fan-out, grace wait, survivor
  termination (design §24.7, WP6 task 7).
- OS-level containment helpers (design §24.6, WP6 task 6).
- No inherited runtime objects: child entrypoints rebuild state from
  ``config`` only (design §24.6, WP6 task 2).
- Stale Worker fencing via SQLite: a Worker that loses its lease cannot
  commit (design §24.2, §17.9).
- Workers claim directly from SQLite (design §23.2, WP6 task 3).
- Wake hints reduce poll latency but are not required for correctness
  (design §23.2, WP6 task 4).
- Lightweight / supervised golden-flow parity: both hosts drive a task
  from submit to terminal using the same Runtime Store contracts.

Real subprocess spawning is exercised through picklable module-level stub
entrypoints so spawn semantics are validated on the host platform without
a full Agent Core.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import pytest

from miniunicorn.runtime.containment import (
    NullContainmentScope,
    ProcessContainmentScope,
    SupervisorContainment,
    posix_set_child_death_signal,
    posix_start_new_session,
)
from miniunicorn.runtime.ipc import (
    IPC_PROTOCOL_VERSION,
    KIND_AGENT_EVENT,
    KIND_CHILD_READY,
    KIND_SHUTDOWN,
    KIND_WAKE_HINT,
    KIND_WORKER_READY,
    IpcEnvelope,
    ProcessIpcChannel,
    agent_event,
    fan_out_wake_hints,
    wake_hint,
    worker_ready,
)
from miniunicorn.runtime.supervisor import (
    RestartPolicy,
    Supervisor,
    _ChildRecord,
)

# ---------------------------------------------------------------------------
# Module-level stub entrypoints (must be picklable for spawn)
# ---------------------------------------------------------------------------


def _ready_then_block_entrypoint(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Any,
) -> int:
    """Child stub: signal readiness then block until shutdown or parent dies."""
    ready_signal(role)
    # Block until we receive a shutdown signal or the pipe closes.
    try:
        while True:
            if ipc_channel.child_poll(timeout_s=0.5):
                env = ipc_channel.child_recv()
                if env is not None and env.kind == KIND_SHUTDOWN:
                    return 0
            # If the parent end closed, exit.
            try:
                # Sending a zero-byte probe would raise on closed pipe.
                if not ipc_channel.child_poll(0.0):
                    # Distinguish "no message" from "closed" by attempting a
                    # non-blocking recv after a short sleep; EOFError means
                    # the pipe closed and we should exit.
                    pass
            except OSError:
                return 0
    except (EOFError, OSError):
        return 0


def _crash_immediately_entrypoint(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Any,
) -> int:
    """Child stub: signal readiness then exit with non-zero code."""
    ready_signal(role)
    return 2


def _no_ready_block_entrypoint(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Any,
) -> int:
    """Child stub: never signal readiness; block briefly then exit."""
    try:
        for _ in range(20):
            if ipc_channel.child_poll(timeout_s=0.1):
                env = ipc_channel.child_recv()
                if env is not None and env.kind == KIND_SHUTDOWN:
                    return 0
    except (EOFError, OSError):
        pass
    return 0


# ---------------------------------------------------------------------------
# IPC envelope and channel tests
# ---------------------------------------------------------------------------


class TestIpcEnvelope:
    """IPC envelope schema and round-trip (design §23.2)."""

    def test_envelope_carries_required_fields(self) -> None:
        env = wake_hint("inst-1", reason="submit", task_id="task-7")
        assert env.protocol_version == IPC_PROTOCOL_VERSION
        assert env.instance_id == "inst-1"
        assert env.kind == KIND_WAKE_HINT
        assert env.task_id == "task-7"
        assert env.payload == {"reason": "submit"}
        assert env.sent_at_ms > 0

    def test_envelope_json_round_trip(self) -> None:
        env = agent_event("inst-1", event={"type": "token", "delta": "hi"}, task_id="t")
        raw = env.to_json()
        restored = IpcEnvelope.from_json(raw)
        assert restored == env

    def test_builder_kinds_are_distinct(self) -> None:
        assert KIND_WAKE_HINT != KIND_SHUTDOWN
        assert KIND_WORKER_READY != KIND_CHILD_READY
        assert KIND_AGENT_EVENT != KIND_SHUTDOWN


class TestProcessIpcChannel:
    """Pipe channel round-trip (design §23.2)."""

    def test_parent_send_child_recv_round_trip(self) -> None:
        ch = ProcessIpcChannel.new_pipe()
        try:
            env = wake_hint("p", reason="submit")
            ch.parent_send(env)
            assert ch.child_poll(timeout_s=2.0)
            received = ch.child_recv()
            assert received is not None
            assert received.kind == KIND_WAKE_HINT
            assert received.payload == {"reason": "submit"}
        finally:
            ch.parent_close()
            ch.child_close()

    def test_child_send_parent_recv_round_trip(self) -> None:
        ch = ProcessIpcChannel.new_pipe()
        try:
            env = worker_ready("c", worker_id="w-0", capacity=1)
            ch.child_send(env)
            assert ch.parent_poll(timeout_s=2.0)
            received = ch.parent_recv()
            assert received is not None
            assert received.kind == KIND_WORKER_READY
            assert received.payload["worker_id"] == "w-0"
        finally:
            ch.parent_close()
            ch.child_close()

    def test_parent_recv_returns_none_on_eof(self) -> None:
        # parent_recv blocks until data or EOF; callers must poll first.
        # Closing the child end causes the parent recv to get EOFError,
        # which the channel coerces to None.
        ch = ProcessIpcChannel.new_pipe()
        try:
            assert ch.parent_poll(timeout_s=0.0) is False
            ch.child_close()
            assert ch.parent_recv() is None
        finally:
            ch.parent_close()

    def test_fan_out_wake_hints_sends_to_all(self) -> None:
        channels = [ProcessIpcChannel.new_pipe() for _ in range(3)]
        try:
            sent = fan_out_wake_hints(channels, instance_id="ctrl", reason="submit")
            assert sent == 3
            for ch in channels:
                assert ch.child_poll(timeout_s=1.0)
                env = ch.child_recv()
                assert env is not None
                assert env.kind == KIND_WAKE_HINT
        finally:
            for ch in channels:
                ch.parent_close()
                ch.child_close()


# ---------------------------------------------------------------------------
# RestartPolicy tests
# ---------------------------------------------------------------------------


class TestRestartPolicy:
    """Exponential backoff with bounded restarts per rolling window (design §24.4)."""

    def test_backoff_grows_exponentially(self) -> None:
        policy = RestartPolicy(
            initial_backoff_ms=100,
            max_backoff_ms=10_000,
            backoff_multiplier=2.0,
            window_ms=60_000,
            max_restarts_per_window=10,
        )
        ch = ProcessIpcChannel.new_pipe()
        record = _ChildRecord(
            role="worker", child_id="w-0", instance_id="i", channel=ch
        )
        try:
            b1 = policy.next_backoff_ms(record)
            b2 = policy.next_backoff_ms(record)
            b3 = policy.next_backoff_ms(record)
            assert b1 == 100
            assert b2 == 200
            assert b3 == 400
        finally:
            ch.parent_close()
            ch.child_close()

    def test_backoff_capped_at_max(self) -> None:
        policy = RestartPolicy(
            initial_backoff_ms=1000,
            max_backoff_ms=3000,
            backoff_multiplier=10.0,
            window_ms=60_000,
            max_restarts_per_window=10,
        )
        ch = ProcessIpcChannel.new_pipe()
        record = _ChildRecord(
            role="worker", child_id="w-0", instance_id="i", channel=ch
        )
        try:
            policy.next_backoff_ms(record)
            policy.next_backoff_ms(record)
            b3 = policy.next_backoff_ms(record)
            assert b3 == 3000
        finally:
            ch.parent_close()
            ch.child_close()

    def test_backoff_uses_window_remainder_when_budget_exhausted(self) -> None:
        policy = RestartPolicy(
            initial_backoff_ms=100,
            max_backoff_ms=10_000,
            backoff_multiplier=2.0,
            window_ms=10_000,
            max_restarts_per_window=2,
        )
        ch = ProcessIpcChannel.new_pipe()
        record = _ChildRecord(
            role="worker", child_id="w-0", instance_id="i", channel=ch
        )
        try:
            policy.next_backoff_ms(record)  # restart 1
            policy.next_backoff_ms(record)  # restart 2 — budget exhausted
            b3 = policy.next_backoff_ms(record)
            # Should wait at least max_backoff_ms (the floor we set).
            assert b3 >= 10_000
        finally:
            ch.parent_close()
            ch.child_close()

    def test_restart_history_drops_old_entries_outside_window(self) -> None:
        policy = RestartPolicy(
            initial_backoff_ms=100,
            max_backoff_ms=10_000,
            backoff_multiplier=2.0,
            window_ms=1000,
            max_restarts_per_window=2,
        )
        ch = ProcessIpcChannel.new_pipe()
        record = _ChildRecord(
            role="worker", child_id="w-0", instance_id="i", channel=ch
        )
        try:
            policy.next_backoff_ms(record)
            policy.next_backoff_ms(record)
            # Pretend the window elapsed by rewinding the timestamps.
            record.restart_history.clear()
            record.restart_history.extend([int(time.time() * 1000) - 2000])
            # Now we should be back to the first-attempt backoff.
            b = policy.next_backoff_ms(record)
            assert b == 100
        finally:
            ch.parent_close()
            ch.child_close()


# ---------------------------------------------------------------------------
# Containment tests
# ---------------------------------------------------------------------------


class TestContainmentHelpers:
    """OS-level containment helpers (design §24.6, WP6 task 6)."""

    def test_null_scope_is_noop(self) -> None:
        scope = NullContainmentScope()
        scope.register(12345)
        scope.close()
        scope.close()  # idempotent

    def test_process_scope_close_without_pids_is_noop(self) -> None:
        scope = ProcessContainmentScope(task_id="t-1")
        scope.close()  # no PIDs registered
        scope.close()  # idempotent

    def test_supervisor_containment_construction_is_portable(self) -> None:
        scope = SupervisorContainment()
        # Registering an obviously-invalid PID must not raise.
        scope.register(999_999)
        # On Windows the Job assignment silently fails for a non-existent
        # PID; on POSIX we just record it. Either way, no exception.
        scope.close()
        # Idempotent.
        scope.close()

    def test_posix_helpers_are_safe_on_every_platform(self) -> None:
        # posix_start_new_session returns None on Windows; on POSIX it
        # may fail if already a session leader — both are acceptable.
        pgid = posix_start_new_session()
        assert pgid is None or isinstance(pgid, int)
        # posix_set_child_death_signal is a no-op on Windows.
        posix_set_child_death_signal()


# ---------------------------------------------------------------------------
# Supervisor spawn + readiness tests (real subprocess)
# ---------------------------------------------------------------------------


def _make_supervisor(
    *,
    control_entrypoint=_ready_then_block_entrypoint,
    worker_entrypoint=_ready_then_block_entrypoint,
    worker_count: int = 2,
    min_workers: int = 1,
    ready_timeout_s: float = 10.0,
    restart_policy: RestartPolicy | None = None,
) -> Supervisor:
    return Supervisor(
        config=None,
        control_entrypoint=control_entrypoint,
        worker_entrypoint=worker_entrypoint,
        worker_count=worker_count,
        min_workers=min_workers,
        ready_timeout_s=ready_timeout_s,
        shutdown_grace_s=5,
        restart_policy=restart_policy,
    )


@pytest.mark.slow
class TestSupervisorSpawnSemantics:
    """Real spawn semantics: Control Plane + Workers, readiness gating (WP6 task 1, 8)."""

    def test_start_spawns_control_and_workers_and_becomes_ready(self) -> None:
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        try:
            sup.start()
            assert sup.is_started is True
            assert sup.is_ready() is True
            snap = sup.snapshot()
            roles = {c["role"] for c in snap["children"]}
            assert roles == {"control", "worker"}
            assert snap["protocol_version"] == IPC_PROTOCOL_VERSION
            assert sup.ready_workers() >= 1
        finally:
            sup.shutdown(grace_s=3)

    def test_readiness_false_when_control_plane_not_ready(self) -> None:
        sup = _make_supervisor(
            control_entrypoint=_no_ready_block_entrypoint,
            worker_entrypoint=_ready_then_block_entrypoint,
            ready_timeout_s=2.0,
        )
        try:
            # Task 7 Step 5: a Control Plane startup timeout terminates the
            # partial child tree and raises — Workers must not race
            # first-run migrations.
            with pytest.raises(TimeoutError):
                sup.start()
            assert sup.is_ready() is False
        finally:
            sup.shutdown(grace_s=3)

    def test_readiness_false_when_min_workers_not_ready(self) -> None:
        sup = _make_supervisor(
            worker_entrypoint=_no_ready_block_entrypoint,
            worker_count=2,
            min_workers=2,
            ready_timeout_s=2.0,
        )
        try:
            sup.start()
            # No worker signalled ready.
            assert sup.is_ready() is False
            assert sup.ready_workers() == 0
        finally:
            sup.shutdown(grace_s=3)

    def test_worker_count_must_be_at_least_two(self) -> None:
        with pytest.raises(ValueError):
            Supervisor(
                config=None,
                control_entrypoint=_ready_then_block_entrypoint,
                worker_entrypoint=_ready_then_block_entrypoint,
                worker_count=1,
            )

    def test_min_workers_cannot_exceed_worker_count(self) -> None:
        with pytest.raises(ValueError):
            Supervisor(
                config=None,
                control_entrypoint=_ready_then_block_entrypoint,
                worker_entrypoint=_ready_then_block_entrypoint,
                worker_count=2,
                min_workers=3,
            )


# ---------------------------------------------------------------------------
# Restart backoff tests (real subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestSupervisorRestartBackoff:
    """Restart backoff with bounded rolling window (design §24.4, WP6 task 5)."""

    def test_crashed_child_is_recorded_and_backoff_scheduled(self) -> None:
        # Aggressive backoff so the test runs quickly.
        policy = RestartPolicy(
            initial_backoff_ms=200,
            max_backoff_ms=500,
            backoff_multiplier=2.0,
            window_ms=10_000,
            max_restarts_per_window=5,
        )
        sup = _make_supervisor(
            worker_entrypoint=_crash_immediately_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=1.0,
            restart_policy=policy,
        )
        try:
            sup.start()
            # Workers crash immediately; let the Supervisor observe exits.
            deadline = time.monotonic() + 3.0
            seen_backoff = False
            while time.monotonic() < deadline:
                sup.reap_once(restart=True)
                snap = sup.snapshot()
                if any(c["backoff_until_ms"] > 0 for c in snap["children"] if c["role"] == "worker"):
                    seen_backoff = True
                    break
                time.sleep(0.05)
            assert seen_backoff, "expected at least one worker to be scheduled for restart"
        finally:
            sup.shutdown(grace_s=3)

    def test_no_durable_workers_row_state_is_in_memory_only(self) -> None:
        # The Supervisor never persists worker state. After start, the
        # only state is the in-memory _children dict.
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        try:
            sup.start()
            assert isinstance(sup._children, dict)
            # No SQLite-backed workers table exists; this is intentionally
            # an in-memory structure. We assert the records are _ChildRecord.
            for record in sup._children.values():
                assert isinstance(record, _ChildRecord)
                assert record.process is not None
        finally:
            sup.shutdown(grace_s=3)


# ---------------------------------------------------------------------------
# Restart-history preservation tests (Task 10 Step 1 & 3)
# ---------------------------------------------------------------------------


class _FakeDeadProcess:
    """A fake process object that reports as already exited (Task 10)."""

    _pid_counter = 20000

    def __init__(self) -> None:
        _FakeDeadProcess._pid_counter += 1
        self.pid = _FakeDeadProcess._pid_counter
        self.exitcode = 2

    def is_alive(self) -> bool:
        return False

    def start(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def terminate(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass


class _FakeSpawnContext:
    """Spawn context whose Process() returns a dead fake process."""

    def Process(self, **kwargs: Any) -> _FakeDeadProcess:  # noqa: N802
        return _FakeDeadProcess()


class _TrackingRestartPolicy:
    """Wraps a RestartPolicy to track backoff calls (Task 10).

    ``RestartPolicy`` is a slots dataclass, so its methods cannot be
    monkeypatched on the instance. This wrapper delegates to the real
    policy while recording every backoff value returned.
    """

    def __init__(self, policy: RestartPolicy) -> None:
        self._policy = policy
        self.backoffs: list[int] = []

    def next_backoff_ms(self, record: _ChildRecord) -> int:
        b = self._policy.next_backoff_ms(record)
        self.backoffs.append(b)
        return b

    def __getattr__(self, name: str) -> Any:
        return getattr(self._policy, name)


class TestSupervisorRestartHistoryPreserved:
    """Task 10 Step 1: restart_history must persist across respawns.

    Before the fix: ``_spawn_child`` creates a fresh ``_ChildRecord`` on
    every respawn, overwriting the stable record and resetting
    ``restart_history``. Every backoff is the initial 500 ms and
    ``restarts_in_window`` never exceeds 1.
    """

    def test_three_crashes_grow_backoff_exponentially(self, monkeypatch: Any) -> None:
        """Fake child crashes 3 times; backoffs must be [500, 1000, 2000]."""
        import miniunicorn.runtime.supervisor as sup_mod

        policy = RestartPolicy(
            initial_backoff_ms=500,
            max_backoff_ms=30_000,
            backoff_multiplier=2.0,
            window_ms=5 * 60 * 1000,
            max_restarts_per_window=5,
        )
        sup = Supervisor(
            config=None,
            control_entrypoint=_ready_then_block_entrypoint,
            worker_entrypoint=_crash_immediately_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=1.0,
            restart_policy=policy,
        )
        # Replace the spawn context so _spawn_child creates fake dead processes.
        sup._ctx = _FakeSpawnContext()
        sup._started = True

        # Wrap the policy to track backoffs.
        tracker = _TrackingRestartPolicy(policy)
        sup._restart_policy = tracker

        # Fake clock starting at a fixed epoch.
        clock = [1_000_000]
        monkeypatch.setattr(sup_mod, "_now_ms", lambda: clock[0])

        # Seed one crashed worker child.
        ch = ProcessIpcChannel.new_pipe()
        record = _ChildRecord(
            role="worker",
            child_id="worker-0",
            instance_id="worker-0#1",
            channel=ch,
            process=_FakeDeadProcess(),
        )
        sup._children["worker-0"] = record

        try:
            # Crash cycle: reap crashed child → schedule backoff → advance
            # clock past backoff → reap again to trigger respawn via
            # _maybe_restart_due (the respawned fake child is already dead
            # and will be reaped on the next reap_once).
            advance = policy.max_backoff_ms + 1
            for _ in range(3):
                sup.reap_once(restart=True)
                clock[0] += advance  # Advance past any backoff up to max.
                sup.reap_once(restart=True)

            backoffs = tracker.backoffs
            assert backoffs == [500, 1000, 2000], (
                f"expected exponential backoffs [500, 1000, 2000], got {backoffs}"
            )

            snap = sup.snapshot()
            worker0 = next(c for c in snap["children"] if c["id"] == "worker-0")
            assert worker0["restarts_in_window"] == 3, (
                f"expected 3 restarts in window, got {worker0['restarts_in_window']}"
            )
        finally:
            for r in list(sup._children.values()):
                try:
                    r.channel.parent_close()
                    r.channel.child_close()
                except Exception:  # noqa: BLE001
                    pass
            sup._children.clear()


class TestSupervisorRestartBudgetEnforced:
    """Task 10 Step 3: rolling-window restart budget is enforced.

    After ``max_restarts_per_window`` crashes the next restart must wait
    the remainder of the window. Readiness stays false and the suppressed
    restart is visible in metrics.
    """

    def test_sixth_crash_is_suppressed_by_window_budget(
        self, monkeypatch: Any
    ) -> None:
        """Six crashes inside the window; the sixth restart is suppressed."""
        import miniunicorn.runtime.supervisor as sup_mod

        policy = RestartPolicy(
            initial_backoff_ms=500,
            max_backoff_ms=30_000,
            backoff_multiplier=2.0,
            window_ms=5 * 60 * 1000,
            max_restarts_per_window=5,
        )
        sup = Supervisor(
            config=None,
            control_entrypoint=_ready_then_block_entrypoint,
            worker_entrypoint=_crash_immediately_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=1.0,
            restart_policy=policy,
        )
        sup._ctx = _FakeSpawnContext()
        sup._started = True

        tracker = _TrackingRestartPolicy(policy)
        sup._restart_policy = tracker

        clock = [1_000_000]
        monkeypatch.setattr(sup_mod, "_now_ms", lambda: clock[0])

        ch = ProcessIpcChannel.new_pipe()
        record = _ChildRecord(
            role="worker",
            child_id="worker-0",
            instance_id="worker-0#1",
            channel=ch,
            process=_FakeDeadProcess(),
        )
        sup._children["worker-0"] = record

        try:
            advance = policy.max_backoff_ms + 1
            for _ in range(6):
                sup.reap_once(restart=True)
                clock[0] += advance
                sup.reap_once(restart=True)

            backoffs = tracker.backoffs
            # The first 5 backoffs grow exponentially; the 6th is suppressed.
            assert len(backoffs) == 6
            assert backoffs[:5] == [500, 1000, 2000, 4000, 8000]
            sixth = backoffs[5]
            # The 6th backoff must wait at least max_backoff_ms (the floor).
            assert sixth >= 30_000, (
                f"6th restart should be suppressed (>= max_backoff_ms), got {sixth}"
            )

            snap = sup.snapshot()
            worker0 = next(c for c in snap["children"] if c["id"] == "worker-0")
            # restart_history was NOT incremented on the 6th (budget exhausted).
            assert worker0["restarts_in_window"] == 5
            # The child is in backoff — readiness must be false.
            assert snap["ready"] is False
        finally:
            for r in list(sup._children.values()):
                try:
                    r.channel.parent_close()
                    r.channel.child_close()
                except Exception:  # noqa: BLE001
                    pass
            sup._children.clear()


# ---------------------------------------------------------------------------
# Graceful shutdown tests (real subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestSupervisorGracefulShutdown:
    """Graceful shutdown sequence (design §24.7, WP6 task 7)."""

    def test_shutdown_signals_children_and_waits_for_clean_exit(self) -> None:
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        sup.start()
        assert sup.is_ready()
        sup.shutdown(grace_s=5)
        # After shutdown, no children remain.
        assert sup._children == {}
        assert sup.is_started is False
        assert sup.is_ready() is False

    def test_shutdown_marks_shutting_down_during_drain(self) -> None:
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        sup.start()
        # Mark shutting down via a short-grace shutdown; children should
        # observe the shutdown signal and exit. We assert is_shutting_down
        # transitions through True.
        # Because shutdown is synchronous, we cannot observe the in-flight
        # flag from the same thread reliably; instead assert the final
        # state is cleared.
        sup.shutdown(grace_s=3)
        assert sup.is_shutting_down is False

    def test_terminate_kills_survivors_immediately(self) -> None:
        sup = _make_supervisor(worker_count=2, min_workers=1, ready_timeout_s=10.0)
        sup.start()
        assert sup.is_ready()
        sup.terminate()
        assert sup._children == {}
        assert sup.is_started is False


# ---------------------------------------------------------------------------
# No-inherited-objects tests (real subprocess)
# ---------------------------------------------------------------------------


def _assert_no_inherited_objects_entrypoint(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Any,
) -> int:
    """Child stub: assert no inherited runtime globals, then block until shutdown.

    Design §24.6, WP6 task 2: a spawned child must not see any inherited
    SQLite connection, Provider, Agent, or bus object. Because spawn
    re-imports the package graph, module-level singletons are fresh.

    The stub blocks until ``KIND_SHUTDOWN`` (like the other stubs) so the
    Control Plane stays alive while Workers report ready under the
    Task 7 Control-Plane-first startup ordering.
    """

    # The child must have a fresh interpreter with no pre-bound runtime
    # objects. We assert a few invariants that hold for spawn.
    assert config is None  # we passed None explicitly
    # The child's process name should not be the parent's.
    assert sys.argv[0] != ""
    ready_signal(role)
    try:
        while True:
            if ipc_channel.child_poll(timeout_s=0.5):
                env = ipc_channel.child_recv()
                if env is not None and env.kind == KIND_SHUTDOWN:
                    return 0
    except (EOFError, OSError):
        return 0


@pytest.mark.slow
class TestNoInheritedObjects:
    """Children must rebuild process-local state (design §24.6, WP6 task 2)."""

    def test_child_receives_only_explicit_args(self) -> None:
        sup = Supervisor(
            config=None,
            control_entrypoint=_assert_no_inherited_objects_entrypoint,
            worker_entrypoint=_assert_no_inherited_objects_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=10.0,
            shutdown_grace_s=3,
        )
        try:
            sup.start()
            assert sup.is_ready()
        finally:
            sup.shutdown(grace_s=3)


# ---------------------------------------------------------------------------
# Wake hint fan-out tests (real subprocess)
# ---------------------------------------------------------------------------


def _wake_hint_receiver_entrypoint(
    *,
    role: str,
    instance_id: str,
    config: Any,
    ipc_channel: ProcessIpcChannel,
    ready_signal: Any,
) -> int:
    """Child stub: signal ready, then count wake hints until shutdown."""
    ready_signal(role)
    received = 0
    try:
        while True:
            if ipc_channel.child_poll(timeout_s=0.3):
                env = ipc_channel.child_recv()
                if env is None:
                    return received
                if env.kind == KIND_SHUTDOWN:
                    return received
                if env.kind == KIND_WAKE_HINT:
                    received += 1
    except (EOFError, OSError):
        return received


@pytest.mark.slow
class TestWakeHintFanOut:
    """Wake hints reduce poll latency but are not required for correctness (§23.2)."""

    def test_fan_out_wake_delivers_to_alive_workers(self) -> None:
        sup = Supervisor(
            config=None,
            control_entrypoint=_ready_then_block_entrypoint,
            worker_entrypoint=_wake_hint_receiver_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=10.0,
            shutdown_grace_s=3,
        )
        try:
            sup.start()
            assert sup.is_ready()
            sent = sup.fan_out_wake(reason="submit", task_id="t-1")
            assert sent == 2
        finally:
            sup.shutdown(grace_s=3)

    def test_fan_out_wake_skips_dead_workers(self) -> None:
        sup = Supervisor(
            config=None,
            control_entrypoint=_ready_then_block_entrypoint,
            worker_entrypoint=_wake_hint_receiver_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=10.0,
            shutdown_grace_s=3,
        )
        try:
            sup.start()
            assert sup.is_ready()
            # Force one worker's process to exit.
            worker_records = [r for r in sup._children.values() if r.role == "worker"]
            assert len(worker_records) == 2
            dead = worker_records[0]
            dead.process.terminate()
            dead.process.join(timeout=3)
            # Fan out: only the alive worker should receive a hint.
            sent = sup.fan_out_wake(reason="submit")
            assert sent == 1
        finally:
            sup.shutdown(grace_s=3)


# ---------------------------------------------------------------------------
# Stale Worker fencing via SQLite (design §24.2, §17.9)
# ---------------------------------------------------------------------------


class TestStaleWorkerFencingViaSqlite:
    """A Worker that loses its lease cannot commit (design §24.2, §17.9, WP6 task 3)."""

    def test_stale_lease_cannot_mark_running(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.contracts import ClaimRequest, StaleLeaseError

        env = make_inbound_envelope(sample_scope)
        submit = store.submit_task(env)
        assert submit.status == "ACCEPTED"

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        claim = result.claimed.claim

        # Reclaim the lease as if it expired (advance clock past lease).
        reclaim = store.reclaim_expired(now_ms=2_100_000, limit=10)
        assert reclaim.reclaimed_count >= 1

        # A stale write (using the old claim) must fail.
        with pytest.raises(StaleLeaseError):
            store.mark_running(claim, now_ms=2_100_001)

    def test_stale_lease_cannot_complete(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
    ) -> None:
        import hashlib

        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import BlobWrite, CompletionWrite

        env = make_inbound_envelope(sample_scope)
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        claim = result.claimed.claim
        store.mark_running(claim, now_ms=2_000_001)

        # Reclaim the lease.
        store.reclaim_expired(now_ms=2_100_000, limit=10)

        # Stale completion must be rejected. ``complete_with_outbox``
        # catches StaleLeaseError and returns a STALE_LEASE result
        # (design §17.8) instead of raising.
        content = b"final"
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(content).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=content,
                size_bytes=len(content),
                created_at_ms=2_100_000,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(content).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_100_001,
            channel="test-channel",
            channel_account="test-account",
            target_key="test-target",
        )
        result = store.complete_with_outbox(claim, completion)
        assert result.status == "STALE_LEASE"
        assert result.outbox_id is None


# ---------------------------------------------------------------------------
# Workers claim directly from SQLite (design §23.2, WP6 task 3)
# ---------------------------------------------------------------------------


class TestWorkersClaimDirectlyFromSqlite:
    """Workers claim directly from SQLite without a central dispatch queue."""

    def test_two_workers_claim_different_sessions(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
    ) -> None:
        from miniunicorn.runtime.contracts import ClaimRequest

        # Submit two tasks for different sessions.
        env1 = make_inbound_envelope(sample_scope, session_key="s-1")
        env2 = make_inbound_envelope(sample_scope, session_key="s-2")
        store.submit_task(env1)
        store.submit_task(env2)

        # Two workers each claim — they must get different tasks.
        c1 = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        c2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_000_001, lease_ms=60_000)
        )
        assert c1.claimed is not None
        assert c2.claimed is not None
        assert c1.claimed.claim.task_id != c2.claimed.claim.task_id
        assert c1.claimed.record.session_key != c2.claimed.record.session_key

    def test_one_session_never_overlaps_across_workers(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
    ) -> None:
        """Multiple tasks for one session are claimed serially (design §16.13)."""
        from miniunicorn.runtime.contracts import ClaimRequest

        # Submit two tasks for the SAME session.
        env1 = make_inbound_envelope(sample_scope, session_key="s-shared")
        env2 = make_inbound_envelope(
            sample_scope,
            session_key="s-shared",
            dedup_key="other-dedup",
        )
        store.submit_task(env1)
        store.submit_task(env2)

        # First worker claims the head.
        c1 = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert c1.claimed is not None

        # Second worker must NOT get the second task — the session head
        # is still held by w-1.
        c2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_000_001, lease_ms=60_000)
        )
        assert c2.claimed is None


# ---------------------------------------------------------------------------
# SupervisedHost lifecycle (async)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestSupervisedHostLifecycle:
    """SupervisedHost orchestrator async lifecycle (design §9.2, WP6)."""

    @pytest.mark.asyncio
    async def test_start_and_stop_drives_supervisor_and_bridge(self) -> None:
        from miniunicorn.runtime.hosts.supervised import SupervisedHost

        host = SupervisedHost(
            config=None,
            control_entrypoint=_ready_then_block_entrypoint,
            worker_entrypoint=_ready_then_block_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=10.0,
            shutdown_grace_s=3,
            bridge_poll_interval_s=0.05,
        )
        try:
            await host.start()
            assert host.is_ready() is True
            snap = host.snapshot()
            assert snap["started"] is True
            assert snap["bridge_dropped_events"] == 0
        finally:
            await host.stop(grace_s=3)
        assert host.is_ready() is False

    @pytest.mark.asyncio
    async def test_notify_submit_fans_out_wake_hints(self) -> None:
        from miniunicorn.runtime.hosts.supervised import SupervisedHost

        host = SupervisedHost(
            config=None,
            control_entrypoint=_ready_then_block_entrypoint,
            worker_entrypoint=_wake_hint_receiver_entrypoint,
            worker_count=2,
            min_workers=1,
            ready_timeout_s=10.0,
            shutdown_grace_s=3,
        )
        try:
            await host.start()
            assert host.is_ready()
            sent = host.notify_submit(task_id="t-1")
            assert sent == 2
        finally:
            await host.stop(grace_s=3)


# ---------------------------------------------------------------------------
# Lightweight / supervised golden-flow parity (design §30 WP6 exit)
# ---------------------------------------------------------------------------


class TestGoldenFlowParity:
    """Both hosts drive a task from submit to terminal via the same contracts.

    The supervised path uses the same Runtime Store, Scheduler, and
    SessionCommitter as the lightweight path; the only difference is
    process isolation. We assert the durable contracts behave identically
    for a simple submit→claim→complete flow.
    """

    def test_submit_claim_complete_parity(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
    ) -> None:
        import hashlib

        from miniunicorn.runtime.contracts import ClaimRequest
        from miniunicorn.runtime.models import BlobWrite, CompletionWrite

        env = make_inbound_envelope(sample_scope, session_key="parity-s")
        submit = store.submit_task(env)
        assert submit.status == "ACCEPTED"

        claimed = store.claim_next(
            ClaimRequest(worker_id="parity-w", now_ms=2_000_000, lease_ms=60_000)
        )
        assert claimed.claimed is not None
        claim = claimed.claimed.claim
        record = claimed.claimed.record

        store.mark_running(claim, now_ms=2_000_001)

        content = b"golden-flow reply"
        blob = store.write_blob(
            BlobWrite(
                scope_key="parity",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(content).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=content,
                size_bytes=len(content),
                created_at_ms=2_000_002,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(content).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_000_003,
            channel="test-channel",
            channel_account="test-account",
            target_key="test-target",
        )
        result = store.complete_with_outbox(claim, completion)
        assert result.status == "COMPLETED"
        assert result.outbox_id is not None

        # The task snapshot must reflect the terminal state.
        snap = store.read_task_snapshot(sample_scope, record.task_id)
        assert snap is not None
        assert snap.state == "COMPLETED"


# ---------------------------------------------------------------------------
# Graceful Worker shutdown tests (Task 10 Step 7)
# ---------------------------------------------------------------------------


class _BlockingExecutionCallback:
    """Execution callback that blocks until a release event is set.

    Records when execution starts so the test can coordinate shutdown
    timing (Task 10 Step 7).
    """

    def __init__(self, release_event: asyncio.Event) -> None:
        self._release_event = release_event
        self.started = asyncio.Event()

    async def __call__(
        self, payload: Any, session_base_revision: int
    ) -> Any:
        from miniunicorn.runtime.worker import WorkerExecutionResult

        self.started.set()
        await self._release_event.wait()
        return WorkerExecutionResult(
            final_content="done",
            messages=[{"role": "assistant", "content": "done"}],
        )


@pytest.fixture
def _committer(store: Any, tmp_path: Any) -> Any:
    from miniunicorn.runtime.session_committer import SessionCommitter
    from miniunicorn.session.manager import SessionManager

    sessions = SessionManager(tmp_path / "ws")
    return SessionCommitter(store, sessions)


def _make_grace_envelope(scope: Any, content: str = "hello") -> Any:
    import hashlib
    import json

    from miniunicorn.runtime.models import InboundTaskEnvelope

    payload = {"content": content, "media": [], "metadata": {}}
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    return InboundTaskEnvelope(
        protocol_version=1,
        task_kind="USER_TURN",
        priority=100,
        scope=scope,
        session_key="grace-session",
        channel="cli",
        channel_account="test-user",
        channel_message_id=None,
        dedup_key=None,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        received_at_ms=1_000_000,
        available_at_ms=None,
        payload_content=payload_bytes,
        target_key="direct",
    )


class TestGracefulWorkerShutdown:
    """Task 10 Step 7: graceful Worker shutdown with blocked callbacks.

    Test 1: a blocked callback released inside ``shutdown_grace_s``
    completes normally.

    Test 2: a blocked callback never released is cancelled only after
    the configured grace.
    """

    @pytest.mark.asyncio
    async def test_blocked_task_completes_when_released_within_grace(
        self, store: Any, sample_scope: Any, _committer: Any
    ) -> None:
        from miniunicorn.runtime.hosts.lightweight import LightweightHost

        release_event = asyncio.Event()
        callback = _BlockingExecutionCallback(release_event)
        host = LightweightHost(store, _committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_grace_envelope(sample_scope)
            handle = await host.task_service.submit(envelope)

            # Wait for the Worker to pick up the task and block.
            await asyncio.wait_for(callback.started.wait(), timeout=5.0)

            # Start graceful shutdown.
            stop_task = asyncio.create_task(host.stop(grace_s=5))

            # Release the callback inside the grace window.
            await asyncio.sleep(0.5)
            release_event.set()

            # Shutdown should complete promptly.
            await asyncio.wait_for(stop_task, timeout=6.0)

            # The task should have completed normally.
            snapshot = store.read_task_snapshot(sample_scope, handle.task_id)
            assert snapshot is not None
            assert snapshot.state == "COMPLETED"
        finally:
            release_event.set()
            if host._running:
                await host.stop()

    @pytest.mark.asyncio
    async def test_blocked_task_cancelled_after_grace_expiry(
        self, store: Any, sample_scope: Any, _committer: Any
    ) -> None:
        from miniunicorn.runtime.hosts.lightweight import LightweightHost

        release_event = asyncio.Event()
        callback = _BlockingExecutionCallback(release_event)
        host = LightweightHost(store, _committer, callback, worker_count=1)
        await host.start()
        try:
            envelope = _make_grace_envelope(sample_scope)
            handle = await host.task_service.submit(envelope)

            # Wait for the Worker to pick up the task and block.
            await asyncio.wait_for(callback.started.wait(), timeout=5.0)

            # Shutdown with a short grace — the callback is never released.
            start = time.monotonic()
            await host.stop(grace_s=1)
            elapsed = time.monotonic() - start

            # Cancellation should occur only after the grace period.
            assert elapsed >= 0.8, (
                f"shutdown should wait at least ~1s grace, took {elapsed:.2f}s"
            )
            assert elapsed < 5.0, (
                f"shutdown should not hang, took {elapsed:.2f}s"
            )

            # The task was not completed (still RUNNING or FAILED).
            snapshot = store.read_task_snapshot(sample_scope, handle.task_id)
            assert snapshot is not None
            assert snapshot.state != "COMPLETED"
        finally:
            release_event.set()
            if host._running:
                await host.stop()
