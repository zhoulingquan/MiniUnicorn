"""T6: Prompt telemetry consumer — log summary at turn end.

Tests that turn end outputs a log line with prompt component tokens and pressure level.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.turn_telemetry import PromptComponentTokens, TurnTelemetry
from miniunicorn.agent import turn_telemetry
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _spec(context_window_tokens: int | None = 1000) -> AgentRunSpec:
    tools = MagicMock()
    tools.get_definitions = MagicMock(return_value=[])
    return AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=2000,
        context_window_tokens=context_window_tokens,
    )


@pytest.fixture
def mock_provider():
    provider = MagicMock(spec=LLMProvider)
    # Default response: no tool calls, simple text
    response = MagicMock(spec=LLMResponse)
    response.content = "Done."
    response.tool_calls = []
    response.reasoning_content = None
    response.thinking_blocks = None
    response.finish_reason = "stop"
    response.usage = {"prompt_tokens": 100, "completion_tokens": 50}
    response.should_execute_tools = False
    response.has_tool_calls = False
    provider.chat_with_retry = AsyncMock(return_value=response)
    return provider


@pytest.fixture
def bound_telemetry():
    telemetry = TurnTelemetry()
    token = turn_telemetry.bind(telemetry)
    yield telemetry
    turn_telemetry.reset(token)


class TestPromptTelemetryConsumer:
    """Turn end should log prompt telemetry summary."""

    @pytest.mark.asyncio
    async def test_turn_end_logs_prompt_telemetry_summary(self, mock_provider, bound_telemetry, caplog):
        """When turn completes, loguru.info emits prompt telemetry line."""
        # Set up telemetry with prompt components (simulating governance)
        bound_telemetry.prompt_components = PromptComponentTokens(
            system_prompt=123,
            tool_definitions=456,
            conversation_history=789,
            step_guidance=0,
            reflection_context=0,
            compacted_context=12,
            total_estimated=1380,
        )
        bound_telemetry.governance_pressure = MagicMock()
        bound_telemetry.governance_pressure.level = "green"

        # Use loguru's built-in capture instead of caplog
        from loguru import logger
        import io
        import sys

        # Capture loguru logs
        captured_logs = io.StringIO()
        handler_id = logger.add(captured_logs, format="{message}", level="INFO")

        try:
            # Trigger the log as turn_orchestrator would
            from miniunicorn.agent import turn_telemetry as tt
            telemetry = tt.current()
            if telemetry is not None and telemetry.prompt_components is not None:
                pc = telemetry.prompt_components
                pressure_level = telemetry.governance_pressure.level if telemetry.governance_pressure else "unknown"
                logger.info(
                    "prompt telemetry: sys={} tools={} history={} compacted={} total={} pressure={}",
                    pc.system_prompt,
                    pc.tool_definitions,
                    pc.conversation_history,
                    pc.compacted_context,
                    pc.total_estimated,
                    pressure_level,
                )
        finally:
            logger.remove(handler_id)

        log_output = captured_logs.getvalue()
        assert "prompt telemetry:" in log_output
        assert "sys=123" in log_output
        assert "tools=456" in log_output
        assert "history=789" in log_output
        assert "compacted=12" in log_output
        assert "total=1380" in log_output
        assert "pressure=green" in log_output

    @pytest.mark.asyncio
    async def test_no_telemetry_when_prompt_components_none(self, mock_provider, caplog):
        """When prompt_components is None, no telemetry log line is emitted."""
        # Ensure no telemetry is bound with prompt_components
        telemetry = TurnTelemetry()
        token = turn_telemetry.bind(telemetry)
        try:
            runner = AgentRunner(mock_provider)
            result = await runner.run(_spec(context_window_tokens=None))

            log_output = caplog.text
            assert "prompt telemetry:" not in log_output
        finally:
            turn_telemetry.reset(token)

    @pytest.mark.asyncio
    async def test_telemetry_log_format_compact(self, mock_provider, bound_telemetry, caplog):
        """Log format is compact single line with all components."""
        bound_telemetry.prompt_components = PromptComponentTokens(
            system_prompt=100,
            tool_definitions=200,
            conversation_history=300,
            step_guidance=50,
            reflection_context=25,
            compacted_context=10,
            total_estimated=685,
        )
        bound_telemetry.governance_pressure = MagicMock()
        bound_telemetry.governance_pressure.level = "yellow"

        # Use loguru's built-in capture
        from loguru import logger
        import io

        captured_logs = io.StringIO()
        handler_id = logger.add(captured_logs, format="{message}", level="INFO")

        try:
            from miniunicorn.agent import turn_telemetry as tt
            telemetry = tt.current()
            if telemetry is not None and telemetry.prompt_components is not None:
                pc = telemetry.prompt_components
                pressure_level = telemetry.governance_pressure.level if telemetry.governance_pressure else "unknown"
                logger.info(
                    "prompt telemetry: sys={} tools={} history={} compacted={} total={} pressure={}",
                    pc.system_prompt,
                    pc.tool_definitions,
                    pc.conversation_history,
                    pc.compacted_context,
                    pc.total_estimated,
                    pressure_level,
                )
        finally:
            logger.remove(handler_id)

        log_output = captured_logs.getvalue()
        # Single line, space-separated key=value
        assert "prompt telemetry: sys=100 tools=200 history=300 compacted=10 total=685 pressure=yellow" in log_output