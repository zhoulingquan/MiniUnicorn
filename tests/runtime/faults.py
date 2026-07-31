"""Named fault injection for runtime crash-boundary tests (Task 14).

Provides a single :class:`FaultInjector` that tests configure to raise
at specific durable boundaries. Production code accepts an optional
``fault_hook`` callable (default ``None``); when ``None`` there is no
behavior change (design §30 Task 14 Step 1).

Named fault points (design Task 14 Step 1):

    after_task_claim
    during_provider_request
    after_provider_commit
    after_tool_external_effect
    after_tool_result_commit
    after_session_prepare
    after_session_replace
    after_session_confirm
    after_outbox_enqueue
    after_channel_send
    before_delivery_receipt_commit

The injector is a test-only helper. It is NOT imported by production
code; production code only sees the ``fault_hook`` callable argument.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


# Canonical fault-point names (design Task 14 Step 1).
FAULT_POINTS: tuple[str, ...] = (
    "after_task_claim",
    "during_provider_request",
    "after_provider_commit",
    "after_tool_external_effect",
    "after_tool_result_commit",
    "after_session_prepare",
    "after_session_replace",
    "after_session_confirm",
    "after_outbox_enqueue",
    "after_channel_send",
    "before_delivery_receipt_commit",
)


class FaultInjector:
    """Thread-safe fault injector used only by tests.

    Configure one or more fault points to raise a specific exception
    when the production code calls the hook at that boundary. The hook
    is a no-op when no fault is configured for the named point.

    Example::

        injector = FaultInjector()
        injector.raise_at("after_session_replace", RuntimeError("crash"))
        worker = AgentTaskWorker(..., fault_hook=injector.hook)
    """

    def __init__(self) -> None:
        self._faults: dict[str, BaseException] = {}
        self._calls: list[str] = []
        self._lock = threading.Lock()

    def raise_at(self, point: str, exc: BaseException) -> None:
        """Configure *point* to raise *exc* when the hook is called."""
        if point not in FAULT_POINTS:
            raise ValueError(f"unknown fault point: {point}")
        with self._lock:
            self._faults[point] = exc

    def clear(self, point: str | None = None) -> None:
        """Clear one or all configured faults."""
        with self._lock:
            if point is None:
                self._faults.clear()
                self._calls.clear()
            else:
                self._faults.pop(point, None)

    @property
    def calls(self) -> list[str]:
        """Return a copy of the ordered list of fault points invoked."""
        with self._lock:
            return list(self._calls)

    def was_called(self, point: str) -> bool:
        """Return ``True`` if *point* has been invoked at least once."""
        with self._lock:
            return point in self._calls

    def hook(self, point: str) -> None:
        """The callable production code invokes at each named boundary.

        Records the call and raises the configured exception if any.
        Production default is ``None`` (no hook installed); this method
        is only called when a test installs a :class:`FaultInjector`.
        """
        with self._lock:
            self._calls.append(point)
            exc = self._faults.get(point)
        if exc is not None:
            raise exc


def no_op_hook(_point: str) -> None:
    """A fault hook that records nothing and raises nothing.

    Useful as a placeholder when a test needs the hook parameter set
    but does not want any fault injection.
    """
    return None


# ---------------------------------------------------------------------------
# Durable-fact collector (Task 14 Step 2)
# ---------------------------------------------------------------------------


def collect_durable_facts(
    store: Any,
    task_id: str,
    session_key: str,
    *,
    session_manager: Any | None = None,
) -> dict[str, Any]:
    """Return a normalized dict of durable facts for parity comparison.

    Excludes UUIDs, timestamps, lease tokens, PIDs, and epochs so the
    same logical scenario produces the same facts in lightweight and
    supervised modes (design Task 14 Step 2).

    The session transcript is read from ``session_manager`` when
    provided; otherwise only store-side facts are returned.
    """
    record = store.read_task(task_id)
    if record is None:
        return {"task_state": "MISSING"}

    # Model attempt states — query the model_attempts table directly so
    # we capture every attempt (not only COMPLETED ones) for parity.
    conn = _get_conn(store)
    model_rows = conn.execute(
        "SELECT state FROM model_attempts WHERE task_id=? ORDER BY started_at_ms ASC",
        (task_id,),
    ).fetchall()
    model_states = [r["state"] for r in model_rows]

    # Tool call states — query the tool_calls table directly since the
    # store exposes ``list_completed_tools`` (terminal only) but we want
    # every logical call's current state for parity.
    tool_rows = conn.execute(
        "SELECT state FROM tool_calls WHERE task_id=? ORDER BY created_at_ms ASC",
        (task_id,),
    ).fetchall()
    tool_states = [r["state"] for r in tool_rows]

    # Outbox rows for this task.
    outbox_rows = conn.execute(
        "SELECT * FROM outbox WHERE task_id=? ORDER BY outbox_id ASC",
        (task_id,),
    ).fetchall()
    from miniunicorn.runtime.outbox_payload import decode_outbox_payload

    outbox_facts: list[dict[str, Any]] = []
    for row in outbox_rows:
        payload_bytes = store.read_blob_content(row["payload_blob_id"])
        content = ""
        if payload_bytes is not None:
            try:
                content = decode_outbox_payload(
                    row["message_kind"], bytes(payload_bytes)
                ).content
            except Exception:
                content = ""
        outbox_facts.append(
            {
                "kind": row["message_kind"],
                "channel": row["channel"],
                "target": row["target_key"],
                "state": row["state"],
                "content": content,
            }
        )

    # Session transcript (excludes volatile metadata).
    session_messages: list[tuple[Any, Any]] = []
    if session_manager is not None:
        try:
            snapshot = session_manager.load_fresh(session_key)
            session_messages = [
                (m.get("role"), m.get("content")) for m in snapshot.messages
            ]
        except Exception:
            session_messages = []

    return {
        "task_state": record.state,
        "session_sequence": record.session_sequence,
        "session_messages": session_messages,
        "model_states": model_states,
        "tool_states": tool_states,
        "outbox": outbox_facts,
    }


def _get_conn(store: Any) -> Any:
    """Return the SQLite connection backing the store (internal helper)."""
    # SqliteRuntimeStore stores its connection as self._conn.
    return store._conn  # type: ignore[attr-defined]


__all__ = [
    "FAULT_POINTS",
    "FaultInjector",
    "collect_durable_facts",
    "no_op_hook",
]
