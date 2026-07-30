"""Tests for structured tool-event progress metadata emitted by AgentLoop."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import miniunicorn.agent.runner as runner_module
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMResponse, ToolCallRequest
from miniunicorn.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


class TestToolEventProgress:
    """_run_agent_loop emits structured tool_events via on_progress."""

    # NOTE: Tests that exercised the legacy ``AgentLoop._dispatch`` ->
    # MessageBus outbound-forwarding path (tool_events / file_edit_events
    # metadata, _stream_delta / _stream_end / _turn_end markers,
    # _session_updated, WebUI title generation) were removed in design
    # Task 10. ``_dispatch`` was deleted alongside ``process_direct`` and the
    # bus-consume loop; inbound work now flows through ``RuntimeApplication``
    # and the ``AgentExecutionCallback`` -> ProgressPort / Outbox path, whose
    # forwarding model differs fundamentally (final replies go through the
    # Outbox, not the bus). Re-asserting those behaviors against the durable
    # runtime would constitute new test coverage and is out of scope here.

    @pytest.mark.asyncio
    async def test_start_and_finish_events_emitted(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="custom_tool", arguments={"path": "foo.txt"})
        calls = iter(
            [
                LLMResponse(content="Visible", tool_calls=[tool_call]),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(return_value=(None, {"path": "foo.txt"}, None))
        loop.tools.execute = AsyncMock(return_value="ok")

        progress: list[tuple[str, bool, list[dict] | None]] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            progress.append((content, tool_hint, tool_events))

        result = await loop._run_agent_loop([], on_progress=on_progress)

        assert result.final_content == "Done"
        assert progress == [
            ("Visible", False, None),
            (
                'custom_tool("foo.txt")',
                True,
                [
                    {
                        "version": 1,
                        "phase": "start",
                        "call_id": "call1",
                        "name": "custom_tool",
                        "arguments": {"path": "foo.txt"},
                        "result": None,
                        "error": None,
                        "files": [],
                        "embeds": [],
                    }
                ],
            ),
            (
                "",
                False,
                [
                    {
                        "version": 1,
                        "phase": "end",
                        "call_id": "call1",
                        "name": "custom_tool",
                        "arguments": {"path": "foo.txt"},
                        "result": "ok",
                        "error": None,
                        "files": [],
                        "embeds": [],
                    }
                ],
            ),
        ]

    @pytest.mark.asyncio
    async def test_write_file_emits_file_edit_progress(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        target = tmp_path / "foo.txt"
        target.write_text("old\n", encoding="utf-8")
        tool_call = ToolCallRequest(
            id="call-write",
            name="write_file",
            arguments={"path": "foo.txt", "content": "new\nextra\n"},
        )
        calls = iter(
            [
                LLMResponse(content="", tool_calls=[tool_call]),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(
            return_value=(None, {"path": "foo.txt", "content": "new\nextra\n"}, None),
        )

        async def execute(name: str, params: dict) -> str:
            target.write_text(params["content"], encoding="utf-8")
            return "ok"

        loop.tools.execute = AsyncMock(side_effect=execute)
        file_events: list[dict] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
            file_edit_events: list[dict] | None = None,
        ) -> None:
            if file_edit_events:
                file_events.extend(file_edit_events)

        result = await loop._run_agent_loop([], on_progress=on_progress)

        assert result.final_content == "Done"
        assert [event["phase"] for event in file_events] == ["start", "end"]
        assert file_events[0] == {
            "version": 1,
            "call_id": "call-write",
            "tool": "write_file",
            "path": "foo.txt",
            "absolute_path": (tmp_path / "foo.txt").resolve().as_posix(),
            "phase": "start",
            "added": 2,
            "deleted": 1,
            "approximate": True,
            "status": "editing",
        }
        assert file_events[1]["status"] == "done"
        assert file_events[1]["approximate"] is False
        assert (file_events[1]["added"], file_events[1]["deleted"]) == (2, 1)

    @pytest.mark.asyncio
    async def test_file_edit_snapshot_skipped_when_progress_callback_cannot_emit_file_edits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        loop = _make_loop(tmp_path)
        target = tmp_path / "foo.txt"
        target.write_text("old\n", encoding="utf-8")
        tool_call = ToolCallRequest(
            id="call-write",
            name="write_file",
            arguments={"path": "foo.txt", "content": "new\n"},
        )
        calls = iter(
            [
                LLMResponse(content="", tool_calls=[tool_call]),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(
            return_value=(None, {"path": "foo.txt", "content": "new\n"}, None),
        )

        async def execute(name: str, params: dict) -> str:
            target.write_text(params["content"], encoding="utf-8")
            return "ok"

        loop.tools.execute = AsyncMock(side_effect=execute)
        prepare_tracker = MagicMock(side_effect=AssertionError("unexpected file snapshot"))
        monkeypatch.setattr(runner_module, "prepare_file_edit_tracker", prepare_tracker)

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            pass

        result = await loop._run_agent_loop([], on_progress=on_progress)

        assert result.final_content == "Done"
        assert target.read_text(encoding="utf-8") == "new\n"
        prepare_tracker.assert_not_called()

    @pytest.mark.asyncio
    async def test_exec_does_not_emit_file_edit_progress(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call-exec",
            name="exec",
            arguments={"command": "printf hi > foo.txt"},
        )
        calls = iter(
            [
                LLMResponse(content="", tool_calls=[tool_call]),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(
            return_value=(None, {"command": "printf hi > foo.txt"}, None),
        )
        loop.tools.execute = AsyncMock(return_value="ok")
        file_events: list[dict] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
            file_edit_events: list[dict] | None = None,
        ) -> None:
            if file_edit_events:
                file_events.extend(file_edit_events)

        await loop._run_agent_loop([], on_progress=on_progress)

        assert file_events == []

    @pytest.mark.asyncio
    async def test_bus_progress_forwards_file_edit_events_for_websocket_only(
        self, tmp_path: Path
    ) -> None:
        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
        edit_events = [
            {
                "call_id": "call-write",
                "tool": "write_file",
                "path": "foo.txt",
                "phase": "start",
                "added": 1,
                "deleted": 0,
                "approximate": True,
                "status": "editing",
            }
        ]

        websocket_progress = await loop._build_bus_progress_callback(
            InboundMessage(
                channel="websocket",
                sender_id="u1",
                chat_id="chat1",
                content="edit",
            )
        )
        assert on_progress_accepts_file_edit_events(websocket_progress) is True
        await websocket_progress("", file_edit_events=edit_events)
        outbound = await bus.consume_outbound()
        assert outbound.metadata["_file_edit_events"] == edit_events

        telegram_progress = await loop._build_bus_progress_callback(
            InboundMessage(
                channel="telegram",
                sender_id="u1",
                chat_id="chat2",
                content="edit",
            )
        )
        assert on_progress_accepts_file_edit_events(telegram_progress) is False
        await invoke_file_edit_progress(telegram_progress, edit_events)
        assert bus.outbound_size == 0

    @pytest.mark.asyncio
    async def test_streamed_progress_is_not_repeated_before_tool_execution(
        self,
        tmp_path: Path,
    ) -> None:
        """If content was already streamed as progress, tool setup should not repeat it."""
        loop = _make_loop(tmp_path)
        loop.provider.supports_progress_deltas = True
        tool_call = ToolCallRequest(id="call1", name="custom_tool", arguments={"path": "foo.txt"})
        calls = iter(
            [
                LLMResponse(content="I will inspect it.", tool_calls=[tool_call]),
                LLMResponse(content="Done", tool_calls=[]),
            ]
        )

        async def chat_stream_with_retry(*, on_content_delta, **kwargs):
            response = next(calls)
            if response.tool_calls:
                await on_content_delta("I will ")
                await on_content_delta("inspect it.")
            return response

        loop.provider.chat_stream_with_retry = chat_stream_with_retry
        loop.provider.chat_with_retry = AsyncMock()
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(return_value=(None, {"path": "foo.txt"}, None))
        loop.tools.execute = AsyncMock(return_value="ok")

        streamed: list[str] = []
        progress: list[tuple[str, bool, list[dict] | None]] = []

        async def on_stream(delta: str) -> None:
            streamed.append(delta)

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            progress.append((content, tool_hint, tool_events))

        result = await loop._run_agent_loop(
            [],
            on_progress=on_progress,
            on_stream=on_stream,
        )

        assert result.final_content == "Done"
        assert streamed == ["I will", " inspect it."]
        assert progress[0][0] == 'custom_tool("foo.txt")'
        assert all(item[0] != "I will inspect it." for item in progress)
