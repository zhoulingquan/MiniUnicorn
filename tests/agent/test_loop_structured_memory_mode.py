"""AgentLoop -> AgentRunSpec structured_memory_mode wiring (C2 plan B task 3).

Proves the configured StructuredMemoryConfig mode is propagated into the
AgentRunSpec handed to the runner for governed, shadow and legacy (None) loops.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from miniunicorn.config.schema import StructuredMemoryConfig


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


def _build_loop(bus, provider, workspace, mode):
    from miniunicorn.agent.loop_builder import AgentLoopBuilder

    builder = AgentLoopBuilder(bus, provider, workspace)
    if mode is None:
        return builder.build()
    return builder.with_structured_memory_config(StructuredMemoryConfig(mode=mode)).build()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("governed", "governed"),
        ("shadow", "shadow"),
        ("legacy", "legacy"),
        (None, None),
    ],
)
async def test_loop_propagates_structured_memory_mode(workspace, bus, provider, mode, expected):
    from miniunicorn.agent.memory import MemoryStore

    _write_sources(workspace)
    if mode == "governed":
        store = MemoryStore(workspace, structured_config=StructuredMemoryConfig(mode="governed"))
        store.run_migration()

    loop = _build_loop(bus, provider, workspace, mode)
    capture = _CaptureRunner()
    loop.runner = capture  # type: ignore[assignment]

    await loop._run_agent_loop([])

    assert capture.spec is not None
    assert capture.spec.structured_memory_mode == expected
