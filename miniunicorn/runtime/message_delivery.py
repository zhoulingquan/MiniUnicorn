"""Agent-owned outbound delivery backed by the Outbox (design §20.6, §17.8).

This module implements the :class:`~miniunicorn.agent.ports.OutboundPort`
contract on the Runtime side. The Agent Core ``MessageTool`` calls
``runtime.outbound_port.enqueue(OutboundRequest(...))``; this adapter writes
the payload blob, computes the stable dedup key, and enqueues one Outbox row
under the active task lease via ``enqueue_message_tool_outbox``.

It also provides :class:`LocalResultSender`, a minimal
:class:`~miniunicorn.runtime.outbox.ChannelSender` for local CLI/API
delivery. The Outbox remains the delivery authority even for synchronous
results — the sender is only invoked after the Outbox claims the row.
"""

from __future__ import annotations

import hashlib
import time

from miniunicorn.agent.ports import OutboundReceipt, OutboundRequest
from miniunicorn.bus.events import OutboundMessage
from miniunicorn.runtime.models import DeliveryReceipt
from miniunicorn.runtime.session_committer import (
    _get_active_tool_call_id,
    _get_claim_for_task,
    _get_delivery_ledger_for_task,
)

# ---------------------------------------------------------------------------
# DurableMessageDelivery — OutboundPort implementation (design §20.6)
# ---------------------------------------------------------------------------


class DurableMessageDelivery:
    """OutboundPort backed by the Outbox (design §20.6, WP5 task 5).

    Constructed with the active ``task_id``. The claim, delivery ledger
    (Runtime Store), and current ``tool_call_id`` are resolved from the
    Worker-bound context helpers in
    :mod:`miniunicorn.runtime.session_committer`.

    The dedup key is ``sha256(task_id:tool_call_id:message)`` so repeated
    Message Tool calls with the same arguments collapse to one Outbox row
    (design §16.6).
    """

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    async def enqueue(self, request: OutboundRequest) -> OutboundReceipt:
        claim = _get_claim_for_task(self._task_id)
        if claim is None:
            raise RuntimeError(
                f"No active claim bound for task {self._task_id}; "
                "the Worker must bind the claim before Agent execution"
            )
        store = _get_delivery_ledger_for_task(self._task_id)
        if store is None:
            raise RuntimeError(f"No delivery ledger bound for task {self._task_id}")
        tool_call_id = _get_active_tool_call_id(self._task_id)
        if tool_call_id is None:
            raise RuntimeError(
                f"No tool_call_id bound for task {self._task_id}; "
                "the ToolGateway must bind it before invoking the tool"
            )

        # Build the payload blob (design §16.15).
        # Task 7 Step 6: use the versioned Outbox codec envelope so the
        # OutboxSender decodes content+media+metadata instead of receiving
        # raw JSON bytes that drop media on the floor (design §20.6).
        from miniunicorn.runtime.outbox_payload import encode_outbox_payload

        payload_bytes = encode_outbox_payload(
            content=request.content,
            media=tuple(request.media),
        )
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        from miniunicorn.runtime.models import BlobWrite

        scope_key = f"message_tool:{self._task_id}"
        blob = store.write_blob(
            BlobWrite(
                scope_key=scope_key,
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=payload_hash,
                encoding="RAW_BYTES",
                inline_content=payload_bytes,
                size_bytes=len(payload_bytes),
                created_at_ms=int(time.time() * 1000),
            )
        )

        # Stable dedup key: sha256(task_id:tool_call_id:message)
        # (design §16.6). The literal "message" is the tool name.
        dedup_key = hashlib.sha256(
            f"{self._task_id}:{tool_call_id}:message".encode("utf-8")
        ).hexdigest()

        # Resolve channel_account from the task record if available;
        # otherwise use an empty string (the Message Tool send is a
        # proactive/cross-channel delivery that may not carry the
        # original inbound account).
        channel_account = ""
        task = store.read_task(self._task_id)
        if task is not None and task.channel_account:
            channel_account = task.channel_account

        outbox_id = store.enqueue_message_tool_outbox(
            claim,
            channel=request.channel,
            channel_account=channel_account,
            target_key=request.target_key,
            payload_blob_id=blob.blob_id,
            payload_hash=payload_hash,
            dedup_key=dedup_key,
        )

        return OutboundReceipt(outbox_id=outbox_id, dedup_key=dedup_key)


# ---------------------------------------------------------------------------
# LocalResultSender — ChannelSender for CLI/API (design §20.6)
# ---------------------------------------------------------------------------


class LocalResultSender:
    """Minimal ChannelSender for local CLI/API delivery (design §20.6).

    Used only after an Outbox claim; the Outbox remains the delivery
    authority even for synchronous results. This is not a direct Agent
    return path — it only marks the claimed Outbox row as delivered.
    """

    def __init__(self, channels: frozenset[str] = frozenset({"cli", "api"})) -> None:
        self._channels = channels

    async def send_with_receipt(
        self,
        channel_name: str,
        message: OutboundMessage,
    ) -> DeliveryReceipt:
        if channel_name not in self._channels:
            return DeliveryReceipt(
                status="PERMANENT_FAILURE",
                safe_error_code="CHANNEL_NOT_CONFIGURED",
            )
        return DeliveryReceipt(
            status="DELIVERED",
            receipt_ref=(message.metadata or {}).get("message_id"),
        )

    def get_channel_recovery(self, channel_name: str) -> str:
        return "NATIVE_IDEMPOTENCY" if channel_name in self._channels else "NONE"


__all__ = [
    "DurableMessageDelivery",
    "LocalResultSender",
]
