"""P1-T1: PlanningPolicy — deterministic FAST/MANAGED selection.

Covers the PlanningMode/PlanningPolicy contract, AgentLoop resolution
(direct policy + use_planner backward compat), AgentRunSpec propagation,
and init_planner() behavior in both modes.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.config.schema import Config
from miniunicorn.providers.base import LLMProvider, LLMResponse


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return LLMResponse(content="", tool_calls=[], usage={})


def _make_config(tmp_path) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": {"usePlanner": True}},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


# --- 1-5: PlanningPolicy dataclass contract ---------------------------------


def test_planning_mode_enum_values() -> None:
    assert PlanningMode.FAST == "fast"
    assert PlanningMode.MANAGED == "managed"


def test_planning_policy_defaults() -> None:
    policy = PlanningPolicy()
    assert policy.mode == PlanningMode.FAST
    assert policy.planner_model is None
    assert policy.planner_max_replans == 3


def test_from_use_planner_true_maps_to_managed() -> None:
    policy = PlanningPolicy.from_use_planner(True)
    assert policy.mode == PlanningMode.MANAGED


def test_from_use_planner_false_maps_to_fast() -> None:
    policy = PlanningPolicy.from_use_planner(False)
    assert policy.mode == PlanningMode.FAST


def test_from_use_planner_preserves_model_and_replans() -> None:
    policy = PlanningPolicy.from_use_planner(True, planner_model="planner-x", planner_max_replans=5)
    assert policy.mode == PlanningMode.MANAGED
    assert policy.planner_model == "planner-x"
    assert policy.planner_max_replans == 5


# --- 6-7: AgentLoop resolution ----------------------------------------------


def test_agent_loop_accepts_direct_planning_policy(tmp_path) -> None:
    policy = PlanningPolicy(mode=PlanningMode.MANAGED)
    loop = AgentLoop(
        bus=MessageBus(),
        workspace=tmp_path,
        provider=FakeProvider(),
        planning_policy=policy,
    )
    assert loop.planning_policy is policy
    assert loop.planning_policy.mode == PlanningMode.MANAGED


def test_agent_loop_backward_compat_use_planner_true(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        workspace=tmp_path,
        provider=FakeProvider(),
        use_planner=True,
    )
    assert loop.use_planner is True
    assert loop.planning_policy.mode == PlanningMode.MANAGED


# --- 8: AgentRunSpec propagation ---------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_spec_carries_planning_policy(tmp_path) -> None:
    loop = AgentLoop.from_config(_make_config(tmp_path), provider=FakeProvider())
    captured_spec: AgentRunSpec | None = None
    original_run = loop.runner.run

    async def capturing_run(spec: Any) -> Any:
        nonlocal captured_spec
        captured_spec = spec
        return await original_run(spec)

    loop.runner.run = capturing_run

    await loop._process_message(
        InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="test",
            content="hello",
        )
    )

    assert captured_spec is not None
    assert captured_spec.planning_policy is not None
    assert captured_spec.planning_policy.mode == PlanningMode.MANAGED


# --- 9-10: init_planner mode dispatch ----------------------------------------


@pytest.mark.asyncio
async def test_fast_mode_skips_planner() -> None:
    provider = MagicMock(spec=LLMProvider)
    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "ship"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.FAST),
    )

    result = await runner._init_planner(spec)

    assert result == (None, None, None, None)


@pytest.mark.asyncio
async def test_managed_mode_invokes_planner_create_plan() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "test"}]}),
            tool_calls=[],
            usage={},
        )
    )
    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "ship"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=1000,
        planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
    )

    planner, plan, task_text, _tools_summary = await runner._init_planner(spec)

    assert planner is not None
    assert plan is not None
    assert plan.goal == "ship"
    provider.chat_with_retry.assert_awaited_once()
