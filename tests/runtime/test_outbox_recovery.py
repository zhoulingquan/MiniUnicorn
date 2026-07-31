"""Task 8 — Recover expired Outbox sends according to Channel policy (design §17.13).

Covers:

- Expired ``SENDING`` rows are recovered based on the Channel's
  recovery capability:
  - ``NATIVE_IDEMPOTENCY`` → ``RETRY_WAIT`` (safe to re-send).
  - ``QUERYABLE_RECEIPT`` → ``DELIVERED`` or ``OUTCOME_UNKNOWN``.
  - ``NONE`` → ``OUTCOME_UNKNOWN``.
- A later row for the same target is no longer wedged behind the
  recovered row.
- Response-lost-after-send: the Channel records the message, then the
  receipt is lost (timeout). For ``NONE``, the row becomes
  ``OUTCOME_UNKNOWN`` with exactly one external send.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import pytest

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.runtime.contracts import ClaimRequest
from miniunicorn.runtime.models import (
    BlobWrite,
    CompletionWrite,
    DeliveryReceipt,
)
from miniunicorn.runtime.outbox import OutboxSender

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class RecoveryChannelSender:
    """ChannelSender stub with configurable recovery policy.

    Records every send and can optionally simulate a timeout after
    recording the message (response-lost scenario).
    """

    def __init__(
        self,
        *,
        recovery: str = "NONE",
        receipt: DeliveryReceipt | None = None,
        timeout: bool = False,
        query_receipt: DeliveryReceipt | None = None,
    ) -> None:
        self._recovery = recovery
        self._receipt = receipt
        self._timeout = timeout
        self._query_receipt = query_receipt
        self.send_count = 0
        self.sent_messages: list[OutboundMessage] = []

    async def send_with_receipt(
        self, channel_name: str, msg: OutboundMessage
    ) -> DeliveryReceipt:
        self.send_count += 1
        self.sent_messages.append(msg)
        if self._timeout:
            raise asyncio.TimeoutError()
        if self._receipt is not None:
            return self._receipt
        return DeliveryReceipt(status="DELIVERED")

    def get_channel_recovery(self, channel_name: str) -> str:
        return self._recovery

    def query_delivery_receipt(
        self, channel_name: str, dedup_key: str
    ) -> DeliveryReceipt | None:
        return self._query_receipt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_payload_blob(store: Any, content: str, scope_key: str = "test") -> str:
    payload_bytes = content.encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    blob = store.write_blob(
        BlobWrite(
            scope_key=scope_key,
            blob_kind="OUTBOX_PAYLOAD",
            content_hash=payload_hash,
            encoding="RAW_BYTES",
            inline_content=payload_bytes,
            size_bytes=len(payload_bytes),
            created_at_ms=1_000_000,
        )
    )
    return blob.blob_id


def _claim_and_run_task(store: Any, sample_scope: Any, make_inbound_envelope: Any):
    """Submit, claim, and mark-running a task. Returns (record, claim)."""
    now_ms = int(time.time() * 1000)
    env = make_inbound_envelope(sample_scope)
    submit = store.submit_task(env)
    assert submit.status == "ACCEPTED"
    result = store.claim_next(
        ClaimRequest(worker_id="test-worker", now_ms=now_ms, lease_ms=60_000)
    )
    assert result.claimed is not None
    record = store.mark_running(result.claimed.claim, now_ms=now_ms + 1)
    return record, result.claimed.claim


def _enqueue_final_reply(
    store: Any,
    claim: Any,
    content: str = "hello",
    channel: str = "test-channel",
    channel_account: str = "test-account",
    target_key: str = "test-target",
) -> int:
    blob_id = _write_payload_blob(store, content)
    payload_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    completion = CompletionWrite(
        final_reply_blob_id=blob_id,
        final_reply_hash=payload_hash,
        final_reply_dedup_key=None,
        suppress_final=False,
        completed_at_ms=2_000_002,
        channel=channel,
        channel_account=channel_account,
        target_key=target_key,
    )
    result = store.complete_with_outbox(claim, completion)
    assert result.outbox_id is not None
    return result.outbox_id


def _seed_expired_sending(
    store: Any,
    sample_scope: Any,
    make_inbound_envelope: Any,
    *,
    channel: str = "test-channel",
    channel_account: str = "test-account",
    target_key: str = "test-target",
    content: str = "expired message",
) -> int:
    """Seed a SENDING row with an already-expired lease.

    Returns the outbox_id of the expired row.
    """
    record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
    outbox_id = _enqueue_final_reply(
        store,
        claim,
        content=content,
        channel=channel,
        channel_account=channel_account,
        target_key=target_key,
    )
    # Claim the delivery to transition PENDING → SENDING.
    # Use real-time now_ms so available_at_ms <= now_ms passes.
    now_ms = int(time.time() * 1000)
    delivery_claim = store.claim_next_delivery(
        "original-sender", now_ms=now_ms, lease_ms=60_000
    )
    assert delivery_claim is not None
    assert delivery_claim.outbox_id == outbox_id
    # Simulate the lease expiring by backdating lease_until_ms.
    store._conn.execute(
        "UPDATE outbox SET lease_until_ms=? WHERE outbox_id=?",
        (1_000, outbox_id),  # expired long ago
    )
    store._conn.commit()
    return outbox_id


def _enqueue_second_row(
    store: Any,
    sample_scope: Any,
    make_inbound_envelope: Any,
    target_key: str = "test-target",
) -> int:
    """Enqueue a second row for the same target."""
    record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
    return _enqueue_final_reply(
        store, claim, content="second message", target_key=target_key
    )


# ---------------------------------------------------------------------------
# Tests: expired SENDING recovery (Task 8 Step 1)
# ---------------------------------------------------------------------------


class TestExpiredSendingRecovery:
    """Expired SENDING rows are recovered according to channel policy."""

    def test_native_idempotency_recovers_to_retry_wait(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """NATIVE_IDEMPOTENCY → RETRY_WAIT (safe to re-send with same dedup key)."""
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)
        # Preserve the dedup_key for assertion.
        row_before = store.read_outbox_record(outbox_id)
        assert row_before is not None
        dedup_before = row_before.dedup_key

        sender = RecoveryChannelSender(recovery="NATIVE_IDEMPOTENCY")
        outbox_sender = OutboxSender(
            store, sender, sender_id="recovery-sender", poll_interval_s=0.01
        )
        recovered = asyncio.run(outbox_sender._recover_expired())
        assert recovered is True

        row_after = store.read_outbox_record(outbox_id)
        assert row_after is not None
        assert row_after.state == "RETRY_WAIT"
        # Same dedup key preserved for idempotent retry.
        assert row_after.dedup_key == dedup_before

    def test_queryable_receipt_confirmed_delivered(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """QUERYABLE_RECEIPT with confirmed receipt → DELIVERED."""
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)

        sender = RecoveryChannelSender(
            recovery="QUERYABLE_RECEIPT",
            query_receipt=DeliveryReceipt(
                status="DELIVERED",
                provider_message_id="provider-123",
            ),
        )
        outbox_sender = OutboxSender(
            store, sender, sender_id="recovery-sender", poll_interval_s=0.01
        )
        recovered = asyncio.run(outbox_sender._recover_expired())
        assert recovered is True

        row_after = store.read_outbox_record(outbox_id)
        assert row_after is not None
        assert row_after.state == "DELIVERED"
        assert row_after.provider_receipt_ref == "provider-123"

    def test_queryable_receipt_inconclusive_outcome_unknown(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """QUERYABLE_RECEIPT with inconclusive query → OUTCOME_UNKNOWN."""
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)

        sender = RecoveryChannelSender(
            recovery="QUERYABLE_RECEIPT",
            query_receipt=None,  # Channel cannot find the receipt.
        )
        outbox_sender = OutboxSender(
            store, sender, sender_id="recovery-sender", poll_interval_s=0.01
        )
        recovered = asyncio.run(outbox_sender._recover_expired())
        assert recovered is True

        row_after = store.read_outbox_record(outbox_id)
        assert row_after is not None
        assert row_after.state == "OUTCOME_UNKNOWN"

    def test_none_recovery_marks_outcome_unknown(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """NONE → OUTCOME_UNKNOWN."""
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)

        sender = RecoveryChannelSender(recovery="NONE")
        outbox_sender = OutboxSender(
            store, sender, sender_id="recovery-sender", poll_interval_s=0.01
        )
        recovered = asyncio.run(outbox_sender._recover_expired())
        assert recovered is True

        row_after = store.read_outbox_record(outbox_id)
        assert row_after is not None
        assert row_after.state == "OUTCOME_UNKNOWN"

    @pytest.mark.parametrize(
        "recovery,expected_state",
        [
            ("NATIVE_IDEMPOTENCY", "RETRY_WAIT"),
            ("QUERYABLE_RECEIPT", "OUTCOME_UNKNOWN"),
            ("NONE", "OUTCOME_UNKNOWN"),
        ],
    )
    def test_later_row_not_wedged_after_recovery(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
        recovery: str,
        expected_state: str,
    ) -> None:
        """After recovery, the expired row is no longer silently stuck in SENDING.

        The row transitions to a visible, actionable state. For
        ``OUTCOME_UNKNOWN`` the second row is still ordered behind it
        (design §16.13 ordering invariant), but the blockage is now
        visible — not a silent expired ``SENDING``. The operator can
        resolve it via ``resolve_unknown_delivery``.

        For ``NATIVE_IDEMPOTENCY`` the recovered row becomes
        ``RETRY_WAIT`` and will be re-delivered, eventually unblocking
        the second row.
        """
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)
        second_id = _enqueue_second_row(store, sample_scope, make_inbound_envelope)

        # Before recovery: the first row is silently stuck in SENDING
        # with an expired lease. The second row is wedged behind it.
        row_before = store.read_outbox_record(outbox_id)
        assert row_before is not None
        assert row_before.state == "SENDING"

        # Recover the expired row.
        sender = RecoveryChannelSender(recovery=recovery)
        outbox_sender = OutboxSender(
            store, sender, sender_id="recovery-sender", poll_interval_s=0.01
        )
        asyncio.run(outbox_sender._recover_expired())

        # The expired row is no longer SENDING — it has been recovered
        # to a visible, actionable state.
        row_after = store.read_outbox_record(outbox_id)
        assert row_after is not None
        assert row_after.state == expected_state
        assert row_after.state != "SENDING"

        # For OUTCOME_UNKNOWN: the row can now be explicitly resolved.
        # After resolution to DELIVERED, the second row is claimable.
        if expected_state == "OUTCOME_UNKNOWN":
            store.resolve_unknown_delivery(
                outbox_id,
                "MARK_DELIVERED",
                resolved_by="operator",
            )
            now_ms = int(time.time() * 1000)
            claim = store.claim_next_delivery(
                "sender", now_ms=now_ms, lease_ms=60_000
            )
            assert claim is not None
            assert claim.outbox_id == second_id


# ---------------------------------------------------------------------------
# Tests: response-lost-after-send (Task 8 Step 2)
# ---------------------------------------------------------------------------


class TestResponseLostAfterSend:
    """When the Channel records the message but the receipt is lost."""

    def test_none_timeout_marks_outcome_unknown_no_retry(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """For NONE recovery, a timeout after the Channel records the
        message results in OUTCOME_UNKNOWN with exactly one send.

        The OutboxSender must not automatically retry.
        """
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        outbox_id = _enqueue_final_reply(store, claim)

        # Channel records the message then raises TimeoutError.
        sender = RecoveryChannelSender(recovery="NONE", timeout=True)
        outbox_sender = OutboxSender(
            store,
            sender,
            sender_id="test-sender",
            poll_interval_s=0.01,
            lease_ms=60_000,
            send_timeout_s=5,
        )
        delivered = asyncio.run(outbox_sender._deliver_one())
        assert delivered is True

        # Exactly one external send was attempted.
        assert sender.send_count == 1

        # The row is OUTCOME_UNKNOWN, not RETRY_WAIT.
        row = store.read_outbox_record(outbox_id)
        assert row is not None
        assert row.state == "OUTCOME_UNKNOWN"

    def test_native_idempotency_timeout_is_retryable(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """For NATIVE_IDEMPOTENCY, a timeout is safe to retry."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        outbox_id = _enqueue_final_reply(store, claim)

        sender = RecoveryChannelSender(recovery="NATIVE_IDEMPOTENCY", timeout=True)
        outbox_sender = OutboxSender(
            store,
            sender,
            sender_id="test-sender",
            poll_interval_s=0.01,
            lease_ms=60_000,
            send_timeout_s=5,
        )
        delivered = asyncio.run(outbox_sender._deliver_one())
        assert delivered is True

        assert sender.send_count == 1

        row = store.read_outbox_record(outbox_id)
        assert row is not None
        assert row.state == "RETRY_WAIT"

    def test_queryable_timeout_marks_outcome_unknown(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """For QUERYABLE_RECEIPT, a timeout defers to OUTCOME_UNKNOWN
        pending reconciliation by the recovery loop."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        outbox_id = _enqueue_final_reply(store, claim)

        sender = RecoveryChannelSender(recovery="QUERYABLE_RECEIPT", timeout=True)
        outbox_sender = OutboxSender(
            store,
            sender,
            sender_id="test-sender",
            poll_interval_s=0.01,
            lease_ms=60_000,
            send_timeout_s=5,
        )
        delivered = asyncio.run(outbox_sender._deliver_one())
        assert delivered is True

        assert sender.send_count == 1

        row = store.read_outbox_record(outbox_id)
        assert row is not None
        assert row.state == "OUTCOME_UNKNOWN"


# ---------------------------------------------------------------------------
# Tests: claim_expired_deliveries store method (Task 8 Step 3)
# ---------------------------------------------------------------------------


class TestClaimExpiredDeliveries:
    """The store method for claiming expired SENDING rows."""

    def test_returns_empty_when_no_expired(self, store: Any) -> None:
        """No expired rows → empty tuple."""
        claims = store.claim_expired_deliveries("sender", now_ms=3_000_000, lease_ms=60_000)
        assert claims == ()

    def test_claims_expired_sending_row(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """An expired SENDING row is claimed with a fresh lease."""
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)

        claims = store.claim_expired_deliveries(
            "recovery-sender", now_ms=3_000_000, lease_ms=60_000
        )
        assert len(claims) == 1
        assert claims[0].outbox_id == outbox_id
        # The lease_epoch should have been bumped.
        assert claims[0].lease_epoch >= 1
        # The new lease_until_ms should be in the future.
        assert claims[0].lease_until_ms >= 3_000_000

    def test_does_not_claim_active_sending(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """A SENDING row with an active (non-expired) lease is not claimed."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        _enqueue_final_reply(store, claim)
        # Claim the delivery — this creates an active SENDING lease.
        store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)

        claims = store.claim_expired_deliveries(
            "recovery-sender", now_ms=3_000_100, lease_ms=60_000
        )
        assert claims == ()

    def test_fencing_prevents_stale_write(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """After recovery claims the row, the old lease cannot write."""
        outbox_id = _seed_expired_sending(store, sample_scope, make_inbound_envelope)

        # Get the old claim (before recovery).
        old_row = store._conn.execute(
            "SELECT lease_token, lease_epoch FROM outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        from miniunicorn.runtime.models import OutboxClaim

        # Build a stale claim with the old lease.
        stale_claim = OutboxClaim(
            outbox_id=outbox_id,
            task_id="test-task",
            channel="test-channel",
            channel_account="test-account",
            target_key="test-target",
            message_kind="FINAL_REPLY",
            payload_blob_id="blob",
            payload_hash="hash",
            dedup_key="dedup",
            lease_token=old_row["lease_token"],
            lease_epoch=old_row["lease_epoch"],
            lease_until_ms=0,
            attempt_count=1,
        )

        # Recovery claims the row with a fresh lease.
        claims = store.claim_expired_deliveries(
            "recovery-sender", now_ms=3_000_000, lease_ms=60_000
        )
        assert len(claims) == 1

        # The stale claim cannot write.
        with pytest.raises(Exception):
            store.mark_delivered(
                stale_claim, DeliveryReceipt(status="DELIVERED")
            )
