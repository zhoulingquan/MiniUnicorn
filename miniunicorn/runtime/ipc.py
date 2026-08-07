"""Inter-process IPC for supervised mode (design §23.2, §24, WP6).

Supervised mode uses three IPC flows:

- **Control Plane → Worker wake hints** — reduce poll latency after a new
  task is submitted. Correctness does *not* depend on delivery; Workers
  poll SQLite with exponential idle backoff (design §23.2).
- **Worker → Control Plane typed Agent events** — realtime Token deltas,
  progress, and lifecycle markers for the bus. Lossy: drops/coalesces
  deltas first when the queue is full (design §23.2).
- **Supervisor lifecycle signals** — child ready, child exiting, child
  crashed with exit code.

Every IPC message carries ``protocol_version``, ``instance_id`` (the
sending process instance id), ``task_id`` when applicable, and
``trace_id`` (design §23.2). Critical state is never sent through IPC;
the Runtime Store remains the single authority.

This module is deliberately transport-agnostic: it defines typed message
dataclasses and a :class:`ProcessIpcChannel` implementation backed by
:class:`multiprocessing.connection.Connection` (a duplex pipe). The
Supervisor wires one pipe per child process.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from loguru import logger

IPC_PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IpcEnvelope:
    """Common envelope for every IPC message (design §23.2).

    - ``protocol_version`` — IPC schema version.
    - ``instance_id`` — process instance id of the sender.
    - ``task_id`` — task id when applicable, else ``None``.
    - ``trace_id`` — trace id when applicable, else ``None``.
    - ``kind`` — message kind discriminator.
    - ``payload`` — kind-specific payload (JSON-serialisable).
    - ``sent_at_ms`` — sender wall-clock milliseconds.
    """

    protocol_version: int
    instance_id: str
    kind: str
    payload: dict[str, Any]
    task_id: str | None = None
    trace_id: str | None = None
    sent_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "IpcEnvelope":
        return cls(**json.loads(raw))


# ---------------------------------------------------------------------------
# Kinds and typed payload builders
# ---------------------------------------------------------------------------

# Control Plane → Worker
KIND_WAKE_HINT = "wake_hint"  # payload: {"reason": "submit"|"control"|"shutdown"}
KIND_SHUTDOWN = "shutdown"  # payload: {"grace_s": int}

# Worker → Control Plane
KIND_WORKER_READY = "worker_ready"  # payload: {"worker_id": str, "capacity": int}
KIND_AGENT_EVENT = "agent_event"  # payload: {"event": {...}} (typed Agent event)
KIND_TASK_PROGRESS = "task_progress"  # payload: {"phase": str, "detail": str}
KIND_WORKER_EXITING = "worker_exiting"  # payload: {"worker_id": str, "reason": str}

# Supervisor ↔ child lifecycle
KIND_CHILD_READY = "child_ready"  # payload: {"role": "control"|"worker", "id": str}
KIND_CHILD_STOPPED = "child_stopped"  # payload: {"role": str, "id": str, "exit_code": int}


def wake_hint(
    instance_id: str,
    *,
    reason: str = "submit",
    task_id: str | None = None,
) -> IpcEnvelope:
    """Build a wake-hint message (Control Plane → Worker)."""
    return IpcEnvelope(
        protocol_version=IPC_PROTOCOL_VERSION,
        instance_id=instance_id,
        kind=KIND_WAKE_HINT,
        payload={"reason": reason},
        task_id=task_id,
    )


def shutdown_signal(
    instance_id: str,
    *,
    grace_s: int,
) -> IpcEnvelope:
    """Build a shutdown signal (Supervisor/Control Plane → child)."""
    return IpcEnvelope(
        protocol_version=IPC_PROTOCOL_VERSION,
        instance_id=instance_id,
        kind=KIND_SHUTDOWN,
        payload={"grace_s": grace_s},
    )


def worker_ready(
    instance_id: str,
    *,
    worker_id: str,
    capacity: int,
) -> IpcEnvelope:
    """Build a worker-ready announcement (Worker → Control Plane)."""
    return IpcEnvelope(
        protocol_version=IPC_PROTOCOL_VERSION,
        instance_id=instance_id,
        kind=KIND_WORKER_READY,
        payload={"worker_id": worker_id, "capacity": capacity},
    )


def agent_event(
    instance_id: str,
    *,
    event: dict[str, Any],
    task_id: str | None = None,
    trace_id: str | None = None,
) -> IpcEnvelope:
    """Build a typed Agent event message (Worker → Control Plane)."""
    return IpcEnvelope(
        protocol_version=IPC_PROTOCOL_VERSION,
        instance_id=instance_id,
        kind=KIND_AGENT_EVENT,
        payload={"event": event},
        task_id=task_id,
        trace_id=trace_id,
    )


def child_ready(
    instance_id: str,
    *,
    role: str,
    id_: str,
) -> IpcEnvelope:
    """Build a child-ready signal (child → Supervisor)."""
    return IpcEnvelope(
        protocol_version=IPC_PROTOCOL_VERSION,
        instance_id=instance_id,
        kind=KIND_CHILD_READY,
        payload={"role": role, "id": id_},
    )


def child_stopped(
    instance_id: str,
    *,
    role: str,
    id_: str,
    exit_code: int,
) -> IpcEnvelope:
    """Build a child-stopped signal (Supervisor-internal)."""
    return IpcEnvelope(
        protocol_version=IPC_PROTOCOL_VERSION,
        instance_id=instance_id,
        kind=KIND_CHILD_STOPPED,
        payload={"role": role, "id": id_, "exit_code": exit_code},
    )


# ---------------------------------------------------------------------------
# ProcessIpcChannel — duplex pipe wrapper
# ---------------------------------------------------------------------------


class ProcessIpcChannel:
    """Duplex IPC channel over a :mod:`multiprocessing` pipe.

    Wraps a :class:`multiprocessing.connection.Connection` pair. The
    Supervisor creates one channel per child process and hands the child
    end to the spawned process; the parent end is polled by the
    Supervisor / Control Plane.

    The channel is intentionally non-blocking on the read side: callers
    poll with :meth:`poll` then :meth:`recv`. Writes are blocking but
    bounded by the OS pipe buffer; lossy realtime events should use
    :meth:`send_or_drop` to avoid blocking task checkpoints
    (design §23.2).
    """

    def __init__(self, parent_end: Any, child_end: Any) -> None:
        self._parent = parent_end
        self._child = child_end

    @classmethod
    def new_pipe(cls) -> "ProcessIpcChannel":
        """Create a fresh duplex pipe channel (design §23.2)."""
        parent_end, child_end = mp.Pipe(duplex=True)
        return cls(parent_end, child_end)

    @property
    def parent_end(self) -> Any:
        return self._parent

    @property
    def child_end(self) -> Any:
        return self._child

    # ------------------------------------------------------------------
    # Parent side
    # ------------------------------------------------------------------

    def parent_poll(self, timeout_s: float = 0.0) -> bool:
        """Return ``True`` if a message is readable on the parent end."""
        return bool(self._parent.poll(timeout_s))

    def parent_recv(self) -> IpcEnvelope | None:
        """Receive one envelope on the parent end, or ``None`` if empty."""
        try:
            raw = self._parent.recv()
        except EOFError:
            return None
        except OSError as exc:
            logger.warning("IPC parent recv failed: {}", exc)
            return None
        return _coerce_envelope(raw)

    def parent_send(self, env: IpcEnvelope) -> None:
        """Send an envelope from parent to child (blocking)."""
        try:
            self._parent.send(env)
        except (OSError, BrokenPipeError) as exc:
            logger.warning("IPC parent send failed: {}", exc)

    def parent_close(self) -> None:
        try:
            self._parent.close()
        except OSError:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Child side
    # ------------------------------------------------------------------

    def child_poll(self, timeout_s: float = 0.0) -> bool:
        return bool(self._child.poll(timeout_s))

    def child_recv(self) -> IpcEnvelope | None:
        try:
            raw = self._child.recv()
        except EOFError:
            return None
        except OSError as exc:
            logger.warning("IPC child recv failed: {}", exc)
            return None
        return _coerce_envelope(raw)

    def child_send(self, env: IpcEnvelope) -> None:
        """Send an envelope from child to parent (blocking)."""
        try:
            self._child.send(env)
        except (OSError, BrokenPipeError) as exc:
            logger.warning("IPC child send failed: {}", exc)

    def child_send_or_drop(self, env: IpcEnvelope) -> bool:
        """Non-best-effort send for lossy realtime events (design §23.2).

        Returns ``True`` if sent, ``False`` if dropped because the pipe
        buffer is full. Token deltas and progress updates use this path
        so a slow parent never blocks a task checkpoint or lease renewal.
        """
        # ``fileno()`` + select avoids a blocking write. If the pipe
        # buffer has no space, we drop the event and let the caller
        # increment a dropped-event metric.
        import select

        try:
            _, writable, _ = select.select([], [self._child.fileno()], [], 0.0)
        except (OSError, ValueError):
            # Not selectable (e.g. closed) — treat as drop.
            return False
        if not writable:
            return False
        try:
            self._child.send(env)
            return True
        except (OSError, BrokenPipeError):
            return False

    def child_close(self) -> None:
        try:
            self._child.close()
        except OSError:  # noqa: BLE001
            pass


def _coerce_envelope(raw: Any) -> IpcEnvelope | None:
    """Coerce a received raw payload into an :class:`IpcEnvelope`.

    Accepts :class:`IpcEnvelope` directly (when pickled) or a JSON string
    (when the channel is used in JSON mode for cross-version safety).
    """
    if raw is None:
        return None
    if isinstance(raw, IpcEnvelope):
        return raw
    if isinstance(raw, str):
        try:
            return IpcEnvelope.from_json(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(raw, dict):
        try:
            return IpcEnvelope(**raw)
        except TypeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Wake hint fan-out helper
# ---------------------------------------------------------------------------


def fan_out_wake_hints(
    channels: Iterable[ProcessIpcChannel],
    *,
    instance_id: str,
    reason: str = "submit",
    task_id: str | None = None,
) -> int:
    """Send a wake hint to every worker channel. Returns the count sent.

    Used by the Control Plane after a task is submitted. Delivery is
    best-effort; a missed hint just means the Worker polls slightly later
    (design §23.2).
    """
    env = wake_hint(instance_id, reason=reason, task_id=task_id)
    sent = 0
    for ch in channels:
        try:
            ch.parent_send(env)
            sent += 1
        except Exception:  # noqa: BLE001
            continue
    return sent


__all__ = [
    "IPC_PROTOCOL_VERSION",
    "IpcEnvelope",
    "ProcessIpcChannel",
    # Kinds
    "KIND_WAKE_HINT",
    "KIND_SHUTDOWN",
    "KIND_WORKER_READY",
    "KIND_AGENT_EVENT",
    "KIND_TASK_PROGRESS",
    "KIND_WORKER_EXITING",
    "KIND_CHILD_READY",
    "KIND_CHILD_STOPPED",
    # Builders
    "wake_hint",
    "shutdown_signal",
    "worker_ready",
    "agent_event",
    "child_ready",
    "child_stopped",
    "fan_out_wake_hints",
]
