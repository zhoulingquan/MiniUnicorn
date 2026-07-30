"""Test message tool suppress logic for final replies."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.tools.message import MessageTool
from miniunicorn.bus.events import InboundMessage, OutboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


class TestMessageToolSuppressLogic:
    """Final reply suppressed only when message tool sends to the same target."""

    @pytest.mark.asyncio
    async def test_suppress_when_sent_to_same_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1",
            name="message",
            arguments={"content": "Hello", "channel": "feishu", "chat_id": "chat123"},
        )
        calls = iter(
            [
                LLMResponse(content="", tool_calls=[tool_call]),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send")
        result = await loop._process_message(msg)

        # Message tool output went to the bus via DirectOutboundPort.
        # Filter out progress messages (metadata["_progress"]).
        outbound = []
        while loop.bus.outbound_size > 0:
            ob = await loop.bus.consume_outbound()
            if not ob.metadata.get("_progress"):
                outbound.append(ob)
        assert len(outbound) == 1
        assert outbound[0].content == "Hello"
        assert result is None  # suppressed

    @pytest.mark.asyncio
    async def test_not_suppress_when_sent_to_different_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1",
            name="message",
            arguments={
                "content": "Email content",
                "channel": "email",
                "chat_id": "user@example.com",
            },
        )
        calls = iter(
            [
                LLMResponse(content="", tool_calls=[tool_call]),
                LLMResponse(content="I've sent the email.", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(
            channel="feishu", sender_id="user1", chat_id="chat123", content="Send email"
        )
        result = await loop._process_message(msg)

        # Message tool output went to the bus via DirectOutboundPort.
        # Filter out progress messages (metadata["_progress"]).
        outbound = []
        while loop.bus.outbound_size > 0:
            ob = await loop.bus.consume_outbound()
            if not ob.metadata.get("_progress"):
                outbound.append(ob)
        assert len(outbound) == 1
        assert outbound[0].channel == "email"
        assert result is not None  # not suppressed
        assert result.channel == "feishu"

    @pytest.mark.asyncio
    async def test_not_suppress_when_no_message_tool_used(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="Hello!", tool_calls=[])
        )
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Hi")
        result = await loop._process_message(msg)

        assert result is not None
        assert "Hello" in result.content

    @pytest.mark.asyncio
    async def test_injected_followup_with_message_tool_does_not_emit_empty_fallback(
        self, tmp_path: Path
    ) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1",
            name="message",
            arguments={"content": "Tool reply", "channel": "feishu", "chat_id": "chat123"},
        )
        calls = iter(
            [
                LLMResponse(content="First answer", tool_calls=[]),
                LLMResponse(content="", tool_calls=[tool_call]),
                LLMResponse(content="", tool_calls=[]),
                LLMResponse(content="", tool_calls=[]),
                LLMResponse(content="", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        pending_queue = asyncio.Queue()
        await pending_queue.put(
            InboundMessage(
                channel="feishu", sender_id="user1", chat_id="chat123", content="follow-up"
            )
        )

        msg = InboundMessage(
            channel="feishu", sender_id="user1", chat_id="chat123", content="Start"
        )
        result = await loop._process_message(msg, pending_queue=pending_queue)

        # Message tool output went to the bus via DirectOutboundPort.
        # Filter out progress messages (metadata["_progress"]).
        outbound = []
        while loop.bus.outbound_size > 0:
            ob = await loop.bus.consume_outbound()
            if not ob.metadata.get("_progress"):
                outbound.append(ob)
        assert len(outbound) == 1
        assert outbound[0].content == "Tool reply"
        assert result is None

    async def test_progress_hides_internal_reasoning(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="read_file", arguments={"path": "foo.txt"})
        calls = iter(
            [
                LLMResponse(
                    content="Visible<think>hidden</think>",
                    tool_calls=[tool_call],
                    reasoning_content="secret reasoning",
                    thinking_blocks=[{"signature": "sig", "thought": "secret thought"}],
                ),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute = AsyncMock(return_value="ok")

        progress: list[tuple[str, bool]] = []

        async def on_progress(content: str, *, tool_hint: bool = False) -> None:
            progress.append((content, tool_hint))

        result = await loop._run_agent_loop([], on_progress=on_progress)

        assert result.final_content == "Done"
        assert progress == [
            ("Visible", False),
            ("read foo.txt", True),
        ]


class TestMessageToolTurnTracking:
    def test_sent_in_turn_tracks_same_target(self) -> None:
        tool = MessageTool()
        from miniunicorn.agent.tools.context import RequestContext

        tool.set_context(RequestContext(channel="feishu", chat_id="chat1"))
        assert not tool._sent_in_turn
        tool._sent_in_turn = True
        assert tool._sent_in_turn

    def test_start_turn_resets(self) -> None:
        tool = MessageTool()
        tool._sent_in_turn = True
        tool.start_turn()
        assert not tool._sent_in_turn

    def test_schema_discourages_current_chat_replies(self) -> None:
        tool = MessageTool()

        assert "Do not use this for the normal reply in the current chat" in tool.description
        assert "generate_image creates images in the current chat" in tool.description
        assert (
            "Do not use this for a normal reply in the current chat"
            in tool.parameters["properties"]["content"]["description"]
        )
