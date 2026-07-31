"""Tests for production lightweight composition (design Task 5).

Verifies ``build_lightweight_runtime`` assembles the full durable kernel
into a lifecycle-managed ``RuntimeResources`` object and that a real
turn can execute end-to-end against a fake provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from miniunicorn.providers.base import LLMProvider, LLMResponse
from miniunicorn.runtime.application import RuntimeInboundRequest
from miniunicorn.runtime.bootstrap import build_lightweight_runtime
from miniunicorn.runtime.ingress import local_request_scope
from miniunicorn.runtime.models import RequestScope

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------


class _FakeProvider(LLMProvider):
    """Fake provider returning a canned response for bootstrap tests."""

    def __init__(self, response: LLMResponse | None = None) -> None:
        super().__init__()
        self._response = response or LLMResponse(
            content="hello from fake provider",
            finish_reason="stop",
        )

    def get_default_model(self) -> str:
        return "fake-model"

    async def chat(
        self,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return self._response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_provider() -> _FakeProvider:
    return _FakeProvider()


@pytest.fixture
def runtime_root_config(tmp_path: Path) -> Any:
    """A root Config with a temporary workspace and lightweight runtime."""
    from miniunicorn.config.schema import Config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "provider": "custom",
                    "model": "fake-model",
                    "contextWindowTokens": 8192,
                },
            },
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "http://localhost:1/v1",
                },
            },
            "runtime": {
                "mode": "lightweight",
                "workerCount": 2,
                "lightweightExecutionSlots": 1,
                "heartbeatIntervalS": 2,
                "leaseTimeoutS": 30,
                "progressTimeoutS": 60,
                "queuePollMaxMs": 500,
                "leaseScanIntervalS": 5,
                "outboxLeaseTimeoutS": 30,
                "channelSendTimeoutS": 10,
                "sqliteBusyTimeoutMs": 1000,
            },
        }
    )
    return config


@pytest.fixture
def sample_scope(runtime_root_config: Any) -> RequestScope:
    return local_request_scope(runtime_root_config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildLightweightRuntime:
    """build_lightweight_runtime assembles a working RuntimeResources."""

    @pytest.mark.asyncio
    async def test_lightweight_bootstrap_starts_and_stops(
        self,
        runtime_root_config: Any,
        fake_provider: _FakeProvider,
    ) -> None:
        resources = build_lightweight_runtime(
            runtime_root_config,
            provider_override=fake_provider,
        )
        await resources.start()
        try:
            assert resources.application is not None
            assert resources.host.worker_count == 1
            assert resources.outbox_sender.is_running
        finally:
            await resources.stop()

    @pytest.mark.asyncio
    async def test_lightweight_bootstrap_runs_a_real_turn(
        self,
        runtime_root_config: Any,
        fake_provider: _FakeProvider,
        sample_scope: RequestScope,
    ) -> None:
        resources = build_lightweight_runtime(
            runtime_root_config,
            provider_override=fake_provider,
        )
        await resources.start()
        try:
            result = await resources.application.submit_and_wait(
                RuntimeInboundRequest(
                    content="hello",
                    media=(),
                    metadata={},
                    session_key="cli:test",
                    channel="cli",
                    channel_account="local-user",
                    channel_message_id=None,
                    scope=sample_scope,
                ),
                timeout_s=30,
            )
            assert result.snapshot.state == "COMPLETED"
        finally:
            await resources.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(
        self,
        runtime_root_config: Any,
        fake_provider: _FakeProvider,
    ) -> None:
        resources = build_lightweight_runtime(
            runtime_root_config,
            provider_override=fake_provider,
        )
        await resources.start()
        await resources.stop()
        # Second stop must not raise.
        await resources.stop()

    @pytest.mark.asyncio
    async def test_stop_accepting_rejects_new_submit(
        self,
        runtime_root_config: Any,
        fake_provider: _FakeProvider,
        sample_scope: RequestScope,
    ) -> None:
        resources = build_lightweight_runtime(
            runtime_root_config,
            provider_override=fake_provider,
        )
        await resources.start()
        try:
            resources.application.stop_accepting()
            with pytest.raises(RuntimeError, match="draining"):
                await resources.application.submit(
                    RuntimeInboundRequest(
                        content="hello",
                        media=(),
                        metadata={},
                        session_key="cli:test",
                        channel="cli",
                        channel_account="local-user",
                        channel_message_id=None,
                        scope=sample_scope,
                    )
                )
        finally:
            await resources.stop()
