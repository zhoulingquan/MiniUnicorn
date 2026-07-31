"""Durable Outbox Sender loop (design §8.7, §17.9, §17.13, WP5).

``OutboxSender`` is the delivery-side consumer of the Outbox table. It:

- claims the earliest pending record per delivery target;
- invokes the existing ``ChannelManager`` via ``send_with_receipt()``;
- stores a provider receipt when available;
- retries transient failures with bounded backoff;
- marks permanent failures and raises an operational alert.

Outbox delivery does not consume an Agent Worker slot (design §8.7).

The sender renews the delivery lease while the bounded Channel call is
in progress. Loss of the delivery lease prevents that sender from
recording a result and invokes the recovery policy in §17.13.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from miniunicorn.runtime.contracts import DeliveryLedger
from miniunicorn.runtime.models import (
    DeliveryReceipt,
    OutboxClaim,
    OutboxRecord,
    RetryDecision,
)
from miniunicorn.agent.ports import SafeError


# ---------------------------------------------------------------------------
# Channel sender protocol (avoids importing ChannelManager at runtime)
# ---------------------------------------------------------------------------


@runtime_checkable
class ChannelSender(Protocol):
    """Minimal interface for sending a message with receipt (design §23.5).

    Task 8: ``query_delivery_receipt`` is optional — channels that
    declare ``QUERYABLE_RECEIPT`` recovery should implement it so the
    recovery loop can reconcile an interrupted send.
    """

    async def send_with_receipt(
        self, channel_name: str, msg: Any
    ) -> DeliveryReceipt:
        ...

    def get_channel_recovery(self, channel_name: str) -> str:
        """Return the delivery recovery capability for a channel."""
        ...


class DeliveryLeaseLost(Exception):
    """Raised when the delivery lease is lost during an in-progress send (Task 8 Step 6).

    The OutboxSender must **not** call ``_record_result`` with the stale
    claim. The row remains ``SENDING`` until the recovery loop reclaims
    it and applies the channel recovery policy (design §17.13).
    """


# ---------------------------------------------------------------------------
# OutboxSender
# ---------------------------------------------------------------------------


class OutboxSender:
    """Delivery loop that drains the Outbox table (design §8.7, §17.9, WP5).

    Runs as an independent coroutine. Does not consume Worker slots.

    Usage::

        sender = OutboxSender(store, channel_sender, config)
        await sender.start()
        ...
        await sender.stop()
    """

    def __init__(
        self,
        store: DeliveryLedger,
        channel_sender: ChannelSender,
        *,
        sender_id: str = "outbox-sender",
        poll_interval_s: float = 0.5,
        lease_ms: int = 120_000,
        send_timeout_s: int = 60,
        renew_interval_s: float = 15.0,
    ) -> None:
        self._store = store
        self._channels = channel_sender
        self._sender_id = sender_id
        self._poll_interval_s = poll_interval_s
        self._lease_ms = lease_ms
        self._send_timeout_s = send_timeout_s
        self._renew_interval_s = renew_interval_s
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the sender loop as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="outbox-sender")

    async def stop(self) -> None:
        """Stop the sender loop gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            async with suppress_cancelled():
                await self._task
            self._task = None

    async def drain(self, timeout_s: float) -> None:
        """Wait until no immediately deliverable rows remain (Task 5 Step 3).

        Polls the Outbox for claimable rows and delivers them until none
        remain or the timeout expires. Does not wait forever on future
        retries — only drains rows that are immediately claimable now.
        """
        deadline = time.monotonic() + timeout_s
        while self._running and time.monotonic() < deadline:
            delivered = await self._deliver_one()
            if not delivered:
                return

    async def _run_loop(self) -> None:
        """Main poll-and-deliver loop.

        Task 8: each iteration first recovers expired SENDING rows, then
        delivers the next pending row. Recovery runs before delivery so
        a wedged target is unblocked before a later row is attempted.
        """
        logger.info("OutboxSender {} started", self._sender_id)
        while self._running:
            try:
                recovered = await self._recover_expired()
                delivered = await self._deliver_one()
                if not delivered and not recovered:
                    await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("OutboxSender {} error in loop", self._sender_id)
                await asyncio.sleep(self._poll_interval_s)
        logger.info("OutboxSender {} stopped", self._sender_id)

    async def _recover_expired(self) -> bool:
        """Recover expired SENDING rows according to channel policy (Task 8 Step 4).

        Claims expired SENDING rows durably, then — outside the SQLite
        transaction — asks the Channel for its recovery capability and
        writes PENDING/RETRY_WAIT, DELIVERED, or OUTCOME_UNKNOWN.
        """
        now_ms = _now_ms()
        claims = self._store.claim_expired_deliveries(
            self._sender_id, now_ms, self._lease_ms, limit=10
        )
        if not claims:
            return False
        for claim in claims:
            try:
                self._apply_recovery_policy(claim)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "OutboxSender recovery error outbox_id={}", claim.outbox_id
                )
        return True

    def _apply_recovery_policy(self, claim: OutboxClaim) -> None:
        """Apply the channel recovery policy to one expired claim (Task 8 Step 4).

        - ``NATIVE_IDEMPOTENCY``: safe to retry — schedule RETRY_WAIT.
        - ``QUERYABLE_RECEIPT``: query the Channel receipt; if delivered,
          mark DELIVERED, else mark OUTCOME_UNKNOWN.
        - ``NONE``: mark OUTCOME_UNKNOWN.
        """
        recovery = self._channels.get_channel_recovery(claim.channel)
        if recovery == "NATIVE_IDEMPOTENCY":
            retry = RetryDecision(
                kind="TRANSIENT",
                available_at_ms=_now_ms(),
                error=SafeError(
                    error_code="DELIVERY_RECOVERED",
                    error_summary="expired lease recovered for native-idempotent retry",
                ),
            )
            self._store.retry_delivery(claim, retry)
            logger.info(
                "OutboxSender recovered outbox_id={} as RETRY_WAIT (native idempotency)",
                claim.outbox_id,
            )
        elif recovery == "QUERYABLE_RECEIPT":
            receipt = self._query_channel_receipt(claim)
            if receipt is not None and receipt.status == "DELIVERED":
                self._store.mark_delivered(claim, receipt)
                logger.info(
                    "OutboxSender recovered outbox_id={} as DELIVERED (queryable receipt)",
                    claim.outbox_id,
                )
            else:
                self._store.mark_delivery_outcome_unknown(
                    claim,
                    SafeError(
                        error_code="DELIVERY_OUTCOME_UNKNOWN",
                        error_summary="expired lease; queryable receipt inconclusive",
                    ),
                )
                logger.warning(
                    "OutboxSender recovered outbox_id={} as OUTCOME_UNKNOWN (queryable)",
                    claim.outbox_id,
                )
        else:
            # NONE or unknown — the outcome cannot be reconciled.
            self._store.mark_delivery_outcome_unknown(
                claim,
                SafeError(
                    error_code="DELIVERY_OUTCOME_UNKNOWN",
                    error_summary="expired lease; channel has no recovery capability",
                ),
            )
            logger.warning(
                "OutboxSender recovered outbox_id={} as OUTCOME_UNKNOWN (no recovery)",
                claim.outbox_id,
            )

    def _query_channel_receipt(self, claim: OutboxClaim) -> DeliveryReceipt | None:
        """Query the Channel for a delivery receipt (Task 8 Step 4).

        Optional: only channels that declare ``QUERYABLE_RECEIPT`` and
        implement ``query_delivery_receipt`` can reconcile. Returns
        ``None`` when the Channel cannot be queried.
        """
        query_fn = getattr(self._channels, "query_delivery_receipt", None)
        if query_fn is None:
            return None
        try:
            return query_fn(claim.channel, claim.dedup_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "OutboxSender receipt query failed for outbox_id={}", claim.outbox_id
            )
            return None

    async def _deliver_one(self) -> bool:
        """Claim and deliver one outbox row. Returns True if a row was processed."""
        now_ms = _now_ms()
        claim = self._store.claim_next_delivery(self._sender_id, now_ms, self._lease_ms)
        if claim is None:
            return False

        logger.info(
            "OutboxSender claimed outbox_id={} task={} target={}",
            claim.outbox_id,
            claim.task_id,
            claim.target_key,
        )

        # Read the payload blob.
        payload_content = self._read_payload(claim)
        if payload_content is None:
            self._store.fail_delivery(
                claim,
                SafeError(
                    error_code="DELIVERY_PERMANENT",
                    error_summary="payload blob not found",
                ),
            )
            return True

        # Build the outbound message.
        msg = self._build_outbound_message(claim, payload_content)

        # Send with lease renewal.
        try:
            receipt = await self._send_with_lease_renewal(claim, msg)
        except DeliveryLeaseLost:
            # Lease was lost during the send — do NOT record a result.
            # The row stays SENDING and will be recovered by the
            # recovery loop on the next iteration (Task 8 Step 6).
            return True

        # Record the result.
        self._record_result(claim, receipt)
        return True

    def _read_payload(self, claim: OutboxClaim) -> bytes | None:
        """Read the payload blob bytes from the store (Task 7 Step 6)."""
        if not hasattr(self._store, "read_blob_content"):
            return None
        return self._store.read_blob_content(claim.payload_blob_id)

    @staticmethod
    def _build_outbound_message(claim: OutboxClaim, payload: bytes) -> Any:
        """Build an OutboundMessage from the claim and decoded payload.

        Task 7 Step 6: the payload blob is a versioned Outbox codec
        envelope. Decode by ``message_kind`` so the Channel receives
        ``str`` content and decoded media/metadata instead of raw JSON
        bytes (design §17.8, §20.6).
        """
        from miniunicorn.bus.events import OutboundMessage
        from miniunicorn.runtime.outbox_payload import decode_outbox_payload

        decoded = decode_outbox_payload(claim.message_kind, payload)
        return OutboundMessage(
            channel=claim.channel,
            chat_id=claim.target_key,
            content=decoded.content,
            reply_to=None,
            media=list(decoded.media),
            metadata=dict(decoded.metadata),
            buttons=None,
        )

    async def _send_with_lease_renewal(
        self, claim: OutboxClaim, msg: Any
    ) -> DeliveryReceipt:
        """Send the message with concurrent lease renewal (design §17.9).

        Task 8 Step 5: timeout handling is now channel-policy aware:
        - ``NATIVE_IDEMPOTENCY``: safe to retry with the same dedup key.
        - ``QUERYABLE_RECEIPT``: the outcome is unknown until the
          recovery loop reconciles the receipt.
        - ``NONE``: the outcome is unknown and cannot be reconciled.

        Task 8 Step 6: if the lease is lost during the send, raise
        ``DeliveryLeaseLost`` so the caller does not record a result
        with a stale claim.
        """
        renew_task = asyncio.create_task(
            self._renew_loop(claim), name=f"outbox-renew-{claim.outbox_id}"
        )
        try:
            receipt = await asyncio.wait_for(
                self._channels.send_with_receipt(claim.channel, msg),
                timeout=self._send_timeout_s,
            )
            return receipt
        except asyncio.TimeoutError:
            return self._timeout_receipt(claim)
        except DeliveryLeaseLost:
            # Lease was lost — do NOT record a result. The row stays
            # SENDING until the recovery loop reclaims it.
            logger.warning(
                "OutboxSender lease lost for outbox_id={}, deferring to recovery",
                claim.outbox_id,
            )
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return DeliveryReceipt(
                status="RETRYABLE_FAILURE",
                safe_error_code="DELIVERY_RETRYABLE",
                retry_after_ms=2000,
            )
        finally:
            renew_task.cancel()
            async with suppress_cancelled():
                await renew_task

    def _timeout_receipt(self, claim: OutboxClaim) -> DeliveryReceipt:
        """Build a timeout receipt based on channel recovery policy (Task 8 Step 5)."""
        recovery = self._channels.get_channel_recovery(claim.channel)
        if recovery == "NATIVE_IDEMPOTENCY":
            return DeliveryReceipt(
                status="RETRYABLE_FAILURE",
                safe_error_code="DELIVERY_TIMEOUT",
                retry_after_ms=2000,
            )
        # QUERYABLE_RECEIPT and NONE both defer to OUTCOME_UNKNOWN.
        # QUERYABLE_RECEIPT rows will be reconciled by the recovery
        # loop; NONE rows stay OUTCOME_UNKNOWN until manual resolution.
        return DeliveryReceipt(
            status="OUTCOME_UNKNOWN",
            safe_error_code="DELIVERY_TIMEOUT",
        )

    async def _renew_loop(self, claim: OutboxClaim) -> None:
        """Renew the delivery lease while the send is in progress (design §17.9).

        Task 8 Step 6: raises ``DeliveryLeaseLost`` when the lease
        cannot be renewed, so the send is cancelled and no stale
        result is recorded.
        """
        while True:
            await asyncio.sleep(self._renew_interval_s)
            until_ms = _now_ms() + self._lease_ms
            if not self._store.renew_delivery_lease(claim, until_ms):
                logger.warning(
                    "OutboxSender lost delivery lease for outbox_id={}",
                    claim.outbox_id,
                )
                raise DeliveryLeaseLost(claim.outbox_id)

    def _record_result(self, claim: OutboxClaim, receipt: DeliveryReceipt) -> None:
        """Record the delivery result to the store (design §17.9 finish transaction).

        Task 8: ``OUTCOME_UNKNOWN`` transitions the row to
        ``OUTCOME_UNKNOWN`` for explicit resolution (design §17.13).
        """
        if receipt.status == "DELIVERED":
            self._store.mark_delivered(claim, receipt)
            logger.info(
                "OutboxSender delivered outbox_id={} task={}",
                claim.outbox_id,
                claim.task_id,
            )
        elif receipt.status == "RETRYABLE_FAILURE":
            retry_after_ms = receipt.retry_after_ms or 2000
            retry = RetryDecision(
                kind="TRANSIENT",
                available_at_ms=_now_ms() + retry_after_ms,
                error=SafeError(
                    error_code=receipt.safe_error_code or "DELIVERY_RETRYABLE",
                    error_summary="retryable delivery failure",
                ),
            )
            self._store.retry_delivery(claim, retry)
            logger.warning(
                "OutboxSender retry outbox_id={} task={} after {}ms",
                claim.outbox_id,
                claim.task_id,
                retry_after_ms,
            )
        elif receipt.status == "PERMANENT_FAILURE":
            error = SafeError(
                error_code=receipt.safe_error_code or "DELIVERY_PERMANENT",
                error_summary="permanent delivery failure",
            )
            self._store.fail_delivery(claim, error)
            logger.error(
                "OutboxSender permanent failure outbox_id={} task={}",
                claim.outbox_id,
                claim.task_id,
            )
        elif receipt.status == "OUTCOME_UNKNOWN":
            error = SafeError(
                error_code=receipt.safe_error_code or "DELIVERY_OUTCOME_UNKNOWN",
                error_summary="delivery outcome unknown",
            )
            self._store.mark_delivery_outcome_unknown(claim, error)
            logger.warning(
                "OutboxSender outcome unknown outbox_id={} task={}",
                claim.outbox_id,
                claim.task_id,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Current UTC Unix milliseconds."""
    return int(time.time() * 1000)


class suppress_cancelled:
    """Context manager that suppresses asyncio.CancelledError."""

    async def __aenter__(self) -> "suppress_cancelled":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return exc_info[0] is asyncio.CancelledError
