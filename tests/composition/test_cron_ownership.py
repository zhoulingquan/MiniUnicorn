"""W1-2 Commit 1: guard that cron ownership stays at the composition root.

CronService is already owned by the composition root: the gateway creates it
and is the only entry point that starts/stops it; the agent application path
only constructs/injects it.  These tests lock that in so a future batch cannot
silently move cron's start/stop back into AgentLoop.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.factory import ProviderSnapshot


class _FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        from miniunicorn.providers.base import LLMResponse

        return LLMResponse(content="", tool_calls=[], usage={})

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        from miniunicorn.providers.base import LLMResponse

        return LLMResponse(content="", tool_calls=[], usage={})


def _fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


@pytest.mark.asyncio
async def test_agent_loop_turn_never_touches_cron_lifecycle(tmp_path: Path, monkeypatch) -> None:
    """Running one minimal turn must not start or stop the injected cron."""
    from miniunicorn.agent.loop import AgentLoop
    from miniunicorn.cron.service import CronService

    start_spy = AsyncMock(wraps=CronService.start)
    stop_spy = MagicMock(wraps=CronService.stop)
    monkeypatch.setattr(CronService, "start", start_spy)
    monkeypatch.setattr(CronService, "stop", stop_spy)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeProvider(),
        workspace=tmp_path,
        model="test-model",
        cron_service=CronService(tmp_path / "cron" / "jobs.json"),
    )

    await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="test",
            content="hello",
        )
    )

    start_spy.assert_not_awaited()
    stop_spy.assert_not_called()


def test_gateway_start_invokes_cron_start(tmp_path: Path, monkeypatch) -> None:
    """The gateway startup sequence calls cron.start (ownership lives there)."""
    from miniunicorn.composition.gateway import GatewayApplication

    def _build_snapshot(_config) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider=_fake_provider(),
            model="test-model",
            context_window_tokens=128_000,
            signature=("test",),
        )

    monkeypatch.setattr("miniunicorn.providers.factory.build_provider_snapshot", _build_snapshot)
    monkeypatch.setattr("miniunicorn.providers.factory.load_provider_snapshot", _build_snapshot)
    monkeypatch.setattr(
        "miniunicorn.providers.factory.make_provider", lambda _config: _fake_provider()
    )

    _config = _make_config(tmp_path)
    app = GatewayApplication(_config)

    cron_start_spy = AsyncMock()
    app.cron.start = cron_start_spy
    app.cron.stop = MagicMock()

    # Neutralise the blocking gather and the layered teardown so _run returns.
    app.agent.run = AsyncMock(return_value=None)
    app.channels.start_all = AsyncMock(return_value=None)
    app.agent.close_mcp = AsyncMock(return_value=None)
    app.agent.stop = MagicMock()
    app.agent._resources.shutdown = AsyncMock(return_value=None)
    app.channels.stop_all = AsyncMock(return_value=None)
    app.agent.sessions.flush_all = MagicMock(return_value=0)

    import asyncio

    asyncio.run(app._run())

    cron_start_spy.assert_awaited_once()


def _make_config(tmp_path: Path):
    from miniunicorn.config.schema import Config

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return config
