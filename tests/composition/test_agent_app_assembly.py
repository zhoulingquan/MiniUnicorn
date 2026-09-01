"""Smoke tests for the headless agent application composition root."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


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
    from miniunicorn.config.schema import Config

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return config


def test_build_agent_application_reuses_explicit_bus(tmp_path, monkeypatch):
    """Passing an explicit bus must not create a second MessageBus."""
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.composition.agent_app import build_agent_application

    config = _make_config(tmp_path)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )

    created: list[MessageBus] = []
    original_init = MessageBus.__init__

    def _counting_init(self):
        created.append(self)
        original_init(self)

    monkeypatch.setattr(MessageBus, "__init__", _counting_init)

    bus = MessageBus()
    created_before = len(created)

    loop = build_agent_application(config, bus=bus)

    assert len(created) == created_before
    assert loop.bus is bus


def test_build_agent_application_creates_default_bus(tmp_path, monkeypatch):
    """Without an explicit bus, build_agent_application creates exactly one."""
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.composition.agent_app import build_agent_application

    config = _make_config(tmp_path)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )

    created: list[MessageBus] = []
    original_init = MessageBus.__init__

    def _counting_init(self):
        created.append(self)
        original_init(self)

    monkeypatch.setattr(MessageBus, "__init__", _counting_init)

    loop = build_agent_application(config)

    assert len(created) == 1
    assert loop.bus is created[0]


def test_build_agent_application_forwards_extra_overrides(tmp_path, monkeypatch):
    """Extra keyword arguments are forwarded to AgentLoop.from_config."""
    from miniunicorn.composition.agent_app import build_agent_application

    config = _make_config(tmp_path)
    provider = _fake_provider()
    monkeypatch.setattr("miniunicorn.providers.factory.make_provider", lambda _config: provider)

    sentinel_session_manager = object()

    loop = build_agent_application(config, session_manager=sentinel_session_manager)

    assert loop.sessions is sentinel_session_manager
    assert loop.cron_service is not None
