"""WP1 — Worker ledger: claim, fence, renew, reclaim (design §15, §17, §24)."""

from __future__ import annotations

import pytest

from miniunicorn.agent.ports import SafeError
from miniunicorn.runtime.contracts import (
    ClaimRequest,
    ClaimedTask,
    ClaimResult,
    CompletionWrite,
    InternalCompletionWrite,
    ReclaimResult,
    StaleLeaseError,
    TaskClaim,
)
from miniunicorn.runtime.models import (
    BlobWrite,
    CheckpointWrite,
    RetryDecision,
    SessionCommitWrite,
    TaskFailure,
)
from miniunicorn.runtime.scheduler import Scheduler


# ---------------------------------------------------------------------------
# claim_next
# ---------------------------------------------------------------------------


class TestClaimNext:
    def test_claim_returns_claimed_task(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-claim-1", available_at_ms=1_000_000
        )
        submit = store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.task_id == submit.task_id
        assert result.claimed.record.state == "LEASED"
        assert result.claimed.claim.leased_by == "w-1"
        assert result.claimed.claim.lease_token
        assert result.claimed.claim.lease_epoch == 1
        assert result.claimed.claim.lease_until_ms == 2_060_000

    def test_claim_nothing_when_empty(self, store) -> None:
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is None

    def test_claim_nothing_when_not_available_yet(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """Tasks with ``available_at_ms > now_ms`` are not claimable yet."""
        env = make_inbound_envelope(
            sample_scope,
            channel_message_id="msg-future-1",
            available_at_ms=3_000_000,
        )
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is None

    def test_claim_increments_root_attempt_count(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-root-1")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.root_attempt_count == 1

    def test_claim_appends_task_leased_event(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-evt-1")
        submit = store.submit_task(env)

        store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        events = store.list_events(submit.task_id)
        types = [e.event_type for e in events]
        assert "TASK_LEASED" in types

    def test_claim_sets_session_active_task(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-active-1")
        submit = store.submit_task(env)

        store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        row = store.connection.execute(
            "SELECT active_task_id FROM session_slots WHERE session_key=?",
            (env.session_key,),
        ).fetchone()
        assert row["active_task_id"] == submit.task_id


# ---------------------------------------------------------------------------
# Session head claim ordering (design §15.2)
# ---------------------------------------------------------------------------


class TestSessionHeadClaimOrdering:
    def test_earlier_sequence_claimed_first(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """When multiple tasks are queued in the same session, the earliest
        non-terminal sequence must be claimed first (design §15.2)."""
        env1 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-order-1"
        )
        env2 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-order-2"
        )
        env3 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-order-3"
        )
        r1 = store.submit_task(env1)
        r2 = store.submit_task(env2)
        r3 = store.submit_task(env3)

        # Claim should return the first task (sequence 0)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.task_id == r1.task_id

    def test_later_sequence_blocked_by_earlier_non_terminal(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """A later task cannot be claimed while an earlier task is non-terminal
        (design §15.2)."""
        env1 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-block-1"
        )
        env2 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-block-2"
        )
        store.submit_task(env1)
        r2 = store.submit_task(env2)

        # Claim the first task (now LEASED)
        result1 = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result1.claimed is not None

        # Second task cannot be claimed (first is LEASED, not terminal)
        result2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result2.claimed is None

    def test_later_sequence_claimable_after_earlier_completes(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env1 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-comp-1"
        )
        env2 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-comp-2"
        )
        r1 = store.submit_task(env1)
        r2 = store.submit_task(env2)

        # Claim and complete the first task
        claim1 = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert claim1.claimed is not None
        store.mark_running(claim1.claimed.claim, now_ms=2_000_001)
        store.complete_with_outbox(
            claim1.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=None,
                final_reply_hash=None,
                final_reply_dedup_key=None,
                suppress_final=True,
                completed_at_ms=2_000_002,
            ),
        )

        # Now the second task can be claimed
        claim2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_000_003, lease_ms=60_000)
        )
        assert claim2.claimed is not None
        assert claim2.claimed.record.task_id == r2.task_id


# ---------------------------------------------------------------------------
# Priority across different sessions (design §15.2)
# ---------------------------------------------------------------------------


class TestPriorityAcrossSessions:
    def test_higher_priority_claimed_first(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """When tasks are in different sessions, higher priority wins
        (design §15.2)."""
        env_low = make_inbound_envelope(
            sample_scope,
            session_key="session-low",
            channel_message_id="msg-prio-low",
            priority=50,
        )
        env_high = make_inbound_envelope(
            sample_scope,
            session_key="session-high",
            channel_message_id="msg-prio-high",
            priority=200,
        )
        r_low = store.submit_task(env_low)
        r_high = store.submit_task(env_high)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.task_id == r_high.task_id

    def test_recovery_pending_claimed_before_normal(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """``recovery_pending=1`` tasks are claimed before normal tasks
        regardless of priority (design §15.2)."""
        env_normal = make_inbound_envelope(
            sample_scope,
            session_key="session-normal",
            channel_message_id="msg-normal",
            priority=200,
        )
        env_recovery = make_inbound_envelope(
            sample_scope,
            session_key="session-recovery",
            channel_message_id="msg-recovery",
            priority=50,
        )
        r_normal = store.submit_task(env_normal)
        r_recovery = store.submit_task(env_recovery)

        # Manually set recovery_pending on the lower-priority task
        store.connection.execute(
            "UPDATE tasks SET recovery_pending=1 WHERE task_id=?",
            (r_recovery.task_id,),
        )

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.task_id == r_recovery.task_id


# ---------------------------------------------------------------------------
# Active session slot prevents concurrent claim (design §15.2)
# ---------------------------------------------------------------------------


class TestActiveSessionSlot:
    def test_second_worker_cannot_claim_same_session(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env1 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-slot-1"
        )
        env2 = make_inbound_envelope(
            sample_scope, channel_message_id="msg-slot-2"
        )
        store.submit_task(env1)
        store.submit_task(env2)

        # First worker claims the head task
        result1 = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result1.claimed is not None

        # Second worker cannot claim the next task (active slot is held)
        result2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result2.claimed is None


# ---------------------------------------------------------------------------
# mark_running (design §17.3)
# ---------------------------------------------------------------------------


class TestMarkRunning:
    def test_mark_running_transitions_to_running(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-run-1")
        store.submit_task(env)

        claim = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert claim.claimed is not None

        record = store.mark_running(claim.claimed.claim, now_ms=2_000_001)
        assert record.state == "RUNNING"

    def test_mark_running_appends_event(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-run-2")
        submit = store.submit_task(env)

        claim = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(claim.claimed.claim, now_ms=2_000_001)

        events = store.list_events(submit.task_id)
        types = [e.event_type for e in events]
        assert "TASK_RUNNING" in types


# ---------------------------------------------------------------------------
# Fencing: stale token and epoch rejection (design §6.10, §6.11)
# ---------------------------------------------------------------------------


class TestFencing:
    def test_stale_token_rejected_on_mark_running(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-fence-1")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None

        stale_claim = TaskClaim(
            task_id=result.claimed.claim.task_id,
            lease_token="wrong-token",
            lease_epoch=result.claimed.claim.lease_epoch,
            leased_by="w-1",
            lease_until_ms=result.claimed.claim.lease_until_ms,
        )
        with pytest.raises(StaleLeaseError):
            store.mark_running(stale_claim, now_ms=2_000_001)

    def test_stale_epoch_rejected_on_checkpoint(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-fence-2")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        stale_claim = TaskClaim(
            task_id=result.claimed.claim.task_id,
            lease_token=result.claimed.claim.lease_token,
            lease_epoch=999,  # wrong epoch
            leased_by="w-1",
            lease_until_ms=result.claimed.claim.lease_until_ms,
        )
        with pytest.raises(StaleLeaseError):
            store.checkpoint(
                stale_claim,
                CheckpointWrite(
                    phase="CONTEXT_BUILT",
                    run_segment=0,
                    ordinal=1,
                    payload_blob_id="blob-1",
                    payload_hash="hash-1",
                    lease_epoch=999,
                    created_at_ms=2_000_002,
                ),
            )

    def test_stale_token_rejected_on_complete(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-fence-3")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        stale_claim = TaskClaim(
            task_id=result.claimed.claim.task_id,
            lease_token="wrong-token",
            lease_epoch=result.claimed.claim.lease_epoch,
            leased_by="w-1",
            lease_until_ms=result.claimed.claim.lease_until_ms,
        )
        completion = store.complete_with_outbox(
            stale_claim,
            CompletionWrite(
                final_reply_blob_id=None,
                final_reply_hash=None,
                final_reply_dedup_key=None,
                suppress_final=True,
                completed_at_ms=2_000_002,
            ),
        )
        assert completion.status == "STALE_LEASE"

    def test_lease_epoch_increments_on_reclaim(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """After reclaim, a new claim must have a higher lease_epoch
        (design §6.11)."""
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-epoch-1")
        store.submit_task(env)

        result1 = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result1.claimed.claim.lease_epoch == 1

        # Lease expires
        reclaim = store.reclaim_expired(now_ms=3_000_000, limit=10)
        assert reclaim.reclaimed_count == 1

        # New claim must have epoch 2
        result2 = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=3_000_001, lease_ms=60_000)
        )
        assert result2.claimed is not None
        assert result2.claimed.claim.lease_epoch == 2
        assert result2.claimed.claim.lease_token != result1.claimed.claim.lease_token


# ---------------------------------------------------------------------------
# Renew lease without state-version change (design §6.11, §14.4)
# ---------------------------------------------------------------------------


class TestRenewLease:
    def test_renew_extends_lease_until(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-renew-1")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        original = store.read_task(result.claimed.claim.task_id)
        assert original is not None

        renewed = store.renew_lease(
            result.claimed.claim, lease_until_ms=2_200_000, now_ms=2_010_000
        )
        assert renewed is True

        after = store.read_task(result.claimed.claim.task_id)
        assert after is not None
        assert after.lease_until_ms == 2_200_000

    def test_renew_does_not_change_state_version(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """Lease renewal must not increment ``state_version`` (design §6.11, §14.4)."""
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-renew-2")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        before = store.read_task(result.claimed.claim.task_id)
        assert before is not None

        store.renew_lease(result.claimed.claim, lease_until_ms=2_200_000, now_ms=2_010_000)

        after = store.read_task(result.claimed.claim.task_id)
        assert after is not None
        assert after.state_version == before.state_version

    def test_renew_with_stale_token_returns_false(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-renew-3")
        store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )

        stale_claim = TaskClaim(
            task_id=result.claimed.claim.task_id,
            lease_token="wrong-token",
            lease_epoch=result.claimed.claim.lease_epoch,
            leased_by="w-1",
            lease_until_ms=result.claimed.claim.lease_until_ms,
        )
        assert store.renew_lease(stale_claim, lease_until_ms=2_200_000, now_ms=2_010_000) is False


# ---------------------------------------------------------------------------
# Heartbeat without state-version change (design §24.1)
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_updates_timestamp(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-hb-1")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )

        ok = store.heartbeat(result.claimed.claim, now_ms=2_010_000)
        assert ok is True

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.last_heartbeat_at_ms == 2_010_000

    def test_heartbeat_does_not_change_state_version(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-hb-2")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        before = store.read_task(result.claimed.claim.task_id)
        assert before is not None

        store.heartbeat(result.claimed.claim, now_ms=2_010_000)

        after = store.read_task(result.claimed.claim.task_id)
        assert after is not None
        assert after.state_version == before.state_version

    def test_heartbeat_stale_token_returns_false(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-hb-3")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )

        stale_claim = TaskClaim(
            task_id=result.claimed.claim.task_id,
            lease_token="wrong",
            lease_epoch=result.claimed.claim.lease_epoch,
            leased_by="w-1",
            lease_until_ms=result.claimed.claim.lease_until_ms,
        )
        assert store.heartbeat(stale_claim, now_ms=2_010_000) is False


# ---------------------------------------------------------------------------
# Checkpoint (design §17.4)
# ---------------------------------------------------------------------------


class TestCheckpoint:
    def test_checkpoint_saves_and_updates_phase(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-ckpt-1")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        # Write a blob for the checkpoint payload
        blob = store.write_blob(
            BlobWrite(
                scope_key="test/ckpt",
                blob_kind="CHECKPOINT",
                content_hash="ckpt-hash-1",
                encoding="RAW_JSON",
                inline_content=b'{"phase":"CONTEXT_BUILT"}',
            )
        )

        cp_id = store.checkpoint(
            result.claimed.claim,
            CheckpointWrite(
                phase="CONTEXT_BUILT",
                run_segment=0,
                ordinal=1,
                payload_blob_id=blob.blob_id,
                payload_hash="ckpt-hash-1",
                lease_epoch=result.claimed.claim.lease_epoch,
                created_at_ms=2_000_002,
            ),
        )
        assert cp_id

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.checkpoint_phase == "CONTEXT_BUILT"

    def test_checkpoint_appends_event(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-ckpt-2")
        submit = store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        blob = store.write_blob(
            BlobWrite(
                scope_key="test/ckpt",
                blob_kind="CHECKPOINT",
                content_hash="ckpt-hash-2",
                encoding="RAW_JSON",
                inline_content=b"{}",
            )
        )
        store.checkpoint(
            result.claimed.claim,
            CheckpointWrite(
                phase="COMMAND_DONE",
                run_segment=0,
                ordinal=1,
                payload_blob_id=blob.blob_id,
                payload_hash="ckpt-hash-2",
                lease_epoch=result.claimed.claim.lease_epoch,
                created_at_ms=2_000_002,
            ),
        )

        events = store.list_events(submit.task_id)
        types = [e.event_type for e in events]
        assert "CHECKPOINT_SAVED" in types


# ---------------------------------------------------------------------------
# enter_retry_wait and promote_due_retries (design §8.2, §17.6)
# ---------------------------------------------------------------------------


class TestRetryWait:
    def test_enter_retry_wait_transitions_state(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-retry-1")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        store.enter_retry_wait(
            result.claimed.claim,
            RetryDecision(
                kind="TRANSIENT",
                available_at_ms=3_000_000,
                error=SafeError(
                    error_code="PROVIDER_5XX",
                    error_summary="provider returned 503",
                ),
                increment_root_attempt=False,
            ),
        )

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "RETRY_WAIT"
        assert task.available_at_ms == 3_000_000
        assert task.wait_until_ms == 3_000_000

    def test_promote_due_retries_returns_to_queued(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-retry-2")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        store.enter_retry_wait(
            result.claimed.claim,
            RetryDecision(
                kind="TRANSIENT",
                available_at_ms=3_000_000,
                error=SafeError(error_code="PROVIDER_5XX", error_summary="503"),
            ),
        )

        # Before the wait_until_ms: not promoted
        count = store.promote_due_retries(now_ms=2_500_000, limit=10)
        assert count == 0

        # After the wait_until_ms: promoted
        count = store.promote_due_retries(now_ms=3_000_001, limit=10)
        assert count == 1

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "QUEUED"

    def test_retry_wait_releases_session_slot(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-retry-3")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        store.enter_retry_wait(
            result.claimed.claim,
            RetryDecision(
                kind="TRANSIENT",
                available_at_ms=3_000_000,
                error=SafeError(error_code="PROVIDER_5XX", error_summary="503"),
            ),
        )

        row = store.connection.execute(
            "SELECT active_task_id FROM session_slots WHERE session_key=?",
            (env.session_key,),
        ).fetchone()
        assert row["active_task_id"] is None


# ---------------------------------------------------------------------------
# Lease reclaim (design §24.2)
# ---------------------------------------------------------------------------


class TestLeaseReclaim:
    def test_reclaim_expired_lease(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-reclaim-1")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed.claim.lease_until_ms == 2_060_000

        # Before expiry: nothing to reclaim
        reclaim = store.reclaim_expired(now_ms=2_060_000, limit=10)
        assert reclaim.reclaimed_count == 0

        # After expiry: reclaimed (no root_attempt_count increment — Task 2 Step 6)
        reclaim = store.reclaim_expired(now_ms=2_060_001, limit=10)
        assert reclaim.reclaimed_count == 1

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "QUEUED"
        assert task.recovery_pending == 1
        assert task.root_attempt_count == 1  # not incremented by reclaim

    def test_reclaim_does_not_fail_exhausted_until_next_claim(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """With ``max_root_attempts=1``: reclaim returns the task to QUEUED;
        the terminal failure happens at the next recovery claim (Task 2 Step 6)."""
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-reclaim-2")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(
                worker_id="w-1",
                now_ms=2_000_000,
                lease_ms=60_000,
                max_root_attempts=1,
            )
        )
        assert result.claimed is not None

        # After expiry: reclaim returns to QUEUED (no terminal failure here).
        reclaim = store.reclaim_expired(now_ms=2_060_001, limit=10)
        assert reclaim.reclaimed_count == 1
        assert reclaim.failed_count == 0

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "QUEUED"
        assert task.recovery_pending == 1

        # The next recovery claim increments root_attempt_count beyond the
        # limit and fails terminally.
        claim_result = store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=2_060_002, lease_ms=60_000)
        )
        assert claim_result.claimed is None

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "FAILED"
        assert task.error_code == "TASK_ATTEMPTS_EXHAUSTED"

    def test_reclaim_releases_session_slot(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-reclaim-3")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )

        store.reclaim_expired(now_ms=2_060_001, limit=10)

        row = store.connection.execute(
            "SELECT active_task_id FROM session_slots WHERE session_key=?",
            (env.session_key,),
        ).fetchone()
        assert row["active_task_id"] is None

    def test_reclaim_appends_event(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-reclaim-4")
        submit = store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )

        store.reclaim_expired(now_ms=2_060_001, limit=10)

        events = store.list_events(submit.task_id)
        types = [e.event_type for e in events]
        assert "LEASE_RECLAIMED" in types


# ---------------------------------------------------------------------------
# Completion (design §17.8, §17.10)
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_complete_with_outbox_enqueues_reply(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-done-1")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        # Write a blob for the reply payload
        blob = store.write_blob(
            BlobWrite(
                scope_key="test/reply",
                blob_kind="OUTBOX_PAYLOAD",
                content_hash="reply-hash-1",
                encoding="RAW_JSON",
                inline_content=b'{"text":"done"}',
            )
        )

        completion = store.complete_with_outbox(
            result.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=blob.blob_id,
                final_reply_hash="reply-hash-1",
                final_reply_dedup_key=None,
                suppress_final=False,
                completed_at_ms=2_000_002,
                cumulative_input_tokens=100,
                cumulative_output_tokens=50,
                channel="test-channel",
                channel_account="test-account",
                target_key="test-target",
            ),
        )
        assert completion.status == "COMPLETED"
        assert completion.outbox_id is not None

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "COMPLETED"
        assert task.completed_at_ms == 2_000_002
        assert task.cumulative_input_tokens == 100
        assert task.cumulative_output_tokens == 50

    def test_complete_with_suppress_final(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-done-2")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        completion = store.complete_with_outbox(
            result.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=None,
                final_reply_hash=None,
                final_reply_dedup_key=None,
                suppress_final=True,
                completed_at_ms=2_000_002,
            ),
        )
        assert completion.status == "COMPLETED"
        assert completion.outbox_id is None

    def test_complete_releases_session_slot(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-done-3")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        store.complete_with_outbox(
            result.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=None,
                final_reply_hash=None,
                final_reply_dedup_key=None,
                suppress_final=True,
                completed_at_ms=2_000_002,
            ),
        )

        row = store.connection.execute(
            "SELECT active_task_id FROM session_slots WHERE session_key=?",
            (env.session_key,),
        ).fetchone()
        assert row["active_task_id"] is None

    def test_complete_internal(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-done-4")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        completion = store.complete_internal(
            result.claimed.claim,
            InternalCompletionWrite(
                result_ref="inline:result",
                completed_at_ms=2_000_002,
            ),
        )
        assert completion.status == "COMPLETED"
        assert completion.outbox_id is None


# ---------------------------------------------------------------------------
# fail_task and cancel_task (design §17.10)
# ---------------------------------------------------------------------------


class TestFailAndCancel:
    def test_fail_task_transitions_to_failed(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-fail-1")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        store.fail_task(
            result.claimed.claim,
            TaskFailure(
                error=SafeError(
                    error_code="AGENT_ERROR",
                    error_summary="agent failed",
                ),
                failed_at_ms=2_000_002,
            ),
        )

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "FAILED"
        assert task.error_code == "AGENT_ERROR"

    def test_cancel_task_transitions_to_cancelled(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-cancel-2")
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        store.cancel_task(
            result.claimed.claim,
            reason=SafeError(error_code="CANCELLED", error_summary="user cancelled"),
        )

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "CANCELLED"


# ---------------------------------------------------------------------------
# Task 2: heartbeat renews lease and expired owners are fenced
# ---------------------------------------------------------------------------


class TestHeartbeatRenewsLease:
    """A successful heartbeat atomically renews ``lease_until_ms`` and
    ``last_heartbeat_at_ms`` (Task 2 Step 1)."""

    def test_heartbeat_extends_lease_until_via_scheduler(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-hb-ext-1", available_at_ms=0
        )
        submit = store.submit_task(env)

        scheduler = Scheduler(store, lease_ms=3_000)
        outcome = scheduler.claim_next("w-1", now_ms=1_000)
        assert outcome.claimed is not None
        claim = outcome.claimed.claim

        # Original deadline: 1_000 + 3_000 = 4_000.
        assert claim.lease_until_ms == 4_000

        # Heartbeat at 2_500 renews the lease to 2_500 + 3_000 = 5_500.
        assert scheduler.heartbeat(claim, now_ms=2_500) is True

        task = store.read_task(submit.task_id)
        assert task is not None
        assert task.lease_until_ms == 5_500

        # At 4_001 the original lease would have expired, but the renewed
        # one is still valid.
        result = scheduler.reclaim_expired(now_ms=4_001)
        assert result.reclaimed_count == 0

    def test_store_heartbeat_also_renews_lease(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """``WorkerLedger.heartbeat`` must not be a timestamp-only path."""
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-hb-ext-2", available_at_ms=0
        )
        submit = store.submit_task(env)

        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000, lease_ms=3_000)
        )
        assert result.claimed is not None
        before = store.read_task(submit.task_id)
        assert before is not None
        assert before.lease_until_ms == 4_000

        ok = store.heartbeat(result.claimed.claim, now_ms=2_500)
        assert ok is True

        after = store.read_task(submit.task_id)
        assert after is not None
        # Lease was renewed (3_000 ms lease duration preserved).
        assert after.lease_until_ms == 5_500
        assert after.last_heartbeat_at_ms == 2_500


class TestExpiredOwnerFenced:
    """Mutations after ``lease_until_ms`` without a heartbeat must raise
    ``StaleLeaseError`` (Task 2 Step 2)."""

    def test_expired_owner_checkpoint_rejected(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-fence-exp-1", available_at_ms=0
        )
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000, lease_ms=3_000)
        )
        store.mark_running(result.claimed.claim, now_ms=1_001)

        blob = store.write_blob(
            BlobWrite(
                scope_key="test/ckpt",
                blob_kind="CHECKPOINT",
                content_hash="ckpt-hash",
                encoding="RAW_JSON",
                inline_content=b"{}",
            )
        )

        # Advance the clock past the lease deadline without heartbeat.
        with pytest.raises(StaleLeaseError):
            store.checkpoint(
                result.claimed.claim,
                CheckpointWrite(
                    phase="CONTEXT_BUILT",
                    run_segment=0,
                    ordinal=1,
                    payload_blob_id=blob.blob_id,
                    payload_hash="ckpt-hash",
                    lease_epoch=result.claimed.claim.lease_epoch,
                    created_at_ms=5_000,
                ),
            )

    def test_expired_owner_record_progress_rejected(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-fence-exp-2", available_at_ms=0
        )
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000, lease_ms=3_000)
        )
        store.mark_running(result.claimed.claim, now_ms=1_001)

        with pytest.raises(StaleLeaseError):
            store.record_progress(
                result.claimed.claim, {"phase": "x"}, now_ms=5_000
            )

    def test_expired_owner_prepare_session_commit_rejected(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-fence-exp-3", available_at_ms=0
        )
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000, lease_ms=3_000)
        )
        store.mark_running(result.claimed.claim, now_ms=1_001)

        blob = store.write_blob(
            BlobWrite(
                scope_key="test/session",
                blob_kind="SESSION_COMMIT",
                content_hash="hash",
                encoding="RAW_JSON",
                inline_content=b"{}",
            )
        )

        with pytest.raises(StaleLeaseError):
            store.prepare_session_commit(
                result.claimed.claim,
                SessionCommitWrite(
                    session_key=env.session_key,
                    commit_kind="INBOUND",
                    base_revision=0,
                    target_revision=1,
                    content_hash="hash",
                    payload_blob_id=blob.blob_id,
                    created_at_ms=5_000,
                ),
            )

    def test_expired_owner_complete_with_outbox_rejected(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-fence-exp-4", available_at_ms=0
        )
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=1_000, lease_ms=3_000)
        )
        store.mark_running(result.claimed.claim, now_ms=1_001)

        completion = store.complete_with_outbox(
            result.claimed.claim,
            CompletionWrite(
                final_reply_blob_id=None,
                final_reply_hash=None,
                final_reply_dedup_key=None,
                suppress_final=True,
                completed_at_ms=5_000,
            ),
        )
        assert completion.status == "STALE_LEASE"


class TestRootAttemptCharging:
    """``reclaim_expired`` must not increment ``root_attempt_count``;
    only the recovery claim increments (Task 2 Step 6)."""

    def test_reclaim_does_not_increment_root_attempt(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-root-no-incr"
        )
        store.submit_task(env)
        result = store.claim_next(
            ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000)
        )
        assert result.claimed is not None
        assert result.claimed.record.root_attempt_count == 1

        reclaim = store.reclaim_expired(now_ms=2_060_001, limit=10)
        assert reclaim.reclaimed_count == 1

        task = store.read_task(result.claimed.claim.task_id)
        assert task is not None
        assert task.state == "QUEUED"
        assert task.recovery_pending == 1
        # Reclaim must NOT increment; still 1.
        assert task.root_attempt_count == 1

    def test_initial_plus_two_recovery_claims_then_fourth_fails(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """With ``max_root_attempts=3``: initial claim + 2 recovery claims
        are allowed; the 4th charged claim fails terminally."""
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-root-3", available_at_ms=0
        )
        store.submit_task(env)

        scheduler = Scheduler(store, lease_ms=1_000, max_root_attempts=3)

        # Claim 1 (initial): root_attempt_count 0 -> 1.
        c1 = scheduler.claim_next("w-1", now_ms=1_000)
        assert c1.claimed is not None
        assert c1.claimed.record.root_attempt_count == 1

        # Lease expires; reclaim sets recovery_pending=1 without incrementing.
        reclaim = scheduler.reclaim_expired(now_ms=2_001)
        assert reclaim.reclaimed_count == 1

        # Claim 2 (recovery): root_attempt_count 1 -> 2.
        c2 = scheduler.claim_next("w-2", now_ms=3_000)
        assert c2.claimed is not None
        assert c2.claimed.record.root_attempt_count == 2

        # Lease expires; reclaim.
        scheduler.reclaim_expired(now_ms=4_001)

        # Claim 3 (recovery): root_attempt_count 2 -> 3.
        c3 = scheduler.claim_next("w-3", now_ms=5_000)
        assert c3.claimed is not None
        assert c3.claimed.record.root_attempt_count == 3

        # Lease expires; reclaim.
        scheduler.reclaim_expired(now_ms=6_001)

        # Claim 4 (recovery): root_attempt_count 3 -> 4 > 3, fails terminally.
        c4 = scheduler.claim_next("w-4", now_ms=7_000)
        assert c4.claimed is None

        task = store.read_task(c1.claimed.claim.task_id)
        assert task is not None
        assert task.state == "FAILED"
        assert task.error_code == "TASK_ATTEMPTS_EXHAUSTED"
