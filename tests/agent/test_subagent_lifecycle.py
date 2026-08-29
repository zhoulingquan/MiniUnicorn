"""Tests for SubagentManager lifecycle — spawn, run, announce, cancel."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.hook import AgentHookContext
from miniunicorn.agent.runner import AgentRunResult
from miniunicorn.agent.subagent import (
    SubagentManager,
    SubagentStatus,
    _SubagentHook,
)
from miniunicorn.bus.events import make_session_key, session_key_base
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manager(tmp_path: Path, **kw) -> SubagentManager:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    defaults = dict(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
        max_tool_result_chars=16_000,
    )
    defaults.update(kw)
    return SubagentManager(**defaults)


def _make_hook_context(**overrides) -> AgentHookContext:
    defaults = dict(
        iteration=1,
        tool_calls=[],
        tool_events=[],
        messages=[],
        usage={},
        error=None,
        stop_reason="completed",
        final_content="ok",
    )
    defaults.update(overrides)
    return AgentHookContext(**defaults)


# ---------------------------------------------------------------------------
# SubagentStatus defaults
# ---------------------------------------------------------------------------


class TestSubagentStatus:
    def test_defaults(self):
        s = SubagentStatus(
            task_id="abc",
            label="test",
            task_description="do stuff",
            started_at=time.monotonic(),
        )
        assert s.phase == "initializing"
        assert s.iteration == 0
        assert s.tool_events == []
        assert s.usage == {}
        assert s.stop_reason is None
        assert s.error is None


# ---------------------------------------------------------------------------
# set_provider
# ---------------------------------------------------------------------------


class TestSetProvider:
    def test_updates_provider_model_runner(self, tmp_path):
        sm = _manager(tmp_path)
        new_provider = MagicMock(spec=LLMProvider)
        sm.set_provider(new_provider, "new-model")
        assert sm.provider is new_provider
        assert sm.model == "new-model"
        assert sm.runner.provider is new_provider


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


class TestSpawn:
    @pytest.mark.asyncio
    async def test_returns_string_with_task_id(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )
        )
        result = await sm.spawn("do something")
        assert "started" in result
        assert "id:" in result

    @pytest.mark.asyncio
    async def test_creates_task_in_running_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        await sm.spawn("task", session_key="s1")
        assert len(sm._running_tasks) == 1

        block.set()
        await asyncio.sleep(0.1)
        assert len(sm._running_tasks) == 0

    @pytest.mark.asyncio
    async def test_creates_status(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )
        )
        await sm.spawn("my task")
        await asyncio.sleep(0.1)
        # Status cleaned up after task completes
        assert len(sm._task_statuses) == 0

    @pytest.mark.asyncio
    async def test_registers_in_session_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        await sm.spawn("task", session_key="s1")
        assert "s1" in sm._session_tasks
        assert len(sm._session_tasks["s1"]) == 1

        block.set()
        await asyncio.sleep(0.1)
        assert "s1" not in sm._session_tasks

    @pytest.mark.asyncio
    async def test_no_session_key_no_registration(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        await sm.spawn("task")
        assert len(sm._session_tasks) == 0

        block.set()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_label_defaults_to_truncated_task(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        long_task = "A" * 50
        await sm.spawn(long_task, session_key="s1")
        status = next(iter(sm._task_statuses.values()))
        assert status.label == long_task[:30] + "..."

        block.set()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_custom_label(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        await sm.spawn("task", label="Custom Label", session_key="s1")
        status = next(iter(sm._task_statuses.values()))
        assert status.label == "Custom Label"

        block.set()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_cleanup_callback_removes_all_entries(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )
        )
        await sm.spawn("task", session_key="s1")
        await asyncio.sleep(0.1)
        assert len(sm._running_tasks) == 0
        assert len(sm._task_statuses) == 0
        assert len(sm._session_tasks) == 0


# ---------------------------------------------------------------------------
# _run_subagent
# ---------------------------------------------------------------------------


class TestRunSubagent:
    @pytest.mark.asyncio
    async def test_successful_run(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content="Task done!",
                messages=[],
                stop_reason="completed",
            )
        )
        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as mock_announce:
            await sm._run_subagent(
                "t1",
                "do task",
                "label",
                {"channel": "cli", "chat_id": "direct"},
                SubagentStatus(
                    task_id="t1",
                    label="label",
                    task_description="do task",
                    started_at=time.monotonic(),
                ),
            )
            mock_announce.assert_called_once()
            assert mock_announce.call_args.args[-2] == "ok"

    @pytest.mark.asyncio
    async def test_tool_error_run(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content=None,
                messages=[],
                stop_reason="tool_error",
                tool_events=[{"name": "read_file", "status": "error", "detail": "not found"}],
            )
        )
        status = SubagentStatus(
            task_id="t1", label="label", task_description="do task", started_at=time.monotonic()
        )
        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as mock_announce:
            await sm._run_subagent(
                "t1",
                "do task",
                "label",
                {"channel": "cli", "chat_id": "direct"},
                status,
            )
            assert mock_announce.call_args.args[-2] == "error"

    @pytest.mark.asyncio
    async def test_exception_run(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(side_effect=RuntimeError("LLM down"))
        status = SubagentStatus(
            task_id="t1", label="label", task_description="do task", started_at=time.monotonic()
        )
        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as mock_announce:
            await sm._run_subagent(
                "t1",
                "do task",
                "label",
                {"channel": "cli", "chat_id": "direct"},
                status,
            )
            assert status.phase == "error"
            assert "LLM down" in status.error
            assert mock_announce.call_args.args[-2] == "error"

    @pytest.mark.asyncio
    async def test_status_updated_on_success(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content="ok",
                messages=[],
                stop_reason="completed",
            )
        )
        status = SubagentStatus(
            task_id="t1", label="label", task_description="do task", started_at=time.monotonic()
        )
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1",
                "do task",
                "label",
                {"channel": "cli", "chat_id": "direct"},
                status,
            )
            assert status.phase == "done"
            assert status.stop_reason == "completed"


# ---------------------------------------------------------------------------
# _announce_result
# ---------------------------------------------------------------------------


class TestAnnounceResult:
    @pytest.mark.asyncio
    async def test_publishes_inbound_message(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1",
            "label",
            "task",
            "result text",
            {"channel": "cli", "chat_id": "direct"},
            "ok",
        )

        assert len(published) == 1
        msg = published[0]
        assert msg.channel == "system"
        assert msg.sender_id == "subagent"
        assert msg.metadata["injected_event"] == "subagent_result"
        assert msg.metadata["subagent_task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_session_key_override(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1",
            "label",
            "task",
            "result",
            {"channel": "telegram", "chat_id": "123", "session_key": "s1"},
            "ok",
        )

        assert published[0].session_key_override == "s1"

    @pytest.mark.asyncio
    async def test_session_key_override_fallback(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1",
            "label",
            "task",
            "result",
            {"channel": "telegram", "chat_id": "123"},
            "ok",
        )

        assert published[0].session_key_override == "telegram:123"

    @pytest.mark.asyncio
    async def test_ok_status_text(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1",
            "label",
            "task",
            "result",
            {"channel": "cli", "chat_id": "direct"},
            "ok",
        )

        assert "completed successfully" in published[0].content

    @pytest.mark.asyncio
    async def test_error_status_text(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1",
            "label",
            "task",
            "error details",
            {"channel": "cli", "chat_id": "direct"},
            "error",
        )

        assert "failed" in published[0].content

    @pytest.mark.asyncio
    async def test_origin_message_id_in_metadata(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1",
            "label",
            "task",
            "result",
            {"channel": "cli", "chat_id": "direct"},
            "ok",
            origin_message_id="msg-123",
        )

        assert published[0].metadata["origin_message_id"] == "msg-123"


# ---------------------------------------------------------------------------
# _format_partial_progress
# ---------------------------------------------------------------------------


class TestFormatPartialProgress:
    def _make_result(self, tool_events=None, error=None):
        return MagicMock(tool_events=tool_events or [], error=error)

    def test_completed_only(self):
        result = self._make_result(
            tool_events=[
                {"name": "read_file", "status": "ok", "detail": "file content"},
                {"name": "exec", "status": "ok", "detail": "output"},
            ]
        )
        text = SubagentManager._format_partial_progress(result)
        assert "Completed steps:" in text
        assert "read_file" in text
        assert "exec" in text

    def test_failure_only(self):
        result = self._make_result(
            tool_events=[
                {"name": "read_file", "status": "error", "detail": "not found"},
            ]
        )
        text = SubagentManager._format_partial_progress(result)
        assert "Failure:" in text
        assert "not found" in text

    def test_completed_and_failure(self):
        result = self._make_result(
            tool_events=[
                {"name": "read_file", "status": "ok", "detail": "content"},
                {"name": "exec", "status": "error", "detail": "timeout"},
            ]
        )
        text = SubagentManager._format_partial_progress(result)
        assert "Completed steps:" in text
        assert "Failure:" in text

    def test_limited_to_last_three(self):
        result = self._make_result(
            tool_events=[
                {"name": f"tool_{i}", "status": "ok", "detail": f"result_{i}"} for i in range(5)
            ]
        )
        text = SubagentManager._format_partial_progress(result)
        assert "tool_2" in text
        assert "tool_3" in text
        assert "tool_4" in text
        assert "tool_0" not in text
        assert "tool_1" not in text

    def test_error_without_failure_event(self):
        result = self._make_result(
            tool_events=[{"name": "read_file", "status": "ok", "detail": "ok"}],
            error="Something went wrong",
        )
        text = SubagentManager._format_partial_progress(result)
        assert "Something went wrong" in text

    def test_empty_events_with_error(self):
        result = self._make_result(error="Total failure")
        text = SubagentManager._format_partial_progress(result)
        assert "Total failure" in text

    def test_empty_no_error_returns_fallback(self):
        result = self._make_result()
        text = SubagentManager._format_partial_progress(result)
        assert "Error" in text


# ---------------------------------------------------------------------------
# cancel_by_session
# ---------------------------------------------------------------------------


class TestCancelBySession:
    @pytest.mark.asyncio
    async def test_cancels_running_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        await sm.spawn("task1", session_key="s1")
        await sm.spawn("task2", session_key="s1")
        assert len(sm._session_tasks.get("s1", set())) == 2

        count = await sm.cancel_by_session("s1")
        assert count == 2
        block.set()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_no_tasks_returns_zero(self, tmp_path):
        sm = _manager(tmp_path)
        count = await sm.cancel_by_session("nonexistent")
        assert count == 0

    @pytest.mark.asyncio
    async def test_already_done_not_counted(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(
            return_value=AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )
        )
        await sm.spawn("task1", session_key="s1")
        await asyncio.sleep(0.1)  # Wait for completion

        count = await sm.cancel_by_session("s1")
        assert count == 0


# ---------------------------------------------------------------------------
# get_running_count / get_running_count_by_session
# ---------------------------------------------------------------------------


class TestRunningCounts:
    @pytest.mark.asyncio
    async def test_running_count_zero(self, tmp_path):
        sm = _manager(tmp_path)
        assert sm.get_running_count() == 0

    @pytest.mark.asyncio
    async def test_running_count_tracks_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()

        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run

        await sm.spawn("t1", session_key="s1")
        await sm.spawn("t2", session_key="s1")
        assert sm.get_running_count() == 2
        assert sm.get_running_count_by_session("s1") == 2

        block.set()
        await asyncio.sleep(0.1)
        assert sm.get_running_count() == 0

    @pytest.mark.asyncio
    async def test_running_count_by_session_nonexistent(self, tmp_path):
        sm = _manager(tmp_path)
        assert sm.get_running_count_by_session("nonexistent") == 0


# ---------------------------------------------------------------------------
# _SubagentHook
# ---------------------------------------------------------------------------


class TestSubagentHook:
    @pytest.mark.asyncio
    async def test_before_execute_tools_logs(self, tmp_path):
        hook = _SubagentHook("t1")
        tool_call = MagicMock()
        tool_call.name = "read_file"
        tool_call.arguments = {"path": "/tmp/test"}
        ctx = _make_hook_context(tool_calls=[tool_call])
        # Should not raise
        await hook.before_execute_tools(ctx)

    @pytest.mark.asyncio
    async def test_after_iteration_updates_status(self):
        status = SubagentStatus(
            task_id="t1",
            label="test",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("t1", status)
        ctx = _make_hook_context(
            iteration=3,
            tool_events=[{"name": "read_file", "status": "ok", "detail": ""}],
            usage={"prompt_tokens": 100},
        )
        await hook.after_iteration(ctx)
        assert status.iteration == 3
        assert len(status.tool_events) == 1
        assert status.usage == {"prompt_tokens": 100}

    @pytest.mark.asyncio
    async def test_after_iteration_no_status_noop(self):
        hook = _SubagentHook("t1", status=None)
        ctx = _make_hook_context(iteration=5)
        # Should not raise
        await hook.after_iteration(ctx)

    @pytest.mark.asyncio
    async def test_after_iteration_sets_error(self):
        status = SubagentStatus(
            task_id="t1",
            label="test",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("t1", status)
        ctx = _make_hook_context(error="something broke")
        await hook.after_iteration(ctx)
        assert status.error == "something broke"


# ---------------------------------------------------------------------------
# _SubagentHook activity forwarding (upgrade 1: subagent activity stream)
# ---------------------------------------------------------------------------


class TestSubagentHookActivity:
    """Verify the hook publishes _subagent_activity outbound frames when bus
    and origin routing are provided, and stays silent otherwise."""

    @pytest.mark.asyncio
    async def test_before_execute_tools_publishes_activity(self):
        bus = MessageBus()
        status = SubagentStatus(
            task_id="t1",
            label="researcher",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook(
            "t1",
            status,
            bus=bus,
            origin_channel="websocket",
            origin_chat_id="chat-1",
        )
        tool_call = MagicMock()
        tool_call.name = "read_file"
        tool_call.arguments = {"path": "/tmp/test"}
        ctx = _make_hook_context(tool_calls=[tool_call])
        await hook.before_execute_tools(ctx)

        msg = bus.outbound.get_nowait()
        assert msg.channel == "websocket"
        assert msg.chat_id == "chat-1"
        assert msg.metadata["_subagent_activity"] is True
        assert msg.metadata["_subagent_label"] == "researcher"
        assert msg.metadata["_subagent_task_id"] == "t1"
        assert msg.metadata["_progress"] is True
        assert "[researcher] calling read_file" in msg.content

    @pytest.mark.asyncio
    async def test_after_iteration_publishes_tool_events(self):
        bus = MessageBus()
        status = SubagentStatus(
            task_id="t2",
            label="worker",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook(
            "t2",
            status,
            bus=bus,
            origin_channel="websocket",
            origin_chat_id="chat-2",
        )
        ctx = _make_hook_context(
            iteration=1,
            tool_events=[
                {"name": "read_file", "status": "ok", "detail": "10 lines"},
                {"name": "exec", "status": "error", "detail": "timeout"},
            ],
        )
        await hook.after_iteration(ctx)

        # Two tool events → two outbound activity messages.
        first = bus.outbound.get_nowait()
        second = bus.outbound.get_nowait()
        assert first.metadata["_subagent_activity"] is True
        assert second.metadata["_subagent_activity"] is True
        assert "read_file" in first.content
        assert "ok" in first.content
        assert "exec" in second.content
        assert "error" in second.content

    @pytest.mark.asyncio
    async def test_no_bus_no_publish(self):
        """Hook without bus must not publish (backward-compat with _run_subagent)."""
        status = SubagentStatus(
            task_id="t3",
            label="solo",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("t3", status)  # no bus, no origin
        tool_call = MagicMock()
        tool_call.name = "read_file"
        tool_call.arguments = {}
        ctx = _make_hook_context(tool_calls=[tool_call])
        await hook.before_execute_tools(ctx)
        await hook.after_iteration(ctx)
        # No bus to inspect; just verify no exception was raised.

    @pytest.mark.asyncio
    async def test_emit_reasoning_publishes_activity(self):
        bus = MessageBus()
        status = SubagentStatus(
            task_id="t4",
            label="thinker",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook(
            "t4",
            status,
            bus=bus,
            origin_channel="websocket",
            origin_chat_id="chat-4",
        )
        await hook.emit_reasoning("analyzing the problem")
        msg = bus.outbound.get_nowait()
        assert msg.metadata["_subagent_activity"] is True
        assert "analyzing the problem" in msg.content
        assert "thinking" in msg.content

    @pytest.mark.asyncio
    async def test_emit_reasoning_empty_no_publish(self):
        bus = MessageBus()
        status = SubagentStatus(
            task_id="t5",
            label="quiet",
            task_description="do",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook(
            "t5",
            status,
            bus=bus,
            origin_channel="websocket",
            origin_chat_id="chat-5",
        )
        await hook.emit_reasoning(None)
        await hook.emit_reasoning("")
        assert bus.outbound.empty()


# ---------------------------------------------------------------------------
# make_session_key / session_key_base (upgrade 2: session namespace)
# ---------------------------------------------------------------------------


class TestMakeSessionKey:
    def test_legacy_form_without_agent_id(self):
        assert make_session_key("websocket", "chat-1") == "websocket:chat-1"

    def test_namespaced_form_with_agent_id(self):
        key = make_session_key("websocket", "chat-1", "sub:abc123")
        assert key == "websocket:chat-1#sub:abc123"

    def test_base_recovery_from_namespaced_key(self):
        key = make_session_key("cli", "direct", "sub:xyz")
        assert session_key_base(key) == "cli:direct"

    def test_base_recovery_from_legacy_key(self):
        # Legacy keys without ``#`` round-trip through session_key_base unchanged.
        assert session_key_base("websocket:chat-1") == "websocket:chat-1"

    def test_base_recovery_strips_only_first_namespace(self):
        # Only the first ``#`` splits; a base that itself contains ``#`` (which
        # is unusual but possible if a parent session was already namespaced)
        # preserves the rest. This matches the documented contract
        # ``split("#", 1)[0]``.
        assert session_key_base("a:b#sub:1#extra") == "a:b"


# ---------------------------------------------------------------------------
# _run_subagent_direct session isolation (upgrade 2)
# ---------------------------------------------------------------------------


class TestRunSubagentDirectSessionIsolation:
    @pytest.mark.asyncio
    async def test_uses_namespaced_session_key(self, tmp_path):
        """The runner must receive a namespaced session key so the subagent's
        consolidation history is isolated from the parent session."""
        sm = _manager(tmp_path)
        captured_spec = {}

        async def _capture_run(spec):
            captured_spec["session_key"] = spec.session_key
            captured_spec["hook"] = spec.hook
            return AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )

        sm.runner.run = _capture_run
        status = SubagentStatus(
            task_id="abc123",
            label="worker",
            task_description="do",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "abc123",
            "do task",
            "worker",
            {"channel": "websocket", "chat_id": "chat-1", "session_key": "websocket:chat-1"},
            status,
        )
        # Subagent session key must be namespaced and contain the task_id.
        assert captured_spec["session_key"] == "websocket:chat-1#sub:abc123"

    @pytest.mark.asyncio
    async def test_hook_receives_bus_and_origin(self, tmp_path):
        """The hook must be constructed with bus + origin so activity events
        are forwarded to subscribed clients."""
        sm = _manager(tmp_path)
        captured_hook = {}

        async def _capture_run(spec):
            captured_hook["hook"] = spec.hook
            return AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )

        sm.runner.run = _capture_run
        status = SubagentStatus(
            task_id="t9",
            label="researcher",
            task_description="do",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "t9",
            "do task",
            "researcher",
            {"channel": "websocket", "chat_id": "chat-9", "session_key": "websocket:chat-9"},
            status,
        )
        hook = captured_hook["hook"]
        assert hook._bus is sm.bus
        assert hook._origin_channel == "websocket"
        assert hook._origin_chat_id == "chat-9"
        assert hook._label == "researcher"

    @pytest.mark.asyncio
    async def test_llm_timeout_uses_parent_session_key(self, tmp_path):
        """The LLM wall timeout must be looked up against the parent session
        key, not the namespaced subagent key, so per-session limits still apply."""
        seen_keys = []

        def _timeout_lookup(key):
            seen_keys.append(key)
            return None

        sm = _manager(
            tmp_path,
            llm_wall_timeout_for_session=_timeout_lookup,
        )

        async def _noop_run(spec):
            return AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )

        sm.runner.run = _noop_run
        status = SubagentStatus(
            task_id="t10",
            label="worker",
            task_description="do",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "t10",
            "do task",
            "worker",
            {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
            status,
        )
        assert seen_keys == ["cli:direct"]


# ---------------------------------------------------------------------------
# Merged from test_subagent.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_uses_tool_loader():
    """Verify subagent registers tools via ToolLoader, not hard-coded imports."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=Path("/tmp"),
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )
    tools = sm._build_tools()
    assert tools.has("read_file")
    assert tools.has("write_file")
    assert not tools.has("message")
    assert not tools.has("spawn")


@pytest.mark.asyncio
async def test_subagent_build_tools_isolates_file_read_state(tmp_path):
    """Each spawned subagent needs a fresh file-state cache."""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )

    first_read = sm._build_tools().get("read_file")
    second_read = sm._build_tools().get("read_file")

    assert first_read is not second_read
    assert (await first_read.execute(path="note.txt")).startswith("1| hello")
    second_result = await second_read.execute(path="note.txt")
    assert second_result.startswith("1| hello")
    assert "File unchanged" not in second_result


# ---------------------------------------------------------------------------
# TurnBudget factory injection (fix: main-turn budget escape via subagents)
# ---------------------------------------------------------------------------


def _ok_result() -> AgentRunResult:
    return AgentRunResult(final_content="done", messages=[], stop_reason="completed")


class TestTurnBudgetFactory:
    """SubagentManager 必须把主循环的预算工厂接到每次子代理 run 上。"""

    @pytest.mark.asyncio
    async def test_factory_injected_spec_gets_budget_spawn(self, tmp_path):
        calls: list[object] = []

        def _factory():
            budget = object()
            calls.append(budget)
            return budget

        sm = _manager(tmp_path, turn_budget_factory=_factory)
        captured_specs = []

        async def _capture_run(spec):
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run
        await sm.spawn("do task")
        await asyncio.gather(*list(sm._running_tasks.values()))

        assert len(calls) == 1
        assert captured_specs[0].turn_budget is not None

    @pytest.mark.asyncio
    async def test_factory_injected_spec_gets_budget_direct(self, tmp_path):
        calls: list[object] = []

        def _factory():
            budget = object()
            calls.append(budget)
            return budget

        sm = _manager(tmp_path, turn_budget_factory=_factory)
        captured_specs = []

        async def _capture_run(spec):
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run
        status = SubagentStatus(
            task_id="t-bud",
            label="worker",
            task_description="do task",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "t-bud",
            "do task",
            "worker",
            {"channel": "cli", "chat_id": "direct"},
            status,
        )

        assert len(calls) == 1
        assert captured_specs[0].turn_budget is not None

    @pytest.mark.asyncio
    async def test_no_factory_turn_budget_is_none(self, tmp_path):
        """不传工厂时保持旧行为：spec.turn_budget 为 None（向后兼容）。"""
        sm = _manager(tmp_path)
        captured_specs = []

        async def _capture_run(spec):
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run
        status = SubagentStatus(
            task_id="t-nofactory",
            label="worker",
            task_description="do task",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "t-nofactory",
            "do task",
            "worker",
            {"channel": "cli", "chat_id": "direct"},
            status,
        )

        assert captured_specs[0].turn_budget is None

    @pytest.mark.asyncio
    async def test_concurrent_subagents_get_independent_budgets(self, tmp_path):
        """工厂每次返回新对象；并发子代理各自持有独立的 TurnBudget 实例。"""
        created: list[object] = []

        def _factory():
            budget = object()
            created.append(budget)
            return budget

        sm = _manager(
            tmp_path,
            turn_budget_factory=_factory,
            max_concurrent_subagents=4,
        )
        captured_specs = []

        async def _capture_run(spec):
            await asyncio.sleep(0.05)  # 让两个 run 真正并发重叠
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run

        await sm.spawn("task-a", session_key="s1")
        await sm.spawn("task-b", session_key="s1")
        await asyncio.gather(*list(sm._running_tasks.values()))

        assert len(created) == 2
        assert len(captured_specs) == 2
        b1 = captured_specs[0].turn_budget
        b2 = captured_specs[1].turn_budget
        assert b1 is not None
        assert b2 is not None
        assert b1 is not b2


class TestHighRiskPolicyPropagation:
    """主循环的 high_risk_policy 必须传播到子代理 spec，否则 deny 可被 spawn 绕过。"""

    @pytest.mark.asyncio
    async def test_deny_policy_propagates_to_spawn_spec(self, tmp_path):
        sm = _manager(tmp_path, high_risk_policy="deny")
        captured_specs = []

        async def _capture_run(spec):
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run
        await sm.spawn("do task")
        await asyncio.gather(*list(sm._running_tasks.values()))

        assert captured_specs[0].high_risk_policy == "deny"

    @pytest.mark.asyncio
    async def test_deny_policy_propagates_to_direct_spec(self, tmp_path):
        sm = _manager(tmp_path, high_risk_policy="deny")
        captured_specs = []

        async def _capture_run(spec):
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run
        status = SubagentStatus(
            task_id="t-deny",
            label="worker",
            task_description="do task",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "t-deny",
            "do task",
            "worker",
            {"channel": "cli", "chat_id": "direct"},
            status,
        )

        assert captured_specs[0].high_risk_policy == "deny"

    @pytest.mark.asyncio
    async def test_default_policy_is_allow(self, tmp_path):
        """默认构造（不传 high_risk_policy）保持旧行为：spec 为 allow。"""
        sm = _manager(tmp_path)
        captured_specs = []

        async def _capture_run(spec):
            captured_specs.append(spec)
            return _ok_result()

        sm.runner.run = _capture_run
        status = SubagentStatus(
            task_id="t-allow",
            label="worker",
            task_description="do task",
            started_at=time.monotonic(),
        )
        await sm._run_subagent_direct(
            "t-allow",
            "do task",
            "worker",
            {"channel": "cli", "chat_id": "direct"},
            status,
        )

        assert captured_specs[0].high_risk_policy == "allow"
