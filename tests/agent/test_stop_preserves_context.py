"""Tests for /stop preserving partial context from interrupted turns.

When /stop cancels an active task, the runtime checkpoint (tool results,
assistant messages accumulated so far) should be materialized into session
history rather than silently discarded.

See: https://github.com/HKUDS/erza/issues/2966
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from erza.agent.loop import AgentLoop
from erza.bus.queue import MessageBus


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
        patch("erza.agent.loop.ContextBuilder"),
        patch("erza.agent.loop.SessionManager"),
        patch("erza.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        return AgentLoop(bus=bus, provider=provider, workspace=tmp_path)


class TestStopPreservesContext:
    """Verify that /stop restores partial context via checkpoint."""

    def test_restore_checkpoint_method_exists(self, tmp_path):
        """AgentLoop should have _restore_runtime_checkpoint."""
        loop = _make_loop(tmp_path)
        assert hasattr(loop, "_restore_runtime_checkpoint")

    def test_checkpoint_key_constant(self, tmp_path):
        """The runtime checkpoint key should be defined."""
        loop = _make_loop(tmp_path)
        assert loop._RUNTIME_CHECKPOINT_KEY == "runtime_checkpoint"

    def test_cancel_dispatch_restores_checkpoint(self, tmp_path):
        """When a task is cancelled, the checkpoint should be restored."""
        loop = _make_loop(tmp_path)
        session = MagicMock()
        session.metadata = {
            "runtime_checkpoint": {
                "phase": "awaiting_tools",
                "iteration": 0,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search for that.",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "completed_tool_results": [
                    {"role": "tool", "tool_call_id": "tc_1", "content": "Search results: ..."},
                ],
                "pending_tool_calls": [],
            }
        }
        session.messages = [
            {"role": "user", "content": "Search for something"},
        ]
        loop.sessions.get_or_create.return_value = session

        restored = loop._restore_runtime_checkpoint(session)
        assert restored is True
        assert len(session.messages) > 1
        assert "runtime_checkpoint" not in session.metadata


class TestAuditOnlyCheckpointsNotPersisted:
    """仅审计用途的 checkpoint phase 不得覆盖单槽 runtime_checkpoint 恢复位。

    tool_started/tool_completed/tool_blocked payload 不含恢复字段
    (assistant_message/completed_tool_results/pending_tool_calls)，若持久化进
    单槽 runtime_checkpoint，崩溃后 restore 读到空 payload，中断轮次不再被
    物化（回归）。
    """

    @staticmethod
    def _session():
        session = MagicMock()
        session.metadata = {}
        session.messages = []
        return session

    def test_tool_started_not_persisted(self, tmp_path):
        loop = _make_loop(tmp_path)
        session = self._session()
        loop._set_runtime_checkpoint(
            session,
            {"phase": "tool_started", "iteration": 1},
        )
        assert "runtime_checkpoint" not in session.metadata

    def test_tool_completed_not_persisted(self, tmp_path):
        loop = _make_loop(tmp_path)
        session = self._session()
        loop._set_runtime_checkpoint(
            session,
            {
                "phase": "tool_completed",
                "iteration": 1,
                "tool_checkpoint": {"tool_name": "read_file", "status": "ok"},
            },
        )
        assert "runtime_checkpoint" not in session.metadata

    def test_tool_blocked_not_persisted(self, tmp_path):
        loop = _make_loop(tmp_path)
        session = self._session()
        loop._set_runtime_checkpoint(
            session,
            {
                "phase": "tool_blocked",
                "iteration": 1,
                "tool_checkpoint": {"tool_name": "exec", "status": "blocked"},
            },
        )
        assert "runtime_checkpoint" not in session.metadata

    def test_awaiting_tools_still_persisted(self, tmp_path):
        """对照组：含恢复字段的 awaiting_tools checkpoint 正常持久化。"""
        loop = _make_loop(tmp_path)
        session = self._session()
        loop._set_runtime_checkpoint(
            session,
            {
                "phase": "awaiting_tools",
                "iteration": 1,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search.",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "completed_tool_results": [],
                "pending_tool_calls": [],
            },
        )
        saved = session.metadata["runtime_checkpoint"]
        assert saved["phase"] == "awaiting_tools"
        assert saved["assistant_message"]["content"] == "Let me search."
        loop.sessions.save.assert_called_with(session)


@pytest.mark.asyncio
async def test_dispatch_cancellation_restores_checkpoint():
    """Regression for #2966: /stop interrupting _dispatch must materialize the
    in-flight runtime checkpoint into session.messages before the cancellation
    unwinds, so the next turn can see the partial work.

    This exercises the real _dispatch path (locks, pending queues, the
    CancelledError handler) rather than poking _restore_runtime_checkpoint in
    isolation, so a future refactor that drops the cancel-time restore is
    caught by CI instead of silently regressing.
    """
    from erza.bus.events import InboundMessage
    from erza.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with (
        patch("erza.agent.loop.ContextBuilder"),
        patch("erza.agent.loop.SessionManager"),
        patch("erza.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)

    checkpoint_key = loop._RUNTIME_CHECKPOINT_KEY
    session = SimpleNamespace(
        key="test:c1",
        metadata={
            checkpoint_key: {
                "phase": "awaiting_tools",
                "iteration": 0,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search.",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "completed_tool_results": [
                    {"role": "tool", "tool_call_id": "tc_1", "content": "Search hit."},
                ],
                "pending_tool_calls": [],
            }
        },
        messages=[{"role": "user", "content": "Search for something"}],
    )

    loop.sessions.get_or_create = MagicMock(return_value=session)
    loop.sessions.save = MagicMock()

    async def _cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    loop._process_message = _cancel

    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="work")

    with pytest.raises(asyncio.CancelledError):
        await loop._dispatch(msg)

    roles = [m.get("role") for m in session.messages]
    assert roles == ["user", "assistant", "tool"], (
        "Expected the assistant message and completed tool result from the "
        f"interrupted turn to be materialized into session.messages; got {roles}"
    )
    assert checkpoint_key not in session.metadata, (
        "Checkpoint metadata should be cleared after restore"
    )
    assert loop.sessions.save.called, (
        "Session should be persisted so the restored state survives process restart"
    )
