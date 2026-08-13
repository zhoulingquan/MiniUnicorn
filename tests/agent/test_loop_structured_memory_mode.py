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
    from miniunicorn.bus.queue import MessageBus

    return MessageBus()


@pytest.fixture
def provider():
    from miniunicorn.providers.base import GenerationSettings

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


def _write_sources(workspace: Path) -> None:
    (workspace / "USER.md").write_text("## Identity\n- 我叫 OOOH\n", encoding="utf-8")
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "MEMORY.md").write_text(
        "## Decision\n- 用 SQLite 做存储\n", encoding="utf-8"
    )
    (workspace / "memory" / "procedural.jsonl").write_text(
        '{"content": "先跑测试再打包"}\n', encoding="utf-8"
    )


def _build_loop(bus, provider, workspace):
    from miniunicorn.agent.loop_builder import AgentLoopBuilder

    return AgentLoopBuilder(bus, provider, workspace).build()


@pytest.mark.asyncio
async def test_loop_has_no_structured_memory_mode(workspace, bus, provider):
    _write_sources(workspace)
    loop = _build_loop(bus, provider, workspace)
    capture = _CaptureRunner()
    loop.runner = capture  # type: ignore[assignment]

    await loop._run_agent_loop([])

    assert capture.spec is not None
    assert not hasattr(capture.spec, "structured_memory_mode")
