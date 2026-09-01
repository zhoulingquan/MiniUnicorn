"""Tests for the tools.cliApps.enabled feature gate.

The CLI Apps ecosystem (``miniunicorn/apps/``, ``run_cli_app``, WebUI
settings API) is optional and disabled by default. These tests pin the
three mount points: tool registration, gateway main flow, and WebUI API.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from miniunicorn.config.schema import Config, ToolsConfig
from miniunicorn.tools.context import ToolContext
from miniunicorn.tools.loader import ToolLoader
from miniunicorn.tools.registry import ToolRegistry


def _config(enabled: bool) -> Config:
    return Config.model_validate({"tools": {"cliApps": {"enabled": enabled}}})


def _ctx(tmp_path: Path, enabled: bool) -> ToolContext:
    # ToolContext.config is the ToolsConfig slice (mirrors AgentLoop behavior).
    return ToolContext(
        config=_config(enabled).tools,
        workspace=str(tmp_path),
    )


# --- Mount point 1: tool registration ---------------------------------------


def test_cli_apps_tool_not_registered_when_disabled(tmp_path) -> None:
    registry = ToolRegistry()
    registered = ToolLoader().load(_ctx(tmp_path, enabled=False), registry)
    assert "run_cli_app" not in registered
    assert not registry.has("run_cli_app")


def test_cli_apps_tool_registered_when_enabled(tmp_path) -> None:
    registry = ToolRegistry()
    registered = ToolLoader().load(_ctx(tmp_path, enabled=True), registry)
    assert "run_cli_app" in registered
    assert registry.has("run_cli_app")


def test_cli_apps_enabled_classmethod_follows_config(tmp_path) -> None:
    from miniunicorn.tools.cli_apps import CliAppsTool

    assert CliAppsTool.enabled(_ctx(tmp_path, enabled=False)) is False
    assert CliAppsTool.enabled(_ctx(tmp_path, enabled=True)) is True


def test_default_tools_config_disables_cli_apps() -> None:
    assert ToolsConfig().cli_apps.enabled is False


# --- Gateway main flow: no apps service initialization -----------------------


def test_runtime_lines_skip_cli_apps_when_disabled(tmp_path, monkeypatch) -> None:
    from miniunicorn.agent import context as agent_context
    from miniunicorn.apps.cli import utils as cli_app_utils

    def _boom(*_args: Any, **_kwargs: Any) -> list[str]:
        raise AssertionError("apps service must not be touched when disabled")

    monkeypatch.setattr(cli_app_utils, "runtime_lines", _boom)
    state = SimpleNamespace(mcp_servers=(), mcp_stacks=())
    msg = SimpleNamespace(content="try @gimp please", metadata={})
    lines = agent_context.runtime_lines(
        state,
        msg,
        tmp_path,
        skip=False,
        cli_apps_enabled=False,
    )
    assert isinstance(lines, list)


def test_context_builder_propagates_cli_apps_flag(tmp_path) -> None:
    from miniunicorn.agent.context import ContextBuilder

    assert ContextBuilder(tmp_path, cli_apps_enabled=False).cli_apps_enabled is False
    assert ContextBuilder(tmp_path).cli_apps_enabled is True


# --- Mount point 3: WebUI API ------------------------------------------------


@pytest.fixture()
def gate_config(monkeypatch):
    """Install a patched ``load_config`` plus a fail-loud ``_manager``."""

    def _install(enabled: bool) -> Config:
        cfg = _config(enabled)

        def _fake_load_config(*_args: Any, **_kwargs: Any) -> Config:
            return cfg

        def _fail_manager(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("CliAppManager must not be constructed when disabled")

        import miniunicorn.webui.cli_apps_api as api

        monkeypatch.setattr(api, "load_config", _fake_load_config)
        monkeypatch.setattr(api, "_manager", _fail_manager)
        return cfg

    return _install


def test_webui_payload_reports_disabled_without_service(tmp_path, gate_config) -> None:
    from miniunicorn.webui import cli_apps_api

    gate_config(False)

    payload = cli_apps_api.cli_apps_payload()

    assert payload["enabled"] is False
    assert payload["apps"] == []
    assert payload["installed_count"] == 0


def test_webui_action_returns_403_when_disabled(tmp_path, gate_config) -> None:
    from miniunicorn.apps.cli.service import CliAppError
    from miniunicorn.webui import cli_apps_api

    gate_config(False)
    query = SimpleNamespace()

    for action in ("install", "update", "uninstall", "test"):
        with pytest.raises(CliAppError) as excinfo:
            cli_apps_api.cli_apps_action(action, query)
        assert excinfo.value.status == 403
        assert "disabled" in excinfo.value.message.lower()


def test_webui_payload_marks_enabled_and_serves_service_payload(
    tmp_path, gate_config, monkeypatch
) -> None:
    from miniunicorn.webui import cli_apps_api

    gate_config(True)

    class _StubManager:
        def payload(self) -> dict[str, Any]:
            return {"apps": [{"name": "gimp"}], "installed_count": 1, "catalog_updated_at": "x"}

    monkeypatch.setattr(cli_apps_api, "_manager", lambda config=None: _StubManager())

    payload = cli_apps_api.cli_apps_payload()

    assert payload["enabled"] is True
    assert payload["apps"] == [{"name": "gimp"}]
