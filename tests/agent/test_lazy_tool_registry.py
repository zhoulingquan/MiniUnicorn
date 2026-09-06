"""W1-1: tool registry loads lazily on first read.

AgentLoop previously registered its default tool set during ``__init__``,
paying the full module-import + instantiation cost even for state/management
operations. A ``LazyToolRegistry`` defers that hook until a tool is actually
read, and ``AgentLoop`` now wires it in so construction no longer loads tools.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from erza.agent.loop import AgentLoop
from erza.bus.queue import MessageBus
from erza.config.schema import Config
from erza.providers.base import LLMResponse
from erza.tools.base import Tool
from erza.tools.registry import LazyToolRegistry, ToolRegistry


class _FakeTool(Tool):
    name = "fake_tool"
    description = "fake tool for lazy-registry tests"

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    async def execute(self) -> str:
        return "ok"


def _registry(load_hook=None) -> LazyToolRegistry:
    return LazyToolRegistry(
        load_hook=load_hook or (lambda: None),
    )


# --- 1: constructed registry does not load -----------------------------------


def test_no_read_does_not_run_hook() -> None:
    called: list[str] = []

    def load_hook() -> None:
        called.append("load")

    _registry(load_hook)

    assert called == []


# --- 2: read triggers once, and only once ------------------------------------


def test_read_triggers_load_only_once() -> None:
    calls: list[str] = []

    def load_hook() -> None:
        calls.append("load")

    registry = _registry(load_hook)

    assert registry.has("x") is False
    assert registry.get("x") is None
    assert registry.get_definitions() == []
    registry.prepare_call("x", {})

    assert len(calls) == 1


# --- 3: register does not trigger load, tool survives -------------------------


def test_register_before_load_does_not_trigger_hook() -> None:
    calls: list[str] = []

    def load_hook() -> None:
        calls.append("load")

    registry = _registry(load_hook)
    fake = _FakeTool()
    registry.register(fake)

    assert calls == []
    assert registry.get("fake_tool") is fake
    assert len(calls) == 1


# --- 4: recursion protection -------------------------------------------------


def test_hook_calling_registry_does_not_recurse() -> None:
    calls: list[str] = []

    def load_hook() -> None:
        calls.append("load")
        registry.has("whatever")

    registry = _registry(load_hook)

    registry.get("anything")

    assert len(calls) == 1


# --- 4b: tool_names is a read and loads (loop tool-context propagation) -------


def test_tool_names_triggers_load() -> None:
    calls: list[str] = []

    def load_hook() -> None:
        calls.append("load")

    registry = _registry(load_hook)
    registry.register(_FakeTool())

    assert registry.tool_names == ["fake_tool"]
    assert len(calls) == 1


# --- 5: integration - AgentLoop construction does not load tools -------------


class _FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})


def _minimal_config(tmp_path) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": {"usePlanner": True}},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )


def test_agent_loop_does_not_load_tools_at_construction(monkeypatch, tmp_path) -> None:
    from erza.agent._mcp_lifecycle import McpLifecycleMixin

    calls: list[str] = []
    original = McpLifecycleMixin._register_default_tools

    def recording(self: AgentLoop) -> None:
        calls.append("load")
        original(self)

    # Patch the class method before construction so the LazyToolRegistry's
    # captured load hook is this recorder (it records + still registers tools).
    monkeypatch.setattr(McpLifecycleMixin, "_register_default_tools", recording)

    config = _minimal_config(tmp_path)
    loop = AgentLoop.from_config(
        config,
        bus=MessageBus(),
        provider=_FakeProvider(),
        session_manager=MagicMock(),
    )

    # Construction alone must not materialize any tools.
    assert calls == []

    # First real read triggers the load exactly once and yields the tool.
    tool = loop.tools.get("write_file")
    assert tool is not None
    assert calls == ["load"]


# --- 6: plain ToolRegistry public behavior is untouched -----------------------


def test_plain_tool_registry_behavior_unchanged() -> None:
    reg = ToolRegistry()
    fake = _FakeTool()
    reg.register(fake)

    assert reg.get("fake_tool") is fake
    assert reg.has("fake_tool") is True
    assert reg.tool_names == ["fake_tool"]
