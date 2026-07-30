"""Tests for /stop preserving partial context from interrupted turns.

Legacy ``runtime_checkpoint`` / ``pending_user_turn`` session-metadata
writers and readers were removed in design Task 10. Durable tasks own
recovery through the Runtime Store state machine and
``TurnJournalPort.save_checkpoint()`` (design §6.22, §29.4).

See: https://github.com/HKUDS/miniunicorn/issues/2966
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.bus.queue import MessageBus


def _make_provider():
    """Create an LLM provider mock with required attributes."""
    from types import SimpleNamespace

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


def _make_loop(tmp_path: Path) -> AgentLoop:
    """Create a real AgentLoop with mocked provider — avoids patching __init__."""
    bus = MessageBus()
    provider = _make_provider()
    with (
        patch("miniunicorn.agent.loop.ContextBuilder"),
        patch("miniunicorn.agent.loop.SessionManager"),
        patch("miniunicorn.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = MagicMock(return_value=0)
        return AgentLoop(bus=bus, provider=provider, workspace=tmp_path)


class TestLegacyCheckpointWritersRemoved:
    """Design §6.22, §29.4: ``runtime_checkpoint`` / ``pending_user_turn``
    session-metadata writers and readers were removed in design Task 10.

    Durable tasks own recovery through the Runtime Store state machine and
    ``TurnJournalPort.save_checkpoint()``.
    """

    def test_loop_does_not_define_restore_runtime_checkpoint(self, tmp_path) -> None:
        loop = _make_loop(tmp_path)
        assert not hasattr(loop, "_restore_runtime_checkpoint"), (
            "AgentLoop._restore_runtime_checkpoint was removed in design Task 10; "
            "durable checkpoints are owned by TurnJournalPort.save_checkpoint()."
        )

    def test_loop_does_not_define_runtime_checkpoint_key_constant(self, tmp_path) -> None:
        loop = _make_loop(tmp_path)
        assert not hasattr(AgentLoop, "_RUNTIME_CHECKPOINT_KEY"), (
            "AgentLoop._RUNTIME_CHECKPOINT_KEY was removed in design Task 10; "
            "durable checkpoints are owned by TurnJournalPort.save_checkpoint()."
        )

    def test_loop_does_not_define_pending_user_turn_key_constant(self, tmp_path) -> None:
        loop = _make_loop(tmp_path)
        assert not hasattr(AgentLoop, "_PENDING_USER_TURN_KEY"), (
            "AgentLoop._PENDING_USER_TURN_KEY was removed in design Task 10; "
            "durable tasks use the WAITING_USER state instead."
        )


# Note: test_dispatch_cancellation_restores_checkpoint was removed in
# design Task 10. The _dispatch method (legacy bus-consume per-session
# dispatch) was removed; cancellation-time checkpoint restore is no
# longer reached through that path. Legacy checkpoint reader/writer
# behavior is also gone — durable recovery is owned by the Runtime Store
# state machine and TurnJournalPort.save_checkpoint().
