"""Supervisor: spawns and supervises Control Plane + Worker children (design §9, §24, WP6).

The Supervisor is the only component that owns child processes. It:

- spawns one Control Plane process and N Worker processes via
  ``multiprocessing.spawn`` (design §24.6: "use spawn even on POSIX");
- initializes process-local dependencies inside each child after spawn
  (no inherited SQLite, socket, Provider, or Agent object — design §24.6,
  WP6 task 2);
- tracks process exits **in memory** with exponential restart backoff
  and a bounded restart count per rolling window (design §24.4). There
  is no durable ``workers`` row that could become a second truth;
- exposes readiness: Control Plane ready AND at least ``min_workers``
  Workers ready (design §24.4, §24.5);
- performs graceful shutdown (design §24.7): stop accepting work, signal
  children, wait grace, terminate survivors;
- ensures all descendants terminate with the Supervisor via
  :class:`SupervisorContainment` (Windows Job Object kill-on-close,
  POSIX process-group + parent-death) — design §24.6, WP6 task 6.

Children communicate with the Supervisor over per-child IPC pipes
(:mod:`miniunicorn.runtime.ipc`). Critical state is never sent through
IPC; children read and write the shared Runtime Store directly
(design §23.2). Wake hints only reduce poll latency.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue as _queue_mod
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from loguru import logger

from miniunicorn.runtime.containment import (
    SupervisorContainment,
    posix_set_child_death_signal,
    posix_start_new_session,
)
from miniunicorn.runtime.ipc import (
    IPC_PROTOCOL_VERSION,
    IpcEnvelope,
    ProcessIpcChannel,
    KIND_AGENT_EVENT,
    KIND_CHILD_READY,
    KIND_SHUTDOWN,
    KIND_TASK_PROGRESS,
    KIND_WAKE_HINT,
    KIND_WORKER_READY,
    child_ready,
    shutdown_signal,
)


# ---------------------------------------------------------------------------
# Child entry-point protocol
# ---------------------------------------------------------------------------


class ChildEntrypoint(Protocol):
    """Callable that runs inside a spawned child process (design §24.6).

    The Supervisor serialises the entrypoint (and its arguments) via
    ``multiprocessing.spawn`` so the child starts with a clean import
    graph. The entrypoint MUST NOT rely on any inherited runtime object
    (SQLite connection, Provider, Agent, bus) — it rebuilds them from
    ``config`` (WP6 task 2).
    """

    def __call__(
        self,
        *,
        role: str,
        instance_id: str,
        config: Any,
        ipc_channel: ProcessIpcChannel,
        ready_signal: Callable[[str], None],
    ) -> int: ...


# ---------------------------------------------------------------------------
# Per-child tracked state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ChildRecord:
    """In-memory record of one supervised child (design §24.4)."""

    role: str  # "control" | "worker"
    child_id: str  # stable id (e.g. "worker-0")
    instance_id: str  # per-spawn process instance id (changes on restart)
    channel: ProcessIpcChannel
    process: Any = None  # multiprocessing.Process
    ready: bool = False
    last_exit_code: int | None = None
    last_exit_at_ms: int | None = None
    restart_history: deque[int] = field(default_factory=lambda: deque(maxlen=64))
    backoff_until_ms: int = 0


# ---------------------------------------------------------------------------
# Restart backoff policy (design §24.4)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RestartPolicy:
    """Exponential backoff with bounded restarts per rolling window.

    Defaults follow design §24.4:
    - exponential restart backoff;
    - bounded restarts per rolling window;
    - readiness false when minimum Worker capacity is unavailable.
    """

    initial_backoff_ms: int = 500
    max_backoff_ms: int = 30_000
    backoff_multiplier: float = 2.0
    window_ms: int = 5 * 60 * 1000  # 5 minutes
    max_restarts_per_window: int = 5

    def next_backoff_ms(self, record: _ChildRecord) -> int:
        """Compute the next backoff and record the restart timestamp."""
        now = _now_ms()
        # Drop timestamps outside the rolling window.
        cutoff = now - self.window_ms
        while record.restart_history and record.restart_history[0] < cutoff:
            record.restart_history.popleft()
        # If we have exceeded the per-window restart budget, wait the
        # remainder of the window before retrying (readiness stays false).
        if len(record.restart_history) >= self.max_restarts_per_window:
            wait = self.window_ms - (now - record.restart_history[0])
            return max(wait, self.max_backoff_ms)
        record.restart_history.append(now)
        # Exponential backoff based on recent failure count.
        attempt = len(record.restart_history)
        backoff = int(self.initial_backoff_ms * (self.backoff_multiplier ** (attempt - 1)))
        return min(backoff, self.max_backoff_ms)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Spawn-and-supervise the Control Plane and Worker children (WP6).

    Lifecycle:

    - :meth:`start` spawns the Control Plane and ``worker_count`` Workers,
      assigns each to the :class:`SupervisorContainment` group, and waits
      up to ``ready_timeout_s`` for each to report readiness.
    - :meth:`is_ready` reports combined readiness (Control Plane ready
      AND at least ``min_workers`` Workers ready).
    - :meth:`reap` polls for child exits and restarts them (subject to
      :class:`RestartPolicy`). Call this from the host's event loop.
    - :meth:`shutdown` performs graceful shutdown (design §24.7).
    - :meth:`terminate` performs immediate containment close.

    The Supervisor never writes a durable ``workers`` row; restart state
    is in-memory only (design §24.4).
    """

    def __init__(
        self,
        *,
        config: Any,
        control_entrypoint: ChildEntrypoint,
        worker_entrypoint: ChildEntrypoint,
        worker_count: int = 2,
        min_workers: int = 1,
        ready_timeout_s: float = 30.0,
        shutdown_grace_s: int = 60,
        restart_policy: RestartPolicy | None = None,
        spawn_context: Any | None = None,
    ) -> None:
        if worker_count < 2:
            # Design §27: supervised worker_count must be at least 2.
            raise ValueError("supervised worker_count must be at least 2")
        if min_workers > worker_count:
            raise ValueError("min_workers cannot exceed worker_count")
        self._config = config
        self._control_entrypoint = control_entrypoint
        self._worker_entrypoint = worker_entrypoint
        self._worker_count = worker_count
        self._min_workers = min_workers
        self._ready_timeout_s = ready_timeout_s
        self._shutdown_grace_s = shutdown_grace_s
        self._restart_policy = restart_policy or RestartPolicy()
        # Use spawn explicitly on every platform (design §32.5).
        self._ctx = spawn_context or mp.get_context("spawn")
        self._containment = SupervisorContainment()
        self._children: dict[str, _ChildRecord] = {}
        self._started = False
        self._shutting_down = False
        self._instance_counter = 0
        # Control-relay queue: Worker → Supervisor → Control Plane (Task 7 Step 3).
        # Bounded so a saturated transient relay never blocks readiness, restart,
        # or wake-hint handling. The relay thread is the only writer to the
        # Control Plane child's parent pipe end.
        self._control_relay_queue: _queue_mod.Queue[IpcEnvelope | None] = (
            _queue_mod.Queue(maxsize=1024)
        )
        self._relay_thread: threading.Thread | None = None
        self._relay_dropped_events = 0
        self._relay_running = threading.Event()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def worker_count(self) -> int:
        return self._worker_count

    @property
    def min_workers(self) -> int:
        return self._min_workers

    @property
    def relay_dropped_events(self) -> int:
        """Transient realtime events dropped because the Control Plane relay was full."""
        return self._relay_dropped_events

    def is_ready(self) -> bool:
        """Combined readiness (design §24.4, §24.5, WP6 task 8)."""
        if not self._started or self._shutting_down:
            return False
        control = self._children.get("control")
        if control is None or not control.ready or not _is_alive(control):
            return False
        ready_workers = sum(
            1
            for r in self._children.values()
            if r.role == "worker" and r.ready and _is_alive(r)
        )
        return ready_workers >= self._min_workers

    def ready_workers(self) -> int:
        return sum(
            1
            for r in self._children.values()
            if r.role == "worker" and r.ready and _is_alive(r)
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of supervised children (for metrics/health)."""
        return {
            "started": self._started,
            "shutting_down": self._shutting_down,
            "ready": self.is_ready(),
            "protocol_version": IPC_PROTOCOL_VERSION,
            "relay_dropped_events": self._relay_dropped_events,
            "children": [
                {
                    "role": r.role,
                    "id": r.child_id,
                    "instance_id": r.instance_id,
                    "ready": r.ready,
                    "alive": _is_alive(r),
                    "last_exit_code": r.last_exit_code,
                    "restarts_in_window": len(r.restart_history),
                    "backoff_until_ms": r.backoff_until_ms,
                }
                for r in self._children.values()
            ],
        }

    # ------------------------------------------------------------------
    # Start / spawn
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the Control Plane, wait for its ready signal, then spawn Workers.

        Design Task 7 Step 5: the Control Plane runs migrations before
        readiness. Workers must not race first-run migrations, so they are
        spawned only after the Control Plane reports ready.
        """
        if self._started:
            return
        self._started = True
        logger.info(
            "Supervisor starting: {} workers, min_workers={}",
            self._worker_count,
            self._min_workers,
        )
        # Spawn Control Plane first and wait for its ready signal.
        self._spawn_child("control", "control")
        self._await_control_ready()
        # Start the relay thread now that the Control Plane exists.
        self._start_relay_thread()
        # Spawn Workers only after the Control Plane is ready.
        for i in range(self._worker_count):
            self._spawn_child("worker", f"worker-{i}")
        # Wait for combined readiness.
        self._await_readiness()

    def _await_control_ready(self) -> None:
        """Wait up to ``ready_timeout_s`` for the Control Plane to report ready.

        A Control Plane startup timeout terminates the partial child tree
        and raises (design Task 7 Step 5).
        """
        deadline = time.monotonic() + self._ready_timeout_s
        control = self._children.get("control")
        while time.monotonic() < deadline:
            self._drain_ipc(timeout_s=0.1)
            control = self._children.get("control")
            if control is not None and control.ready and _is_alive(control):
                return
            self.reap_once(restart=False)
            time.sleep(0.05)
        # Control Plane did not become ready — tear down partial tree.
        logger.error("Supervisor Control Plane readiness wait expired")
        self._shutting_down = True
        self._close_containment()
        for record in self._children.values():
            proc = record.process
            if proc is not None and proc.is_alive():
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                record.channel.parent_close()
            except Exception:  # noqa: BLE001
                pass
        self._children.clear()
        self._started = False
        self._shutting_down = False
        raise TimeoutError(
            "Control Plane did not report ready within "
            f"{self._ready_timeout_s}s — aborting supervised start"
        )

    def _spawn_child(self, role: str, child_id: str) -> _ChildRecord:
        """Spawn (or respawn) one child process."""
        self._instance_counter += 1
        instance_id = f"{child_id}#{self._instance_counter}"
        channel = ProcessIpcChannel.new_pipe()
        entrypoint = (
            self._control_entrypoint if role == "control" else self._worker_entrypoint
        )

        # NOTE: All args must be picklable for ``multiprocessing.spawn``
        # (design §24.6, §32.5). The ready-signal callback is built inside
        # the trampoline from the raw ``instance_id``/``child_id`` args
        # and the child-end connection — a closure here would fail to
        # pickle on Windows.
        process = self._ctx.Process(
            target=_child_trampoline,
            name=child_id,
            args=(
                entrypoint,
                role,
                instance_id,
                child_id,
                self._config,
                channel.child_end,
            ),
            daemon=False,
        )
        record = _ChildRecord(
            role=role,
            child_id=child_id,
            instance_id=instance_id,
            channel=channel,
            process=process,
        )
        # Register with OS containment before start so the Job Object
        # assignment races with neither spawn nor first syscall.
        self._children[child_id] = record
        process.start()
        # After start, record the PID and (POSIX) PGID.
        pid = process.pid
        pgid: int | None = None
        if not _IS_WINDOWS:
            # The child runs posix_start_new_session inside the trampoline;
            # the PGID equals the child PID.
            pgid = pid
        self._containment.register(pid, pgid=pgid)
        logger.info("Supervisor spawned {} pid={} instance={}", child_id, pid, instance_id)
        return record

    def _await_readiness(self) -> None:
        """Wait up to ``ready_timeout_s`` for the required children to be ready."""
        deadline = time.monotonic() + self._ready_timeout_s
        while time.monotonic() < deadline:
            self._drain_ipc(timeout_s=0.1)
            if self.is_ready():
                return
            # Reap any early exits so we can restart promptly.
            self.reap_once(restart=False)
            time.sleep(0.05)
        logger.warning(
            "Supervisor readiness wait expired; ready={} ready_workers={}",
            self.is_ready(),
            self.ready_workers(),
        )

    # ------------------------------------------------------------------
    # IPC drain
    # ------------------------------------------------------------------

    def _drain_ipc(self, *, timeout_s: float = 0.0) -> None:
        """Read pending IPC messages from every child and update state."""
        for record in list(self._children.values()):
            # Poll the parent end of this child's pipe.
            try:
                while record.channel.parent_poll(timeout_s=0.0):
                    env = record.channel.parent_recv()
                    if env is None:
                        break
                    self._handle_ipc(record, env)
            except (OSError, ValueError):
                continue

    def _handle_ipc(self, record: _ChildRecord, env: IpcEnvelope) -> None:
        if env.kind in (KIND_CHILD_READY, KIND_WORKER_READY):
            role = env.payload.get("role") or record.role
            if role == record.role or env.kind == KIND_WORKER_READY:
                record.ready = True
                logger.info(
                    "Supervisor child {} reported ready (instance={})",
                    record.child_id,
                    record.instance_id,
                )
            return
        # Worker → Control Plane realtime relay (design Task 7 Step 3).
        # The Supervisor is the only parent-side pipe reader; it enqueues
        # the envelope for the relay thread, which forwards it unchanged to
        # the Control Plane child. Direct _handle_ipc never blocks on a full
        # pipe — a full relay drops the transient event.
        if record.role == "worker" and env.kind in (KIND_AGENT_EVENT, KIND_TASK_PROGRESS):
            self._enqueue_control_relay(env)
            return
        # Control Plane → Workers wake-hint relay (design Task 7 Step 3).
        if record.role == "control" and env.kind == KIND_WAKE_HINT:
            self.fan_out_wake(
                reason=env.payload.get("reason", "submit"),
                task_id=env.task_id,
            )
            return

    def _enqueue_control_relay(self, env: IpcEnvelope) -> None:
        """Enqueue a Worker event for relay to the Control Plane (Task 7 Step 3).

        Uses ``put_nowait()`` on a bounded process-local queue. When full,
        the transient event is dropped and ``relay_dropped_events`` is
        incremented — readiness, restart, and wake-hint handling continue
        unaffected.
        """
        try:
            self._control_relay_queue.put_nowait(env)
        except _queue_mod.Full:
            self._relay_dropped_events += 1

    def _start_relay_thread(self) -> None:
        """Start the lifecycle-owned relay thread (Task 7 Step 3).

        One thread reads the bounded relay queue and performs the blocking
        ``parent_send()`` to the Control Plane child. Closing the IPC handle
        unblocks the thread during shutdown.
        """
        if self._relay_thread is not None:
            return
        self._relay_running.set()
        self._relay_thread = threading.Thread(
            target=self._relay_loop, name="supervisor-control-relay", daemon=True
        )
        self._relay_thread.start()

    def _relay_loop(self) -> None:
        """Drain the relay queue and forward envelopes to the Control Plane."""
        while self._relay_running.is_set():
            try:
                env = self._control_relay_queue.get(timeout=0.2)
            except _queue_mod.Empty:
                continue
            if env is None:
                return
            control = self._children.get("control")
            if control is None or control.process is None or not control.process.is_alive():
                self._relay_dropped_events += 1
                continue
            try:
                control.channel.parent_send(env)
            except Exception:  # noqa: BLE001
                # IPC closed or broken — drop the transient event.
                self._relay_dropped_events += 1

    def _stop_relay_thread(self) -> None:
        """Signal the relay thread to drain and exit."""
        self._relay_running.clear()
        try:
            self._control_relay_queue.put_nowait(None)
        except _queue_mod.Full:
            pass
        thread = self._relay_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._relay_thread = None

    # ------------------------------------------------------------------
    # Reap / restart
    # ------------------------------------------------------------------

    def reap_once(self, *, restart: bool = True) -> bool:
        """Reap one exited child and optionally restart it.

        Returns ``True`` if a child was reaped during this call.
        """
        reaped = False
        for record in list(self._children.values()):
            proc = record.process
            if proc is None:
                continue
            if proc.is_alive():
                continue
            exit_code = proc.exitcode
            # Only reap once per exit.
            if record.last_exit_code is not None and not record.ready:
                # Already recorded and waiting for backoff.
                continue
            reaped = True
            record.last_exit_code = exit_code
            record.last_exit_at_ms = _now_ms()
            record.ready = False
            logger.warning(
                "Supervisor child {} exited code={} instance={}",
                record.child_id,
                exit_code,
                record.instance_id,
            )
            # Close the stale pipe; a fresh one is created on respawn.
            record.channel.parent_close()
            if not restart or self._shutting_down:
                continue
            # Schedule restart after backoff.
            backoff_ms = self._restart_policy.next_backoff_ms(record)
            record.backoff_until_ms = _now_ms() + backoff_ms
            logger.info(
                "Supervisor will restart {} in {} ms",
                record.child_id,
                backoff_ms,
            )
        # Try to restart any child whose backoff has elapsed.
        if restart and not self._shutting_down:
            self._maybe_restart_due()
        return reaped

    def _maybe_restart_due(self) -> None:
        """Restart children whose backoff window has elapsed."""
        now = _now_ms()
        for record in list(self._children.values()):
            if record.process is not None and record.process.is_alive():
                continue
            if record.backoff_until_ms == 0:
                continue
            if now < record.backoff_until_ms:
                continue
            # Backoff elapsed — respawn.
            logger.info("Supervisor respawning {} after backoff", record.child_id)
            record.backoff_until_ms = 0
            self._spawn_child(record.role, record.child_id)

    def reap(self, *, timeout_s: float = 0.0) -> None:
        """Convenience: drain IPC, reap exits, restart due children."""
        self._drain_ipc(timeout_s=timeout_s)
        self.reap_once(restart=True)

    # ------------------------------------------------------------------
    # Wake hint fan-out
    # ------------------------------------------------------------------

    def fan_out_wake(self, *, reason: str = "submit", task_id: str | None = None) -> int:
        """Send a wake hint to every alive worker (design §23.2)."""
        env_sender = "supervisor"
        from miniunicorn.runtime.ipc import wake_hint

        env = wake_hint(env_sender, reason=reason, task_id=task_id)
        sent = 0
        for record in self._children.values():
            if record.role != "worker":
                continue
            if record.process is None or not record.process.is_alive():
                continue
            try:
                record.channel.parent_send(env)
                sent += 1
            except Exception:  # noqa: BLE001
                continue
        return sent

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, *, grace_s: int | None = None) -> None:
        """Graceful shutdown (design §24.7, WP6 task 7).

        Sequence:
        1. mark shutting down (stops restarts and readiness);
        2. send shutdown signal to every alive child;
        3. wait up to grace for children to exit cleanly;
        4. terminate survivors;
        5. close IPC pipes and the containment group.
        """
        if not self._started:
            return
        self._shutting_down = True
        grace = grace_s if grace_s is not None else self._shutdown_grace_s
        logger.info("Supervisor graceful shutdown grace={}s", grace)

        # 2. Send shutdown signal to every alive child.
        for record in self._children.values():
            proc = record.process
            if proc is not None and proc.is_alive():
                try:
                    record.channel.parent_send(
                        shutdown_signal("supervisor", grace_s=grace)
                    )
                except Exception:  # noqa: BLE001
                    pass

        # 3. Wait up to grace for clean exits.
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            for record in self._children.values():
                proc = record.process
                if proc is not None and proc.is_alive():
                    proc.join(timeout=0.2)
            if all(
                record.process is None or not record.process.is_alive()
                for record in self._children.values()
            ):
                break

        # 4. Terminate survivors.
        for record in self._children.values():
            proc = record.process
            if proc is not None and proc.is_alive():
                logger.warning("Supervisor terminating survivor {}", record.child_id)
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                proc.join(timeout=5)
                if proc.is_alive() and not _IS_WINDOWS:
                    try:
                        import signal as _sig

                        os.kill(proc.pid, _sig.SIGKILL)
                    except Exception:  # noqa: BLE001
                        pass

        # 5. Stop the relay thread before closing pipes (Task 7 Step 3).
        self._stop_relay_thread()
        # 6. Close IPC pipes and containment.
        for record in self._children.values():
            try:
                record.channel.parent_close()
            except Exception:  # noqa: BLE001
                pass
        self._close_containment()
        self._children.clear()
        self._started = False
        self._shutting_down = False
        logger.info("Supervisor shutdown complete")

    def terminate(self) -> None:
        """Immediate containment close (crash-mode exit)."""
        logger.warning("Supervisor immediate terminate")
        self._shutting_down = True
        self._stop_relay_thread()
        self._close_containment()
        for record in self._children.values():
            proc = record.process
            if proc is not None and proc.is_alive():
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._children.clear()
        self._started = False
        self._shutting_down = False

    def _close_containment(self) -> None:
        try:
            self._containment.close()
        except Exception:  # noqa: BLE001
            logger.warning("Supervisor containment close raised")


# ---------------------------------------------------------------------------
# Child trampoline
# ---------------------------------------------------------------------------


def _child_trampoline(
    entrypoint: ChildEntrypoint,
    role: str,
    instance_id: str,
    child_id: str,
    config: Any,
    ipc_child_end: Any,
) -> int:
    """Process entrypoint that prepares OS containment then calls the user entrypoint.

    Runs *inside* the spawned child. Sets up:

    - POSIX: a new session/process-group via :func:`posix_start_new_session`
      so the Supervisor can signal the whole group, and
      ``PR_SET_PDEATHSIG`` so the OS kills the child if the Supervisor
      dies (design §24.6).
    - Windows: nothing extra here — the Supervisor has already assigned
      the PID to the kill-on-close Job Object.

    Then calls the user-supplied :class:`ChildEntrypoint`, which performs
    all process-local initialization (open SQLite, build Provider/Agent,
    etc.) and returns the exit code.

    The ``ready_signal`` callback passed to the entrypoint is built here
    (in the child) from the picklable ``instance_id``/``child_id`` args
    and the raw child-end connection — a closure in the parent would
    fail to pickle on Windows spawn (design §24.6, §32.5).
    """
    # POSIX: new session + parent-death signal.
    if not _IS_WINDOWS:
        posix_start_new_session()
        posix_set_child_death_signal()
    try:
        # Wrap the raw connection in a ProcessIpcChannel so the
        # entrypoint can use the same API as the parent.
        channel = ProcessIpcChannel(parent_end=ipc_child_end, child_end=ipc_child_end)

        def _ready_signal(role_tag: str) -> None:
            channel.child_send(
                child_ready(instance_id, role=role_tag, id_=child_id)
            )

        return int(
            entrypoint(
                role=role,
                instance_id=instance_id,
                config=config,
                ipc_channel=channel,
                ready_signal=_ready_signal,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Supervisor child {} crashed: {}", child_id, exc)
        return 1
    finally:
        try:
            ipc_child_end.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_alive(record: _ChildRecord) -> bool:
    proc = record.process
    return proc is not None and proc.is_alive()


def _now_ms() -> int:
    return int(time.time() * 1000)


_IS_WINDOWS = sys.platform == "win32"


__all__ = [
    "ChildEntrypoint",
    "RestartPolicy",
    "Supervisor",
    "SupervisorContainment",
    "posix_set_child_death_signal",
    "posix_start_new_session",
]
