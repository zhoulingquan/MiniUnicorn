"""Smoke tests for the gateway composition root.

These tests exercise the real assembly path of ``GatewayApplication`` (the
static composition root moved out of ``cli/_gateway_runner.py``) with a real
``Config``, real ``MessageBus`` / ``SessionManager`` / ``CronService`` and a
real ``AgentLoop``; only the provider factory is stubbed so construction is
hermetic (no network / API keys).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from erza.composition.gateway import GatewayApplication
from erza.providers.factory import ProviderSnapshot


def _fake_provider():
    """Return a minimal fake provider that satisfies AgentLoop construction."""
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


def _make_config(tmp_path: Path):
    from erza.config.schema import Config

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return config


def _snapshot(provider) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider=provider,
        model="test-model",
        context_window_tokens=128_000,
        signature=("test",),
    )


def _patch_provider_factory(monkeypatch, provider):
    """Stub the provider factory so real AgentLoop construction is hermetic."""

    def _build_snapshot(_config) -> ProviderSnapshot:
        return _snapshot(provider)

    def _load_snapshot(_config_path=None, **kwargs) -> ProviderSnapshot:
        return _snapshot(provider)

    monkeypatch.setattr("erza.providers.factory.build_provider_snapshot", _build_snapshot)
    monkeypatch.setattr("erza.providers.factory.load_provider_snapshot", _load_snapshot)
    monkeypatch.setattr("erza.providers.factory.make_provider", lambda _config: provider)
    return _load_snapshot


def test_gateway_application_assembles_all_parts(tmp_path, monkeypatch):
    """Assembly products are non-None and provider matches the snapshot loader."""
    config = _make_config(tmp_path)
    provider = _fake_provider()
    loader = _patch_provider_factory(monkeypatch, provider)

    app = GatewayApplication(config)

    assert app.bus is not None
    assert app.session_manager is not None
    assert app.cron is not None
    assert app.agent is not None
    assert app.channels is not None

    # The agent was built with the pre-built provider snapshot…
    assert app.agent.provider is provider
    # …and the runtime snapshot loader passed to AgentLoop is the one provided
    # by the gateway composition root (load_provider_snapshot).
    assert app.agent._provider_snapshot_loader is loader


async def test_stop_shuts_down_in_reverse_order(tmp_path, monkeypatch):
    """stop() runs the legacy layered cleanup in its exact existing order."""
    config = _make_config(tmp_path)
    provider = _fake_provider()
    _patch_provider_factory(monkeypatch, provider)

    app = GatewayApplication(config)

    order: list[str] = []

    agent = MagicMock()
    agent.close_mcp = AsyncMock(side_effect=lambda: order.append("agent.close_mcp"))
    agent.stop = MagicMock(side_effect=lambda: order.append("agent.stop"))
    agent._resources.shutdown = AsyncMock(side_effect=lambda: order.append("registry.shutdown"))
    agent.sessions.flush_all = MagicMock(
        return_value=0, side_effect=lambda: order.append("sessions.flush_all")
    )
    cron = MagicMock()
    cron.stop = MagicMock(side_effect=lambda: order.append("cron.stop"))
    cron.await_stop = AsyncMock(side_effect=lambda: order.append("cron.await_stop"))
    channels = MagicMock()
    channels.stop_all = AsyncMock(side_effect=lambda: order.append("channels.stop_all"))

    app.agent, app.cron, app.channels = agent, cron, channels

    await app.stop()

    assert order == [
        "agent.close_mcp",
        "cron.stop",
        "cron.await_stop",
        "agent.stop",
        "registry.shutdown",
        "channels.stop_all",
        "sessions.flush_all",
    ]
