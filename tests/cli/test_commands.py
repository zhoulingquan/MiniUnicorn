import asyncio
import json
import re
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.cli.commands import app
from miniunicorn.config.schema import Config
from miniunicorn.cron.types import CronJob, CronPayload
from miniunicorn.providers.factory import ProviderSnapshot, make_provider

runner = CliRunner()


def _fake_provider():
    """Return a minimal fake provider that satisfies AgentLoop.__init__."""
    p = MagicMock()
    p.generation.max_tokens = 4096
    return p


class _StopGatewayError(RuntimeError):
    pass


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with (
        patch("miniunicorn.config.loader.get_config_path") as mock_cp,
        patch("miniunicorn.config.loader.save_config") as mock_sc,
        patch("miniunicorn.config.loader.load_config") as mock_lc,
        patch("miniunicorn.cli.commands.get_workspace_path") as mock_ws,
    ):
        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"
        workspace_dir = base_dir / "workspace"

        mock_cp.return_value = config_file
        mock_ws.return_value = workspace_dir
        mock_lc.side_effect = lambda _config_path=None: Config()

        def _save_config(config: Config, config_path: Path | None = None):
            target = config_path or config_file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(config.model_dump(by_alias=True)), encoding="utf-8")

        mock_sc.side_effect = _save_config

        yield config_file, workspace_dir, mock_ws

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    """No existing config — should create from scratch."""
    config_file, workspace_dir, mock_ws = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "Created workspace" in result.stdout
    assert "MiniUnicorn is ready" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "history.jsonl").exists()
    assert not (workspace_dir / "USER.md").exists()
    assert not (workspace_dir / "memory" / "MEMORY.md").exists()
    expected_workspace = Config().workspace_path
    assert mock_ws.call_args.args == (expected_workspace,)


def test_onboard_existing_config_refresh(mock_paths):
    """Config exists, user declines overwrite — should refresh (load-merge-save)."""
    config_file, workspace_dir, _ = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout
    assert workspace_dir.exists()
    assert (workspace_dir / "AGENTS.md").exists()


def test_onboard_existing_config_overwrite(mock_paths):
    """Config exists, user confirms overwrite — should reset to defaults."""
    config_file, workspace_dir, _ = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout
    assert workspace_dir.exists()


def test_onboard_existing_workspace_safe_create(mock_paths):
    """Workspace exists — should not recreate, but still add missing templates."""
    config_file, workspace_dir, _ = mock_paths
    workspace_dir.mkdir(parents=True)
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Created workspace" not in result.stdout
    assert "Created AGENTS.md" in result.stdout
    assert (workspace_dir / "AGENTS.md").exists()


def _strip_ansi(text):
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    return ansi_escape.sub("", text)


def test_onboard_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["onboard", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output
    assert "--wizard" in stripped_output
    assert "--dir" not in stripped_output


def test_onboard_interactive_discard_does_not_save_or_create_workspace(mock_paths, monkeypatch):
    config_file, workspace_dir, _ = mock_paths

    from miniunicorn.cli.onboard import OnboardResult

    monkeypatch.setattr(
        "miniunicorn.cli.onboard.run_onboard",
        lambda initial_config: OnboardResult(config=initial_config, should_save=False),
    )

    result = runner.invoke(app, ["onboard", "--wizard"])

    assert result.exit_code == 0
    assert "No changes were saved" in result.stdout
    assert not config_file.exists()
    assert not workspace_dir.exists()


def test_onboard_uses_explicit_config_and_workspace_paths(tmp_path, monkeypatch):
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"

    monkeypatch.setattr("miniunicorn.channels.registry.discover_all", lambda: {})

    result = runner.invoke(
        app,
        ["onboard", "--config", str(config_path), "--workspace", str(workspace_path)],
    )

    assert result.exit_code == 0
    saved = Config.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    assert saved.workspace_path == workspace_path
    assert (workspace_path / "AGENTS.md").exists()
    stripped_output = _strip_ansi(result.stdout)
    compact_output = stripped_output.replace("\n", "")
    resolved_config = str(config_path.resolve())
    assert resolved_config in compact_output
    assert f"--config {resolved_config}" in compact_output


def test_onboard_wizard_preserves_explicit_config_in_next_steps(tmp_path, monkeypatch):
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"

    from miniunicorn.cli.onboard import OnboardResult

    monkeypatch.setattr(
        "miniunicorn.cli.onboard.run_onboard",
        lambda initial_config: OnboardResult(config=initial_config, should_save=True),
    )
    monkeypatch.setattr("miniunicorn.channels.registry.discover_all", lambda: {})

    result = runner.invoke(
        app,
        ["onboard", "--wizard", "--config", str(config_path), "--workspace", str(workspace_path)],
    )

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    compact_output = stripped_output.replace("\n", "")
    resolved_config = str(config_path.resolve())
    assert f'miniunicorn agent -m "Hello!" --config {resolved_config}' in compact_output
    assert f"miniunicorn gateway --config {resolved_config}" in compact_output


def test_provider_logout_rejects_unknown_provider():
    result = runner.invoke(app, ["provider", "logout", "not-a-real-provider"])

    assert result.exit_code == 1
    assert "Unknown OAuth provider" in result.stdout


def test_provider_login_rejects_unknown_provider():
    result = runner.invoke(app, ["provider", "login", "not-a-real-provider"])

    assert result.exit_code == 1
    assert "Unknown OAuth provider" in result.stdout


def test_openai_compat_provider_passes_model_through():
    from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider

    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(default_model="github-copilot/gpt-5.3-codex")

    assert provider.get_default_model() == "github-copilot/gpt-5.3-codex"


def test_make_provider_passes_extra_headers_to_custom_provider():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "gpt-4o-mini"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "https://example.com/v1",
                    "extraHeaders": {
                        "APP-Code": "demo-app",
                        "x-session-affinity": "sticky-session",
                    },
                }
            },
        }
    )

    with patch("miniunicorn.providers.openai_compat_provider.AsyncOpenAI") as mock_async_openai:
        provider = make_provider(config)
        asyncio.run(provider._ensure_client())

    kwargs = mock_async_openai.call_args.kwargs
    assert kwargs["api_key"] == "test-key"
    assert kwargs["base_url"] == "https://example.com/v1"
    assert kwargs["default_headers"]["APP-Code"] == "demo-app"
    assert kwargs["default_headers"]["x-session-affinity"] == "sticky-session"


@pytest.fixture
def mock_agent_runtime(tmp_path):
    """Mock agent command dependencies for focused CLI tests."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "default-workspace")

    with (
        patch("miniunicorn.config.loader.load_config", return_value=config) as mock_load_config,
        patch("miniunicorn.config.loader.resolve_config_env_vars", side_effect=lambda c: c),
        patch("miniunicorn.cli.commands.sync_workspace_templates") as mock_sync_templates,
        patch("miniunicorn.providers.factory.make_provider", return_value=_fake_provider()),
        patch("miniunicorn.cli.commands._print_agent_response") as mock_print_response,
        patch("miniunicorn.bus.queue.MessageBus"),
        patch("miniunicorn.cron.service.CronService"),
        patch("miniunicorn.cli.commands.AgentLoop.from_config") as mock_from_config,
    ):
        agent_loop = MagicMock()
        agent_loop.channels_config = None
        agent_loop.process_direct = AsyncMock(
            return_value=OutboundMessage(channel="cli", chat_id="direct", content="mock-response"),
        )
        agent_loop.close_mcp = AsyncMock(return_value=None)
        mock_from_config.return_value = agent_loop

        yield {
            "config": config,
            "load_config": mock_load_config,
            "sync_templates": mock_sync_templates,
            "from_config": mock_from_config,
            "agent_loop": agent_loop,
            "print_response": mock_print_response,
        }


def test_agent_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["agent", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output


def test_agent_uses_default_config_when_no_workspace_or_config_flags(mock_agent_runtime):
    result = runner.invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (None,)
    assert mock_agent_runtime["sync_templates"].call_args.args == (
        mock_agent_runtime["config"].workspace_path,
    )
    passed_config = mock_agent_runtime["from_config"].call_args.args[0]
    assert passed_config.workspace_path == mock_agent_runtime["config"].workspace_path
    mock_agent_runtime["agent_loop"].process_direct.assert_awaited_once()
    mock_agent_runtime["print_response"].assert_called_once_with(
        "mock-response",
        render_markdown=True,
        metadata={},
    )


def test_agent_uses_explicit_config_path(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)


def test_agent_config_sets_active_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "miniunicorn.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )
    monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("miniunicorn.cron.service.CronService", lambda _store: object())

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs):
            return OutboundMessage(channel="cli", chat_id="direct", content="ok")

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr(
        "miniunicorn.cli.commands._print_agent_response", lambda *_args, **_kwargs: None
    )

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["config_path"] == config_file.resolve()


def test_agent_uses_workspace_directory_for_cron_store(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "agent-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr("miniunicorn.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )
    monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", lambda: object())

    class _FakeCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs):
            return OutboundMessage(channel="cli", chat_id="direct", content="ok")

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("miniunicorn.cron.service.CronService", _FakeCron)
    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr(
        "miniunicorn.cli.commands._print_agent_response", lambda *_args, **_kwargs: None
    )

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["cron_store"] == config.workspace_path / "cron" / "jobs.json"


def test_agent_workspace_override_does_not_migrate_legacy_cron(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    legacy_dir = tmp_path / "global" / "cron"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "jobs.json"
    legacy_file.write_text('{"jobs": []}')

    override = tmp_path / "override-workspace"
    config = Config()
    seen: dict[str, Path] = {}

    monkeypatch.setattr("miniunicorn.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )
    monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("miniunicorn.config.paths.get_cron_dir", lambda: legacy_dir)

    class _FakeCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs):
            return OutboundMessage(channel="cli", chat_id="direct", content="ok")

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("miniunicorn.cron.service.CronService", _FakeCron)
    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr(
        "miniunicorn.cli.commands._print_agent_response", lambda *_args, **_kwargs: None
    )

    result = runner.invoke(
        app,
        ["agent", "-m", "hello", "-c", str(config_file), "-w", str(override)],
    )

    assert result.exit_code == 0
    assert seen["cron_store"] == override / "cron" / "jobs.json"
    assert legacy_file.exists()
    assert not (override / "cron" / "jobs.json").exists()


def test_agent_custom_config_workspace_does_not_migrate_legacy_cron(
    monkeypatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    legacy_dir = tmp_path / "global" / "cron"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "jobs.json"
    legacy_file.write_text('{"jobs": []}')

    custom_workspace = tmp_path / "custom-workspace"
    config = Config()
    config.agents.defaults.workspace = str(custom_workspace)
    seen: dict[str, Path] = {}

    monkeypatch.setattr("miniunicorn.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )
    monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("miniunicorn.config.paths.get_cron_dir", lambda: legacy_dir)

    class _FakeCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs):
            return OutboundMessage(channel="cli", chat_id="direct", content="ok")

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("miniunicorn.cron.service.CronService", _FakeCron)
    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr(
        "miniunicorn.cli.commands._print_agent_response", lambda *_args, **_kwargs: None
    )

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["cron_store"] == custom_workspace / "cron" / "jobs.json"
    assert legacy_file.exists()
    assert not (custom_workspace / "cron" / "jobs.json").exists()


def test_agent_overrides_workspace_path(mock_agent_runtime):
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(app, ["agent", "-m", "hello", "-w", str(workspace_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    passed_config = mock_agent_runtime["from_config"].call_args.args[0]
    assert passed_config.workspace_path == workspace_path


def test_agent_workspace_override_wins_over_config_workspace(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(
        app,
        ["agent", "-m", "hello", "-c", str(config_path), "-w", str(workspace_path)],
    )

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    passed_config = mock_agent_runtime["from_config"].call_args.args[0]
    assert passed_config.workspace_path == workspace_path


def test_agent_hints_about_deprecated_memory_window(mock_agent_runtime, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"agents": {"defaults": {"memoryWindow": 42}}}))

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert "memoryWindow" in result.stdout
    assert "no longer used" in result.stdout


def test_memory_migrate_absent_from_cli_help():
    """Assert /memory-migrate is not in CLI help text."""
    result = runner.invoke(app, ["agent", "--help"])
    assert result.exit_code == 0
    stripped = result.stdout.replace("\x1b[[][0-9;]*m", "")
    assert "/memory-migrate" not in stripped


def test_heartbeat_retains_recent_messages_by_default():
    config = Config()

    assert config.gateway.heartbeat.keep_recent_messages == 8


def _write_instance_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")
    return config_file


def _stop_gateway_provider(_config) -> object:
    raise _StopGatewayError("stop")


def _test_provider_snapshot(provider: object, config: Config) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider=provider,
        model=config.agents.defaults.model,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        signature=("test",),
    )


def _patch_cli_command_runtime(
    monkeypatch,
    config: Config,
    *,
    set_config_path=None,
    sync_templates=None,
    make_provider=None,
    message_bus=None,
    session_manager=None,
    cron_service=None,
    get_cron_dir=None,
) -> None:
    provider_factory = make_provider or (lambda _config: _fake_provider())

    monkeypatch.setattr(
        "miniunicorn.config.loader.set_config_path",
        set_config_path or (lambda _path: None),
    )
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.config.loader.resolve_config_env_vars", lambda c: c)
    monkeypatch.setattr(
        "miniunicorn.cli.commands.sync_workspace_templates",
        sync_templates or (lambda _path: None),
    )
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider",
        provider_factory,
    )
    monkeypatch.setattr(
        "miniunicorn.providers.factory.build_provider_snapshot",
        lambda _config: _test_provider_snapshot(provider_factory(_config), _config),
    )
    monkeypatch.setattr(
        "miniunicorn.providers.factory.load_provider_snapshot",
        lambda _config_path=None: _test_provider_snapshot(provider_factory(config), config),
    )

    if message_bus is not None:
        monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", message_bus)
    if session_manager is not None:
        monkeypatch.setattr("miniunicorn.session.manager.SessionManager", session_manager)
    if cron_service is not None:
        monkeypatch.setattr("miniunicorn.cron.service.CronService", cron_service)
    if get_cron_dir is not None:
        monkeypatch.setattr("miniunicorn.config.paths.get_cron_dir", get_cron_dir)


def _patch_serve_runtime(monkeypatch, config: Config, seen: dict[str, object]) -> None:
    pytest.importorskip("aiohttp")

    class _FakeApiApp:
        def __init__(self) -> None:
            self.on_startup: list[object] = []
            self.on_cleanup: list[object] = []

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(workspace=config.workspace_path, **extra)

        def __init__(self, **kwargs) -> None:
            seen["workspace"] = kwargs["workspace"]

        async def _connect_mcp(self) -> None:
            return None

        async def close_mcp(self) -> None:
            return None

    def _fake_create_app(
        agent_loop, model_name: str, request_timeout: float, api_key: str = "", **kwargs
    ):
        seen["agent_loop"] = agent_loop
        seen["model_name"] = model_name
        seen["request_timeout"] = request_timeout
        seen["api_key"] = api_key
        return _FakeApiApp()

    def _fake_run_app(api_app, host: str, port: int, print):
        seen["api_app"] = api_app
        seen["host"] = host
        seen["port"] = port

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        message_bus=lambda: object(),
        session_manager=lambda _workspace: object(),
    )
    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("miniunicorn.api.server.create_app", _fake_create_app)
    monkeypatch.setattr("aiohttp.web.run_app", _fake_run_app)


def test_gateway_uses_workspace_from_config_by_default(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        set_config_path=lambda path: seen.__setitem__("config_path", path),
        sync_templates=lambda path: seen.__setitem__("workspace", path),
        make_provider=_stop_gateway_provider,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["config_path"] == config_file.resolve()
    assert seen["workspace"] == Path(config.agents.defaults.workspace)


def test_gateway_workspace_option_overrides_config(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    override = tmp_path / "override-workspace"
    seen: dict[str, Path] = {}

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        sync_templates=lambda path: seen.__setitem__("workspace", path),
        make_provider=_stop_gateway_provider,
    )

    result = runner.invoke(
        app,
        ["gateway", "--config", str(config_file), "--workspace", str(override)],
    )

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["workspace"] == override
    assert config.workspace_path == override


def test_gateway_uses_workspace_directory_for_cron_store(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    class _StopCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path
            raise _StopGatewayError("stop")

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        message_bus=lambda: object(),
        session_manager=lambda _workspace: object(),
        cron_service=_StopCron,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["cron_store"] == config.workspace_path / "cron" / "jobs.json"


def test_gateway_cron_evaluator_receives_scheduled_reminder_context(
    monkeypatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    provider = _fake_provider()
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    seen: dict[str, object] = {}

    monkeypatch.setattr("miniunicorn.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr("miniunicorn.providers.factory.make_provider", lambda _config: provider)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.build_provider_snapshot",
        lambda _config: _test_provider_snapshot(provider, _config),
    )
    monkeypatch.setattr(
        "miniunicorn.providers.factory.load_provider_snapshot",
        lambda _config_path=None: _test_provider_snapshot(provider, config),
    )
    monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", lambda: bus)

    class _FakeSession:
        def __init__(self) -> None:
            self.messages = []

        def add_message(self, role: str, content: str, **kwargs) -> None:
            self.messages.append({"role": role, "content": content, **kwargs})

    class _FakeSessionManager:
        def __init__(self, _workspace: Path) -> None:
            self.session = _FakeSession()
            seen["session_manager"] = self

        def get_or_create(self, key: str) -> _FakeSession:
            seen["session_key"] = key
            return self.session

        def save(self, session: _FakeSession) -> None:
            seen["saved_session"] = session

        @property
        def writes(self):
            return _FakeSessionWrites(self)

    class _FakeSessionWrites:
        def __init__(self, manager: _FakeSessionManager) -> None:
            self._manager = manager

        def record_message(self, session: _FakeSession, role: str, content: str, **extra: object) -> None:
            session.add_message(role, content, **extra)
            self._manager.save(session)

    monkeypatch.setattr("miniunicorn.session.manager.SessionManager", _FakeSessionManager)

    class _FakeCron:
        def __init__(self, _store_path: Path) -> None:
            self.on_job = None
            seen["cron"] = self

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)

        def __init__(self, *args, **kwargs) -> None:
            self.model = "test-model"
            self.provider = kwargs.get("provider", object())
            self.tools = {}
            seen["agent"] = self

        async def process_direct(self, *_args, **_kwargs):
            return OutboundMessage(
                channel="telegram",
                chat_id="user-1",
                content="Time to stretch.",
            )

        async def close_mcp(self) -> None:
            return None

        async def run(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class _StopAfterCronSetup:
        def __init__(self, *_args, **_kwargs) -> None:
            raise _StopGatewayError("stop")

    async def _capture_evaluate_response(
        response: str,
        task_context: str,
        provider_arg: object,
        model: str,
    ) -> bool:
        seen["response"] = response
        seen["task_context"] = task_context
        seen["provider"] = provider_arg
        seen["model"] = model
        return True

    monkeypatch.setattr("miniunicorn.cron.service.CronService", _FakeCron)
    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("miniunicorn.channels.manager.ChannelManager", _StopAfterCronSetup)
    monkeypatch.setattr(
        "miniunicorn.cli.commands.evaluate_response",
        _capture_evaluate_response,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    cron = seen["cron"]
    assert isinstance(cron, _FakeCron)
    assert cron.on_job is not None

    runtime_provider = object()
    agent = seen["agent"]
    agent.provider = runtime_provider
    agent.model = "runtime-model"

    job = CronJob(
        id="cron-1",
        name="stretch",
        payload=CronPayload(
            message="Remind me to stretch.",
            deliver=True,
            channel="telegram",
            to="user-1",
        ),
    )

    response = asyncio.run(cron.on_job(job))

    assert response == "Time to stretch."
    assert seen["response"] == "Time to stretch."
    assert seen["provider"] is runtime_provider
    assert seen["model"] == "runtime-model"
    assert seen["task_context"] == (
        "The scheduled time has arrived. Deliver this reminder to the user now, "
        "as a brief and natural message in their language. Speak directly to them — "
        "do not narrate progress, summarize, include user IDs, or add status reports "
        "like 'Done' or 'Reminded'.\n\n"
        "Reminder: Remind me to stretch."
    )
    bus.publish_outbound.assert_awaited_once_with(
        OutboundMessage(
            channel="telegram",
            chat_id="user-1",
            content="Time to stretch.",
        )
    )
    assert seen["session_key"] == "telegram:user-1"
    saved_session = seen["saved_session"]
    assert isinstance(saved_session, _FakeSession)
    assert saved_session.messages == [
        {
            "role": "assistant",
            "content": "Time to stretch.",
            "_channel_delivery": True,
        }
    ]


def test_gateway_cron_job_suppresses_intermediate_progress(monkeypatch, tmp_path: Path) -> None:
    """Cron jobs must pass on_progress=_silent to process_direct so that
    tool hints and streaming deltas are never leaked to the user channel
    before evaluate_response decides whether to deliver."""
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    seen: dict[str, object] = {}

    monkeypatch.setattr("miniunicorn.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("miniunicorn.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("miniunicorn.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )
    monkeypatch.setattr(
        "miniunicorn.providers.factory.build_provider_snapshot",
        lambda _config: _test_provider_snapshot(object(), _config),
    )
    monkeypatch.setattr(
        "miniunicorn.providers.factory.load_provider_snapshot",
        lambda _config_path=None: _test_provider_snapshot(object(), config),
    )
    monkeypatch.setattr("miniunicorn.bus.queue.MessageBus", lambda: bus)
    monkeypatch.setattr("miniunicorn.session.manager.SessionManager", lambda _workspace: object())

    class _FakeCron:
        def __init__(self, _store_path: Path) -> None:
            self.on_job = None
            seen["cron"] = self

    class _FakeAgentLoop:
        @classmethod
        def from_config(cls, config, bus=None, **extra):
            return cls(**extra)

        def __init__(self, *args, **kwargs) -> None:
            self.model = "test-model"
            self.provider = object()
            self.tools = {}

        async def process_direct(self, *_args, on_progress=None, **_kwargs):
            seen["on_progress"] = on_progress
            return OutboundMessage(
                channel="telegram",
                chat_id="user-1",
                content="Done.",
            )

        async def close_mcp(self) -> None:
            return None

        async def run(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class _StopAfterCronSetup:
        def __init__(self, *_args, **_kwargs) -> None:
            raise _StopGatewayError("stop")

    async def _always_reject(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr("miniunicorn.cron.service.CronService", _FakeCron)
    monkeypatch.setattr("miniunicorn.cli.commands.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("miniunicorn.channels.manager.ChannelManager", _StopAfterCronSetup)
    monkeypatch.setattr(
        "miniunicorn.cli.commands.evaluate_response",
        _always_reject,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])
    assert isinstance(result.exception, _StopGatewayError)

    cron = seen["cron"]
    job = CronJob(
        id="cron-silent-test",
        name="test-silent",
        payload=CronPayload(
            message="Run something.",
            deliver=True,
            channel="telegram",
            to="user-1",
        ),
    )
    response = asyncio.run(cron.on_job(job))

    assert response == "Done."
    # on_progress must be a callable (the _silent noop), not None and not bus_progress
    assert seen["on_progress"] is not None
    assert callable(seen["on_progress"])
    # Verify it actually swallows calls (no side effects)
    asyncio.run(seen["on_progress"]("tool_hint", "🔧 $ echo test"))
    # Nothing published to bus since evaluator rejected
    bus.publish_outbound.assert_not_awaited()


def test_gateway_workspace_override_does_not_migrate_legacy_cron(
    monkeypatch, tmp_path: Path
) -> None:
    config_file = _write_instance_config(tmp_path)
    legacy_dir = tmp_path / "global" / "cron"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "jobs.json"
    legacy_file.write_text('{"jobs": []}')

    override = tmp_path / "override-workspace"
    config = Config()
    seen: dict[str, Path] = {}

    class _StopCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path
            raise _StopGatewayError("stop")

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        message_bus=lambda: object(),
        session_manager=lambda _workspace: object(),
        cron_service=_StopCron,
        get_cron_dir=lambda: legacy_dir,
    )

    result = runner.invoke(
        app,
        ["gateway", "--config", str(config_file), "--workspace", str(override)],
    )

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["cron_store"] == override / "cron" / "jobs.json"
    assert legacy_file.exists()
    assert not (override / "cron" / "jobs.json").exists()


def test_gateway_custom_config_workspace_does_not_migrate_legacy_cron(
    monkeypatch, tmp_path: Path
) -> None:
    config_file = _write_instance_config(tmp_path)
    legacy_dir = tmp_path / "global" / "cron"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "jobs.json"
    legacy_file.write_text('{"jobs": []}')

    custom_workspace = tmp_path / "custom-workspace"
    config = Config()
    config.agents.defaults.workspace = str(custom_workspace)
    seen: dict[str, Path] = {}

    class _StopCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path
            raise _StopGatewayError("stop")

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        message_bus=lambda: object(),
        session_manager=lambda _workspace: object(),
        cron_service=_StopCron,
        get_cron_dir=lambda: legacy_dir,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["cron_store"] == custom_workspace / "cron" / "jobs.json"
    assert legacy_file.exists()
    assert not (custom_workspace / "cron" / "jobs.json").exists()


def test_migrate_cron_store_moves_legacy_file(tmp_path: Path) -> None:
    """Legacy global jobs.json is moved into the workspace on first run."""
    from miniunicorn.cli.commands import _migrate_cron_store

    legacy_dir = tmp_path / "global" / "cron"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "jobs.json"
    legacy_file.write_text('{"jobs": []}')

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    workspace_cron = config.workspace_path / "cron" / "jobs.json"

    with patch("miniunicorn.config.paths.get_cron_dir", return_value=legacy_dir):
        _migrate_cron_store(config)

    assert workspace_cron.exists()
    assert workspace_cron.read_text() == '{"jobs": []}'
    assert not legacy_file.exists()


def test_migrate_cron_store_skips_when_workspace_file_exists(tmp_path: Path) -> None:
    """Migration does not overwrite an existing workspace cron store."""
    from miniunicorn.cli.commands import _migrate_cron_store

    legacy_dir = tmp_path / "global" / "cron"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "jobs.json").write_text('{"old": true}')

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    workspace_cron = config.workspace_path / "cron" / "jobs.json"
    workspace_cron.parent.mkdir(parents=True)
    workspace_cron.write_text('{"new": true}')

    with patch("miniunicorn.config.paths.get_cron_dir", return_value=legacy_dir):
        _migrate_cron_store(config)

    assert workspace_cron.read_text() == '{"new": true}'


def test_gateway_uses_configured_port_when_cli_flag_is_missing(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        make_provider=_stop_gateway_provider,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)


def test_gateway_cli_port_overrides_configured_port(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()

    _patch_cli_command_runtime(
        monkeypatch,
        config,
        make_provider=_stop_gateway_provider,
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)


def test_configure_desktop_gateway_forces_local_websocket_only() -> None:
    from miniunicorn.cli.commands import _configure_desktop_gateway

    config = Config()
    config.channels.__pydantic_extra__ = {
        "telegram": {"enabled": True, "token": "x"},
        "websocket": {"enabled": False, "port": 8765},
    }

    _configure_desktop_gateway(
        config,
        webui_port=29888,
        webui_socket="/tmp/miniunicorn-test.sock",
        token_issue_secret="secret",
    )

    extras = config.channels.__pydantic_extra__ or {}
    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.heartbeat.enabled is False
    assert extras["telegram"]["enabled"] is False
    assert extras["websocket"]["enabled"] is True
    assert extras["websocket"]["host"] == "127.0.0.1"
    assert extras["websocket"]["port"] == 29888
    assert extras["websocket"]["unix_socket_path"] == "/tmp/miniunicorn-test.sock"
    assert extras["websocket"]["token_issue_secret"] == "secret"
    assert extras["websocket"]["websocket_requires_token"] is True


def test_serve_uses_api_config_defaults_and_workspace_override(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    config.api.host = "127.0.0.2"
    config.api.port = 18900
    config.api.timeout = 45.0
    override_workspace = tmp_path / "override-workspace"
    seen: dict[str, object] = {}

    _patch_serve_runtime(monkeypatch, config, seen)

    result = runner.invoke(
        app,
        ["serve", "--config", str(config_file), "--workspace", str(override_workspace)],
    )

    assert result.exit_code == 0
    assert seen["workspace"] == override_workspace
    assert seen["host"] == "127.0.0.2"
    assert seen["port"] == 18900
    assert seen["request_timeout"] == 45.0


def test_serve_cli_options_override_api_config(monkeypatch, tmp_path: Path) -> None:
    config_file = _write_instance_config(tmp_path)
    config = Config()
    config.api.host = "127.0.0.2"
    config.api.port = 18900
    config.api.timeout = 45.0
    seen: dict[str, object] = {}

    _patch_serve_runtime(monkeypatch, config, seen)

    result = runner.invoke(
        app,
        [
            "serve",
            "--config",
            str(config_file),
            "--host",
            "127.0.0.1",
            "--port",
            "18901",
            "--timeout",
            "46",
        ],
    )

    assert result.exit_code == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 18901
    assert seen["request_timeout"] == 46.0


def test_channels_login_requires_channel_name() -> None:
    result = runner.invoke(app, ["channels", "login"])

    assert result.exit_code == 2
