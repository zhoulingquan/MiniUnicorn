"""Provider hot-switch isolation behavior tests.

Locks the promise in ``_apply_provider_snapshot``'s docstring: swapping
model/provider applies to *future* turns only and must not disturb the LLM
request that is currently executing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.bus.events import InboundMessage
from miniunicorn.config.schema import ModelPresetConfig
from miniunicorn.providers.base import LLMResponse
from miniunicorn.providers.factory import ProviderSnapshot
from tests.agent.conftest import make_loop


def _make_provider(default_model: str = "test-model") -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    return provider


@pytest.mark.asyncio
async def test_hot_switch_does_not_disturb_inflight_request(tmp_path):
    old_provider = _make_provider(default_model="old-model")
    new_provider = _make_provider(default_model="new-model")

    request_started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_chat(**kwargs):
        request_started.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return LLMResponse(content="from-old-provider", tool_calls=[], usage={})

    old_provider.chat_with_retry = blocking_chat
    new_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="from-new-provider", tool_calls=[], usage={})
    )

    def _snapshot_loader(name: str) -> ProviderSnapshot:
        assert name == "fast"
        return ProviderSnapshot(
            provider=new_provider,
            model="new-model",
            context_window_tokens=128_000,
            signature=("new-provider",),
        )

    loop = make_loop(
        tmp_path,
        provider=old_provider,
        model_presets={"fast": ModelPresetConfig(model="new-model")},
    )
    loop._preset_snapshot_loader = _snapshot_loader

    msg = InboundMessage(channel="cli", sender_id="user", chat_id="c1", content="hello")
    turn = asyncio.create_task(loop._process_message(msg))
    await asyncio.wait_for(request_started.wait(), timeout=2)

    loop.set_model_preset("fast")

    release.set()
    result = await asyncio.wait_for(turn, timeout=5)

    assert result is not None
    assert result.content == "from-old-provider"
    new_provider.chat_with_retry.assert_not_awaited()