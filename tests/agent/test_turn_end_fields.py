"""Field-lock tests for the WebUI ``turn_end`` event.

Phase 6 convergence: ``_last_call_usage`` moved from cross-session loop state to
per-turn telemetry (``erza.agent.turn_telemetry``).  These tests lock
the ``turn_end`` event fields so the refactor stays behavior-neutral: the event
must carry the *current turn's* usage, an integer latency, a goal-state blob,
and be emitted after the final content message.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from erza.agent.loop import AgentLoop
from erza.bus.events import InboundMessage
from erza.bus.queue import MessageBus
from erza.providers.base import LLMResponse


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


async def _drain(bus: MessageBus) -> list:
    outbound = []
    while bus.outbound_size > 0:
        outbound.append(await bus.consume_outbound())
    return outbound


def _usage(prompt: int, completion: int, cached: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cached_tokens": cached,
    }


class TestTurnEndFields:
    @pytest.mark.asyncio
    async def test_turn_end_carries_current_turn_usage_fields(
        self,
        tmp_path: Path,
    ) -> None:
        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="Done", tool_calls=[], usage=_usage(10, 5, 3))
        )
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
        loop.tools.get_definitions = MagicMock(return_value=[])

        await loop._dispatch(
            InboundMessage(
                channel="websocket",
                sender_id="u1",
                chat_id="chat1",
                content="say hello",
            )
        )

        outbound = await _drain(bus)
        turn_end = [m for m in outbound if m.metadata.get("_turn_end")]
        assert len(turn_end) == 1
        event = turn_end[0]
        assert event.content == ""
        assert event.chat_id == "chat1"
        # Field lock: latency is an integer, goal_state always present.
        assert isinstance(event.metadata["latency_ms"], int)
        assert "goal_state" in event.metadata
        # Field lock: context_usage mirrors the current turn's last call.
        assert event.metadata["context_usage"] == _usage(10, 5, 3)

    @pytest.mark.asyncio
    async def test_turn_end_after_final_content_message(self, tmp_path: Path) -> None:
        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="Final answer", tool_calls=[], usage=_usage(4, 2, 0))
        )
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
        loop.tools.get_definitions = MagicMock(return_value=[])

        await loop._dispatch(
            InboundMessage(
                channel="websocket",
                sender_id="u1",
                chat_id="chat1",
                content="hi",
            )
        )

        outbound = await _drain(bus)
        done = [m for m in outbound if m.content == "Final answer"]
        turn_end = [m for m in outbound if m.metadata.get("_turn_end")]
        assert len(done) == 1
        assert len(turn_end) == 1
        assert outbound.index(done[0]) < outbound.index(turn_end[0])

    @pytest.mark.asyncio
    async def test_command_turn_end_falls_back_to_trailing_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        """Command turns keep the previous trailing usage in turn_end.

        A prior agent turn populates the trailing snapshot; the subsequent
        ``/status`` command turn has empty per-turn telemetry and must fall back
        to the snapshot so the event field is unchanged.
        """
        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="Done", tool_calls=[], usage=_usage(10, 5, 3))
        )
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
        loop.tools.get_definitions = MagicMock(return_value=[])

        # First turn: an agent turn leaves the trailing usage snapshot.
        await loop._dispatch(
            InboundMessage(
                channel="websocket",
                sender_id="u1",
                chat_id="chat1",
                content="say hello",
            )
        )
        await _drain(bus)
        assert loop._response._last_call_usage == _usage(10, 5, 3)

        # Second turn: a command produces no agent loop, so turn_end must fall
        # back to the trailing snapshot.
        await loop._dispatch(
            InboundMessage(
                channel="websocket",
                sender_id="u1",
                chat_id="chat1",
                content="/status",
            )
        )
        outbound = await _drain(bus)
        turn_end = [m for m in outbound if m.metadata.get("_turn_end")]
        assert len(turn_end) == 1
        assert turn_end[0].metadata["context_usage"] == _usage(10, 5, 3)
