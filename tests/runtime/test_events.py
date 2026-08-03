"""WP1 — Event immutability trigger and retention guard (design §16.6)."""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Event append
# ---------------------------------------------------------------------------


class TestEventAppend:
    def test_events_ordered_by_sequence(self, store, sample_scope, make_inbound_envelope) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-evt-1")
        submit = store.submit_task(env)

        # Claim adds TASK_LEASED; mark_running adds TASK_RUNNING
        from miniunicorn.runtime.contracts import ClaimRequest

        result = store.claim_next(ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000))
        store.mark_running(result.claimed.claim, now_ms=2_000_001)

        events = store.list_events(submit.task_id)
        assert len(events) >= 2
        seqs = [e.event_seq for e in events]
        assert seqs == sorted(seqs)

    def test_list_events_after_seq(self, store, sample_scope, make_inbound_envelope) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-evt-2")
        submit = store.submit_task(env)

        from miniunicorn.runtime.contracts import ClaimRequest

        store.claim_next(ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000))

        all_events = store.list_events(submit.task_id)
        assert len(all_events) >= 2

        # Get events after the first
        after_first = store.list_events(submit.task_id, after_seq=all_events[0].event_seq)
        assert all(e.event_seq > all_events[0].event_seq for e in after_first)


# ---------------------------------------------------------------------------
# Event immutability trigger (design §16.6)
# ---------------------------------------------------------------------------


class TestEventImmutability:
    def test_update_on_task_events_rejected(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """The ``trg_task_events_no_update`` trigger must reject UPDATE on
        ``task_events`` (design §16.6)."""
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-imm-1")
        submit = store.submit_task(env)

        events = store.list_events(submit.task_id)
        assert len(events) == 1
        event_id = events[0].event_id

        with pytest.raises(Exception, match="immutable"):
            store.connection.execute(
                "UPDATE task_events SET event_type='TAMPERED' WHERE event_id=?",
                (event_id,),
            )

    def test_delete_on_task_events_blocked_by_retention_guard(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """Deleting a task that has events must fail due to the
        ``ON DELETE RESTRICT`` foreign key (design §16.6, §16.4)."""
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-imm-2")
        submit = store.submit_task(env)

        # task_events has ON DELETE RESTRICT on task_id
        with pytest.raises(Exception):
            store.connection.execute("DELETE FROM tasks WHERE task_id=?", (submit.task_id,))

    def test_event_safe_payload_is_json_or_null(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """Event ``safe_payload_json`` is either NULL or valid JSON (design §16.6)."""
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-imm-3")
        submit = store.submit_task(env)

        from miniunicorn.runtime.contracts import ClaimRequest

        store.claim_next(ClaimRequest(worker_id="w-1", now_ms=2_000_000, lease_ms=60_000))

        events = store.list_events(submit.task_id)
        for event in events:
            if event.safe_payload_json is not None:
                import json

                parsed = json.loads(event.safe_payload_json)
                assert isinstance(parsed, dict)
