"""Agent execution callback bridging the Worker to the existing Agent Core.

Design §8.3, §17.1, §29.3: the Worker Adapter calls a pluggable
:class:`~miniunicorn.runtime.worker.ExecutionCallback` with a decoded task
payload and the current session base revision. The callback runs the
existing Agent Core (TurnExecutor / AgentLoop) and returns a structured
:class:`~miniunicorn.runtime.worker.WorkerExecutionResult`.

This module provides:

- :class:`AgentExecutionCallback` — the production callback that wraps an
  :class:`~miniunicorn.agent.loop.AgentLoop` (or any host satisfying the
  :class:`TurnDispatchHost` protocol).
- :func:`submit_durable` / :func:`dispatch_durable` — runtime entry points
  that map a legacy :class:`~miniunicorn.bus.events.InboundMessage` to a
  durable task envelope and submit it through the Task Service
  (design §29.1).

WP3 scope: the callback wraps the existing Agent loop without per-attempt
Provider journaling or Tool Gateway routing (those are WP4). The Agent loop
runs unchanged; the callback just captures the result for the Worker to
commit durably.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent.ports import SafeError
from miniunicorn.agent.turn_coordinator import DurableIdentifiers
from miniunicorn.bus.events import InboundMessage
from miniunicorn.runtime.worker import (
    WorkerExecutionResult,
    WorkerTaskPayload,
)

if TYPE_CHECKING:
    from miniunicorn.agent.turn_coordinator import TurnCoordinator
    from miniunicorn.agent.turn_dispatcher import TurnDispatcher
    from miniunicorn.runtime.models import RequestScope
    from miniunicorn.runtime.task_service import TaskService


class AgentExecutionCallback:
    """Production execution callback wrapping the existing Agent Core.

    Implements the :class:`~miniunicorn.runtime.worker.ExecutionCallback`
    protocol. Constructed by the LightweightHost (or Supervised Host) with
    the host's :class:`TurnDispatcher` and :class:`TurnCoordinator`.

    The callback is stateless between calls — all task state lives in the
    Runtime Store and the Session Manager.
    """

    def __init__(
        self,
        dispatcher: "TurnDispatcher",
        coordinator: "TurnCoordinator",
    ) -> None:
        self._dispatcher = dispatcher
        self._coordinator = coordinator

    async def __call__(
        self,
        payload: WorkerTaskPayload,
        session_base_revision: int,
    ) -> WorkerExecutionResult:
        """Execute the Agent turn for one claimed durable task.

        Returns a :class:`WorkerExecutionResult` carrying the final content
        and the assistant/tool messages produced after the inbound commit.
        The Worker uses these to build the FINAL session commit and the
        Outbox completion.
        """
        # Build the InboundMessage from the decoded payload.
        msg = InboundMessage(
            channel=payload.channel or "cli",
            sender_id="user",
            chat_id=payload.session_key.split(":", 1)[-1]
            if ":" in payload.session_key
            else payload.session_key,
            content=payload.content,
            media=list(payload.media or []),
            metadata=dict(payload.metadata or {}),
        )

        # Bind a TurnRuntime with durable identifiers (design §29.5).
        durable_ids = DurableIdentifiers(
            task_id=payload.task_id,
            session_sequence=0,  # Not carried in WorkerTaskPayload for WP3
            lease_epoch=0,
            run_segment=0,
            trace_id=None,
        )

        try:
            async with self._coordinator.scope(
                payload.session_key,
                turn_id=payload.turn_id,
                durable_identifiers=durable_ids,
            ) as turn_runtime:
                # Use process_message with runtime_mode. The dispatcher's
                # process_message calls _execute_message which calls
                # TurnExecutor.execute. We need to pass runtime_mode=True.
                # For WP3, we call the host's _process_message directly
                # with runtime_mode. The dispatcher's process_message
                # doesn't expose runtime_mode, so we use _execute_message.
                host = self._dispatcher.host
                result = await host._execute_message(
                    msg,
                    session_key=payload.session_key,
                )
                # Copy cumulative metrics into the bound runtime.
                from miniunicorn.agent.turn_runtime import complete_turn_runtime

                complete_turn_runtime(turn_runtime, result.context)

                outbound = result.outbound
                if outbound is None:
                    return WorkerExecutionResult(
                        final_content=None,
                        messages=[],
                        suppress_final=True,
                    )

                # Extract the assistant messages produced after the inbound
                # commit. For WP3, we use the context's all_messages if
                # available; otherwise, build a single assistant message
                # from the outbound content.
                messages: list[dict[str, Any]] = []
                if result.context is not None and result.context.all_messages:
                    # all_messages includes the history + new messages.
                    # We want only the new assistant/tool messages.
                    skip = result.context.save_skip or 0
                    messages = [
                        m for m in result.context.all_messages[skip:]
                        if m.get("role") in ("assistant", "tool")
                    ]
                if not messages and outbound.content:
                    messages = [{"role": "assistant", "content": outbound.content}]

                return WorkerExecutionResult(
                    final_content=outbound.content,
                    messages=messages,
                    metadata_updates={},
                    suppress_final=False,
                )
        except Exception as exc:
            logger.exception("AgentExecutionCallback failed for task {}", payload.task_id)
            return WorkerExecutionResult(
                final_content=None,
                messages=[],
                error=SafeError(
                    error_code="AGENT_EXECUTION_FAILURE",
                    error_summary=str(exc)[:500],
                ),
            )


__all__ = ["AgentExecutionCallback", "submit_durable", "dispatch_durable"]


# ---------------------------------------------------------------------------
# Durable runtime entry points (design §29.1, §30 WP3)
# ---------------------------------------------------------------------------


async def submit_durable(
    dispatcher: "TurnDispatcher",
    msg: InboundMessage,
    *,
    task_service: "TaskService",
    scope: "RequestScope",
) -> Any:
    """Submit an inbound message as a durable task (design §29.1).

    Maps the legacy :class:`InboundMessage` to an
    :class:`~miniunicorn.runtime.models.InboundTaskEnvelope` and submits
    it through the :class:`~miniunicorn.runtime.task_service.TaskService`.

    The task is durable before this method returns. The caller may await
    completion through the Task Service or the Outbox (design §29.1).
    """
    from miniunicorn.runtime.models import InboundTaskEnvelope, MediaRef

    session_key = dispatcher.host._effective_session_key(msg)
    content = msg.content if isinstance(msg.content, str) else ""
    payload = {
        "content": content,
        "media": list(msg.media or []),
        "metadata": dict(msg.metadata or {}),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    normalized_payload_ref = f"inline:{payload_hash[:16]}"

    # Channel message id for dedup (design §13.2).
    channel_message_id = None
    if isinstance(msg.metadata, dict):
        channel_message_id = msg.metadata.get("message_id")

    # Dedup key: stable per (channel, chat_id, message_id) when available.
    dedup_key = None
    if channel_message_id:
        dedup_key = f"{msg.channel}:{msg.chat_id}:{channel_message_id}"

    envelope = InboundTaskEnvelope(
        protocol_version=1,
        task_kind="USER_TURN",
        priority=100,
        scope=scope,
        session_key=session_key,
        channel=msg.channel,
        channel_account=msg.sender_id,
        channel_message_id=channel_message_id,
        dedup_key=dedup_key,
        normalized_payload_ref=normalized_payload_ref,
        payload_hash=payload_hash,
        media_refs=tuple(
            MediaRef(
                artifact_ref=p,
                content_hash=hashlib.sha256(p.encode("utf-8")).hexdigest(),
                media_kind="path",
                size_bytes=0,
            )
            for p in (msg.media or [])
            if isinstance(p, str) and p
        ),
        received_at_ms=int(time.time() * 1000),
        turn_id=None,
        payload_content=payload_bytes,
    )
    return await task_service.submit(envelope)


async def dispatch_durable(
    dispatcher: "TurnDispatcher",
    msg: InboundMessage,
    *,
    task_service: "TaskService",
    scope: "RequestScope",
    timeout_s: float | None = None,
) -> Any:
    """Submit-and-await for the durable runtime path (design §29.1, WP3 task 6).

    Submits the message as a durable task and waits for it to reach a
    terminal state. Returns the terminal :class:`TaskSnapshot`.

    This is the runtime equivalent of ``TurnDispatcher.dispatch`` — it does
    NOT publish the outbound message directly (design §29.1). Final reply
    delivery is handled by the Outbox (WP5) or the internal fake delivery
    ledger (WP3).
    """
    handle = await submit_durable(dispatcher, msg, task_service=task_service, scope=scope)
    return await task_service.wait_terminal(scope, handle.task_id, timeout_s)
