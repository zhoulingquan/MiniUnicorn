"""WP0 — Characterize current crash-boundary recovery behavior.

Design §2.1 lists the failure modes the durable runtime must correct. This
file pins the *current* in-process recovery contract so WP3 can prove the
durable path preserves (or improves) these guarantees.

Recovery mechanism today (design §29.4):

1. Before a turn runs, ``persist_user_message_early`` writes the user
   message to the session file so a crash before turn completion still
   leaves a recoverable transcript entry.
2. The durable Runtime Store state machine + ``TurnJournalPort`` own
   mid-turn recovery. Legacy ``runtime_checkpoint`` / ``pending_user_turn``
   session-metadata writers were removed in design Task 10.
3. ``COMMAND -> DONE`` shortcut bypasses SAVE/RESPOND for slash commands.

WP3 replaces this dual authority with the durable task state machine +
Runtime Store checkpoints.
"""

from __future__ import annotations

from miniunicorn.agent.turn_persistence import TurnPersistence
from miniunicorn.bus.events import InboundMessage
from miniunicorn.session.manager import Session, SessionManager


# ---------------------------------------------------------------------------
# Task 1: characterize the user-message early-persistence boundary
# ---------------------------------------------------------------------------


class TestEarlyUserMessagePersistence:
    """The triggering user message is persisted before the turn runs.

    This is the current "acceptance" boundary — once the user message is
    on disk, a crash before turn completion still leaves a recoverable
    transcript entry. WP3 replaces this with durable task acceptance in
    Runtime Store (design §17.1).
    """

    def test_persist_user_message_early_writes_message(
        self,
        crash_persistence: TurnPersistence,
        crash_session: Session,
        crash_inbound_message: InboundMessage,
    ) -> None:
        persisted = crash_persistence.persist_user_message_early(
            crash_inbound_message, crash_session
        )

        assert persisted is True, "user message with text must be persisted"
        assert len(crash_session.messages) == 1, "exactly one user message added"
        assert crash_session.messages[0]["role"] == "user"
        assert crash_session.messages[0]["content"] == "hello, please reply"

    def test_empty_message_is_not_persisted_early(
        self,
        crash_persistence: TurnPersistence,
        crash_session: Session,
        crash_inbound_message: InboundMessage,
    ) -> None:
        crash_inbound_message.content = "   "
        persisted = crash_persistence.persist_user_message_early(
            crash_inbound_message, crash_session
        )
        assert persisted is False
        assert crash_session.messages == []


# ---------------------------------------------------------------------------
# Task 2: legacy checkpoint writers removed (design Task 10)
# ---------------------------------------------------------------------------


class TestLegacyCheckpointWritersRemoved:
    """Design §6.22, §29.4: ``runtime_checkpoint`` / ``pending_user_turn``
    session-metadata writers were removed in design Task 10.

    Durable tasks own recovery through the Runtime Store state machine and
    ``TurnJournalPort.save_checkpoint()``.
    """

    def test_turn_persistence_does_not_define_set_runtime_checkpoint(self) -> None:
        assert not hasattr(TurnPersistence, "set_runtime_checkpoint"), (
            "TurnPersistence.set_runtime_checkpoint was removed in design Task 10; "
            "durable checkpoints are owned by TurnJournalPort.save_checkpoint()."
        )

    def test_turn_persistence_does_not_define_mark_pending_user_turn(self) -> None:
        assert not hasattr(TurnPersistence, "mark_pending_user_turn"), (
            "TurnPersistence.mark_pending_user_turn was removed in design Task 10; "
            "durable pending turns are owned by the Runtime Store state machine."
        )

    def test_turn_persistence_does_not_define_clear_runtime_checkpoint(self) -> None:
        assert not hasattr(TurnPersistence, "clear_runtime_checkpoint"), (
            "TurnPersistence.clear_runtime_checkpoint was removed in design Task 10."
        )

    def test_turn_persistence_does_not_define_clear_pending_user_turn(self) -> None:
        assert not hasattr(TurnPersistence, "clear_pending_user_turn"), (
            "TurnPersistence.clear_pending_user_turn was removed in design Task 10."
        )

    def test_turn_persistence_does_not_define_restore_runtime_checkpoint(self) -> None:
        assert not hasattr(TurnPersistence, "restore_runtime_checkpoint"), (
            "TurnPersistence.restore_runtime_checkpoint was removed in design Task 10."
        )

    def test_turn_persistence_does_not_define_restore_pending_user_turn(self) -> None:
        assert not hasattr(TurnPersistence, "restore_pending_user_turn"), (
            "TurnPersistence.restore_pending_user_turn was removed in design Task 10."
        )

    def test_turn_persistence_does_not_define_checkpoint_message_key(self) -> None:
        assert not hasattr(TurnPersistence, "checkpoint_message_key"), (
            "TurnPersistence.checkpoint_message_key was removed in design Task 10."
        )


# ---------------------------------------------------------------------------
# Task 3: characterize the COMMAND -> DONE shortcut
# ---------------------------------------------------------------------------


class TestCommandShortcutBypassesSaveRespond:
    """Design §18.1 requires removing the ``COMMAND -> DONE`` shortcut for
    runtime tasks because it bypasses durable session commit and Outbox.

    This test pins the current transition so WP3 can prove the shortcut is
    removed (or guarded) for durable tasks.
    """

    def test_command_to_done_transition_exists_today(self) -> None:
        from miniunicorn.agent._state_machine import TurnState
        from miniunicorn.agent.turn_executor import TURN_TRANSITIONS

        # The shortcut transition currently exists.
        assert (TurnState.COMMAND, "shortcut") in TURN_TRANSITIONS, (
            "COMMAND -> DONE shortcut has already been removed — WP3 progress."
        )
        assert TURN_TRANSITIONS[(TurnState.COMMAND, "shortcut")] == TurnState.DONE


# ---------------------------------------------------------------------------
# Task 4: legacy fire-and-forget turn task boundary removed (design Task 10)
# ---------------------------------------------------------------------------


class TestFireAndForgetTurnTaskRemoved:
    """Design §6.16: "Required work is never owned only by
    ``asyncio.create_task()``."

    The dispatcher's in-memory ``active_tasks`` registry and
    ``pending_queues`` dict were removed in design Task 10. Turns are
    now durable tasks submitted through ``RuntimeApplication``.
    """

    def test_dispatcher_does_not_initialize_active_tasks(self) -> None:
        """The active-tasks registry must not exist on the dispatcher."""
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        import inspect

        source = inspect.getsource(TurnDispatcher.__init__)
        assert "active_tasks" not in source, (
            "TurnDispatcher still initializes active_tasks; design Task 10 "
            "removed process-local task registries."
        )

    def test_dispatcher_does_not_initialize_pending_queues(self) -> None:
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher
        import inspect

        source = inspect.getsource(TurnDispatcher.__init__)
        assert "pending_queues" not in source, (
            "TurnDispatcher still initializes pending_queues; design Task 10 "
            "removed process-local pending-queue registries."
        )
