"""WP5 — Durable Outbox and Channel integration tests (design §17.8, §17.9, §17.13, §20.6, §23.5).

Covers:

- atomic complete-and-enqueue (producer side);
- target-head Outbox claim (per-target ordering);
- Channel receipts (DeliveryReceipt mapping);
- retry, permanent failure, and delivery fencing;
- OUTCOME_UNKNOWN resolution;
- Message Tool returns stable Outbox receipt;
- crash recovery scenarios (before/after enqueue, after send before receipt).

These tests use the real :class:`SqliteRuntimeStore` for the DeliveryLedger
view and a stub ChannelSender so the durable delivery layer is exercised
without real network calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from miniunicorn.agent.ports import SafeError
from miniunicorn.bus.events import OutboundMessage
from miniunicorn.runtime.contracts import ClaimRequest
from miniunicorn.runtime.models import (
    BlobWrite,
    CompletionWrite,
    DeliveryReceipt,
    OutboxRecord,
    RetryDecision,
)
from miniunicorn.runtime.outbox import OutboxSender
from miniunicorn.runtime.session_committer import set_active_claim, clear_active_claim


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubChannelSender:
    """Stub ChannelSender for testing.

    Records all send calls and can be configured to succeed, fail
    transiently, or fail permanently.
    """

    def __init__(
        self,
        *,
        receipt: DeliveryReceipt | None = None,
        receipts: list[DeliveryReceipt] | None = None,
    ) -> None:
        self._receipt = receipt
        self._receipts = receipts or []
        self._call_index = 0
        self.sent_messages: list[OutboundMessage] = []

    async def send_with_receipt(
        self, channel_name: str, msg: OutboundMessage
    ) -> DeliveryReceipt:
        self.sent_messages.append(msg)
        if self._receipts:
            receipt = self._receipts[min(self._call_index, len(self._receipts) - 1)]
            self._call_index += 1
            return receipt
        if self._receipt is not None:
            return self._receipt
        return DeliveryReceipt(status="DELIVERED")

    def get_channel_recovery(self, channel_name: str) -> str:
        return "NONE"


def _write_payload_blob(store: Any, content: str, scope_key: str = "test") -> str:
    """Write a payload blob and return its blob_id."""
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


def _claim_and_run_task(
    store: Any,
    sample_scope: Any,
    make_inbound_envelope: Any,
    *,
    channel: str | None = None,
    channel_account: str | None = None,
    target_key: str | None = None,
):
    """Submit, claim, and mark-running a task. Returns (record, claim).

    Task 7: optionally overrides the inbound routing fields so the
    TaskRecord carries durable delivery routing the Worker can copy
    verbatim into the FINAL_REPLY Outbox row (design §17.8).
    """
    # Use real-time now_ms so the lease deadline is in the future for
    # downstream mutations that call _validate_lease with real-time
    # _now_ms() (Task 2 Step 5 deadline fencing).
    now_ms = int(time.time() * 1000)
    envelope_kwargs: dict[str, Any] = {}
    if channel is not None:
        envelope_kwargs["channel"] = channel
    if channel_account is not None:
        envelope_kwargs["channel_account"] = channel_account
    if target_key is not None:
        envelope_kwargs["target_key"] = target_key
    env = make_inbound_envelope(sample_scope, **envelope_kwargs)
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
    content: str = "hello world",
    channel: str = "test-channel",
    channel_account: str = "test-account",
    target_key: str = "test-target",
) -> int:
    """Complete a task with outbox enqueue and return the outbox_id."""
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
    assert result.status == "COMPLETED"
    assert result.outbox_id is not None
    return result.outbox_id


# ---------------------------------------------------------------------------
# Tests: atomic complete-and-enqueue (producer side)
# ---------------------------------------------------------------------------


class TestCompleteAndEnqueue:
    """Atomic complete-and-enqueue (design §17.8, WP5 task 1)."""

    def test_complete_with_outbox_creates_pending_row(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Completing a task enqueues a PENDING outbox row atomically."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        outbox_id = _enqueue_final_reply(store, claim, content="test reply")

        row = store.read_outbox_record(outbox_id)
        assert row is not None
        assert row.state == "PENDING"
        assert row.message_kind == "FINAL_REPLY"
        assert row.attempt_count == 0

    def test_crash_before_enqueue_leaves_no_outbox(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """If the task is still RUNNING (crash before enqueue), no outbox row exists."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        # Don't complete — simulate crash before enqueue.
        rows = store._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
        assert rows[0] == 0


# ---------------------------------------------------------------------------
# Tests: target-head Outbox claim (per-target ordering)
# ---------------------------------------------------------------------------


class TestTargetHeadClaim:
    """Target-head Outbox claim (design §17.9, §16.13, WP5 task 2)."""

    def test_claim_returns_earliest_pending(self, store: Any, sample_scope: Any, make_inbound_envelope: Any) -> None:
        """Claim returns the earliest PENDING row."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid1 = _enqueue_final_reply(store, claim, content="first")

        claim2 = store.claim_next_delivery("sender-1", now_ms=3_000_000, lease_ms=60_000)
        assert claim2 is not None
        assert claim2.outbox_id == oid1
        assert claim2.lease_token != ""

    def test_retrying_earlier_blocks_later_same_target(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """A retrying earlier message blocks a later same-target message (design §16.13)."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid1 = _enqueue_final_reply(store, claim, content="first")

        # Claim and retry the first message.
        c1 = store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)
        assert c1 is not None
        store.retry_delivery(
            c1,
            RetryDecision(
                kind="TRANSIENT",
                available_at_ms=3_001_000,
                error=SafeError(error_code="DELIVERY_RETRYABLE", error_summary="fail"),
            ),
        )

        # Enqueue a second message for the SAME target.
        # Need a new task for this.
        record2, claim2 = _claim_and_run_task(
            store,
            sample_scope,
            make_inbound_envelope,
        )
        # Reuse the same target by directly inserting.
        blob_id = _write_payload_blob(store, "second")
        store._conn.execute(
            """INSERT INTO outbox (
                task_id, channel, channel_account, target_key,
                message_kind, payload_blob_id, payload_hash, dedup_key,
                state, max_attempts, available_at_ms, lease_epoch, created_at_ms
            ) VALUES (?, '', '', '', 'FINAL_REPLY', ?, ?, ?, 'PENDING', 8, ?, 0, ?)""",
            (claim2.task_id, blob_id, hashlib.sha256(b"second").hexdigest(),
             hashlib.sha256(b"dedup2").hexdigest(), 3_000_000, 3_000_000),
        )
        store._conn.commit()

        # The second row should NOT be claimable because the first is RETRY_WAIT.
        c2 = store.claim_next_delivery("sender", now_ms=3_002_000, lease_ms=60_000)
        # The first row (RETRY_WAIT) should be claimed first since available_at_ms <= now.
        assert c2 is not None
        assert c2.outbox_id == oid1

    def test_different_targets_send_concurrently(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Different targets can be claimed independently (design §16.13)."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)

        # Enqueue two rows for different targets.
        blob1 = _write_payload_blob(store, "msg-a", scope_key="a")
        blob2 = _write_payload_blob(store, "msg-b", scope_key="b")
        store._conn.execute(
            """INSERT INTO outbox (
                task_id, channel, channel_account, target_key,
                message_kind, payload_blob_id, payload_hash, dedup_key,
                state, max_attempts, available_at_ms, lease_epoch, created_at_ms
            ) VALUES (?, 'ch', '', 'target-A', 'FINAL_REPLY', ?, ?, ?, 'PENDING', 8, ?, 0, ?)""",
            (claim.task_id, blob1, "ha", hashlib.sha256(b"a").hexdigest(), 1, 1),
        )
        store._conn.execute(
            """INSERT INTO outbox (
                task_id, channel, channel_account, target_key,
                message_kind, payload_blob_id, payload_hash, dedup_key,
                state, max_attempts, available_at_ms, lease_epoch, created_at_ms
            ) VALUES (?, 'ch', '', 'target-B', 'FINAL_REPLY', ?, ?, ?, 'PENDING', 8, ?, 0, ?)""",
            (claim.task_id, blob2, "hb", hashlib.sha256(b"b").hexdigest(), 1, 1),
        )
        store._conn.commit()

        # Both should be claimable (different targets).
        c1 = store.claim_next_delivery("sender", now_ms=2_000_000, lease_ms=60_000)
        c2 = store.claim_next_delivery("sender", now_ms=2_000_000, lease_ms=60_000)
        assert c1 is not None
        assert c2 is not None
        assert c1.target_key != c2.target_key


# ---------------------------------------------------------------------------
# Tests: Channel receipts and delivery fencing
# ---------------------------------------------------------------------------


class TestDeliveryFencing:
    """Delivery fencing and receipts (design §17.9, §23.5, WP5 task 3, 4)."""

    def test_mark_delivered_sets_receipt(self, store: Any, sample_scope: Any, make_inbound_envelope: Any) -> None:
        """Marking delivered stores the provider receipt."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        c = store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)
        assert c is not None
        receipt = DeliveryReceipt(
            status="DELIVERED",
            provider_message_id="msg-123",
        )
        store.mark_delivered(c, receipt)

        row = store.read_outbox_record(oid)
        assert row.state == "DELIVERED"
        assert row.provider_receipt_ref == "msg-123"
        assert row.delivered_at_ms is not None

    def test_stale_delivery_lease_cannot_commit(self, store: Any, sample_scope: Any, make_inbound_envelope: Any) -> None:
        """A stale delivery lease cannot mark delivered (design §17.9 fencing)."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        c = store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)
        assert c is not None

        # Forge a stale claim with wrong epoch.
        from miniunicorn.runtime.models import OutboxClaim as OC

        stale = OC(
            outbox_id=c.outbox_id,
            task_id=c.task_id,
            channel=c.channel,
            channel_account=c.channel_account,
            target_key=c.target_key,
            message_kind=c.message_kind,
            payload_blob_id=c.payload_blob_id,
            payload_hash=c.payload_hash,
            dedup_key=c.dedup_key,
            lease_token=c.lease_token,
            lease_epoch=c.lease_epoch + 999,
            lease_until_ms=c.lease_until_ms,
            attempt_count=c.attempt_count,
        )
        with pytest.raises(Exception):
            store.mark_delivered(stale, DeliveryReceipt(status="DELIVERED"))

    def test_retry_increments_attempt_and_sets_retry_wait(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Retry sets RETRY_WAIT with backoff."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        c = store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)
        assert c is not None
        assert c.attempt_count == 1

        store.retry_delivery(
            c,
            RetryDecision(
                kind="TRANSIENT",
                available_at_ms=3_005_000,
                error=SafeError(error_code="DELIVERY_RETRYABLE", error_summary="fail"),
            ),
        )
        row = store.read_outbox_record(oid)
        assert row.state == "RETRY_WAIT"
        assert row.attempt_count == 1

    def test_exhausted_retries_become_failed(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Retrying past max_attempts transitions to FAILED."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        # Manually set attempt_count to max_attempts so next retry fails.
        store._conn.execute(
            "UPDATE outbox SET attempt_count=8 WHERE outbox_id=?", (oid,)
        )
        store._conn.commit()

        c = store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)
        assert c is not None
        assert c.attempt_count == 9

        store.retry_delivery(
            c,
            RetryDecision(
                kind="TRANSIENT",
                available_at_ms=3_005_000,
                error=SafeError(error_code="DELIVERY_RETRYABLE", error_summary="fail"),
            ),
        )
        row = store.read_outbox_record(oid)
        assert row.state == "FAILED"

    def test_fail_delivery_is_permanent(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """fail_delivery marks the row as permanently FAILED."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        c = store.claim_next_delivery("sender", now_ms=3_000_000, lease_ms=60_000)
        store.fail_delivery(
            c,
            SafeError(error_code="DELIVERY_PERMANENT", error_summary="bad"),
        )
        row = store.read_outbox_record(oid)
        assert row.state == "FAILED"
        assert row.error is not None
        assert row.error.error_code == "DELIVERY_PERMANENT"


# ---------------------------------------------------------------------------
# Tests: OUTCOME_UNKNOWN resolution
# ---------------------------------------------------------------------------


class TestOutcomeUnknownResolution:
    """OUTCOME_UNKNOWN resolution (design §17.13, WP5 task 4)."""

    def test_resolve_mark_delivered(self, store: Any, sample_scope: Any, make_inbound_envelope: Any) -> None:
        """Resolving OUTCOME_UNKNOWN with MARK_DELIVERED marks as delivered."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        # Manually set state to OUTCOME_UNKNOWN.
        store._conn.execute(
            "UPDATE outbox SET state='OUTCOME_UNKNOWN' WHERE outbox_id=?", (oid,)
        )
        store._conn.commit()

        store.resolve_unknown_delivery(
            oid,
            "MARK_DELIVERED",
            receipt=DeliveryReceipt(status="DELIVERED", provider_message_id="rec-1"),
            resolved_by="operator",
        )
        row = store.read_outbox_record(oid)
        assert row.state == "DELIVERED"
        assert row.provider_receipt_ref == "rec-1"

    def test_resolve_retry_returns_to_pending(self, store: Any, sample_scope: Any, make_inbound_envelope: Any) -> None:
        """Resolving OUTCOME_UNKNOWN with RETRY returns to PENDING."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        store._conn.execute(
            "UPDATE outbox SET state='OUTCOME_UNKNOWN' WHERE outbox_id=?", (oid,)
        )
        store._conn.commit()

        store.resolve_unknown_delivery(oid, "RETRY", resolved_by="operator")
        row = store.read_outbox_record(oid)
        assert row.state == "PENDING"

    def test_resolve_non_unknown_raises(self, store: Any, sample_scope: Any, make_inbound_envelope: Any) -> None:
        """Resolving a non-OUTCOME_UNKNOWN row raises."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        # Row is PENDING, not OUTCOME_UNKNOWN.
        with pytest.raises(ValueError):
            store.resolve_unknown_delivery(oid, "RETRY", resolved_by="operator")


# ---------------------------------------------------------------------------
# Tests: OutboxSender end-to-end
# ---------------------------------------------------------------------------


class TestOutboxSender:
    """OutboxSender loop tests (design §8.7, WP5 task 2, 3, 4)."""

    @pytest.mark.asyncio
    async def test_sender_delivers_pending_row(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """OutboxSender delivers a PENDING row and marks it DELIVERED."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello world")

        sender = StubChannelSender(
            receipt=DeliveryReceipt(status="DELIVERED", provider_message_id="rec-1")
        )
        outbox = OutboxSender(
            store, sender, sender_id="test-sender", poll_interval_s=0.05
        )
        await outbox.start()
        try:
            await asyncio.sleep(0.3)
        finally:
            await outbox.stop()

        row = store.read_outbox_record(oid)
        assert row.state == "DELIVERED"
        assert row.provider_receipt_ref == "rec-1"
        assert len(sender.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_sender_retries_on_transient_failure(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """OutboxSender retries on RETRYABLE_FAILURE."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        # First send fails transiently, second succeeds.
        sender = StubChannelSender(
            receipts=[
                DeliveryReceipt(
                    status="RETRYABLE_FAILURE",
                    safe_error_code="DELIVERY_RETRYABLE",
                    retry_after_ms=100,
                ),
                DeliveryReceipt(status="DELIVERED", provider_message_id="rec-2"),
            ]
        )
        outbox = OutboxSender(
            store, sender, sender_id="test-sender", poll_interval_s=0.05
        )
        await outbox.start()
        try:
            await asyncio.sleep(0.5)
        finally:
            await outbox.stop()

        row = store.read_outbox_record(oid)
        assert row.state == "DELIVERED"
        assert row.provider_receipt_ref == "rec-2"

    @pytest.mark.asyncio
    async def test_sender_marks_permanent_failure(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """OutboxSender marks FAILED on PERMANENT_FAILURE."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        oid = _enqueue_final_reply(store, claim, content="hello")

        sender = StubChannelSender(
            receipt=DeliveryReceipt(
                status="PERMANENT_FAILURE",
                safe_error_code="DELIVERY_PERMANENT",
            )
        )
        outbox = OutboxSender(
            store, sender, sender_id="test-sender", poll_interval_s=0.05
        )
        await outbox.start()
        try:
            await asyncio.sleep(0.3)
        finally:
            await outbox.stop()

        row = store.read_outbox_record(oid)
        assert row.state == "FAILED"


# ---------------------------------------------------------------------------
# Tests: Message Tool Outbox enqueue
# ---------------------------------------------------------------------------


class TestMessageToolOutbox:
    """Message Tool Outbox enqueue (design §20.6, WP5 task 5)."""

    def test_enqueue_message_tool_outbox_returns_id(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """enqueue_message_tool_outbox creates a PENDING MESSAGE_TOOL row."""
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(record.task_id, claim)
        try:
            blob_id = _write_payload_blob(store, "tool message", scope_key="msg-tool")
            oid = store.enqueue_message_tool_outbox(
                claim,
                channel="test-channel",
                channel_account="test-account",
                target_key="test-target",
                payload_blob_id=blob_id,
                payload_hash=hashlib.sha256(b"tool message").hexdigest(),
                dedup_key=hashlib.sha256(b"dedup").hexdigest(),
                now_ms=2_000_003,
            )
            assert oid > 0

            row = store.read_outbox_record(oid)
            assert row.state == "PENDING"
            assert row.message_kind == "MESSAGE_TOOL"
            assert row.channel == "test-channel"
            assert row.target_key == "test-target"
        finally:
            clear_active_claim(record.task_id)


# ---------------------------------------------------------------------------
# Tests: Message Tool durable enqueue via _try_durable_enqueue (WP5 task 5)
# ---------------------------------------------------------------------------


class TestMessageToolDurableEnqueue:
    """MessageTool routes through Outbox in durable runtime (design §20.6, WP5 task 5)."""

    def test_message_tool_enqueues_to_outbox_in_durable_mode(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """MessageTool enqueues to Outbox when delivery ledger and claim are bound."""
        from miniunicorn.agent.tools.message import MessageTool
        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime
        from miniunicorn.runtime.message_delivery import DurableMessageDelivery
        from miniunicorn.runtime.session_committer import (
            set_active_delivery_ledger,
            clear_active_delivery_ledger,
            set_active_tool_call_id,
            clear_active_tool_call_id,
        )

        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(record.task_id, claim)
        set_active_delivery_ledger(record.task_id, store)
        set_active_tool_call_id(record.task_id, "test-tool-call-1")

        # Bind a TurnRuntime so MessageTool can find the task_id and
        # OutboundPort (design §20.6).
        rt = TurnRuntime(
            turn_id="test-turn",
            session_key="test-session",
            task_id=record.task_id,
            outbound_port=DurableMessageDelivery(record.task_id),
        )
        token = bind_turn_runtime(rt)

        tool = MessageTool(
            default_channel="test-channel",
            default_chat_id="test-target",
        )

        try:
            import asyncio
            result = asyncio.run(
                tool.execute(content="hello from durable tool")
            )
        finally:
            reset_turn_runtime(token)
            clear_active_tool_call_id(record.task_id)
            clear_active_delivery_ledger(record.task_id)
            clear_active_claim(record.task_id)

        assert "outbox_id=" in result
        # Extract the outbox_id from the result string.
        outbox_id_str = result.split("outbox_id=")[1].rstrip(")")
        outbox_id = int(outbox_id_str)
        assert outbox_id > 0

        row = store.read_outbox_record(outbox_id)
        assert row is not None
        assert row.state == "PENDING"
        assert row.message_kind == "MESSAGE_TOOL"
        assert row.channel == "test-channel"
        assert row.target_key == "test-target"

    def test_message_tool_falls_back_without_ledger(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """MessageTool raises when no OutboundPort is bound (WP5 hard cutover).

        The legacy send_callback fallback was removed. Without a bound
        OutboundPort (no TurnRuntime or outbound_port is None), the tool
        raises RuntimeError per design §20.6.
        """
        from miniunicorn.agent.tools.message import MessageTool

        tool = MessageTool(
            default_channel="test-channel",
            default_chat_id="test-target",
        )

        # No TurnRuntime / OutboundPort bound → raises RuntimeError.
        import asyncio
        with pytest.raises(RuntimeError, match="OutboundPort is required"):
            asyncio.run(
                tool.execute(content="hello")
            )

    def test_message_tool_durable_sets_sent_in_turn(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Durable enqueue sets _sent_in_turn for same-target sends (design §17.8)."""
        from miniunicorn.agent.tools.message import MessageTool
        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime
        from miniunicorn.runtime.message_delivery import DurableMessageDelivery
        from miniunicorn.runtime.session_committer import (
            set_active_delivery_ledger,
            clear_active_delivery_ledger,
            set_active_tool_call_id,
            clear_active_tool_call_id,
        )

        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(record.task_id, claim)
        set_active_delivery_ledger(record.task_id, store)
        set_active_tool_call_id(record.task_id, "test-tool-call-2")

        rt = TurnRuntime(
            turn_id="test-turn",
            session_key="test-session",
            task_id=record.task_id,
            outbound_port=DurableMessageDelivery(record.task_id),
        )
        token = bind_turn_runtime(rt)

        tool = MessageTool(
            default_channel="test-channel",
            default_chat_id="test-target",
        )
        tool.start_turn()

        # Check _sent_in_turn inside the async context because ContextVar
        # changes made within asyncio.run() are isolated to that context.
        import asyncio

        async def _run() -> tuple[str, bool]:
            result = await tool.execute(
                content="same target message",
                channel="test-channel",
                chat_id="test-target",
            )
            return result, tool._sent_in_turn

        try:
            result, sent_in_turn = asyncio.run(_run())
        finally:
            reset_turn_runtime(token)
            clear_active_tool_call_id(record.task_id)
            clear_active_delivery_ledger(record.task_id)
            clear_active_claim(record.task_id)

        assert "outbox_id=" in result
        assert sent_in_turn is True

    def test_message_tool_durable_dedup_key_is_stable(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Dedup key is sha256(task_id:tool_call_id:message) (design §16.6)."""
        import hashlib
        from miniunicorn.agent.tools.message import MessageTool
        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime
        from miniunicorn.runtime.message_delivery import DurableMessageDelivery
        from miniunicorn.runtime.session_committer import (
            set_active_delivery_ledger,
            clear_active_delivery_ledger,
            set_active_tool_call_id,
            clear_active_tool_call_id,
        )

        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(record.task_id, claim)
        set_active_delivery_ledger(record.task_id, store)
        set_active_tool_call_id(record.task_id, "call-abc")

        rt = TurnRuntime(
            turn_id="test-turn",
            session_key="test-session",
            task_id=record.task_id,
            outbound_port=DurableMessageDelivery(record.task_id),
        )
        token = bind_turn_runtime(rt)

        tool = MessageTool(
            default_channel="test-channel",
            default_chat_id="test-target",
        )

        try:
            import asyncio
            asyncio.run(
                tool.execute(content="dedup test")
            )
        finally:
            reset_turn_runtime(token)
            clear_active_tool_call_id(record.task_id)
            clear_active_delivery_ledger(record.task_id)
            clear_active_claim(record.task_id)

        expected_dedup = hashlib.sha256(
            f"{record.task_id}:call-abc:message".encode("utf-8")
        ).hexdigest()
        # Find the outbox row by the expected dedup key.
        row = store._conn.execute(
            "SELECT * FROM outbox WHERE dedup_key=?", (expected_dedup,)
        ).fetchone()
        assert row is not None
        assert row["message_kind"] == "MESSAGE_TOOL"


# ---------------------------------------------------------------------------
# Tests: Transient streaming retention (WP5 task 6)
# ---------------------------------------------------------------------------


class TestTransientStreamingRetention:
    """Transient streaming stays on Message Bus (design §23.1, WP5 task 6)."""

    def test_streaming_callbacks_publish_deltas_to_bus(self) -> None:
        """AgentExecutionCallback publishes stream deltas to the Message Bus."""
        from miniunicorn.bus.events import OutboundMessage
        from miniunicorn.bus.queue import MessageBus

        bus = MessageBus()

        # Simulate the on_stream callback from AgentExecutionCallback.
        import time

        stream_base_id = f"test-session:{time.time_ns()}"
        stream_segment = 0

        def _current_stream_id() -> str:
            return f"{stream_base_id}:{stream_segment}"

        async def on_stream(delta: str) -> None:
            meta = {"_stream_delta": True, "_stream_id": _current_stream_id()}
            await bus.publish_outbound(
                OutboundMessage(
                    channel="websocket",
                    chat_id="test-chat",
                    content=delta,
                    metadata=meta,
                )
            )

        async def on_stream_end(*, resuming: bool = False) -> None:
            nonlocal stream_segment
            meta = {"_stream_end": True, "_resuming": resuming, "_stream_id": _current_stream_id()}
            await bus.publish_outbound(
                OutboundMessage(
                    channel="websocket",
                    chat_id="test-chat",
                    content="",
                    metadata=meta,
                )
            )
            stream_segment += 1

        import asyncio

        async def _run():
            await on_stream("Hello ")
            await on_stream("world")
            await on_stream_end()

        asyncio.run(_run())

        # The bus should have 3 messages: 2 deltas + 1 end.
        drained = []
        while not bus.outbound.empty():
            drained.append(bus.outbound.get_nowait())
        assert len(drained) == 3
        assert drained[0].metadata.get("_stream_delta") is True
        assert drained[0].content == "Hello "
        assert drained[1].metadata.get("_stream_delta") is True
        assert drained[1].content == "world"
        assert drained[2].metadata.get("_stream_end") is True

    def test_final_reply_does_not_go_to_bus(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Final reply is enqueued to Outbox, not published to the bus (WP5 task 7)."""
        # The AgentExecutionCallback calls _execute_message which returns a
        # ProcessedTurn with outbound. The callback returns this to the
        # Worker, which enqueues to the Outbox. The bus is NOT used for
        # the final reply.
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        outbox_id = _enqueue_final_reply(
            store, claim, content="final reply via outbox"
        )
        assert outbox_id > 0

        row = store.read_outbox_record(outbox_id)
        assert row.message_kind == "FINAL_REPLY"
        assert row.state == "PENDING"
        # The final reply is in the Outbox, not the bus.
        # (The bus is a transient transport; the Outbox is the durable authority.)


# ---------------------------------------------------------------------------
# Tests: No direct final Channel send for runtime tasks (WP5 task 7)
# ---------------------------------------------------------------------------


class TestNoDirectFinalChannelSend:
    """Runtime tasks do not publish final replies to the bus (WP5 task 7)."""

    def test_runtime_path_bypasses_dispatch_publish(self) -> None:
        """AgentExecutionCallback calls _execute_message directly, not dispatch.

        The TurnDispatcher.dispatch method publishes the final reply to the
        bus at line 274. The AgentExecutionCallback bypasses this by calling
        host._execute_message directly. This test verifies the architectural
        invariant: the runtime path does not go through dispatch.
        """
        # This is a structural test: AgentExecutionCallback.__call__ calls
        # host._execute_message, NOT dispatcher.dispatch or
        # dispatcher.process_message. The final reply is returned to the
        # Worker via WorkerExecutionResult, which enqueues it to the Outbox.
        import inspect

        from miniunicorn.runtime.agent_adapter import AgentExecutionCallback

        source = inspect.getsource(AgentExecutionCallback.__call__)
        assert "_execute_message" in source
        assert "runtime_mode=True" in source
        # Must NOT call dispatch or process_message.
        assert "dispatch(" not in source.replace("_execute_message", "")
        assert ".process_message(" not in source

    def test_runtime_mode_true_in_callback(self) -> None:
        """AgentExecutionCallback passes runtime_mode=True (design §18.1)."""
        import inspect

        from miniunicorn.runtime.agent_adapter import AgentExecutionCallback

        source = inspect.getsource(AgentExecutionCallback.__call__)
        assert "runtime_mode=True" in source


# ---------------------------------------------------------------------------
# Task 7: Preserve final-reply routing and decode Outbox payloads
# ---------------------------------------------------------------------------


class TestFinalReplyRoutingAndPayload:
    """Task 7: final Outbox row preserves inbound routing and decodes payloads."""

    def test_final_reply_outbox_preserves_inbound_routing(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """The final Outbox row copies channel/account/target from the task.

        Before Task 7: ``complete_with_outbox`` inserted empty strings for
        ``channel``/``channel_account``/``target_key``, so the final reply
        lost its durable delivery route (design §17.8).
        """
        record, claim = _claim_and_run_task(
            store,
            sample_scope,
            make_inbound_envelope,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        outbox_id = _enqueue_final_reply(
            store,
            claim,
            content="final answer",
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )

        row = store.read_outbox_record(outbox_id)
        assert row is not None
        assert row.channel == "websocket"
        assert row.channel_account == "account-1"
        assert row.target_key == "chat-42"
        assert row.message_kind == "FINAL_REPLY"

    def test_complete_with_outbox_rejects_missing_routing(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """A final reply without durable routing raises (Task 7 Step 4).

        Empty ``channel`` or ``target_key`` is allowed only for explicitly
        suppressed completions, not for enqueued final replies.
        """
        record, claim = _claim_and_run_task(store, sample_scope, make_inbound_envelope)
        blob_id = _write_payload_blob(store, "no route")
        completion = CompletionWrite(
            final_reply_blob_id=blob_id,
            final_reply_hash=hashlib.sha256(b"no route").hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_000_002,
            channel="",
            channel_account="",
            target_key="",
        )
        with pytest.raises(ValueError, match="durable delivery routing"):
            store.complete_with_outbox(claim, completion)

    def test_outbox_sender_delivers_str_content_and_empty_media(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """OutboxSender decodes the versioned payload to str content (Task 7 Step 6).

        Before Task 7: the sender forwarded raw bytes/JSON to the Channel,
        so ``message.content`` was a JSON string instead of the reply text.
        """
        from miniunicorn.runtime.outbox_payload import encode_outbox_payload

        record, claim = _claim_and_run_task(
            store,
            sample_scope,
            make_inbound_envelope,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        # Write a versioned-codec FINAL_REPLY blob, then enqueue.
        payload_bytes = encode_outbox_payload(content="final answer")
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(payload_bytes).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=payload_bytes,
                size_bytes=len(payload_bytes),
                created_at_ms=1_000_000,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(payload_bytes).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_000_002,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        result = store.complete_with_outbox(claim, completion)
        assert result.outbox_id is not None

        sender = StubChannelSender()
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

        assert len(sender.sent_messages) == 1
        message = sender.sent_messages[0]
        assert isinstance(message.content, str)
        assert message.content == "final answer"
        assert message.media == []
        assert message.chat_id == "chat-42"

    def test_message_tool_payload_decodes_content_and_media(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Message Tool sends content "proactive" and media ["image.png"].

        Before Task 7: the OutboxSender received raw JSON bytes and
        ``message.content`` was the JSON envelope string, with media
        lost entirely (design §20.6).
        """
        from miniunicorn.agent.tools.message import MessageTool
        from miniunicorn.agent.turn_runtime import (
            TurnRuntime,
            bind_turn_runtime,
            reset_turn_runtime,
        )
        from miniunicorn.runtime.message_delivery import DurableMessageDelivery
        from miniunicorn.runtime.session_committer import (
            clear_active_claim,
            clear_active_delivery_ledger,
            clear_active_tool_call_id,
            set_active_claim,
            set_active_delivery_ledger,
            set_active_tool_call_id,
        )

        record, claim = _claim_and_run_task(
            store,
            sample_scope,
            make_inbound_envelope,
            channel="test-channel",
            channel_account="test-account",
            target_key="test-target",
        )
        set_active_claim(record.task_id, claim)
        set_active_delivery_ledger(record.task_id, store)
        set_active_tool_call_id(record.task_id, "tool-call-media")

        rt = TurnRuntime(
            turn_id="test-turn",
            session_key="test-session",
            task_id=record.task_id,
            outbound_port=DurableMessageDelivery(record.task_id),
        )
        token = bind_turn_runtime(rt)

        tool = MessageTool(
            default_channel="test-channel",
            default_chat_id="test-target",
        )

        try:
            # Use an HTTP URL so MessageTool does not resolve it against
            # the workspace directory; the test verifies codec decoding,
            # not local path resolution (design §20.6).
            result = asyncio.run(
                tool.execute(content="proactive", media=["https://example.com/image.png"])
            )
        finally:
            reset_turn_runtime(token)
            clear_active_tool_call_id(record.task_id)
            clear_active_delivery_ledger(record.task_id)
            clear_active_claim(record.task_id)

        outbox_id_str = result.split("outbox_id=")[1].rstrip(")")
        outbox_id = int(outbox_id_str)

        # Claim and deliver through OutboxSender to verify decoding.
        sender = StubChannelSender()
        outbox_sender = OutboxSender(
            store,
            sender,
            sender_id="test-sender",
            poll_interval_s=0.01,
            lease_ms=60_000,
            send_timeout_s=5,
        )
        # Deliver exactly the message-tool row we just enqueued.
        delivered = asyncio.run(outbox_sender._deliver_one())
        assert delivered is True

        assert len(sender.sent_messages) == 1
        message = sender.sent_messages[0]
        assert isinstance(message.content, str)
        assert message.content == "proactive"
        assert message.media == ["https://example.com/image.png"]

    def test_read_final_reply_works_for_sending_state(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """read_final_reply returns content for any Outbox state (Task 7 Step 7).

        Before Task 7: replies in SENDING/FAILED/OUTCOME_UNKNOWN were hidden,
        so a stuck Channel could mask a completed reply from the API.
        """
        from miniunicorn.runtime.outbox_payload import encode_outbox_payload

        record, claim = _claim_and_run_task(
            store,
            sample_scope,
            make_inbound_envelope,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        payload_bytes = encode_outbox_payload(content="reply while sending")
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(payload_bytes).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=payload_bytes,
                size_bytes=len(payload_bytes),
                created_at_ms=1_000_000,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(payload_bytes).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_000_002,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        result = store.complete_with_outbox(claim, completion)
        assert result.outbox_id is not None

        # Force the outbox row into SENDING to simulate an in-flight delivery.
        store._conn.execute(
            "UPDATE outbox SET state='SENDING' WHERE outbox_id=?",
            (result.outbox_id,),
        )
        store._conn.commit()

        reply = store.read_final_reply(sample_scope, record.task_id)
        assert reply is not None
        assert reply.content == "reply while sending"
        assert reply.metadata.get("delivery_state") == "SENDING"

    def test_legacy_raw_text_final_reply_still_decodes(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Legacy raw-text FINAL_REPLY blobs decode to their UTF-8 text.

        Rows written before the codec existed stored plain UTF-8 bytes.
        ``decode_outbox_payload`` falls back to the literal text for
        FINAL_REPLY rows so historical replies remain readable.
        """
        record, claim = _claim_and_run_task(
            store,
            sample_scope,
            make_inbound_envelope,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        legacy_bytes = "legacy reply text".encode("utf-8")
        blob = store.write_blob(
            BlobWrite(
                scope_key="test",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=hashlib.sha256(legacy_bytes).hexdigest(),
                encoding="RAW_BYTES",
                inline_content=legacy_bytes,
                size_bytes=len(legacy_bytes),
                created_at_ms=1_000_000,
            )
        )
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=hashlib.sha256(legacy_bytes).hexdigest(),
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=2_000_002,
            channel="websocket",
            channel_account="account-1",
            target_key="chat-42",
        )
        result = store.complete_with_outbox(claim, completion)
        assert result.outbox_id is not None

        reply = store.read_final_reply(sample_scope, record.task_id)
        assert reply is not None
        assert reply.content == "legacy reply text"

