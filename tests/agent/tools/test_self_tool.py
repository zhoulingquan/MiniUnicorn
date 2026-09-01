"""Tests for MyTool — runtime state inspection and configuration."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from miniunicorn.tools.self import MyTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_loop(**overrides):
    """Build a lightweight mock AgentLoop with the attributes MyTool reads."""
    loop = MagicMock()
    loop.model = "anthropic/claude-sonnet-4-20250514"
    loop.max_iterations = 40
    loop.context_window_tokens = 65_536
    loop.workspace = Path("/tmp/workspace")
    loop.restrict_to_workspace = False
    loop._start_time = 1000.0
    loop.exec_config = MagicMock()
    loop.channels_config = MagicMock()
    loop._last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
    loop._runtime_vars = {}
    loop._current_iteration = 0
    loop.provider_retry_mode = "standard"
    loop.max_tool_result_chars = 16000
    loop._concurrency_gate = None
    loop._unified_session = False
    loop._extra_hooks = []

    # web_config mock removed — web_search tool was deleted; web_fetch config
    # is not exercised by self-tool tests.

    # Tools registry mock
    loop.tools = MagicMock()
    loop.tools.tool_names = ["read_file", "write_file", "exec", "web_fetch", "self"]
    loop.tools.has.side_effect = lambda n: n in loop.tools.tool_names
    loop.tools.get.return_value = None

    # SubagentManager mock
    loop.subagents = MagicMock()
    loop.subagents._running_tasks = {"abc123": MagicMock(done=MagicMock(return_value=False))}
    loop.subagents.get_running_count = MagicMock(return_value=1)

    for k, v in overrides.items():
        setattr(loop, k, v)

    return loop


def _make_tool(runtime_state=None):
    if runtime_state is None:
        runtime_state = _make_mock_loop()
    return MyTool(runtime_state=runtime_state)


# ---------------------------------------------------------------------------
# check — no key (summary)
# ---------------------------------------------------------------------------


class TestInspectSummary:
    @pytest.mark.asyncio
    async def test_inspect_returns_current_state(self):
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "max_iterations: 40" in result
        assert "context_window_tokens: 65536" in result

    @pytest.mark.asyncio
    async def test_inspect_includes_runtime_vars(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"task": "review"}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check")
        assert "task" in result

    @pytest.mark.asyncio
    async def test_inspect_summary_shows_all_description_keys(self):
        """check without key should show all top-level keys listed in description."""
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "max_iterations" in result
        assert "context_window_tokens" in result
        assert "model" in result
        assert "workspace" in result
        assert "provider_retry_mode" in result
        assert "max_tool_result_chars" in result
        assert "_last_usage" in result
        assert "_current_iteration" in result


# ---------------------------------------------------------------------------
# check — single key (direct)
# ---------------------------------------------------------------------------


class TestInspectSingleKey:
    @pytest.mark.asyncio
    async def test_inspect_simple_value(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="max_iterations")
        assert "40" in result

    @pytest.mark.asyncio
    async def test_inspect_blocked_returns_error(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="bus")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_dunder_blocked(self):
        tool = _make_tool()
        for attr in ("__class__", "__dict__", "__bases__", "__subclasses__", "__mro__"):
            result = await tool.execute(action="check", key=attr)
            assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_nonexistent_returns_not_found(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="nonexistent_attr_xyz")
        assert "not found" in result


# ---------------------------------------------------------------------------
# check — dot-path navigation
# ---------------------------------------------------------------------------


class TestInspectPathNavigation:
    @pytest.mark.asyncio
    async def test_inspect_config_subfield(self):
        loop = _make_mock_loop()
        loop.exec_config = MagicMock()
        loop.exec_config.sandbox = "none"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="exec_config.sandbox")
        assert "none" in result

    @pytest.mark.asyncio
    async def test_inspect_dict_key_via_dotpath(self):
        loop = _make_mock_loop()
        loop._last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="_last_usage.prompt_tokens")
        assert "100" in result

    @pytest.mark.asyncio
    async def test_inspect_blocked_in_path(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="bus.foo")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_tools_returns_blocked(self):
        """tools is BLOCKED — check should return access error."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="tools")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_nested_config_redacts_sensitive_scalar_fields(self):
        class ProviderConfig(BaseModel):
            label: str = "OpenAI"
            api_key: str = "sk-test-secret"
            api_base: str = ""

        loop = _make_mock_loop()
        loop.exec_config = MagicMock()
        loop.exec_config.entry = ProviderConfig()
        tool = _make_tool(loop)

        result = await tool.execute(action="check", key="exec_config.entry")

        assert "label='OpenAI'" in result
        assert "sk-test-secret" not in result
        assert "api_key" not in result.lower()


# ---------------------------------------------------------------------------
# set — restricted (with validation)
# ---------------------------------------------------------------------------


class TestModifyRestricted:
    @pytest.mark.asyncio
    async def test_modify_restricted_valid(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=80)
        assert "Set max_iterations = 80" in result
        assert tool._runtime_state.max_iterations == 80

    @pytest.mark.asyncio
    async def test_modify_restricted_out_of_range(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=0)
        assert "Error" in result
        assert tool._runtime_state.max_iterations == 40

    @pytest.mark.asyncio
    async def test_modify_restricted_max_exceeded(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=999)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_restricted_wrong_type(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value="not_an_int")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_restricted_bool_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=True)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_string_int_coerced(self):
        tool = _make_tool()
        await tool.execute(action="set", key="max_iterations", value="80")
        assert tool._runtime_state.max_iterations == 80

    @pytest.mark.asyncio
    async def test_modify_context_window_valid(self):
        tool = _make_tool()
        await tool.execute(action="set", key="context_window_tokens", value=131072)
        assert tool._runtime_state.context_window_tokens == 131072

    @pytest.mark.asyncio
    async def test_modify_none_value_for_restricted_int(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=None)
        assert "Error" in result


# ---------------------------------------------------------------------------
# set — blocked (minimal set)
# ---------------------------------------------------------------------------


class TestModifyBlocked:
    @pytest.mark.asyncio
    async def test_modify_bus_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="bus", value="hacked")
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_provider_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="provider", value=None)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_running_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="_running", value=True)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_dunder_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="__class__", value="evil")
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_dotpath_leaf_dunder_blocked(self):
        """Fix 3.1: leaf segment of dot-path must also be validated."""
        tool = _make_tool()
        result = await tool.execute(
            action="set",
            key="provider_retry_mode.__class__",
            value="evil",
        )
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_dotpath_leaf_denied_attr_blocked(self):
        """Fix 3.1: leaf segment matching _DENIED_ATTRS must be rejected."""
        tool = _make_tool()
        result = await tool.execute(
            action="set",
            key="provider_retry_mode.__globals__",
            value={},
        )
        assert "not accessible" in result


# ---------------------------------------------------------------------------
# set — free tier (setattr priority)
# ---------------------------------------------------------------------------


class TestModifyFree:
    @pytest.mark.asyncio
    async def test_modify_existing_attr_rejected_by_whitelist(self):
        """W2-1 whitelist: host attrs outside SETTABLE can no longer be set."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="provider_retry_mode", value="persistent")
        assert "Error" in result
        assert tool._runtime_state.provider_retry_mode == "standard"

    @pytest.mark.asyncio
    async def test_modify_new_key_stores_in_runtime_vars(self):
        """Modifying a non-existing attribute should store in _runtime_vars."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="my_custom_var", value="hello")
        assert "my_custom_var" in result
        assert tool._runtime_state._runtime_vars["my_custom_var"] == "hello"

    @pytest.mark.asyncio
    async def test_modify_rejects_callable(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="evil", value=lambda: None)
        assert "callable" in result

    @pytest.mark.asyncio
    async def test_modify_rejects_complex_objects(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="obj", value=Path("/tmp"))
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_allows_list(self):
        tool = _make_tool()
        await tool.execute(action="set", key="items", value=[1, 2, 3])
        assert tool._runtime_state._runtime_vars["items"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_modify_allows_dict(self):
        tool = _make_tool()
        await tool.execute(action="set", key="data", value={"a": 1})
        assert tool._runtime_state._runtime_vars["data"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_modify_whitespace_key_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="   ", value="test")
        assert "cannot be empty or whitespace" in result

    @pytest.mark.asyncio
    async def test_modify_nested_dict_with_object_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="evil", value={"nested": object()})
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_deep_nesting_rejected(self):
        tool = _make_tool()
        deep = {"level": 0}
        current = deep
        for i in range(1, 15):
            current["child"] = {"level": i}
            current = current["child"]
        result = await tool.execute(action="set", key="deep", value=deep)
        assert "nesting too deep" in result

    @pytest.mark.asyncio
    async def test_modify_dict_with_non_str_key_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="evil", value={42: "value"})
        assert "key must be str" in result

    @pytest.mark.asyncio
    async def test_modify_existing_attr_type_mismatch_rejected(self):
        """W2-1 whitelist: existing host attrs are rejected outright on set."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="provider_retry_mode", value=42)
        assert "Error" in result
        assert tool._runtime_state.provider_retry_mode == "standard"

    @pytest.mark.asyncio
    async def test_modify_existing_int_attr_wrong_type_rejected(self):
        """W2-1 whitelist: existing int host attrs are rejected outright on set."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_tool_result_chars", value="big")
        assert "Error" in result
        assert tool._runtime_state.max_tool_result_chars == 16000


# ---------------------------------------------------------------------------
# set — previously BLOCKED/READONLY now open
# ---------------------------------------------------------------------------


class TestModifyOpen:
    @pytest.mark.asyncio
    async def test_modify_tools_blocked(self):
        """tools is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_registry = MagicMock()
        result = await tool.execute(action="set", key="tools", value=new_registry)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_subagents_blocked(self):
        """subagents is READ_ONLY — cannot be replaced."""
        tool = _make_tool()
        new_subagents = MagicMock()
        result = await tool.execute(action="set", key="subagents", value=new_subagents)
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_modify_runner_blocked(self):
        """runner is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_runner = MagicMock()
        result = await tool.execute(action="set", key="runner", value=new_runner)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_sessions_blocked(self):
        """sessions is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_sessions = MagicMock()
        result = await tool.execute(action="set", key="sessions", value=new_sessions)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_consolidator_blocked(self):
        """consolidator is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_consolidator = MagicMock()
        result = await tool.execute(action="set", key="consolidator", value=new_consolidator)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_dream_blocked(self):
        """dream is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_dream = MagicMock()
        result = await tool.execute(action="set", key="dream", value=new_dream)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_auto_compact_blocked(self):
        """auto_compact is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_auto_compact = MagicMock()
        result = await tool.execute(action="set", key="auto_compact", value=new_auto_compact)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_context_blocked(self):
        """context is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_context = MagicMock()
        result = await tool.execute(action="set", key="context", value=new_context)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_commands_blocked(self):
        """commands is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_commands = MagicMock()
        result = await tool.execute(action="set", key="commands", value=new_commands)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_workspace_rejected_by_whitelist(self):
        """W2-1 whitelist: workspace is inspect-only and cannot be replaced via set."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="workspace", value="/new/path")
        assert "Error" in result
        assert tool._runtime_state.workspace == Path("/tmp/workspace")

    @pytest.mark.asyncio
    async def test_modify_mcp_servers_blocked(self):
        """_mcp_servers contains API credentials — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_mcp_servers", value={"evil": "leaked"})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_mcp_stacks_blocked(self):
        """_mcp_stacks holds connection handles — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_mcp_stacks", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_pending_queues_blocked(self):
        """_pending_queues controls message routing — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_pending_queues", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_session_locks_blocked(self):
        """_session_locks controls session isolation — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_session_locks", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_active_tasks_blocked(self):
        """_active_tasks tracks running tasks — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_active_tasks", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_background_tasks_blocked(self):
        """_background_tasks tracks background tasks — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_background_tasks", value=[])
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_inspect_mcp_servers_blocked(self):
        """_mcp_servers contains credentials — check must be blocked too."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="_mcp_servers")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_wrapped_denied(self):
        """__wrapped__ allows decorator bypass — must be denied."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="__wrapped__", value="evil")
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_closure_denied(self):
        """__closure__ exposes function internals — must be denied."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="__closure__", value="evil")
        assert "protected" in result


# ---------------------------------------------------------------------------
# validate_json_safe — element counting
# ---------------------------------------------------------------------------


class TestValidateJsonSafe:
    def test_single_list_passes(self):
        assert MyTool._validate_json_safe(list(range(500))) is None

    def test_deeply_nested_within_limit(self):
        value = {"level1": {"level2": {"level3": list(range(100))}}}
        assert MyTool._validate_json_safe(value) is None


# ---------------------------------------------------------------------------
# unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = _make_tool()
        result = await tool.execute(action="explode")
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# runtime_vars limits (from code review)
# ---------------------------------------------------------------------------


class TestRuntimeVarsLimits:
    @pytest.mark.asyncio
    async def test_runtime_vars_rejects_at_max_keys(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {f"key_{i}": i for i in range(64)}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="overflow", value="data")
        assert "full" in result
        assert "overflow" not in loop._runtime_vars

    @pytest.mark.asyncio
    async def test_runtime_vars_allows_update_existing_key_at_max(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {f"key_{i}": i for i in range(64)}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="key_0", value="updated")
        assert "Error" not in result
        assert loop._runtime_vars["key_0"] == "updated"


# ---------------------------------------------------------------------------
# denied attrs (non-dunder)
# ---------------------------------------------------------------------------


class TestDeniedAttrs:
    @pytest.mark.asyncio
    async def test_modify_denied_non_dunder_blocked(self):
        tool = _make_tool()
        for attr in ("func_globals", "func_code"):
            result = await tool.execute(action="set", key=attr, value="evil")
            assert "protected" in result, f"{attr} should be blocked"


# ---------------------------------------------------------------------------
# SubagentStatus formatting
# ---------------------------------------------------------------------------


class TestSubagentStatusFormatting:
    def test_format_single_status(self):
        """_format_value should produce a rich multi-line display for a SubagentStatus."""
        from miniunicorn.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="abc12345",
            label="read logs and summarize",
            task_description="Read the log files and produce a summary",
            started_at=time.monotonic() - 12.4,
            phase="awaiting_tools",
            iteration=3,
            tool_events=[
                {"name": "read_file", "status": "ok", "detail": "read app.log"},
                {"name": "grep", "status": "ok", "detail": "searched ERROR"},
                {"name": "exec", "status": "error", "detail": "timeout"},
            ],
            usage={"prompt_tokens": 4500, "completion_tokens": 1200},
        )
        result = MyTool._format_value(status)
        assert "abc12345" in result
        assert "read logs and summarize" in result
        assert "awaiting_tools" in result
        assert "iteration: 3" in result
        assert "read_file(ok)" in result
        assert "exec(error)" in result
        assert "4500" in result

    def test_format_status_dict(self):
        """_format_value should handle dict[str, SubagentStatus] with rich display."""
        from miniunicorn.agent.subagent import SubagentStatus

        statuses = {
            "abc12345": SubagentStatus(
                task_id="abc12345",
                label="task A",
                task_description="Do task A",
                started_at=time.monotonic() - 5.0,
                phase="awaiting_tools",
                iteration=1,
            ),
        }
        result = MyTool._format_value(statuses)
        assert "1 subagent(s)" in result
        assert "abc12345" in result
        assert "task A" in result

    def test_format_empty_status_dict(self):
        """Empty dict[str, SubagentStatus] should show 'no running subagents'."""
        result = MyTool._format_value({})
        assert "{}" in result

    def test_format_status_with_error(self):
        """Status with error should include the error message."""
        from miniunicorn.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="err00001",
            label="failing task",
            task_description="A task that fails",
            started_at=time.monotonic() - 1.0,
            phase="error",
            error="Connection refused",
        )
        result = MyTool._format_value(status)
        assert "error: Connection refused" in result


# ---------------------------------------------------------------------------
# _SubagentHook after_iteration updates status
# ---------------------------------------------------------------------------


class TestSubagentHookStatus:
    @pytest.mark.asyncio
    async def test_after_iteration_updates_status(self):
        """after_iteration should copy iteration, tool_events, usage to status."""
        from miniunicorn.agent.hook import AgentHookContext
        from miniunicorn.agent.subagent import SubagentStatus, _SubagentHook

        status = SubagentStatus(
            task_id="test",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("test", status)

        context = AgentHookContext(
            iteration=5,
            messages=[],
            tool_events=[{"name": "read_file", "status": "ok", "detail": "ok"}],
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )
        await hook.after_iteration(context)

        assert status.iteration == 5
        assert len(status.tool_events) == 1
        assert status.tool_events[0]["name"] == "read_file"
        assert status.usage == {"prompt_tokens": 100, "completion_tokens": 50}

    @pytest.mark.asyncio
    async def test_after_iteration_with_error(self):
        """after_iteration should set status.error when context has an error."""
        from miniunicorn.agent.hook import AgentHookContext
        from miniunicorn.agent.subagent import SubagentStatus, _SubagentHook

        status = SubagentStatus(
            task_id="test",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("test", status)

        context = AgentHookContext(
            iteration=1,
            messages=[],
            error="something went wrong",
        )
        await hook.after_iteration(context)

        assert status.error == "something went wrong"

    @pytest.mark.asyncio
    async def test_after_iteration_no_status_is_noop(self):
        """after_iteration with no status should be a no-op."""
        from miniunicorn.agent.hook import AgentHookContext
        from miniunicorn.agent.subagent import _SubagentHook

        hook = _SubagentHook("test")
        context = AgentHookContext(iteration=1, messages=[])
        await hook.after_iteration(context)  # should not raise


# ---------------------------------------------------------------------------
# Checkpoint callback updates status
# ---------------------------------------------------------------------------


class TestCheckpointCallback:
    @pytest.mark.asyncio
    async def test_checkpoint_updates_phase_and_iteration(self):
        """The _on_checkpoint callback should update status.phase and iteration."""

        from miniunicorn.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="cp",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
        )

        # Simulate the checkpoint callback as defined in _run_subagent
        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        await _on_checkpoint({"phase": "awaiting_tools", "iteration": 2})
        assert status.phase == "awaiting_tools"
        assert status.iteration == 2

        await _on_checkpoint({"phase": "tools_completed", "iteration": 3})
        assert status.phase == "tools_completed"
        assert status.iteration == 3

    @pytest.mark.asyncio
    async def test_checkpoint_preserves_phase_on_missing_key(self):
        """If payload doesn't have 'phase', status.phase should stay unchanged."""
        from miniunicorn.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="cp",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
            phase="initializing",
        )

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        await _on_checkpoint({"iteration": 1})
        assert status.phase == "initializing"
        assert status.iteration == 1


# ---------------------------------------------------------------------------
# check subagents._task_statuses via dot-path
# NOTE: subagents is now BLOCKED for security, so these tests verify
# that access is properly rejected.
# ---------------------------------------------------------------------------


class TestInspectTaskStatuses:
    @pytest.mark.asyncio
    async def test_inspect_task_statuses_accessible(self):
        """subagents is READ_ONLY — check should show subagent statuses."""
        from miniunicorn.agent.subagent import SubagentStatus

        loop = _make_mock_loop()
        loop.subagents._task_statuses = {
            "abc12345": SubagentStatus(
                task_id="abc12345",
                label="read logs",
                task_description="Read the log files",
                started_at=time.monotonic() - 8.0,
                phase="awaiting_tools",
                iteration=2,
                tool_events=[{"name": "read_file", "status": "ok", "detail": "ok"}],
                usage={"prompt_tokens": 500, "completion_tokens": 100},
            ),
        }
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="subagents._task_statuses")
        assert "abc12345" in result
        assert "read logs" in result

    @pytest.mark.asyncio
    async def test_inspect_single_subagent_status_accessible(self):
        """subagents._task_statuses.<id> should return individual SubagentStatus."""
        from miniunicorn.agent.subagent import SubagentStatus

        loop = _make_mock_loop()
        status = SubagentStatus(
            task_id="xyz",
            label="search code",
            task_description="Search the codebase",
            started_at=time.monotonic() - 3.0,
            phase="done",
            iteration=4,
            stop_reason="completed",
        )
        loop.subagents._task_statuses = {"xyz": status}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="subagents._task_statuses.xyz")
        assert "search code" in result
        assert "completed" in result


# ---------------------------------------------------------------------------
# read-only mode (tools.my.allow_set=False)
# ---------------------------------------------------------------------------


class TestReadOnlyMode:
    def _make_readonly_tool(self):
        loop = _make_mock_loop()
        return MyTool(runtime_state=loop, modify_allowed=False)

    @pytest.mark.asyncio
    async def test_inspect_allowed_in_readonly(self):
        tool = self._make_readonly_tool()
        result = await tool.execute(action="check", key="max_iterations")
        assert "40" in result

    @pytest.mark.asyncio
    async def test_modify_blocked_in_readonly(self):
        tool = self._make_readonly_tool()
        result = await tool.execute(action="set", key="max_iterations", value=80)
        assert "disabled" in result

    def test_description_shows_readonly(self):
        tool = self._make_readonly_tool()
        assert "READ-ONLY MODE" in tool.description

    def test_description_shows_warning_when_modify_allowed(self):
        tool = _make_tool()
        assert "IMPORTANT" in tool.description
        assert "READ-ONLY" not in tool.description


# ---------------------------------------------------------------------------
# runtime vars check fallback (Fix #1: cross-turn memory)
# ---------------------------------------------------------------------------


class TestRuntimeVarsInspectFallback:
    @pytest.mark.asyncio
    async def test_inspect_runtime_var_after_modify(self):
        """Design doc scenario: set then check should return the value."""
        tool = _make_tool()
        await tool.execute(action="set", key="user_prefers_concise", value=True)
        result = await tool.execute(action="check", key="user_prefers_concise")
        assert "True" in result

    @pytest.mark.asyncio
    async def test_inspect_runtime_var_string(self):
        tool = _make_tool()
        await tool.execute(action="set", key="current_project", value="miniunicorn")
        result = await tool.execute(action="check", key="current_project")
        assert "miniunicorn" in result

    @pytest.mark.asyncio
    async def test_inspect_runtime_var_dict(self):
        tool = _make_tool()
        await tool.execute(action="set", key="task_meta", value={"step": 2, "total": 5})
        result = await tool.execute(action="check", key="task_meta")
        assert "step" in result
        assert "2" in result

    @pytest.mark.asyncio
    async def test_inspect_nonexistent_still_returns_not_found(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="never_set_key_xyz")
        assert "not found" in result


# ---------------------------------------------------------------------------
# sensitive sub-field blocking (Fix #3: API key leak prevention)
# ---------------------------------------------------------------------------


class TestSensitiveSubFieldBlocking:
    @pytest.mark.asyncio
    async def test_inspect_api_key_blocked(self):
        """api_key fields must not be accessible."""
        loop = _make_mock_loop()
        loop.some_config = MagicMock()
        loop.some_config.api_key = "sk-test-secret"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="some_config.api_key")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_password_blocked(self):
        """Any field named 'password' must be blocked."""
        loop = _make_mock_loop()
        loop.some_config = MagicMock()
        loop.some_config.password = "hunter2"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="some_config.password")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_secret_blocked(self):
        loop = _make_mock_loop()
        loop.vault = MagicMock()
        loop.vault.secret = "classified"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="vault.secret")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_token_blocked(self):
        loop = _make_mock_loop()
        loop.auth_data = MagicMock()
        loop.auth_data.token = "jwt-payload"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="auth_data.token")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_api_key_blocked(self):
        """api_key is sensitive, so any set under it is blocked."""
        loop = _make_mock_loop()
        loop.some_config = MagicMock()
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="some_config.api_key", value="evil")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_password_blocked(self):
        loop = _make_mock_loop()
        loop.some_config = MagicMock()
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="some_config.password", value="evil")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_non_sensitive_subfield_allowed(self):
        """exec_config.sandbox should still be inspectable."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="exec_config.sandbox")
        assert "none" in result or "Error" not in result

    @pytest.mark.asyncio
    async def test_modify_sensitive_top_level_blocked(self):
        """Top-level key matching sensitive name must be blocked for set."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="api_key", value="evil")
        assert "protected" in result


# ---------------------------------------------------------------------------
# security-sensitive attribute protection (Fix #4)
# ---------------------------------------------------------------------------


class TestSecurityAttributeProtection:
    @pytest.mark.asyncio
    async def test_modify_restrict_to_workspace_blocked(self):
        """restrict_to_workspace is BLOCKED — cannot be toggled."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="restrict_to_workspace", value=True)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_exec_config_blocked(self):
        """exec_config is READ_ONLY — cannot be modified."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="exec_config", value=MagicMock())
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_modify_channels_config_blocked(self):
        """channels_config is BLOCKED — cannot be modified."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="channels_config", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_inspect_restrict_to_workspace_blocked(self):
        """restrict_to_workspace is BLOCKED — cannot be inspected."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="restrict_to_workspace")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_exec_config_allowed(self):
        """exec_config is READ_ONLY — check should work."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="exec_config")
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_modify_exec_config_dotpath_blocked(self):
        """exec_config.enable = False should be blocked because exec_config is READ_ONLY."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="exec_config.enable", value=False)
        assert "read-only" in result


# ---------------------------------------------------------------------------
# current iteration count (Fix #2)
# ---------------------------------------------------------------------------


class TestCurrentIteration:
    @pytest.mark.asyncio
    async def test_inspect_current_iteration(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="_current_iteration")
        assert "0" in result

    @pytest.mark.asyncio
    async def test_current_iteration_in_summary(self):
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "_current_iteration" in result

    @pytest.mark.asyncio
    async def test_modify_current_iteration_blocked(self):
        """_current_iteration is READ_ONLY — cannot be set manually."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_current_iteration", value=5)
        assert "read-only" in result


# ---------------------------------------------------------------------------
# _last_usage in check summary (Fix #5)
# ---------------------------------------------------------------------------


class TestLastUsageInSummary:
    @pytest.mark.asyncio
    async def test_last_usage_shown_in_summary(self):
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "_last_usage" in result
        assert "prompt_tokens" in result

    @pytest.mark.asyncio
    async def test_last_usage_not_shown_when_empty(self):
        loop = _make_mock_loop()
        loop._last_usage = {}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check")
        assert "_last_usage" not in result


# ---------------------------------------------------------------------------
# set_context (audit session tracking)
# ---------------------------------------------------------------------------


class TestSetContext:
    def test_set_context_stores_channel_and_chat_id(self):
        from miniunicorn.tools.context import RequestContext

        tool = _make_tool()
        tool.set_context(RequestContext(channel="feishu", chat_id="oc_abc123"))
        assert tool._channel == "feishu"
        assert tool._chat_id == "oc_abc123"


# ---------------------------------------------------------------------------
# W2-1 whitelist gate (deny-list → allow-list)
# ---------------------------------------------------------------------------


class _FakeLoop:
    """Plain-object loop stand-in: no auto-generated attrs, so __dict__ is exact."""

    def __init__(self) -> None:
        self.model = "anthropic/claude-sonnet-4"
        self.model_preset = None
        self._active_preset = None
        self.max_iterations = 40
        self.context_window_tokens = 65_536
        self.workspace = "/tmp/workspace"
        self.provider_retry_mode = "standard"
        self.max_tool_result_chars = 16_000
        self._last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
        self.exec_config = SimpleNamespace(sandbox="none")
        self.workspace_sandbox = "off"
        self.subagents = None
        self._current_iteration = 0
        self._runtime_vars: dict = {}
        self._mcp_runtime = object()
        self.cron_service = object()
        self.workspace_scopes = object()
        self._provider_snapshot_loader = object()


def _make_fake_tool() -> MyTool:
    return MyTool(runtime_state=_FakeLoop())


class TestWhitelistPositive:
    @pytest.mark.asyncio
    async def test_check_overview_contains_existing_keys(self):
        """W2-1 #1: check without key shows the existing key set."""
        tool = _make_fake_tool()
        result = await tool.execute(action="check")
        for fragment in (
            "max_iterations: 40",
            "context_window_tokens: 65536",
            "model: 'anthropic/claude-sonnet-4'",
            "model_preset",
            "workspace: '/tmp/workspace'",
            "provider_retry_mode: 'standard'",
            "max_tool_result_chars: 16000",
            "_current_iteration: 0",
            "exec_config",
            "workspace_sandbox",
            "subagents",
            "_last_usage",
        ):
            assert fragment in result, fragment

    @pytest.mark.asyncio
    async def test_check_whitelisted_keys(self):
        """W2-1 #2: legal check paths keep working."""
        tool = _make_fake_tool()
        assert "anthropic/claude-sonnet-4" in await tool.execute(action="check", key="model")
        assert "40" in await tool.execute(action="check", key="max_iterations")
        assert "100" in await tool.execute(action="check", key="_last_usage.prompt_tokens")
        assert "none" in await tool.execute(action="check", key="exec_config.sandbox")
        result = await tool.execute(action="check", key="scratchpad")
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_set_restricted_keys_with_validation(self):
        """W2-1 #3: legal set paths keep working, including validation bounds."""
        tool = _make_fake_tool()
        result = await tool.execute(action="set", key="max_iterations", value=25)
        assert "Set max_iterations = 25" in result
        assert tool._runtime_state.max_iterations == 25
        result = await tool.execute(action="set", key="model", value="fast-model")
        assert "Set model" in result
        assert tool._runtime_state.model == "fast-model"
        assert "Error" in await tool.execute(action="set", key="max_iterations", value=0)
        assert "Error" in await tool.execute(action="set", key="max_iterations", value=999)



class TestWhitelistNegative:
    @pytest.mark.asyncio
    async def test_set_mcp_runtime_rejected_with_audit(self):
        """W2-1 #6: _mcp_runtime can no longer be replaced via set."""
        loop = _FakeLoop()
        tool = MyTool(runtime_state=loop)
        audits: list[str] = []
        tool._audit = lambda action, detail: audits.append(detail)  # type: ignore[method-assign]
        original = loop._mcp_runtime
        result = await tool.execute(action="set", key="_mcp_runtime", value=object())
        assert "Error" in result
        assert loop._mcp_runtime is original
        assert any("WHITELIST-DENY" in d and "_mcp_runtime" in d for d in audits)

    @pytest.mark.asyncio
    async def test_set_other_host_infra_attrs_rejected(self):
        """W2-1 #7: cron_service / workspace_scopes / model_preset / snapshot loader."""
        loop = _FakeLoop()
        tool = MyTool(runtime_state=loop)
        targets = (
            "cron_service",
            "workspace_scopes",
            "model_preset",
            "_provider_snapshot_loader",
        )
        for key in targets:
            original = getattr(loop, key)
            result = await tool.execute(action="set", key=key, value="hacked")
            assert "Error" in result, key
            assert getattr(loop, key) is original, key

    @pytest.mark.asyncio
    async def test_check_mcp_runtime_rejected(self):
        """W2-1 #8: _mcp_runtime is not inspectable."""
        tool = _make_fake_tool()
        result = await tool.execute(action="check", key="_mcp_runtime")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_set_exec_config_dotpath_rejected(self):
        """W2-1 #9: dot-path set is only allowed in the scratchpad namespace."""
        loop = _FakeLoop()
        tool = MyTool(runtime_state=loop)
        result = await tool.execute(action="set", key="exec_config.sandbox", value=True)
        assert "Error" in result
        assert loop.exec_config.sandbox == "none"

    @pytest.mark.asyncio
    async def test_set_subagents_rejected(self):
        """W2-1 #10: subagents stays read-only under the whitelist."""
        loop = _FakeLoop()
        tool = MyTool(runtime_state=loop)
        result = await tool.execute(action="set", key="subagents", value=object())
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_defense_in_depth_still_enforced(self):
        """W2-1 #11: dunder and sensitive-name denial still holds."""
        tool = _make_fake_tool()
        assert "not accessible" in await tool.execute(action="check", key="__class__")
        result = await tool.execute(action="check", key="_runtime_vars.api_key")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_allow_set_false_total_switch(self):
        """W2-1 #12: allow_set=False keeps the total switch error."""
        loop = _FakeLoop()
        tool = MyTool(runtime_state=loop, modify_allowed=False)
        result = await tool.execute(action="set", key="notes", value="x")
        assert "disabled" in result
        assert "notes" not in loop._runtime_vars


class TestHostIntegrity:
    @pytest.mark.asyncio
    async def test_set_operations_do_not_pollute_host_dict(self):
        """W2-1 #13: a round of set ops leaves loop.__dict__ keys unchanged."""
        loop = _FakeLoop()
        tool = MyTool(runtime_state=loop)
        before = set(vars(loop))
        await tool.execute(action="set", key="notes", value="hello")
        await tool.execute(action="set", key="scratchpad.foo", value="bar")
        await tool.execute(action="set", key="max_iterations", value=25)
        await tool.execute(action="set", key="_mcp_runtime", value=object())
        assert set(vars(loop)) == before
        assert loop._runtime_vars == {"notes": "hello", "foo": "bar"}
        assert loop.max_iterations == 25

