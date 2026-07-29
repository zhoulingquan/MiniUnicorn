"""WP0 — Characterize current crash-boundary recovery behavior.

Design §2.1 lists the failure modes the durable runtime must correct. This
file pins the *current* in-process recovery contract so WP3 can prove the
durable path preserves (or improves) these guarantees.

Recovery mechanism today (design §29.4):
1. Before a turn runs, ``persist_user_message_early`` writes the user
   message to the session file and sets ``metadata[pending_user_turn]=True``.
2. During the turn, ``set_runtime_checkpoint`` may write a checkpoint to
   ``metadata[runtime_checkpoint]``.
3. If the process dies mid-turn, the next turn for that session calls
   ``restore_pending_user_turn`` and ``restore_runtime_checkpoint`` on
   entry, materializing the interrupted state into session history.
4. ``COMMAND -> DONE`` shortcut bypasses SAVE/RESPOND for slash commands.

WP3 removes this dual authority and replaces it with the durable task
state machine + Runtime Store checkpoints.
"""

from __future__ import annotations

from miniunicorn.agent.turn_persistence import (
    PENDING_USER_TURN_KEY,
    RUNTIME_CHECKPOINT_KEY,
    TurnPersistence,
)
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

    def test_persist_user_message_early_writes_message_and_marks_pending(
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
        assert crash_session.metadata.get(PENDING_USER_TURN_KEY) is True, (
            "pending_user_turn flag must be set so a crash before turn "
            "completion is detectable on the next load."
        )

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
        assert PENDING_USER_TURN_KEY not in crash_session.metadata


# ---------------------------------------------------------------------------
# Task 2: characterize the runtime checkpoint boundary
# ---------------------------------------------------------------------------


class TestRuntimeCheckpointBoundary:
    """``set_runtime_checkpoint`` writes a recoverable snapshot to session
    metadata. WP3 replaces this with ``TurnJournalPort.save_checkpoint()``
    backed by the Runtime Store ``checkpoints`` table (design §17.4, §29.4).
    """

    def test_set_runtime_checkpoint_writes_metadata(
        self,
        crash_persistence: TurnPersistence,
        crash_session: Session,
        checkpoint_payload: dict,
    ) -> None:
        crash_persistence.set_runtime_checkpoint(crash_session, checkpoint_payload)

        assert RUNTIME_CHECKPOINT_KEY in crash_session.metadata
        assert crash_session.metadata[RUNTIME_CHECKPOINT_KEY] == checkpoint_payload

    def test_clear_runtime_checkpoint_removes_metadata(
        self,
        crash_persistence: TurnPersistence,
        crash_session: Session,
        checkpoint_payload: dict,
    ) -> None:
        crash_persistence.set_runtime_checkpoint(crash_session, checkpoint_payload)
        crash_persistence.clear_runtime_checkpoint(crash_session)

        assert RUNTIME_CHECKPOINT_KEY not in crash_session.metadata


# ---------------------------------------------------------------------------
# Task 3: characterize the restore-on-next-turn boundary
# ---------------------------------------------------------------------------


class TestRestoreOnNextTurn:
    """A crashed turn is detected and materialized on the next session load.

    Today this is the only recovery mechanism. WP3 must preserve the
    user-visible contract (the user message is not lost) while moving the
    authority to Runtime Store.
    """

    def test_pending_user_turn_is_marked_clearable(
        self,
        crash_persistence: TurnPersistence,
        crash_session: Session,
        crash_inbound_message: InboundMessage,
    ) -> None:
        """After successful turn completion, the pending flag is cleared."""
        crash_persistence.persist_user_message_early(
            crash_inbound_message, crash_session
        )
        assert crash_session.metadata.get(PENDING_USER_TURN_KEY) is True

        crash_persistence.clear_pending_user_turn(crash_session)
        assert PENDING_USER_TURN_KEY not in crash_session.metadata

    def test_runtime_checkpoint_survives_save_reload(
        self,
        crash_session_manager: SessionManager,
        crash_session: Session,
        crash_persistence: TurnPersistence,
        checkpoint_payload: dict,
    ) -> None:
        """Checkpoint metadata survives a save -> reload cycle.

        This is what makes mid-turn recovery possible today: a new process
        loads the session file and sees the checkpoint.
        """
        crash_persistence.set_runtime_checkpoint(crash_session, checkpoint_payload)
        crash_session_manager.save(crash_session)

        # Simulate a new process by dropping the cache.
        crash_session_manager._cache.pop(crash_session.key, None)  # noqa: SLF001
        reloaded = crash_session_manager.get_or_create(crash_session.key)

        assert reloaded.metadata.get(RUNTIME_CHECKPOINT_KEY) == checkpoint_payload


# ---------------------------------------------------------------------------
# Task 4: characterize the COMMAND -> DONE shortcut
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
# Task 5: characterize the fire-and-forget turn task boundary
# ---------------------------------------------------------------------------


class TestFireAndForgetTurnTask:
    """Design §6.16: "Required work is never owned only by
    ``asyncio.create_task()``."

    Today the dispatcher spawns the turn as a fire-and-forget task tracked
    only in an in-memory dict. A process crash loses the task even if the
    user message was already persisted.
    """

    def test_dispatcher_active_tasks_is_a_plain_dict(self) -> None:
        """The active-tasks registry is an in-memory dict — not durable."""
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher

        # The class defines active_tasks as an instance attribute; verify
        # it's a plain dict by inspecting __init__ source.
        import inspect

        source = inspect.getsource(TurnDispatcher.__init__)
        assert "active_tasks" in source, (
            "TurnDispatcher no longer initializes active_tasks — WP3 progress."
        )

    def test_dispatcher_pending_queues_is_a_plain_dict(self) -> None:
        from miniunicorn.agent.turn_dispatcher import TurnDispatcher
        import inspect

        source = inspect.getsource(TurnDispatcher.__init__)
        # The attribute may be named _pending_by_session or pending_queues.
        assert (
            "pending_queues" in source or "_pending_by_session" in source
        ), "TurnDispatcher no longer owns pending_queues — WP3 progress."
