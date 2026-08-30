"""W1-3: ``activate_plan`` activator tool + main-loop adoption.

``activate_plan`` is an *activator*, not an executor: it parses a JSON plan,
stashes it on a turn-scoped contextvar, and returns immediately. The main loop
adopts it on the next iteration and the existing plan-driving machinery
(step guidance + acceptance) advances it. These tests lock the tool semantics,
the adoption path (replan budget, snapshot origin, observations clear), and the
end-to-end activate -> receipt -> accept closure.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.planner import Plan, PlanStep, StepStatus
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec, _TurnState
from miniunicorn.agent.tools.activate_plan import ActivatePlanTool, take_pending_plan
from miniunicorn.agent.tools.context import ToolContext
from miniunicorn.agent.tools.filesystem import WriteFileTool
from miniunicorn.agent.tools.loader import ToolLoader
from miniunicorn.agent.tools.registry import ToolRegistry
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_MAX_RESULT_CHARS = 10000


def _provider(*responses: Any, captured: list[Any] | None = None) -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    remaining = list(responses)

    async def _chat(messages, *args, **kwargs):
        if captured is not None:
            captured.append(list(messages))
        if not remaining:
            raise AssertionError("fake provider exhausted its response script")
        return remaining.pop(0)

    provider.chat_with_retry = AsyncMock(side_effect=_chat)
    return provider


def _activation_response(plan_dict: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="call_act",
                name="activate_plan",
                arguments={"plan": json.dumps(plan_dict)},
            )
        ],
        usage={},
    )


def _plan_dict(goal: str = "ship", steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "goal": goal,
        "steps": steps
        or [
            {"id": 1, "action": "first", "done_criteria": "first done"},
            {"id": 2, "action": "second", "done_criteria": "second done"},
        ],
    }


def _captured_text(messages: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
    return "\n".join(texts)


# ============================================================================
# Tool-level semantics
# ============================================================================


@pytest.mark.asyncio
async def test_valid_plan_activates_and_stashes() -> None:
    plan_dict = _plan_dict()
    text = await ActivatePlanTool().execute(plan=json.dumps(plan_dict))

    assert text.startswith("Plan activated: 2 steps")
    plan = take_pending_plan()
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].action == "first"


@pytest.mark.parametrize(
    "plan",
    [
        "not json",
        json.dumps({"goal": "x"}),  # no steps
        json.dumps({"goal": "x", "steps": []}),
        json.dumps({"goal": "x", "steps": [{"id": 1} for _ in range(9)]}),  # over 8
        "[1,2,3]",  # not a dict
    ],
)
@pytest.mark.asyncio
async def test_invalid_plans_error_and_do_not_stash(plan: str) -> None:
    text = await ActivatePlanTool().execute(plan=plan)

    assert text.startswith("Error:")
    assert take_pending_plan() is None


@pytest.mark.asyncio
async def test_evidence_level_normalization() -> None:
    plan_dict = {
        "goal": "g",
        "steps": [
            {"id": 1, "action": "a", "evidence_level": "tool"},
            {"id": 2, "action": "b"},  # missing -> text
            {"id": 3, "action": "c", "evidence_level": "none"},  # none -> text
        ],
    }
    await ActivatePlanTool().execute(plan=json.dumps(plan_dict))
    plan = take_pending_plan()

    assert plan is not None
    assert plan.steps[0].evidence_level == "tool"
    assert plan.steps[1].evidence_level == "text"
    assert plan.steps[2].evidence_level == "text"


def test_scopes_core_only() -> None:
    assert ActivatePlanTool._scopes == {"core"}
    loader = ToolLoader(test_classes=[ActivatePlanTool])
    core_registry = ToolRegistry()
    sub_registry = ToolRegistry()
    ctx = ToolContext(config={}, workspace="/tmp")

    loader.load(ctx, core_registry, scope="core")
    loader.load(ctx, sub_registry, scope="subagent")

    assert core_registry.has("activate_plan")
    assert not sub_registry.has("activate_plan")


@pytest.mark.asyncio
async def test_take_pending_plan_resets_contextvar() -> None:
    plan_dict = _plan_dict(goal="g", steps=[{"id": 1, "action": "a"}])
    await ActivatePlanTool().execute(plan=json.dumps(plan_dict))

    first = take_pending_plan()
    assert first is not None
    assert take_pending_plan() is None


@pytest.mark.asyncio
async def test_second_activation_overwrites_pending() -> None:
    await ActivatePlanTool().execute(
        plan=json.dumps({"goal": "one", "steps": [{"id": 1, "action": "a"}]})
    )
    await ActivatePlanTool().execute(
        plan=json.dumps({"goal": "two", "steps": [{"id": 1, "action": "b"}]})
    )

    plan = take_pending_plan()
    assert plan is not None
    assert plan.goal == "two"


# ============================================================================
# Adoption path (unit: replan budget + snapshot origin + observations clear)
# ============================================================================


def _make_state() -> _TurnState:
    state = _TurnState()
    state.turn_id = "turn-1"
    return state


def _make_spec(*, callback=None) -> Any:
    def _noop(payload):
        return None

    return SimpleNamespace(
        checkpoint_callback=callback or AsyncMock(side_effect=_noop),
        model="test-model",
    )


@pytest.mark.asyncio
async def test_adoption_rejects_when_replan_budget_exhausted() -> None:
    runner = AgentRunner(MagicMock())
    current = Plan(
        goal="old",
        steps=[PlanStep(id=1, action="old step")],
        replan_count=3,
        max_replans=3,  # can_replan False
    )
    activated = Plan(goal="new", steps=[PlanStep(id=1, action="new step")])

    state = _make_state()
    spec = _make_spec()

    result = await runner._adopt_activated_plan(spec, state, current, activated)

    assert result is current  # rejected, old plan kept
    assert state.plan_snapshot is None  # not mounted/emitted


@pytest.mark.asyncio
async def test_adoption_mounts_with_replan_increment_and_origin() -> None:
    runner = AgentRunner(MagicMock())
    current = Plan(
        goal="old",
        steps=[PlanStep(id=1, action="old step")],
        replan_count=1,
        max_replans=3,
    )
    activated = Plan(
        goal="new", steps=[PlanStep(id=1, action="new step"), PlanStep(id=2, action="s2")]
    )

    snapshots: list[dict[str, Any]] = []

    async def _cb(payload):
        if payload.get("phase") == "plan_snapshot":
            snapshots.append(payload["plan_snapshot"])

    state = _make_state()
    spec = _make_spec(callback=_cb)

    result = await runner._adopt_activated_plan(spec, state, current, activated)

    assert result is activated
    assert activated.replan_count == 2
    assert activated.max_replans == 3
    assert state.plan_snapshot is not None
    assert state.plan_snapshot.origin == "activated"
    assert state.plan_snapshot.goal == "new"
    # progress_tracker is built (managed path).
    assert state.progress_tracker is not None
    assert snapshots and snapshots[0]["origin"] == "activated"


@pytest.mark.asyncio
async def test_adoption_first_activation_resets_replan_and_builds_tracker() -> None:
    runner = AgentRunner(MagicMock())
    activated = Plan(goal="new", steps=[PlanStep(id=1, action="a")])
    state = _make_state()
    spec = _make_spec()

    result = await runner._adopt_activated_plan(spec, state, None, activated)

    assert result is activated
    assert activated.replan_count == 0
    assert state.progress_tracker is not None


@pytest.mark.asyncio
async def test_adoption_clears_stale_tool_observations() -> None:
    runner = AgentRunner(MagicMock())
    activated = Plan(goal="new", steps=[PlanStep(id=1, action="a")])
    state = _make_state()
    # stale observations from a previous plan/step must not leak into the
    # activated plan's acceptance input.
    state.tool_observations.append({"tool_name": "old_tool", "step_id": 99})

    spec = _make_spec()

    await runner._adopt_activated_plan(spec, state, None, activated)

    assert state.tool_observations == []


# ============================================================================
# Main-loop integration (fake provider full turn)
# ============================================================================


def _registry(tmp_path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ActivatePlanTool())
    registry.register(WriteFileTool(workspace=tmp_path))
    return registry


@pytest.mark.asyncio
async def test_activation_yields_step_guidance_next_iteration(tmp_path) -> None:
    captured: list[Any] = []
    provider = _provider(
        _activation_response(_plan_dict()),  # iter 0: activate_plan
        LLMResponse(content="first", usage={}),  # iter 1: plain text
        LLMResponse(content="first", usage={}),  # iter 2: plain text
        captured=captured,
    )
    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "drive it"}],
            tools=_registry(tmp_path),
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_RESULT_CHARS,
        )
    )

    assert result.plan is not None
    assert any("[Current Plan Step 1/2" in _captured_text(msgs) for msgs in captured[1:])
    assert result.plan.steps[0].status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)


@pytest.mark.asyncio
async def test_fast_mode_activation_builds_tracker_and_advances(tmp_path) -> None:
    plan_dict = _plan_dict(
        steps=[
            {
                "id": 1,
                "action": "write the report",
                "tool_hint": "write_file",
                "done_criteria": "report.md created",
            },
            {"id": 2, "action": "wrap up", "done_criteria": "wrapped"},
        ]
    )
    provider = _provider(
        _activation_response(plan_dict),  # iter 0: activate_plan
        LLMResponse(  # iter 1: real write producing a receipt
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="c1", name="write_file", arguments={"path": "report.md", "content": "x"}
                )
            ],
            usage={},
        ),
        LLMResponse(content="report.md created", usage={}),  # iter 2: step 1 accepted
        LLMResponse(content="wrapped", usage={}),  # iter 3: step 2 accepted -> all done
    )
    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=_registry(tmp_path),
            model="test-model",
            max_iterations=5,
            max_tool_result_chars=_MAX_RESULT_CHARS,
        )
    )

    assert result.plan is not None
    assert result.plan.steps[0].status is StepStatus.COMPLETED
    assert result.plan.step_evidence[0].accepted is True
    assert result.plan.step_evidence[0].evidence_level == "tool"
    assert result.plan.steps[1].status is StepStatus.COMPLETED
    assert result.plan.all_done
    assert (tmp_path / "report.md").exists()


@pytest.mark.asyncio
async def test_text_only_activation_does_not_waive_acceptance(tmp_path) -> None:
    plan_dict = _plan_dict(
        steps=[
            {
                "id": 1,
                "action": "write the report",
                "tool_hint": "write_file",
                "done_criteria": "report.md created",
            }
        ]
    )
    provider = _provider(
        _activation_response(plan_dict),  # iter 0: activate_plan
        LLMResponse(content="report.md created", usage={}),  # iter 1: text only (forgery)
        LLMResponse(content="report.md created", usage={}),
        LLMResponse(content="report.md created", usage={}),
    )
    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=_registry(tmp_path),
            model="test-model",
            max_iterations=4,
            max_tool_result_chars=_MAX_RESULT_CHARS,
        )
    )

    assert result.plan is not None
    step = result.plan.steps[0]
    assert step.status is not StepStatus.COMPLETED
    assert (
        result.plan.step_evidence
        and result.plan.step_evidence[0].rejection_reason == "no_tool_receipt"
    )
    assert not (tmp_path / "report.md").exists()


@pytest.mark.asyncio
async def test_managed_turn_activation_emits_activated_snapshot(tmp_path) -> None:
    snapshots: list[dict[str, Any]] = []

    async def _cb(payload):
        if payload.get("phase") == "plan_snapshot":
            snapshots.append(payload["plan_snapshot"])

    plan_dict = _plan_dict(
        goal="new",
        steps=[{"id": 1, "action": "write", "tool_hint": "write_file", "done_criteria": "r.md"}],
    )
    provider = _provider(
        # planner consumes a plan first (MANAGED mode) -> existing plan
        LLMResponse(
            content=json.dumps(_plan_dict(goal="old", steps=[{"id": 1, "action": "old"}])), usage={}
        ),
        _activation_response(plan_dict),  # model activates a new plan
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="c1", name="write_file", arguments={"path": "r.md", "content": "x"}
                )
            ],
            usage={},
        ),
        LLMResponse(content="r.md", usage={}),
    )
    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=_registry(tmp_path),
            model="test-model",
            max_iterations=6,
            max_tool_result_chars=_MAX_RESULT_CHARS,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
            checkpoint_callback=_cb,
        )
    )

    # The adopted plan carries the activated goal.
    assert result.plan is not None
    assert result.plan.goal == "new"
    # A snapshot with origin="activated" was emitted.
    assert any(
        snap.get("origin") == "activated" and snap.get("goal") == "new" for snap in snapshots
    )


# ============================================================================
# Guard regression: delegate_plan / execute_plan aliases untouched
# ============================================================================


def test_delegate_plan_alias_unchanged() -> None:
    from miniunicorn.agent.tools.execute_plan import ExecutePlanTool

    tool = ExecutePlanTool(manager=MagicMock())
    assert tool.name == "delegate_plan"
    assert tool.aliases == ("execute_plan",)
    assert tool.risk_level.value == "high"
    assert tool._scopes == {"core"}
