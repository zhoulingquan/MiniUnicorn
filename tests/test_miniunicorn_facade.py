"""Tests for the MiniUnicorn programmatic facade."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.miniunicorn import Miniunicorn, RunResult


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    data = {
        "providers": {"openrouter": {"apiKey": "sk-test-key"}},
        "agents": {"defaults": {"model": "openai/gpt-4.1"}},
    }
    if overrides:
        data.update(overrides)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def test_from_config_missing_file():
    with pytest.raises(FileNotFoundError):
        Miniunicorn.from_config("/nonexistent/config.json")


def test_from_config_creates_instance(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)
    assert bot._loop is not None
    assert bot._loop.workspace == tmp_path


def test_from_config_default_path():
    from miniunicorn.config.schema import Config

    with (
        patch("miniunicorn.config.loader.load_config") as mock_load,
        patch("miniunicorn.providers.factory.make_provider") as mock_prov,
    ):
        mock_load.return_value = Config()
        mock_prov.return_value = MagicMock()
        mock_prov.return_value.get_default_model.return_value = "test"
        mock_prov.return_value.generation.max_tokens = 4096
        Miniunicorn.from_config()
        mock_load.assert_called_once_with(None)


@pytest.mark.asyncio
async def test_run_returns_result(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    from miniunicorn.bus.events import OutboundMessage

    mock_response = OutboundMessage(channel="cli", chat_id="direct", content="Hello back!")
    bot._loop.process_direct = AsyncMock(return_value=mock_response)

    result = await bot.run("hi")

    assert isinstance(result, RunResult)
    assert result.content == "Hello back!"
    # Hooks are now passed via the hooks= parameter (one capture hook).
    call_kwargs = bot._loop.process_direct.await_args
    assert call_kwargs.args[0] == "hi"
    assert call_kwargs.kwargs["session_key"] == "sdk:default"
    passed_hooks = call_kwargs.kwargs["hooks"]
    assert len(passed_hooks) == 1  # SDKCaptureHook only


@pytest.mark.asyncio
async def test_run_with_hooks(tmp_path):
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    class TestHook(AgentHook):
        async def before_iteration(self, context: AgentHookContext) -> None:
            pass

    mock_response = OutboundMessage(channel="cli", chat_id="direct", content="done")
    bot._loop.process_direct = AsyncMock(return_value=mock_response)

    user_hook = TestHook()
    result = await bot.run("hi", hooks=[user_hook])

    assert result.content == "done"
    # The SDK no longer mutates _extra_hooks.
    assert bot._loop._extra_hooks == []
    # The user hook is passed alongside the capture hook.
    call_kwargs = bot._loop.process_direct.await_args
    passed_hooks = call_kwargs.kwargs["hooks"]
    assert len(passed_hooks) == 2
    assert passed_hooks[1] is user_hook


@pytest.mark.asyncio
async def test_run_does_not_mutate_extra_hooks_on_error(tmp_path):
    """Errors in process_direct must not leak hooks into _extra_hooks."""
    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    from miniunicorn.agent.hook import AgentHook

    bot._loop.process_direct = AsyncMock(side_effect=RuntimeError("boom"))
    original_hooks = list(bot._loop._extra_hooks)

    with pytest.raises(RuntimeError):
        await bot.run("hi", hooks=[AgentHook()])

    assert bot._loop._extra_hooks == original_hooks


@pytest.mark.asyncio
async def test_run_none_response(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)
    bot._loop.process_direct = AsyncMock(return_value=None)

    result = await bot.run("hi")
    assert result.content == ""


def test_workspace_override(tmp_path):
    config_path = _write_config(tmp_path)
    custom_ws = tmp_path / "custom_workspace"
    custom_ws.mkdir()

    bot = Miniunicorn.from_config(config_path, workspace=custom_ws)
    assert bot._loop.workspace == custom_ws


@pytest.mark.asyncio
async def test_run_custom_session_key(tmp_path):
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    mock_response = OutboundMessage(channel="cli", chat_id="direct", content="ok")
    bot._loop.process_direct = AsyncMock(return_value=mock_response)

    await bot.run("hi", session_key="user-alice")
    call_kwargs = bot._loop.process_direct.await_args
    assert call_kwargs.args[0] == "hi"
    assert call_kwargs.kwargs["session_key"] == "user-alice"


def test_import_from_top_level():
    import miniunicorn

    assert miniunicorn.Miniunicorn is Miniunicorn
    assert miniunicorn.RunResult is RunResult


# ---------------------------------------------------------------------------
# RunResult.tools_used / messages — populated from the agent iterations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_populates_tools_used_across_iterations(tmp_path):
    """tools_used collects every tool name fired across all iterations, in order."""
    from miniunicorn.agent.hook import AgentHookContext
    from miniunicorn.bus.events import OutboundMessage
    from miniunicorn.providers.base import ToolCallRequest

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    async def fake_process_direct(message, *, session_key, hooks=None):
        # Hooks are now passed per-turn via the hooks= parameter.
        turn_hooks = list(hooks or [])
        messages = [{"role": "user", "content": message}]
        ctx1 = AgentHookContext(iteration=0, messages=messages)
        ctx1.tool_calls = [
            ToolCallRequest(id="c1", name="read_file", arguments={}),
            ToolCallRequest(id="c2", name="grep", arguments={}),
        ]
        for h in turn_hooks:
            await h.after_iteration(ctx1)
        messages.append({"role": "assistant", "content": "ok"})
        ctx2 = AgentHookContext(iteration=1, messages=messages)
        ctx2.tool_calls = [ToolCallRequest(id="c3", name="list_dir", arguments={})]
        for h in turn_hooks:
            await h.after_iteration(ctx2)
        return OutboundMessage(channel="cli", chat_id="direct", content="final")

    bot._loop.process_direct = fake_process_direct
    result = await bot.run("do stuff")
    assert result.content == "final"
    assert result.tools_used == ["read_file", "grep", "list_dir"]


@pytest.mark.asyncio
async def test_run_populates_final_messages(tmp_path):
    """messages reflects the agent's message list at the last iteration."""
    from miniunicorn.agent.hook import AgentHookContext
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    async def fake_process_direct(message, *, session_key, hooks=None):
        turn_hooks = list(hooks or [])
        messages = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "hi there"},
        ]
        ctx = AgentHookContext(iteration=0, messages=messages)
        for h in turn_hooks:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="hi there")

    bot._loop.process_direct = fake_process_direct
    result = await bot.run("hello")
    assert result.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


@pytest.mark.asyncio
async def test_run_no_iterations_leaves_defaults_empty(tmp_path):
    """If process_direct never triggers after_iteration, tools_used/messages stay []."""
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)
    bot._loop.process_direct = AsyncMock(
        return_value=OutboundMessage(channel="cli", chat_id="direct", content="noop"),
    )
    result = await bot.run("hi")
    assert result.tools_used == []
    assert result.messages == []


@pytest.mark.asyncio
async def test_run_user_hooks_still_fire_alongside_capture(tmp_path):
    """Capture hook must not displace user-provided hooks."""
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    seen_iterations: list[int] = []

    class UserHook(AgentHook):
        async def after_iteration(self, context: AgentHookContext) -> None:
            seen_iterations.append(context.iteration)

    async def fake_process_direct(message, *, session_key, hooks=None):
        turn_hooks = list(hooks or [])
        assert len(turn_hooks) == 2, f"expected capture + user hook, got {len(turn_hooks)}"
        ctx = AgentHookContext(iteration=7, messages=[])
        for h in turn_hooks:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="ok")

    bot._loop.process_direct = fake_process_direct
    await bot.run("x", hooks=[UserHook()])
    assert seen_iterations == [7]


@pytest.mark.asyncio
async def test_run_does_not_leak_loop_extra_hooks(tmp_path):
    """Loop-level _extra_hooks are NOT mutated by run(); they continue to apply
    alongside per-turn hooks because _run_agent_loop composes both lists."""
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    sentinel_hook = AgentHook()
    bot._loop._extra_hooks = [sentinel_hook]

    async def fake_process_direct(message, *, session_key, hooks=None):
        # Per-turn hooks arrive via the parameter; loop-level hooks remain
        # on _extra_hooks untouched.
        turn_hooks = list(hooks or [])
        ctx = AgentHookContext(iteration=0, messages=[])
        for h in turn_hooks:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="done")

    bot._loop.process_direct = fake_process_direct
    await bot.run("hello")
    assert bot._loop._extra_hooks == [sentinel_hook]


# ---------------------------------------------------------------------------
# NEW: concurrent run() calls with distinct hooks must not cross-contaminate.
# This is the regression test for the shared-_extra_hooks race fixed by
# binding hooks to each turn via process_direct(hooks=...).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_runs_isolate_hooks(tmp_path):
    """Two concurrent run() calls with different hooks must each see only
    their own hooks, even though they share the same AgentLoop."""
    from miniunicorn.agent.hook import AgentHook, AgentHookContext
    from miniunicorn.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Miniunicorn.from_config(config_path, workspace=tmp_path)

    seen_a: list[str] = []
    seen_b: list[str] = []

    class HookA(AgentHook):
        async def after_iteration(self, context: AgentHookContext) -> None:
            seen_a.append("a")

    class HookB(AgentHook):
        async def after_iteration(self, context: AgentHookContext) -> None:
            seen_b.append("b")

    # Gate that forces both runs to be in-flight at the same time.
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_process_direct(message, *, session_key, hooks=None):
        turn_hooks = list(hooks or [])
        # Signal that we've captured our hooks, then wait for release.
        started.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        ctx = AgentHookContext(iteration=0, messages=[])
        for h in turn_hooks:
            await h.after_iteration(ctx)
        # Verify only this run's user hook fired — no cross-contamination.
        hook_names = {type(h).__name__ for h in turn_hooks}
        if "HookA" in hook_names:
            assert "HookB" not in hook_names, "HookB leaked into run A"
        if "HookB" in hook_names:
            assert "HookA" not in hook_names, "HookA leaked into run B"
        return OutboundMessage(channel="cli", chat_id="direct", content=message)

    bot._loop.process_direct = fake_process_direct

    task_a = asyncio.create_task(bot.run("a", hooks=[HookA()], session_key="sdk:a"))
    task_b = asyncio.create_task(bot.run("b", hooks=[HookB()], session_key="sdk:b"))

    # Wait for both to have entered process_direct, then release them together.
    await asyncio.wait_for(started.wait(), timeout=2.0)
    # Give B a chance to also start (event set by whichever ran first).
    await asyncio.sleep(0.05)
    release.set()

    res_a = await task_a
    res_b = await task_b

    assert res_a.content == "a"
    assert res_b.content == "b"
    assert seen_a == ["a"]
    assert seen_b == ["b"]
    # Loop-level _extra_hooks untouched.
    assert bot._loop._extra_hooks == []


# ---------------------------------------------------------------------------
# NEW: from_config() with a custom config_path sets the active config context
# so runtime data (media, cron, logs, webui) routes under that instance dir.
# ---------------------------------------------------------------------------


def test_from_config_sets_active_config_path(tmp_path, monkeypatch):
    """from_config(config_path) must call set_config_path() before building the
    loop, so runtime data directories resolve under the custom instance."""
    from miniunicorn.config import loader as loader_mod

    config_path = _write_config(tmp_path)
    captured: list[Path] = []
    orig_set = loader_mod.set_config_path

    def spy_set_config_path(p: Path) -> None:
        captured.append(p)
        orig_set(p)

    monkeypatch.setattr(loader_mod, "set_config_path", spy_set_config_path)
    # Also patch the symbol imported into paths.py if it cached a reference.
    Miniunicorn.from_config(config_path, workspace=tmp_path)
    assert captured and captured[-1] == config_path.resolve()


def test_from_config_default_path_does_not_override_active_config(tmp_path, monkeypatch):
    """from_config() with no config_path must NOT call set_config_path(); it
    should use the existing active config context (or the default).

    We verify this by inspecting the source contract: set_config_path is only
    invoked inside the ``if config_path is not None`` branch. Here we spy on
    set_config_path and short-circuit AgentLoop construction to confirm the
    spy is never invoked when config_path is None.
    """
    from miniunicorn.config import loader as loader_mod

    calls: list[Path] = []

    def spy_set_config_path(p: Path) -> None:
        calls.append(p)

    monkeypatch.setattr(loader_mod, "set_config_path", spy_set_config_path)
    # Short-circuit the heavy AgentLoop.from_config to keep the test fast and
    # isolated from provider/registry concerns.
    fake_loop = MagicMock()
    monkeypatch.setattr("miniunicorn.miniunicorn.AgentLoop.from_config", lambda config: fake_loop)
    # load_config must return something with .agents.defaults.workspace attr.
    fake_config = MagicMock()
    monkeypatch.setattr(loader_mod, "load_config", lambda *a, **k: fake_config)
    monkeypatch.setattr(loader_mod, "resolve_config_env_vars", lambda c: c)

    Miniunicorn.from_config()
    assert calls == [], "from_config() without config_path must not call set_config_path"
