"""W0-A1: ToolObservation — structured tool evidence plumbing.

Covers observation construction and pairing, argument isolation, excerpt
truncation, defensive length handling, delivery into ``StepEvidence`` from an
earlier iteration, and clearing of accumulated observations on replan.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.execution import tool_execution as tool_execution_module
from miniunicorn.agent.execution.tool_execution import ToolExecutionCoordinator
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.step_acceptance import (
    StepAcceptancePolicy,
    StepEvidence,
    ToolObservation,
)
from miniunicorn.agent.tools.base import Tool
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _coordinator() -> ToolExecutionCoordinator:
    return ToolExecutionCoordinator(AgentRunner(MagicMock(spec=LLMProvider)))


def _request(name: str, arguments: dict[str, Any] | None = None) -> ToolCallRequest:
    return ToolCallRequest(id=f"call_{name}", name=name, arguments=arguments or {})


def _ok_event(name: str) -> dict[str, Any]:
    return {"name": name, "status": "ok"}


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo the given text"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return f"echo:{kwargs.get('text', '')}"


class _BoomTool(Tool):
    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "Always raises"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        raise RuntimeError("boom")


# --- 1: index-wise pairing of calls, results and events ----------------------


def test_build_observations_pairs_calls_with_results_in_order() -> None:
    calls = [_request("a"), _request("b"), _request("c")]
    results: list[Any] = ["ok-a", "ok-b", None]
    events = [
        _ok_event("a"),
        _ok_event("b"),
        {"name": "c", "status": "error", "detail": "boom"},
    ]

    observations = _coordinator().build_observations(calls, results, events, step_id=7)

    assert [o.tool_name for o in observations] == ["a", "b", "c"]
    assert [o.status for o in observations] == ["ok", "ok", "error"]
    assert [o.result_excerpt for o in observations] == ["ok-a", "ok-b", ""]
    assert [o.step_id for o in observations] == [7, 7, 7]
    assert all(o.receipt is None for o in observations)
    assert all(o.occurred_at for o in observations)


# --- 2: arguments are isolated from later mutation ---------------------------


def test_arguments_are_deep_copied() -> None:
    arguments: dict[str, Any] = {"nested": {"value": 1}}

    observations = _coordinator().build_observations(
        [_request("a", arguments)], ["ok"], [_ok_event("a")]
    )

    arguments["nested"]["value"] = 2

    assert observations[0].arguments["nested"]["value"] == 1


# --- 3: result excerpt truncation --------------------------------------------


def test_result_excerpt_truncated_to_200_chars() -> None:
    observations = _coordinator().build_observations([_request("a")], ["x" * 500], [_ok_event("a")])

    assert len(observations[0].result_excerpt) == 200


# --- 4: length mismatch degrades instead of raising --------------------------


def test_length_mismatch_warns_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        tool_execution_module,
        "logger",
        MagicMock(warning=lambda message, *args, **kwargs: warnings.append(message)),
    )

    observations = _coordinator().build_observations(
        [_request("a"), _request("b")], ["only-one"], [_ok_event("a")]
    )

    assert len(observations) == 1
    assert observations[0].tool_name == "a"
    assert any("mismatch" in message for message in warnings)


# --- 5: observations from an earlier iteration reach the acceptance policy ---


@pytest.mark.asyncio
async def test_observations_reach_step_evidence_across_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    real_evaluate = StepAcceptancePolicy.evaluate

    def _spy(self: StepAcceptancePolicy, *args: Any, **kwargs: Any) -> StepEvidence:
        captured.append(kwargs)
        return real_evaluate(self, *args, **kwargs)

    monkeypatch.setattr(StepAcceptancePolicy, "evaluate", _spy)

    tools = ToolRegistry()
    tools.register(_EchoTool())
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=json.dumps(
                    {
                        "goal": "ship",
                        "steps": [{"id": 1, "action": "do a", "done_criteria": "a done"}],
                    }
                ),
                usage={},
            ),
            LLMResponse(
                content=None,
                tool_calls=[_request("echo", {"text": "hi"})],
                usage={},
            ),
            LLMResponse(content="a done", usage={}),
        ]
    )

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=tools,
            model="test-model",
            max_iterations=6,
            max_tool_result_chars=1000,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
        )
    )

    assert captured, "step acceptance was never evaluated"
    observations = captured[0]["observations"]
    assert [o.tool_name for o in observations] == ["echo"]
    assert observations[0].step_id == 1
    assert observations[0].status == "ok"
    assert observations[0].result_excerpt == "echo:hi"
    # The run-level audit surface carries the same observation.
    assert [o["tool_name"] for o in result.tool_observations] == ["echo"]


# --- 6: accumulated observations are dropped when the plan is replaced -------


@pytest.mark.asyncio
async def test_observations_cleared_after_replan(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[list[ToolObservation]] = []
    real_build = ToolExecutionCoordinator.build_observations

    def _spy(
        self: ToolExecutionCoordinator,
        tool_calls: list[ToolCallRequest],
        results: list[Any],
        events: list[dict[str, Any]],
        *,
        step_id: int | None = None,
    ) -> list[ToolObservation]:
        observations = real_build(self, tool_calls, results, events, step_id=step_id)
        built.append(observations)
        return observations

    monkeypatch.setattr(ToolExecutionCoordinator, "build_observations", _spy)

    tools = ToolRegistry()
    tools.register(_BoomTool())
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "do a"}]}),
                usage={},
            ),
            LLMResponse(content=None, tool_calls=[_request("boom")], usage={}),
            LLMResponse(
                content=json.dumps({"goal": "ship", "steps": [{"id": 1, "action": "redo a"}]}),
                usage={},
            ),
            LLMResponse(content="all done", usage={}),
        ]
    )

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=tools,
            model="test-model",
            max_iterations=6,
            max_tool_result_chars=1000,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
            fail_on_tool_error=True,
        )
    )

    # The failing call was observed before the replan...
    assert any(o.tool_name == "boom" for observations in built for o in observations)
    # ...and the replacement plan starts from an empty evidence set.
    assert result.tool_observations == []


# --- 7: StepEvidence serialization -------------------------------------------


def test_step_evidence_dict_carries_observations_only_when_present() -> None:
    empty = StepEvidence(
        step_id=1,
        tool_calls=[],
        tool_results=[],
        final_content="done",
        iterations_used=1,
        accepted=True,
    )
    assert "observations" not in empty.to_dict()

    observation = ToolObservation(
        tool_name="echo",
        arguments={"text": "hi"},
        status="ok",
        result_excerpt="echo:hi",
        step_id=1,
    )
    filled = StepEvidence(
        step_id=1,
        tool_calls=[],
        tool_results=[],
        final_content="done",
        iterations_used=1,
        accepted=True,
        observations=[observation.to_dict()],
    )

    assert filled.to_dict()["observations"][0]["tool_name"] == "echo"
    # receipt stays out of the serialized observation until W0-A2 fills it.
    assert "receipt" not in observation.to_dict()
