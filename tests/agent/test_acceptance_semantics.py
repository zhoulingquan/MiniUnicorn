"""W0-A3: acceptance semantics — tool-level evidence closes the text loophole.

Before this batch a step was judged on the model's final text alone, so a model
could pass acceptance by restating its done_criteria, echoing keywords through
a shell, or reporting a dry-run as real work. These cases lock the new rules:
tool-level steps are judged on receipts only, and the verifier may rescue one
rejection reason and nothing else.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.planner import PlanStep, StepStatus, effective_evidence_level
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.agent.progress_policy import ProgressAction, ProgressPolicy, ProgressTracker
from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.agent.step_acceptance import (
    StepAcceptancePolicy,
    ToolObservation,
    observations_digest,
)
from miniunicorn.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miniunicorn.tools.filesystem import WriteFileTool
from miniunicorn.tools.registry import ToolRegistry

_MAX_RESULT_CHARS = 10000


def _step(**overrides: Any) -> PlanStep:
    defaults: dict[str, Any] = {"id": 1, "action": "do the thing"}
    defaults.update(overrides)
    return PlanStep(**defaults)


def _receipt(target: str = "/tmp/a.txt", digest: str = "d1") -> dict[str, Any]:
    return {
        "tool": "write_file",
        "operation": "write",
        "target": target,
        "committed": True,
        "digest": digest,
        "created_at": "2026-08-29T00:00:00+00:00",
    }


def _obs(
    tool_name: str,
    *,
    receipt: dict[str, Any] | None = None,
    status: str = "ok",
) -> ToolObservation:
    return ToolObservation(
        tool_name=tool_name,
        arguments={},
        status=status,
        result_excerpt="",
        receipt=receipt,
    )


# --- tool-level judgement matrix ---------------------------------------------


@pytest.mark.parametrize(
    ("case", "observations", "criteria", "content", "expected_accepted", "expected_reason"),
    [
        ("receipt_present", "receipt", "report.md created", "report.md created", True, None),
        # Core anti-forgery: restating the criteria without doing the work.
        (
            "text_only_forgery",
            "none",
            "report.md created",
            "report.md created",
            False,
            "no_tool_receipt",
        ),
        # shell status=ok proves nothing without a receipt.
        ("shell_echo", "shell", "report.md created", "report.md created", False, "no_tool_receipt"),
        # dry_run produced no side effect, so no receipt.
        ("dry_run", "dry_run", "report.md created", "report.md created", False, "no_tool_receipt"),
        # No criteria declared: the receipt alone is the evidence.
        ("receipt_empty_text", "receipt", None, "", True, None),
        # Receipt present but criteria unreached: rescuable by the verifier.
        (
            "receipt_criteria_missing",
            "receipt",
            "report.md created",
            "something else",
            False,
            "done_criteria_not_met",
        ),
    ],
)
def test_tool_level_matrix(
    case: str,
    observations: str,
    criteria: str | None,
    content: str,
    expected_accepted: bool,
    expected_reason: str | None,
) -> None:
    obs = {
        "receipt": [_obs("write_file", receipt=_receipt())],
        "none": [],
        "shell": [_obs("shell")],
        "dry_run": [_obs("apply_patch")],
    }[observations]

    evidence = StepAcceptancePolicy().evaluate(
        step=_step(tool_hint="write_file", done_criteria=criteria),
        observations=obs,
        final_content=content,
        iterations_used=1,
    )

    assert evidence.accepted is expected_accepted, case
    assert evidence.rejection_reason == expected_reason, case
    assert evidence.evidence_level == "tool", case


# --- text-level regression ---------------------------------------------------


def test_text_level_semantics_unchanged() -> None:
    policy = StepAcceptancePolicy()

    assert policy.evaluate(_step(), [], "finished", 1).accepted is True
    assert policy.evaluate(_step(done_criteria="ok"), [], "all ok", 1).accepted is True
    assert policy.evaluate(_step(), [], None, 0).rejection_reason == "empty_content_no_tools"
    assert (
        policy.evaluate(_step(), [_obs("shell")], None, 1).rejection_reason
        == "empty_content_with_tools"
    )
    assert (
        policy.evaluate(_step(done_criteria="QED"), [], "not yet", 1).rejection_reason
        == "done_criteria_not_met"
    )


def test_text_level_step_reports_text_evidence() -> None:
    evidence = StepAcceptancePolicy().evaluate(_step(), [], "finished", 1)

    assert evidence.evidence_level == "text"
    assert evidence.evidence_digest == observations_digest([])


# --- static floor ------------------------------------------------------------


@pytest.mark.parametrize("tool_hint", ["write_file", "edit_file", "apply_patch"])
def test_receipt_tool_hint_raises_level_to_tool(tool_hint: str) -> None:
    step = _step(tool_hint=tool_hint, evidence_level="text")

    assert effective_evidence_level(step) == "tool"


def test_non_receipt_tool_hint_stays_text() -> None:
    step = _step(tool_hint="web_search", evidence_level="text")

    assert effective_evidence_level(step) == "text"


def test_declared_tool_level_is_honoured() -> None:
    step = _step(tool_hint="web_search", evidence_level="tool")

    assert effective_evidence_level(step) == "tool"


# --- verifier rescue boundaries ----------------------------------------------


@pytest.mark.asyncio
async def test_verifier_rescues_criteria_mismatch() -> None:
    provider = MagicMock(spec=LLMProvider)
    response = MagicMock()
    response.content = '{"accepted": true, "reason": "receipt covers it"}'
    provider.chat_with_retry = AsyncMock(return_value=response)

    evidence = await StepAcceptancePolicy().evaluate_with_verifier(
        step=_step(tool_hint="write_file", done_criteria="report.md created"),
        observations=[_obs("write_file", receipt=_receipt())],
        final_content="something else",
        iterations_used=1,
        provider=provider,
        model="test-model",
        enable_verifier=True,
    )

    provider.chat_with_retry.assert_called_once()
    assert evidence.accepted is True
    assert evidence.rejection_reason is None


@pytest.mark.asyncio
async def test_verifier_not_called_without_receipt() -> None:
    """No receipt is a hard failure — it must never reach the LLM."""
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock()

    evidence = await StepAcceptancePolicy().evaluate_with_verifier(
        step=_step(tool_hint="write_file", done_criteria="report.md created"),
        observations=[_obs("shell")],
        final_content="report.md created",
        iterations_used=1,
        provider=provider,
        model="test-model",
        enable_verifier=True,
    )

    provider.chat_with_retry.assert_not_called()
    assert evidence.accepted is False
    assert evidence.rejection_reason == "no_tool_receipt"
    assert evidence.verifier_verdict is None


@pytest.mark.asyncio
async def test_verifier_failure_falls_back_and_skips_cache() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(side_effect=Exception("LLM down"))
    cache: dict[tuple[int, str], dict[str, Any]] = {}

    evidence = await StepAcceptancePolicy().evaluate_with_verifier(
        step=_step(done_criteria="QED"),
        observations=[],
        final_content="work in progress",
        iterations_used=1,
        provider=provider,
        model="test-model",
        enable_verifier=True,
        step_evidence_cache=cache,
    )

    assert evidence.accepted is False
    assert evidence.rejection_reason == "done_criteria_not_met"
    assert evidence.verifier_verdict is not None
    assert evidence.verifier_verdict.get("error") == "verifier_failed"
    # A degraded fallback must not be cached as a considered verdict.
    assert cache == {}


# --- cache semantics ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_llm_call() -> None:
    provider = MagicMock(spec=LLMProvider)
    response = MagicMock()
    response.content = '{"accepted": true, "reason": "ok"}'
    provider.chat_with_retry = AsyncMock(return_value=response)
    cache: dict[tuple[int, str], dict[str, Any]] = {}

    for _ in range(2):
        await StepAcceptancePolicy().evaluate_with_verifier(
            step=_step(done_criteria="QED"),
            observations=[_obs("shell")],
            final_content="work in progress",
            iterations_used=1,
            provider=provider,
            model="test-model",
            enable_verifier=True,
            step_evidence_cache=cache,
        )

    assert provider.chat_with_retry.call_count == 1


@pytest.mark.asyncio
async def test_new_evidence_invalidates_cache() -> None:
    provider = MagicMock(spec=LLMProvider)
    response = MagicMock()
    response.content = '{"accepted": true, "reason": "ok"}'
    provider.chat_with_retry = AsyncMock(return_value=response)
    cache: dict[tuple[int, str], dict[str, Any]] = {}

    await StepAcceptancePolicy().evaluate_with_verifier(
        step=_step(done_criteria="QED"),
        observations=[_obs("shell")],
        final_content="work in progress",
        iterations_used=1,
        provider=provider,
        model="test-model",
        enable_verifier=True,
        step_evidence_cache=cache,
    )
    # The step did more work: a new observation changes the digest.
    await StepAcceptancePolicy().evaluate_with_verifier(
        step=_step(done_criteria="QED"),
        observations=[_obs("shell"), _obs("write_file", receipt=_receipt())],
        final_content="work in progress",
        iterations_used=2,
        provider=provider,
        model="test-model",
        enable_verifier=True,
        step_evidence_cache=cache,
    )

    assert provider.chat_with_retry.call_count == 2


def test_digest_stable_under_reordering() -> None:
    a = _obs("write_file", receipt=_receipt("/tmp/a", "d1"))
    b = _obs("edit_file", receipt=_receipt("/tmp/b", "d2"))

    assert observations_digest([a, b]) == observations_digest([b, a])


def test_digest_changes_with_new_receipt() -> None:
    a = _obs("write_file", receipt=_receipt("/tmp/a", "d1"))

    assert observations_digest([a]) != observations_digest([a, _obs("edit_file")])


# --- verifier circuit breaker ------------------------------------------------


def test_two_verifier_failures_trigger_replan() -> None:
    tracker = ProgressTracker(ProgressPolicy())
    step = _step(iterations_used=1)

    assert tracker.check_step_progress(step, None).action is ProgressAction.CONTINUE

    tracker.record_verifier_failure()
    assert tracker.check_step_progress(step, None).action is ProgressAction.CONTINUE

    tracker.record_verifier_failure()
    verdict = tracker.check_step_progress(step, None)
    assert verdict.action is ProgressAction.REPLAN
    assert verdict.reason == "verifier_unavailable"


def test_successful_verifier_resets_failure_count() -> None:
    tracker = ProgressTracker(ProgressPolicy())
    step = _step(iterations_used=1)

    tracker.record_verifier_failure()
    tracker.record_verifier_success()
    tracker.record_verifier_failure()

    assert tracker.check_step_progress(step, None).action is ProgressAction.CONTINUE


# --- integration: a managed turn judged on real side effects -----------------


def _plan_json() -> str:
    return json.dumps(
        {
            "goal": "ship",
            "steps": [
                {
                    "id": 1,
                    "action": "write the report",
                    "tool_hint": "write_file",
                    "done_criteria": "report.md created",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_managed_turn_accepts_step_after_real_write(tmp_path: Any) -> None:
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=tmp_path))
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content=_plan_json(), usage={}),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "report.md", "content": "hello"},
                    )
                ],
                usage={},
            ),
            LLMResponse(content="report.md created", usage={}),
        ]
    )

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=registry,
            model="test-model",
            max_iterations=6,
            max_tool_result_chars=_MAX_RESULT_CHARS,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
        )
    )

    plan = result.plan
    assert plan is not None
    assert plan.steps[0].status is StepStatus.COMPLETED
    evidence = plan.step_evidence[0]
    assert evidence.evidence_level == "tool"
    assert evidence.accepted is True


@pytest.mark.asyncio
async def test_managed_turn_keeps_step_open_when_only_text(tmp_path: Any) -> None:
    """A model that only talks about the write must not complete the step."""
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=tmp_path))
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content=_plan_json(), usage={}),
            # Talks about the criteria without touching the filesystem; the
            # loop keeps asking because the step never completes.
            *[LLMResponse(content="report.md created", usage={}) for _ in range(6)],
        ]
    )

    result = await AgentRunner(provider).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "ship"}],
            tools=registry,
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=_MAX_RESULT_CHARS,
            planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED),
        )
    )

    plan = result.plan
    assert plan is not None
    evidence = plan.step_evidence[0]
    assert evidence.accepted is False
    assert evidence.rejection_reason == "no_tool_receipt"
    # The step stays open: the plan is not marked done behind the model's back.
    assert plan.steps[0].status is not StepStatus.COMPLETED
    assert not (tmp_path / "report.md").exists()
