"""AgentLoop uses the single governed structured-memory path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def bus():
    from erza.bus.queue import MessageBus

    return MessageBus()


@pytest.fixture
def provider():
    from erza.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


class _CaptureRunner:
    """Fake runner that records the AgentRunSpec passed to run()."""

    def __init__(self):
        self.spec = None

    async def run(self, spec):
        self.spec = spec
        result = MagicMock()
        result.stop_reason = "completed"
        result.usage = {}
        result.last_call_usage = {}
        result.final_content = "done"
        result.tools_used = []
        result.messages = []
        result.had_injections = False
        return result


def _build_loop(bus, provider, workspace):
    from erza.agent.loop_builder import AgentLoopBuilder

    return AgentLoopBuilder(bus, provider, workspace).build()


@pytest.mark.asyncio
async def test_loop_has_no_structured_memory_mode(workspace, bus, provider):
    loop = _build_loop(bus, provider, workspace)
    capture = _CaptureRunner()
    loop.runner = capture  # type: ignore[assignment]

    await loop._run_agent_loop([])

    assert capture.spec is not None
    assert not hasattr(capture.spec, "structured_memory_mode")


@pytest.mark.asyncio
async def test_run_agent_loop_propagates_turn_identity_into_spec(workspace, bus, provider):
    loop = _build_loop(bus, provider, workspace)
    capture = _CaptureRunner()
    loop.runner = capture  # type: ignore[assignment]

    await loop._run_agent_loop(
        [],
        session_key="web:chat-7#sub:task-1",
        user_key="user:alice",
    )

    assert capture.spec is not None
    assert capture.spec.session_key == "web:chat-7#sub:task-1"
    assert capture.spec.user_key == "user:alice"
