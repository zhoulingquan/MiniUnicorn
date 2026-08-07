"""Bounded task-scoped realtime subscription hub (design Task 4, Task 7).

The hub provides transient, lossy fan-out of Agent/progress events to
in-process subscribers (CLI renderer, SSE endpoint). Final replies
never enter this hub — they are read from the Runtime Store via
``read_final_reply``.

In lightweight mode the Worker publishes directly to this hub. In
supervised mode (Task 7) an IPC relay adapter forwards envelopes from
Worker processes to the Control Plane's hub instance.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from miniunicorn.bus.agent_events import AgentEvent


class RealtimeSubscriptionHub:
    """Bounded fan-out of transient task events to in-process subscribers.

    Each task_id maintains a set of bounded ``asyncio.Queue`` subscribers.
    When a queue is full the event is dropped and ``dropped_events`` is
    incremented — never block the Agent turn on transient delivery.
    """

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.dropped_events = 0

    @asynccontextmanager
    async def subscribe(self, task_id: str) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Subscribe to events for ``task_id``.

        Yields a bounded queue. Unsubscribes automatically on exit.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._capacity)
        subscribers = self._queues.setdefault(task_id, set())
        subscribers.add(queue)
        try:
            yield queue
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._queues.pop(task_id, None)

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        """Publish an event to all subscribers for ``task_id``.

        Drops the event (and increments ``dropped_events``) when any
        subscriber's queue is full. Never blocks.
        """
        for queue in tuple(self._queues.get(task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped_events += 1

    def publish_envelope(self, env: Any) -> None:
        """Publish a relayed IPC envelope (design Task 7).

        ``env`` is an ``IpcEnvelope`` with ``task_id``, ``kind``, and
        ``payload``. ``KIND_AGENT_EVENT`` carries a serialised event dict;
        ``KIND_TASK_PROGRESS`` carries phase/detail fields.
        """
        if env.task_id is None:
            return
        from miniunicorn.runtime.ipc import KIND_AGENT_EVENT

        if env.kind == KIND_AGENT_EVENT:
            event = dict(env.payload.get("event", {}))
        else:
            event = {
                "kind": "task_progress",
                "phase": env.payload.get("phase"),
                "detail": env.payload.get("detail"),
            }
        self.publish(env.task_id, event)


# ---------------------------------------------------------------------------
# LocalProgressPort — ProgressPort backed by the in-process hub (Task 5)
# ---------------------------------------------------------------------------


class LocalProgressPort:
    """ProgressPort that publishes serialized events to the local hub.

    A direct in-process transient fan-out — not another consumer of
    MessageBus; therefore it cannot race ChannelManager for the same
    queue (design Task 5 Step 4). Final replies bypass this port and
    are written only to the Outbox.
    """

    def __init__(self, task_id: str, hub: "RealtimeSubscriptionHub") -> None:
        self._task_id = task_id
        self._hub = hub

    async def emit(self, event: "AgentEvent") -> None:
        from miniunicorn.bus.agent_events import serialize_agent_event

        self._hub.publish(self._task_id, serialize_agent_event(event))


__all__ = ["RealtimeSubscriptionHub", "LocalProgressPort"]
