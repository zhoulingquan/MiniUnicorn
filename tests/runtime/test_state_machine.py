"""WP1 — Task state machine transitions (design §14.2).

Validates the ``TRANSITIONS`` table and the ``is_allowed_transition``
helper against the design's state machine. Also validates that the
``SqliteRuntimeStore`` enforces transitions via ``_transition_task``.
"""

from __future__ import annotations

import pytest

from miniunicorn.runtime.models import (
    TASK_STATES,
    TERMINAL_TASK_STATES,
    TRANSITIONS,
    is_allowed_transition,
    is_terminal_state,
)


# ---------------------------------------------------------------------------
# Table consistency
# ---------------------------------------------------------------------------


class TestTransitionTable:
    """The transition table must cover every state (design §14.2)."""

    def test_all_states_have_entries(self) -> None:
        for state in TASK_STATES:
            assert state in TRANSITIONS, f"missing transition entry for {state}"

    def test_terminal_states_have_empty_targets(self) -> None:
        for state in TERMINAL_TASK_STATES:
            assert TRANSITIONS[state] == frozenset(), (
                f"terminal state {state} must have no outgoing transitions"
            )

    def test_targets_are_valid_states(self) -> None:
        for source, targets in TRANSITIONS.items():
            for target in targets:
                assert target in TASK_STATES, (
                    f"invalid target {target} from {source}"
                )

    def test_no_self_transitions_in_table(self) -> None:
        """Self-transitions are handled by ``is_allowed_transition`` as no-ops;
        the table itself must not list them (design §14.2)."""
        for source, targets in TRANSITIONS.items():
            assert source not in targets, (
                f"self-transition {source} -> {source} must not be in table"
            )


# ---------------------------------------------------------------------------
# is_allowed_transition
# ---------------------------------------------------------------------------


class TestIsAllowedTransition:
    """``is_allowed_transition`` must match the table exactly (design §14.2)."""

    @pytest.mark.parametrize("source", TASK_STATES)
    @pytest.mark.parametrize("target", TASK_STATES)
    def test_matches_table(self, source: str, target: str) -> None:
        if source == target:
            assert is_allowed_transition(source, target)  # type: ignore[arg-type]
        else:
            expected = target in TRANSITIONS.get(source, frozenset())  # type: ignore[arg-type]
            assert is_allowed_transition(source, target) == expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Forbidden transitions (spot checks)
# ---------------------------------------------------------------------------


class TestForbiddenTransitions:
    """Key forbidden transitions that the runtime must never allow."""

    @pytest.mark.parametrize("terminal", TERMINAL_TASK_STATES)
    def test_terminal_cannot_transition(self, terminal: str) -> None:
        for target in TASK_STATES:
            if target != terminal:
                assert not is_allowed_transition(terminal, target)  # type: ignore[arg-type]

    def test_queued_cannot_go_directly_to_running(self) -> None:
        assert not is_allowed_transition("QUEUED", "RUNNING")

    def test_queued_cannot_go_to_completed(self) -> None:
        assert not is_allowed_transition("QUEUED", "COMPLETED")

    def test_queued_cannot_go_to_retry_wait(self) -> None:
        assert not is_allowed_transition("QUEUED", "RETRY_WAIT")

    def test_queued_cannot_go_to_waiting_user(self) -> None:
        assert not is_allowed_transition("QUEUED", "WAITING_USER")

    def test_leased_cannot_go_to_completed(self) -> None:
        assert not is_allowed_transition("LEASED", "COMPLETED")

    def test_leased_cannot_go_to_retry_wait(self) -> None:
        assert not is_allowed_transition("LEASED", "RETRY_WAIT")

    def test_leased_cannot_go_to_waiting_user(self) -> None:
        assert not is_allowed_transition("LEASED", "WAITING_USER")

    def test_running_cannot_go_to_leased(self) -> None:
        assert not is_allowed_transition("RUNNING", "LEASED")

    def test_running_cannot_go_to_queued(self) -> None:
        assert not is_allowed_transition("RUNNING", "QUEUED")


# ---------------------------------------------------------------------------
# Allowed transitions (spot checks)
# ---------------------------------------------------------------------------


class TestAllowedTransitions:
    """Key allowed transitions from the design."""

    def test_queued_to_leased(self) -> None:
        assert is_allowed_transition("QUEUED", "LEASED")

    def test_queued_to_cancelled(self) -> None:
        assert is_allowed_transition("QUEUED", "CANCELLED")

    def test_leased_to_running(self) -> None:
        assert is_allowed_transition("LEASED", "RUNNING")

    def test_leased_to_queued(self) -> None:
        assert is_allowed_transition("LEASED", "QUEUED")

    def test_leased_to_cancelled(self) -> None:
        assert is_allowed_transition("LEASED", "CANCELLED")

    def test_running_to_completed(self) -> None:
        assert is_allowed_transition("RUNNING", "COMPLETED")

    def test_running_to_retry_wait(self) -> None:
        assert is_allowed_transition("RUNNING", "RETRY_WAIT")

    def test_running_to_waiting_user(self) -> None:
        assert is_allowed_transition("RUNNING", "WAITING_USER")

    def test_running_to_failed(self) -> None:
        assert is_allowed_transition("RUNNING", "FAILED")

    def test_running_to_cancelled(self) -> None:
        assert is_allowed_transition("RUNNING", "CANCELLED")

    def test_retry_wait_to_queued(self) -> None:
        assert is_allowed_transition("RETRY_WAIT", "QUEUED")

    def test_retry_wait_to_failed(self) -> None:
        assert is_allowed_transition("RETRY_WAIT", "FAILED")

    def test_retry_wait_to_cancelled(self) -> None:
        assert is_allowed_transition("RETRY_WAIT", "CANCELLED")

    def test_waiting_user_to_queued(self) -> None:
        assert is_allowed_transition("WAITING_USER", "QUEUED")

    def test_waiting_user_to_failed(self) -> None:
        assert is_allowed_transition("WAITING_USER", "FAILED")

    def test_waiting_user_to_cancelled(self) -> None:
        assert is_allowed_transition("WAITING_USER", "CANCELLED")


# ---------------------------------------------------------------------------
# Terminal state helper
# ---------------------------------------------------------------------------


class TestIsTerminalState:
    @pytest.mark.parametrize("state", TERMINAL_TASK_STATES)
    def test_terminal(self, state: str) -> None:
        assert is_terminal_state(state)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "state", ["QUEUED", "LEASED", "RUNNING", "RETRY_WAIT", "WAITING_USER"]
    )
    def test_non_terminal(self, state: str) -> None:
        assert not is_terminal_state(state)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Store enforces transitions
# ---------------------------------------------------------------------------


class TestStoreEnforcesTransitions:
    """The ``SqliteRuntimeStore`` must reject forbidden transitions at
    runtime via ``_transition_task`` (design §14.2)."""

    def test_forbidden_queued_to_running_raises(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-forbidden-1"
        )
        result = store.submit_task(env)
        task = store.read_task(result.task_id)
        assert task is not None
        assert task.state == "QUEUED"

        with pytest.raises(RuntimeError, match="forbidden transition"):
            store._transition_task(
                result.task_id,
                from_state="QUEUED",
                to_state="RUNNING",
                now_ms=1_000_001,
            )

    def test_forbidden_terminal_to_anything_raises(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-forbidden-2"
        )
        result = store.submit_task(env)

        # Move to CANCELLED via control (QUEUED -> CANCELLED is allowed).
        from miniunicorn.runtime.models import TaskControlRequest

        store.append_control(
            TaskControlRequest(
                task_id=result.task_id,
                kind="CANCEL",
                dedup_key="cancel-1",
                payload_blob_id=None,
                requested_by="test",
                requested_at_ms=1_000_001,
            )
        )

        with pytest.raises(RuntimeError, match="forbidden transition"):
            store._transition_task(
                result.task_id,
                from_state=None,
                to_state="RUNNING",
                now_ms=1_000_002,
            )

    def test_state_mismatch_raises(
        self, store, sample_scope, make_inbound_envelope
    ) -> None:
        env = make_inbound_envelope(
            sample_scope, channel_message_id="msg-mismatch-1"
        )
        result = store.submit_task(env)

        with pytest.raises(RuntimeError, match="state mismatch"):
            store._transition_task(
                result.task_id,
                from_state="RUNNING",  # actual is QUEUED
                to_state="COMPLETED",
                now_ms=1_000_001,
            )
