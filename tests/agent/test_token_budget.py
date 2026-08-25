"""T4: Tool result budget unified token口径.

Tests that max_tool_result_tokens is the primary configuration口径,
max_tool_result_chars is deprecated compat, and the two convert correctly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.bus.events import InboundMessage
from miniunicorn.config.schema import Config
from miniunicorn.providers.base import LLMResponse


class _FakeProvider:
    """Minimal provider stand-in."""

    class Generation:
        max_tokens = 8192

    generation = Generation()

    def get_default_model(self) -> str:
        return "test-model"

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
            reasoning_content="",
            thinking_blocks=[],
        )

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
            reasoning_content="",
            thinking_blocks=[],
        )


def _make_config(**overrides: Any) -> Config:
    base = {
        "agents": {"defaults": {}},
        "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
        "tools": {},
    }
    base["agents"]["defaults"].update(overrides)
    return Config.model_validate(base)


class TestTokenBudgetConfig:
    """Verify token口径 is primary, chars is deprecated compat."""

    @pytest.mark.asyncio
    async def test_default_tokens_equivalent_to_legacy_chars(self) -> None:
        """No explicit config → spec.max_tool_result_chars == 16000 (4000 * 4)."""
        config = _make_config()
        provider = _FakeProvider()
        loop = AgentLoop.from_config(config, provider=provider)

        # AgentLoop exposes both attributes
        assert loop.max_tool_result_tokens == 4000
        assert loop.max_tool_result_chars == 16000

        # Verify AgentRunSpec gets the chars value (16000)
        captured_spec: dict | None = None
        original_run = loop.runner.run

        async def capturing_run(spec: Any) -> Any:
            nonlocal captured_spec
            captured_spec = spec
            return await original_run(spec)

        loop.runner.run = capturing_run

        await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="test", content="hello")
        )

        assert captured_spec is not None
        assert captured_spec.max_tool_result_chars == 16000

    @pytest.mark.asyncio
    async def test_explicit_tokens_wins(self) -> None:
        """maxToolResultTokens=1000 → chars == 4000."""
        config = _make_config(maxToolResultTokens=1000)
        provider = _FakeProvider()
        loop = AgentLoop.from_config(config, provider=provider)

        assert loop.max_tool_result_tokens == 1000
        assert loop.max_tool_result_chars == 4000

        captured_spec: dict | None = None
        original_run = loop.runner.run

        async def capturing_run(spec: Any) -> Any:
            nonlocal captured_spec
            captured_spec = spec
            return await original_run(spec)

        loop.runner.run = capturing_run

        await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="test", content="hello")
        )

        assert captured_spec is not None
        assert captured_spec.max_tool_result_chars == 4000

    @pytest.mark.asyncio
    async def test_explicit_chars_backcompat(self) -> None:
        """maxToolResultChars=8000 → chars == 8000 (tokens=2000)."""
        config = _make_config(maxToolResultChars=8000)
        provider = _FakeProvider()
        loop = AgentLoop.from_config(config, provider=provider)

        assert loop.max_tool_result_tokens == 2000
        assert loop.max_tool_result_chars == 8000

        captured_spec: dict | None = None
        original_run = loop.runner.run

        async def capturing_run(spec: Any) -> Any:
            nonlocal captured_spec
            captured_spec = spec
            return await original_run(spec)

        loop.runner.run = capturing_run

        await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="test", content="hello")
        )

        assert captured_spec is not None
        assert captured_spec.max_tool_result_chars == 8000

    @pytest.mark.asyncio
    async def test_chars_beats_tokens_when_both_set(self) -> None:
        """Both set → chars wins (tokens derived from chars)."""
        config = _make_config(maxToolResultTokens=5000, maxToolResultChars=12000)
        provider = _FakeProvider()
        loop = AgentLoop.from_config(config, provider=provider)

        # chars wins: 12000 chars → 3000 tokens
        assert loop.max_tool_result_tokens == 3000
        assert loop.max_tool_result_chars == 12000

        captured_spec: dict | None = None
        original_run = loop.runner.run

        async def capturing_run(spec: Any) -> Any:
            nonlocal captured_spec
            captured_spec = spec
            return await original_run(spec)

        loop.runner.run = capturing_run

        await loop._process_message(
            InboundMessage(channel="cli", sender_id="user", chat_id="test", content="hello")
        )

        assert captured_spec is not None
        assert captured_spec.max_tool_result_chars == 12000

    @pytest.mark.asyncio
    async def test_red_pressure_halves_effective_budget(self) -> None:
        """RED pressure halves the effective tool result budget (chars halved)."""
        from miniunicorn.agent.context_governor import PressureLevel
        from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
        from miniunicorn.agent.tools.registry import ToolRegistry

        provider = _FakeProvider()
        runner = AgentRunner(provider)

        # Use a small context window to trigger RED pressure
        tools = MagicMock(spec=ToolRegistry)
        tools.get_definitions = MagicMock(return_value=[])

        spec = AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=1000,  # Base budget
            context_window_tokens=100,
        )

        long_result = "x" * 2000
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "test", "content": long_result},
        ]

        from miniunicorn.agent.context_governor import GovernanceContext, PressureSignal

        ctx = GovernanceContext(
            spec=spec,
            tools=tools,
            provider=provider,
            iteration=0,
            runner=runner,
        )
        ctx.pressure = PressureSignal(85, 100, 0.85, PressureLevel.RED)

        from miniunicorn.agent.runner_strategies import ApplyToolResultBudgetStrategy

        strategy = ApplyToolResultBudgetStrategy()
        result = strategy.apply(messages, ctx)
        tool_content = [m for m in result if m.get("role") == "tool"][0]["content"]

        # On RED, max_tool_result_chars is halved to 500 (plus truncation notice)
        NOTICE_LEN = len("\n... (truncated)")
        assert len(tool_content) <= 500 + NOTICE_LEN

    @pytest.mark.asyncio
    async def test_loop_exposes_token_attribute(self) -> None:
        """AgentLoop exposes max_tool_result_tokens attribute."""
        config = _make_config()
        provider = _FakeProvider()
        loop = AgentLoop.from_config(config, provider=provider)

        assert hasattr(loop, "max_tool_result_tokens")
        assert loop.max_tool_result_tokens == 4000
        assert loop.max_tool_result_chars == 16000
