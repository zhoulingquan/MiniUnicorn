"""WP1 — Task ingress: submit, dedup, control, session sequence (design §17.1, §17.12)."""

from __future__ import annotations

import pytest

from miniunicorn.runtime.models import (
    RequestScope,
    TaskControlRequest,
)


# ---------------------------------------------------------------------------
# submit_task
# ---------------------------------------------------------------------------


class TestSubmitTask:
    def test_accepted_returns_task_id_and_sequence(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-1")
        result = store.submit_task(env)
        assert result.status == "ACCEPTED"
        assert result.task_id
        assert result.session_sequence == 0

    def test_task_is_queued_after_submit(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-1")
        result = store.submit_task(env)
        task = store.read_task(result.task_id)
        assert task is not None
        assert task.state == "QUEUED"
        assert task.task_kind == "USER_TURN"
        assert task.checkpoint_phase == "ACCEPTED"
        assert task.run_segment == 0
        assert task.root_attempt_count == 0
        assert task.recovery_pending == 0

    def test_task_accepted_event_appended(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-1")
        result = store.submit_task(env)
        events = store.list_events(result.task_id)
        assert len(events) == 1
        assert events[0].event_type == "TASK_ACCEPTED"
        assert events[0].phase == "ACCEPTED"

    def test_payload_blob_created(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-1")
        result = store.submit_task(env)
        task = store.read_task(result.task_id)
        assert task is not None
        blob = store.read_blob(task.payload_blob_id)
        assert blob is not None
        assert blob.blob_kind == "TASK_PAYLOAD"
        assert blob.content_hash == env.payload_hash

    def test_turn_id_stored(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-1", turn_id="turn-abc"
        )
        result = store.submit_task(env)
        task = store.read_task(result.task_id)
        assert task is not None
        assert task.turn_id == "turn-abc"


# ---------------------------------------------------------------------------
# Duplicate inbound races (design §13.1)
# ---------------------------------------------------------------------------


class TestDuplicateInbound:
    def test_duplicate_channel_message_id(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env1 = make_inbound_envelope(sample_scope, channel_message_id="msg-dup-1")
        r1 = store.submit_task(env1)
        assert r1.status == "ACCEPTED"

        env2 = make_inbound_envelope(sample_scope, channel_message_id="msg-dup-1")
        r2 = store.submit_task(env2)
        assert r2.status == "DUPLICATE"
        assert r2.task_id == r1.task_id
        assert r2.session_sequence == r1.session_sequence

    def test_duplicate_dedup_key(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env1 = make_inbound_envelope(
            sample_scope,
            channel_message_id="msg-dedup-1",
            dedup_key="dedup-key-1",
        )
        r1 = store.submit_task(env1)
        assert r1.status == "ACCEPTED"

        env2 = make_inbound_envelope(
            sample_scope,
            channel_message_id="msg-dedup-2",  # different channel message
            dedup_key="dedup-key-1",  # same dedup key
        )
        r2 = store.submit_task(env2)
        assert r2.status == "DUPLICATE"
        assert r2.task_id == r1.task_id

    def test_different_channel_message_not_duplicate(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env1 = make_inbound_envelope(sample_scope, channel_message_id="msg-uniq-1")
        r1 = store.submit_task(env1)
        env2 = make_inbound_envelope(sample_scope, channel_message_id="msg-uniq-2")
        r2 = store.submit_task(env2)
        assert r2.status == "ACCEPTED"
        assert r2.task_id != r1.task_id

    def test_dedup_does_not_consume_session_sequence(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """A duplicate submission must not advance the session sequence
        (design §15.1)."""
        env1 = make_inbound_envelope(sample_scope, channel_message_id="msg-seq-1")
        r1 = store.submit_task(env1)
        assert r1.session_sequence == 0

        env2 = make_inbound_envelope(sample_scope, channel_message_id="msg-seq-1")
        r2 = store.submit_task(env2)
        assert r2.status == "DUPLICATE"

        env3 = make_inbound_envelope(sample_scope, channel_message_id="msg-seq-3")
        r3 = store.submit_task(env3)
        assert r3.status == "ACCEPTED"
        assert r3.session_sequence == 1  # not 2


# ---------------------------------------------------------------------------
# Session sequence allocation (design §15.1)
# ---------------------------------------------------------------------------


class TestSessionSequenceAllocation:
    def test_sequence_increments_per_session(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        for i in range(3):
            env = make_inbound_envelope(
                sample_scope, channel_message_id=f"msg-seq-{i}"
            )
            result = store.submit_task(env)
            assert result.session_sequence == i

    def test_sequence_independent_per_session(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env_a1 = make_inbound_envelope(
            sample_scope, session_key="session-a", channel_message_id="msg-a-1"
        )
        r_a1 = store.submit_task(env_a1)
        assert r_a1.session_sequence == 0

        env_b1 = make_inbound_envelope(
            sample_scope, session_key="session-b", channel_message_id="msg-b-1"
        )
        r_b1 = store.submit_task(env_b1)
        assert r_b1.session_sequence == 0

        env_a2 = make_inbound_envelope(
            sample_scope, session_key="session-a", channel_message_id="msg-a-2"
        )
        r_a2 = store.submit_task(env_a2)
        assert r_a2.session_sequence == 1

    def test_unique_constraint_on_session_sequence(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        """Two tasks with the same session_key + session_sequence must fail
        (UNIQUE constraint in schema, design §16.4)."""
        env1 = make_inbound_envelope(sample_scope, channel_message_id="msg-uniq-1")
        r1 = store.submit_task(env1)
        assert r1.session_sequence == 0

        # Manually insert a task with the same session_key + session_sequence
        with pytest.raises(Exception):
            store.connection.execute(
                "INSERT INTO tasks (task_id, protocol_version, tenant_id, "
                "principal_id, agent_id, workspace_id, session_key, "
                "session_sequence, task_kind, priority, payload_blob_id, "
                "payload_hash, state, checkpoint_phase, max_root_attempts, "
                "available_at_ms, created_at_ms, updated_at_ms) "
                "VALUES ('dup-seq', 1, 't', 'p', 'a', 'w', ?, 0, "
                "'USER_TURN', 100, ?, 'h', 'QUEUED', 'ACCEPTED', 3, 0, 0, 0)",
                (env1.session_key, r1.task_id),
            )


# ---------------------------------------------------------------------------
# submit_internal
# ---------------------------------------------------------------------------


class TestSubmitInternal:
    def test_internal_task_accepted(
        self, store, sample_scope, make_internal_envelope
    ) -> None:
        env = make_internal_envelope(sample_scope)
        result = store.submit_internal(env)
        assert result.status == "ACCEPTED"
        task = store.read_task(result.task_id)
        assert task is not None
        assert task.task_kind == "MAINTENANCE"
        assert task.state == "QUEUED"

    def test_internal_dedup(
        self, store, sample_scope, make_internal_envelope
    ) -> None:
        env1 = make_internal_envelope(sample_scope, dedup_key="internal-dup-1")
        r1 = store.submit_internal(env1)
        assert r1.status == "ACCEPTED"

        env2 = make_internal_envelope(sample_scope, dedup_key="internal-dup-1")
        r2 = store.submit_internal(env2)
        assert r2.status == "DUPLICATE"
        assert r2.task_id == r1.task_id


# ---------------------------------------------------------------------------
# append_control (design §17.12)
# ---------------------------------------------------------------------------


class TestAppendControl:
    def test_append_control_to_queued_task(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-ctrl-1")
        submit = store.submit_task(env)

        ctrl = TaskControlRequest(
            task_id=submit.task_id,
            kind="STEER",
            dedup_key="ctrl-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_001,
        )
        result = store.append_control(ctrl)
        assert result.status == "APPENDED"
        assert result.control_id is not None

    def test_duplicate_control(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-ctrl-2")
        submit = store.submit_task(env)

        ctrl = TaskControlRequest(
            task_id=submit.task_id,
            kind="STEER",
            dedup_key="ctrl-dup-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_001,
        )
        r1 = store.append_control(ctrl)
        assert r1.status == "APPENDED"

        r2 = store.append_control(ctrl)
        assert r2.status == "DUPLICATE"
        assert r2.control_id == r1.control_id

    def test_control_on_unknown_task(self, store) -> None:
        ctrl = TaskControlRequest(
            task_id="nonexistent",
            kind="STEER",
            dedup_key="ctrl-unknown",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_001,
        )
        result = store.append_control(ctrl)
        assert result.status == "TASK_NOT_FOUND"

    def test_control_on_terminal_task_rejected(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-ctrl-3")
        submit = store.submit_task(env)

        # Cancel the task first (QUEUED -> CANCELLED)
        cancel = TaskControlRequest(
            task_id=submit.task_id,
            kind="CANCEL",
            dedup_key="cancel-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_002,
        )
        store.append_control(cancel)

        # Try to append STEER to a CANCELLED task
        steer = TaskControlRequest(
            task_id=submit.task_id,
            kind="STEER",
            dedup_key="steer-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_003,
        )
        result = store.append_control(steer)
        assert result.status == "TASK_TERMINAL"

    def test_cancel_queued_transitions_to_cancelled(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-cancel-1")
        submit = store.submit_task(env)

        cancel = TaskControlRequest(
            task_id=submit.task_id,
            kind="CANCEL",
            dedup_key="cancel-queued-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_002,
        )
        store.append_control(cancel)

        task = store.read_task(submit.task_id)
        assert task is not None
        assert task.state == "CANCELLED"
        assert task.completed_at_ms is not None

    def test_control_received_event_appended(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-ctrl-evt")
        submit = store.submit_task(env)

        ctrl = TaskControlRequest(
            task_id=submit.task_id,
            kind="STEER",
            dedup_key="ctrl-evt-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_001,
        )
        store.append_control(ctrl)

        events = store.list_events(submit.task_id)
        types = [e.event_type for e in events]
        assert "CONTROL_RECEIVED" in types


# ---------------------------------------------------------------------------
# read_task_snapshot (design §11.3)
# ---------------------------------------------------------------------------


class TestReadTaskSnapshot:
    def test_snapshot_matches_scope(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-snap-1")
        submit = store.submit_task(env)
        snap = store.read_task_snapshot(sample_scope, submit.task_id)
        assert snap is not None
        assert snap.task_id == submit.task_id
        assert snap.state == "QUEUED"

    def test_snapshot_wrong_tenant_returns_none(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-snap-2")
        submit = store.submit_task(env)

        wrong_scope = RequestScope(
            tenant_id="other-tenant",
            principal_id=sample_scope.principal_id,
            agent_id=sample_scope.agent_id,
            workspace_id=sample_scope.workspace_id,
        )
        snap = store.read_task_snapshot(wrong_scope, submit.task_id)
        assert snap is None

    def test_snapshot_includes_error(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(sample_scope, channel_message_id="msg-snap-3")
        submit = store.submit_task(env)

        cancel = TaskControlRequest(
            task_id=submit.task_id,
            kind="CANCEL",
            dedup_key="cancel-snap-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_002,
        )
        store.append_control(cancel)

        snap = store.read_task_snapshot(sample_scope, submit.task_id)
        assert snap is not None
        assert snap.state == "CANCELLED"
        assert snap.error is not None
