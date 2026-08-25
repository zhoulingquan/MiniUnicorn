"""Integration test: verify execution-policy configuration propagates from Config
through AgentLoopBuilder to AgentLoop and TurnBudget."""

from __future__ import annotations

from typing import Any

import pytest

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.bus.events import InboundMessage
from miniunicorn.config.schema import Config
from miniunicorn.providers.base import LLMResponse


def _make_config(tmp_path) -> Config:
    return Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "usePlanner": True,
                    "plannerModel": "planner-model",
                    "plannerMaxReplans": 2,
                    "enableReflection": True,
                    "reflectionInterval": 3,
                    "maxInputTokensPerTurn": 1234,
                    "maxCostPerTurnUsd": 0.25,
                }
            },
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
            reasoning_content="hidden reasoning",
            thinking_blocks=[{"type": "thinking", "thinking": "step"}],
        )

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
            reasoning_content="hidden reasoning",
            thinking_blocks=[{"type": "thinking", "thinking": "step"}],
        )


class TestExecutionPolicyConfigPropagation:
    """Verify that all seven execution-policy fields flow from Config → Builder → Loop → Budget."""

    @pytest.mark.asyncio
    async def test_from_config_passes_all_fields_to_loop_and_budget(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        provider = FakeProvider()

        loop = AgentLoop.from_config(config, provider=provider)

        # All seven fields must be set on the loop
        assert loop.use_planner is True
        assert loop.planner_model == "planner-model"
        assert loop.planner_max_replans == 2
        assert loop.enable_reflection is True
        assert loop.reflection_interval == 3
        assert loop._max_input_tokens_per_turn == 1234
        assert loop._max_cost_per_turn_usd == 0.25

        # TurnBudget must be constructed with the configured limits
        budget = loop._build_turn_budget()
        assert budget is not None
        assert budget.max_input_tokens == 1234
        assert budget.max_cost_usd == 0.25
        assert budget.require_cost_tracking is True

    @pytest.mark.asyncio
    async def test_from_config_passes_fields_to_turn_spec(self, tmp_path) -> None:
        config = _make_config(tmp_path)
        provider = FakeProvider()

        loop = AgentLoop.from_config(config, provider=provider)

        # Monkeypatch runner.run to capture the spec
        captured_spec: dict | None = None

        original_run = loop.runner.run

        async def capturing_run(spec: Any) -> Any:
            nonlocal captured_spec
            captured_spec = spec
            return await original_run(spec)

        loop.runner.run = capturing_run

        # Process a message - this will call _run_agent_loop which builds the spec
        await loop._process_message(
            InboundMessage(
                channel="cli",
                sender_id="user",
                chat_id="test",
                content="hello",
            )
        )

        assert captured_spec is not None
        # Assert the five planner/reflection fields and attached budget in the spec
        assert captured_spec.use_planner is True
        assert captured_spec.planner_model == "planner-model"
        assert captured_spec.planner_max_replans == 2
        assert captured_spec.enable_reflection is True
        assert captured_spec.reflection_interval == 3
        assert captured_spec.turn_budget is not None
        assert captured_spec.turn_budget.max_input_tokens == 1234
        assert captured_spec.turn_budget.max_cost_usd == 0.25

    @pytest.mark.asyncio
    async def test_defaults_when_not_set_in_config(self) -> None:
        """When config omits fields, AgentDefaults values are used (False/None/3)."""
        config = Config.model_validate(
            {
                "agents": {
                    "defaults": {
                        "usePlanner": False,
                        "plannerModel": None,
                        "plannerMaxReplans": 3,
                        "enableReflection": False,
                        "reflectionInterval": 5,
                        "maxInputTokensPerTurn": None,
                        "maxCostPerTurnUsd": None,
                    }
                },
                "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
                "tools": {},
            }
        )

        loop = AgentLoop.from_config(config)

        assert loop.use_planner is False
        assert loop.planner_model is None
        assert loop.planner_max_replans == 3
        assert loop.enable_reflection is False
        assert loop.reflection_interval == 5
        assert loop._max_input_tokens_per_turn is None
        assert loop._max_cost_per_turn_usd is None

        budget = loop._build_turn_budget()
        # P2-T3: explicit fields unset → FAST tiered defaults (80k / $2).
        assert budget is not None
        assert budget.max_input_tokens == 80_000
        assert budget.max_cost_usd == 2.0
        assert budget.require_cost_tracking is False
