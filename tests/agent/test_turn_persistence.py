"""Collaborator-level tests for :class:`TurnPersistence`.

These tests instantiate ``TurnPersistence`` directly with a fake host so the
persist/checkpoint/restore algorithms can be exercised without spinning up a
full :class:`AgentLoop`. The matching delegation tests live alongside to
guarantee the ``AgentLoop`` compatibility wrappers remain thin forwarders.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.tools.message import MessageTool
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.agent.turn_persistence import (
    PENDING_USER_TURN_KEY,
    RUNTIME_CHECKPOINT_KEY,
    TurnPersistence,
)
from miniunicorn.bus.events import InboundMessage
from miniunicorn.session.manager import Session
from miniunicorn.utils.helpers import image_placeholder_text


def _make_host(
    *,
    max_tool_result_chars: int = 16_000,
    tools: ToolRegistry | None = None,
    sessions: Any | None = None,
) -> SimpleNamespace:
    """Build a minimal host satisfying :class:`TurnPersistenceHost`."""

    return SimpleNamespace(
        max_tool_result_chars=max_tool_result_chars,
        tools=tools or ToolRegistry(),
        sessions=sessions or MagicMock(),
        context=ContextBuilder(Path(".")),
    )


def _make_persistence(
    *,
    max_tool_result_chars: int = 16_000,
    tools: ToolRegistry | None = None,
    sessions: Any | None = None,
) -> TurnPersistence:
    return TurnPersistence(
        _make_host(
            max_tool_result_chars=max_tool_result_chars,
            tools=tools,
            sessions=sessions,
        )
    )


# ---------------------------------------------------------------------------
# Step 1: collaborator-level save and sanitize tests
# ---------------------------------------------------------------------------


class TestSanitizePersistedBlocks:
    def test_base64_image_blocks_become_placeholder_with_path(self) -> None:
        persistence = _make_persistence()
        block = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
            "_meta": {"path": "/media/feishu/photo.jpg"},
        }

        result = persistence.sanitize_persisted_blocks([block])

        assert result == [
            {"type": "text", "text": image_placeholder_text("/media/feishu/photo.jpg")}
        ]

    def test_base64_image_blocks_become_placeholder_without_meta(self) -> None:
        persistence = _make_persistence()
        block = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        }

        result = persistence.sanitize_persisted_blocks([block])

        assert result == [{"type": "text", "text": image_placeholder_text("")}]

    def test_runtime_context_text_dropped_only_when_drop_runtime_true(self) -> None:
        persistence = _make_persistence()
        runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now"
        block = {"type": "text", "text": runtime}

        kept = persistence.sanitize_persisted_blocks([block], drop_runtime=False)
        assert kept == [block]

        dropped = persistence.sanitize_persisted_blocks([block], drop_runtime=True)
        assert dropped == []

    def test_tool_text_truncation_uses_max_tool_result_chars(self) -> None:
        persistence = _make_persistence(max_tool_result_chars=128)
        big = "x" * 1_000
        block = {"type": "text", "text": big}

        result = persistence.sanitize_persisted_blocks([block], should_truncate_text=True)

        # ``truncate_text`` appends ``"\n... (truncated)"`` so the total length
        # is ``max_chars + len(suffix)``. Verify the body was capped and the
        # suffix is present rather than asserting an absolute length.
        assert result[0]["text"].startswith("x")
        assert result[0]["text"].endswith("\n... (truncated)")
        assert len(result[0]["text"]) < len(big)

    def test_non_text_non_image_blocks_pass_through(self) -> None:
        persistence = _make_persistence()
        block = {"type": "other", "value": 42}

        result = persistence.sanitize_persisted_blocks([block])

        assert result == [block]

    def test_non_dict_blocks_pass_through(self) -> None:
        persistence = _make_persistence()

        result = persistence.sanitize_persisted_blocks(["raw-string"])  # type: ignore[list-item]

        assert result == ["raw-string"]


class TestSaveTurn:
    def test_empty_assistant_without_tool_calls_is_skipped(self) -> None:
        persistence = _make_persistence()
        session = Session(key="test:empty")

        persistence.save_turn(
            session,
            [
                {"role": "assistant", "content": "", "tool_calls": []},
                {"role": "assistant", "content": "real"},
            ],
            skip=0,
        )

        assert [m["content"] for m in session.messages] == ["real"]

    def test_assistant_latency_attached_to_correct_message(self) -> None:
        persistence = _make_persistence()
        session = Session(key="test:latency")

        persistence.save_turn(
            session,
            [
                {"role": "assistant", "content": "hello", "tool_calls": [{"id": "c1"}]},
                {"role": "assistant", "content": "final answer"},
            ],
            skip=0,
            turn_latency_ms=12345,
        )

        assert session.messages[-1]["content"] == "final answer"
        assert session.messages[-1]["latency_ms"] == 12345
        assert "latency_ms" not in session.messages[0]

    def test_tool_text_truncated_above_limit(self) -> None:
        persistence = _make_persistence(max_tool_result_chars=64)
        session = Session(key="test:tool-trunc")
        big = "y" * 1_000

        persistence.save_turn(
            session,
            [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": big}],
            skip=0,
        )

        # ``truncate_text`` appends a suffix, so verify the body was capped and
        # the suffix is present rather than asserting an absolute length.
        assert session.messages[0]["content"].startswith("y")
        assert session.messages[0]["content"].endswith("\n... (truncated)")
        assert len(session.messages[0]["content"]) < len(big)

    def test_original_input_messages_not_mutated(self) -> None:
        persistence = _make_persistence(max_tool_result_chars=64)
        session = Session(key="test:no-mutate")
        original = {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "read_file",
            "content": "x" * 500,
        }
        snapshot = dict(original)

        persistence.save_turn(session, [original], skip=0)

        assert original == snapshot

    def test_user_string_runtime_suffix_is_stripped(self) -> None:
        persistence = _make_persistence()
        session = Session(key="test:suffix")
        runtime = (
            ContextBuilder._RUNTIME_CONTEXT_TAG
            + "\nCurrent Time: now\n"
            + ContextBuilder._RUNTIME_CONTEXT_END
        )

        persistence.save_turn(
            session,
            [{"role": "user", "content": f"hello world\n\n{runtime}"}],
            skip=0,
        )

        assert session.messages[0]["content"] == "hello world"

    def test_user_string_only_runtime_context_is_dropped(self) -> None:
        persistence = _make_persistence()
        session = Session(key="test:suffix-only")
        runtime = (
            ContextBuilder._RUNTIME_CONTEXT_TAG
            + "\nCurrent Time: now\n"
            + ContextBuilder._RUNTIME_CONTEXT_END
        )

        persistence.save_turn(
            session,
            [{"role": "user", "content": runtime}],
            skip=0,
        )

        assert session.messages == []


def _make_message_tool_with_sent_flag(sent: bool) -> MessageTool:
    """Create a real MessageTool with ``_sent_in_turn`` preset.

    Using a real instance keeps ``isinstance(mt, MessageTool)`` true so the
    suppression branch in ``assemble_outbound`` exercises production logic.
    """

    async def _noop_send(_msg: Any) -> None:
        pass

    tool = MessageTool(send_callback=_noop_send)
    tool._sent_in_turn = sent
    return tool


class TestAssembleOutbound:
    def test_message_tool_suppression_when_no_injections(self) -> None:
        tools = ToolRegistry()
        tools.register(_make_message_tool_with_sent_flag(sent=True))
        persistence = _make_persistence(tools=tools)

        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hi")

        result = persistence.assemble_outbound(
            msg,
            final_content="answer",
            all_msgs=[],
            stop_reason="completed",
            had_injections=False,
            on_stream=None,
        )

        assert result is None

    def test_message_tool_kept_when_injections_present(self) -> None:
        tools = ToolRegistry()
        tools.register(_make_message_tool_with_sent_flag(sent=True))
        persistence = _make_persistence(tools=tools)

        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hi")

        result = persistence.assemble_outbound(
            msg,
            final_content="answer",
            all_msgs=[],
            stop_reason="completed",
            had_injections=True,
            on_stream=None,
        )

        assert result is not None
        assert result.content == "answer"

    def test_streamed_and_latency_metadata_attached(self) -> None:
        persistence = _make_persistence()
        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hi")

        async def _stream(_chunk: str) -> None:
            pass

        result = persistence.assemble_outbound(
            msg,
            final_content="answer",
            all_msgs=[],
            stop_reason="completed",
            had_injections=False,
            on_stream=_stream,
            turn_latency_ms=4321,
        )

        assert result is not None
        assert result.metadata["_streamed"] is True
        assert result.metadata["latency_ms"] == 4321

    def test_error_stop_reason_skips_streamed_flag(self) -> None:
        persistence = _make_persistence()
        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hi")

        async def _stream(_chunk: str) -> None:
            pass

        result = persistence.assemble_outbound(
            msg,
            final_content="answer",
            all_msgs=[],
            stop_reason="error",
            had_injections=False,
            on_stream=_stream,
        )

        assert result is not None
        assert "_streamed" not in result.metadata


class TestPersistSubagentFollowup:
    def test_dedupes_by_subagent_task_id(self) -> None:
        persistence = _make_persistence()
        session = Session(key="cli:dedupe")
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:dedupe",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        )

        assert persistence.persist_subagent_followup(session, msg) is True
        assert persistence.persist_subagent_followup(session, msg) is False
        assert len(session.messages) == 1

    def test_skips_empty_content(self) -> None:
        persistence = _make_persistence()
        session = Session(key="cli:empty")
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:empty",
            content="",
            metadata={"subagent_task_id": "sub-empty"},
        )

        assert persistence.persist_subagent_followup(session, msg) is False
        assert session.messages == []


class TestPersistUserMessageEarly:
    def test_persists_text_message_and_marks_pending(self) -> None:
        sessions = MagicMock()
        persistence = _make_persistence(sessions=sessions)
        session = Session(key="cli:early")
        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hello")

        result = persistence.persist_user_message_early(msg, session)

        assert result is True
        assert session.messages[0]["content"] == "hello"
        assert session.metadata.get(PENDING_USER_TURN_KEY) is True
        sessions.save.assert_called_once_with(session)

    def test_skips_when_no_text_and_no_media(self) -> None:
        persistence = _make_persistence()
        session = Session(key="cli:skip")
        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="")

        result = persistence.persist_user_message_early(msg, session)

        assert result is False
        assert session.messages == []
        assert PENDING_USER_TURN_KEY not in session.metadata


# ---------------------------------------------------------------------------
# Step 2: checkpoint recovery tests
# ---------------------------------------------------------------------------


class TestRestoreRuntimeCheckpoint:
    def test_no_checkpoint_returns_false_and_no_changes(self) -> None:
        persistence = _make_persistence()
        session = Session(key="cli:none", metadata={"unrelated": "value"})
        snapshot_messages = list(session.messages)
        snapshot_metadata = dict(session.metadata)

        result = persistence.restore_runtime_checkpoint(session)

        assert result is False
        assert session.messages == snapshot_messages
        assert session.metadata == snapshot_metadata

    def test_assistant_and_completed_tools_restored_in_order(self) -> None:
        persistence = _make_persistence()
        session = Session(
            key="cli:restore",
            metadata={
                RUNTIME_CHECKPOINT_KEY: {
                    "assistant_message": {
                        "role": "assistant",
                        "content": "working",
                        "tool_calls": [
                            {
                                "id": "call_done",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                    "completed_tool_results": [
                        {
                            "role": "tool",
                            "tool_call_id": "call_done",
                            "name": "read_file",
                            "content": "ok",
                        }
                    ],
                    "pending_tool_calls": [],
                }
            },
        )

        result = persistence.restore_runtime_checkpoint(session)

        assert result is True
        assert [m["role"] for m in session.messages] == ["assistant", "tool"]
        assert session.messages[1]["tool_call_id"] == "call_done"
        assert RUNTIME_CHECKPOINT_KEY not in session.metadata

    def test_pending_tool_calls_become_interrupted_error_messages(self) -> None:
        persistence = _make_persistence()
        session = Session(
            key="cli:pending",
            metadata={
                RUNTIME_CHECKPOINT_KEY: {
                    "assistant_message": None,
                    "completed_tool_results": [],
                    "pending_tool_calls": [
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        }
                    ],
                }
            },
        )

        result = persistence.restore_runtime_checkpoint(session)

        assert result is True
        assert session.messages[-1]["role"] == "tool"
        assert session.messages[-1]["tool_call_id"] == "call_pending"
        assert "interrupted before this tool finished" in session.messages[-1]["content"].lower()

    def test_overlapping_tail_deduplicated(self) -> None:
        persistence = _make_persistence()
        existing_assistant = {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {
                    "id": "call_done",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        }
        existing_tool = {
            "role": "tool",
            "tool_call_id": "call_done",
            "name": "read_file",
            "content": "ok",
        }
        session = Session(
            key="cli:overlap",
            messages=[dict(existing_assistant), dict(existing_tool)],
            metadata={
                RUNTIME_CHECKPOINT_KEY: {
                    "assistant_message": existing_assistant,
                    "completed_tool_results": [existing_tool],
                    "pending_tool_calls": [],
                }
            },
        )

        result = persistence.restore_runtime_checkpoint(session)

        assert result is True
        # The two existing messages should not be duplicated.
        assert len(session.messages) == 2

    def test_checkpoint_and_pending_marker_cleared_after_success(self) -> None:
        persistence = _make_persistence()
        session = Session(
            key="cli:clear",
            metadata={
                RUNTIME_CHECKPOINT_KEY: {
                    "assistant_message": {"role": "assistant", "content": "working"},
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
                PENDING_USER_TURN_KEY: True,
            },
        )

        result = persistence.restore_runtime_checkpoint(session)

        assert result is True
        assert RUNTIME_CHECKPOINT_KEY not in session.metadata
        assert PENDING_USER_TURN_KEY not in session.metadata

    def test_restore_twice_is_idempotent(self) -> None:
        persistence = _make_persistence()
        session = Session(
            key="cli:idempotent",
            metadata={
                RUNTIME_CHECKPOINT_KEY: {
                    "assistant_message": {"role": "assistant", "content": "working"},
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                }
            },
        )

        first = persistence.restore_runtime_checkpoint(session)
        snapshot = list(session.messages)
        second = persistence.restore_runtime_checkpoint(session)

        assert first is True
        assert second is False
        assert session.messages == snapshot


class TestRestorePendingUserTurn:
    def test_user_only_pending_turn_gets_interrupted_response(self) -> None:
        persistence = _make_persistence()
        session = Session(
            key="cli:pending-user",
            messages=[{"role": "user", "content": "hello"}],
            metadata={PENDING_USER_TURN_KEY: True},
        )

        result = persistence.restore_pending_user_turn(session)

        assert result is True
        assert session.messages[-1]["role"] == "assistant"
        assert "interrupted before a response was generated" in session.messages[-1]["content"]
        assert PENDING_USER_TURN_KEY not in session.metadata

    def test_no_pending_marker_returns_false(self) -> None:
        persistence = _make_persistence()
        session = Session(key="cli:none", messages=[{"role": "user", "content": "hi"}])

        result = persistence.restore_pending_user_turn(session)

        assert result is False
        assert len(session.messages) == 1


# ---------------------------------------------------------------------------
# Step 3: delegation compatibility tests
# ---------------------------------------------------------------------------


def _make_loop_for_delegation(tmp_path: Path) -> AgentLoop:
    """Construct a real AgentLoop for delegation tests."""

    from tests.agent.conftest import make_loop

    return make_loop(tmp_path)


class TestDelegationForwarding:
    def test_save_turn_delegates_to_collaborator(self, tmp_path: Path) -> None:
        loop = _make_loop_for_delegation(tmp_path)
        session = Session(key="cli:delegate-save")
        received: dict[str, Any] = {}

        def fake_save_turn(sess, messages, skip, *, turn_latency_ms=None):
            received["args"] = (sess, messages, skip)
            received["turn_latency_ms"] = turn_latency_ms
            sess.messages.append({"role": "assistant", "content": "delegated"})

        loop._turn_persistence.save_turn = fake_save_turn  # type: ignore[method-assign]

        loop._save_turn(
            session,
            [{"role": "assistant", "content": "ignored"}],
            0,
            turn_latency_ms=99,
        )

        assert received["args"] == (session, [{"role": "assistant", "content": "ignored"}], 0)
        assert received["turn_latency_ms"] == 99
        assert session.messages[0]["content"] == "delegated"

    def test_assemble_outbound_delegates_to_collaborator(self, tmp_path: Path) -> None:
        loop = _make_loop_for_delegation(tmp_path)
        msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hi")
        sentinel = object()

        def fake_assemble_outbound(
            msg_,
            final_content,
            all_msgs,
            stop_reason,
            had_injections,
            on_stream,
            *,
            turn_latency_ms=None,
        ):
            assert turn_latency_ms == 7
            return sentinel  # type: ignore[return-value]

        loop._turn_persistence.assemble_outbound = fake_assemble_outbound  # type: ignore[method-assign]

        async def _stream(_chunk: str) -> None:
            pass

        result = loop._assemble_outbound(
            msg,
            final_content="answer",
            all_msgs=[],
            stop_reason="completed",
            had_injections=False,
            on_stream=_stream,
            turn_latency_ms=7,
        )

        assert result is sentinel

    def test_restore_runtime_checkpoint_delegates_to_collaborator(self, tmp_path: Path) -> None:
        loop = _make_loop_for_delegation(tmp_path)
        session = Session(key="cli:delegate-restore")

        def fake_restore(sess):
            sess.messages.append({"role": "assistant", "content": "restored"})
            return True

        loop._turn_persistence.restore_runtime_checkpoint = fake_restore  # type: ignore[method-assign]

        result = loop._restore_runtime_checkpoint(session)

        assert result is True
        assert session.messages[0]["content"] == "restored"

    def test_persist_subagent_followup_delegates_to_collaborator(self, tmp_path: Path) -> None:
        loop = _make_loop_for_delegation(tmp_path)
        session = Session(key="cli:delegate-sub")
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:delegate-sub",
            content="result",
            metadata={"subagent_task_id": "sub-1"},
        )
        captured: dict[str, Any] = {}

        def fake_persist(sess, msg_):
            captured["sess"] = sess
            captured["msg"] = msg_
            return True

        loop._turn_persistence.persist_subagent_followup = fake_persist  # type: ignore[method-assign]

        result = loop._persist_subagent_followup(session, msg)

        assert result is True
        assert captured["sess"] is session
        assert captured["msg"] is msg


class TestConstants:
    def test_runtime_checkpoint_key_matches_legacy_constant(self) -> None:
        assert RUNTIME_CHECKPOINT_KEY == "runtime_checkpoint"
        assert AgentLoop._RUNTIME_CHECKPOINT_KEY == RUNTIME_CHECKPOINT_KEY

    def test_pending_user_turn_key_matches_legacy_constant(self) -> None:
        assert PENDING_USER_TURN_KEY == "pending_user_turn"
        assert AgentLoop._PENDING_USER_TURN_KEY == PENDING_USER_TURN_KEY

    def test_checkpoint_message_key_is_static_delegate(self) -> None:
        message = {
            "role": "tool",
            "content": "ok",
            "tool_call_id": "call_1",
            "name": "read_file",
            "tool_calls": [],
            "reasoning_content": None,
            "thinking_blocks": None,
        }

        loop_result = AgentLoop._checkpoint_message_key(message)
        collab_result = TurnPersistence.checkpoint_message_key(message)

        assert loop_result == collab_result
