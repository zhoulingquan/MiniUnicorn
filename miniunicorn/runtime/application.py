"""Durable Runtime application façade (design §8.1, §11.3, Task 4).

``RuntimeApplication`` is the small surface that CLI, API, Channels, and
test code use to submit work, await completion, and read the durable
final reply. It wraps ``TaskService`` for submit/wait and the Runtime
Store's ``read_final_reply`` for result retrieval. Real-time event
streaming goes through ``RealtimeSubscriptionHub``.

This façade does NOT execute Agent turns — that is the Worker's job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from miniunicorn.runtime.ingress import build_inbound_envelope
from miniunicorn.runtime.models import DurableReply, RequestScope, TaskSnapshot
from miniunicorn.runtime.realtime import RealtimeSubscriptionHub
from miniunicorn.runtime.task_service import TaskService

try:
    from miniunicorn.runtime.contracts import TaskHandle
except ImportError:  # pragma: no cover
    TaskHandle = Any  # type: ignore[assignment,misc]


@dataclass(slots=True, frozen=True)
class RuntimeInboundRequest:
    """Normalized inbound request from any launcher (CLI, API, Channel)."""

    content: str
    media: tuple[str, ...]
    metadata: dict[str, Any]
    session_key: str
    channel: str
    channel_account: str
    channel_message_id: str | None
    scope: RequestScope
    # Task 7 Step 2: immutable delivery target for the final reply.
    # CLI: "direct"; OpenAI API: request/session target; Channel: chat_id.
    target_key: str = ""


@dataclass(slots=True, frozen=True)
class RuntimeTurnResult:
    """Result of ``submit_and_wait``: terminal snapshot + durable reply."""

    snapshot: TaskSnapshot
    reply: DurableReply


class RuntimeApplication:
    """Application-facing submit/wait/result façade (design Task 4).

    Wraps ``TaskService`` for durable submit/wait and the Runtime Store
    for scope-checked final-reply reads. ``RealtimeSubscriptionHub``
    provides transient event streaming for CLI/SSE renderers.
    """

    def __init__(
        self,
        task_service: TaskService,
        result_store: Any,
        realtime: RealtimeSubscriptionHub,
    ) -> None:
        self.task_service = task_service
        self._result_store = result_store
        self._realtime = realtime
        self._accepting = True

    async def submit(self, request: RuntimeInboundRequest) -> Any:
        """Submit a durable task. Raises if ingress is draining."""
        if not self._accepting:
            raise RuntimeError("runtime ingress is draining")
        envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
        return await self.task_service.submit(envelope)

    def start_accepting(self) -> None:
        """Allow new submits."""
        self._accepting = True

    def stop_accepting(self) -> None:
        """Reject new submits. Already-accepted tasks remain queryable."""
        self._accepting = False

    async def wait(
        self,
        scope: RequestScope,
        task_id: str,
        timeout_s: float | None,
    ) -> TaskSnapshot:
        """Wait for a task to reach a terminal state."""
        return await self.task_service.wait_terminal(scope, task_id, timeout_s)

    def read_reply(self, scope: RequestScope, task_id: str) -> DurableReply:
        """Read the durable final reply for a completed task.

        Returns an empty ``DurableReply`` when no final-reply Outbox row
        exists (e.g. suppressed or empty replies).
        """
        reply = self._result_store.read_final_reply(scope, task_id)
        if reply is None:
            return DurableReply(content="", outbox_id=None, metadata={})
        return reply

    def subscribe(self, task_id: str):
        """Subscribe to transient task events for CLI/SSE rendering."""
        return self._realtime.subscribe(task_id)

    async def submit_and_wait(
        self,
        request: RuntimeInboundRequest,
        timeout_s: float | None = None,
    ) -> RuntimeTurnResult:
        """Submit a task and wait for terminal state, then read the reply."""
        handle = await self.submit(request)
        snapshot = await self.wait(request.scope, handle.task_id, timeout_s)
        return RuntimeTurnResult(
            snapshot,
            self.read_reply(request.scope, handle.task_id),
        )


__all__ = [
    "RuntimeApplication",
    "RuntimeInboundRequest",
    "RuntimeTurnResult",
]
