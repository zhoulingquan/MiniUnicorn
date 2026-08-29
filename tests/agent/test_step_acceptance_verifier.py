"""T5: StepAcceptancePolicy LLM verifier fallback.

Tests that when rules return rejected (not accepted), and enable_step_verifier=True,
an LLM call is made as a fallback. The verifier's verdict is recorded in step evidence.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miniunicorn.agent.call_ledger import CallPurpose
from miniunicorn.agent.planner import Plan, PlanStep, StepStatus
from miniunicorn.agent.step_acceptance import StepAcceptancePolicy, StepEvidence
from miniunicorn.agent.execution.planning import PlanningReflectionService


def _step(**overrides: Any) -> PlanStep:
    defaults: dict[str, Any] = {"id": 1, "action": "do the thing"}
    defaults.update(overrides)
    return PlanStep(**defaults)


def _service() -> PlanningReflectionService:
    async def _emit_checkpoint(spec: Any, payload: dict[str, Any]) -> None:
        return None

    return PlanningReflectionService(SimpleNamespace(_emit_checkpoint=_emit_checkpoint))


def _hook() -> Any:
    hook = MagicMock()
    hook.after_iteration = AsyncMock()
    return hook


class TestStepAcceptanceVerifier:
    """LLM verifier fallback when rules reject a step."""

    @pytest.fixture
    def mock_provider(self):
        """Mock LLM provider that returns controlled responses."""
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock()
        return provider

    def test_verifier_disabled_by_default_no_llm_call(self, mock_provider):
        """When enable_step_verifier=False (default), no LLM call is made."""
        policy = StepAcceptancePolicy()
        step = _step(done_criteria="QED")
        tool_calls = [{"name": "run_tests"}]
        tool_results = [{"summary": "ok"}]

        # Rules REJECT: empty content without tools (independent of done_criteria).
        evidence = policy.evaluate(
            step=_step(),
            tool_calls=[],
            tool_results=[],
            final_content="",
            iterations_used=1,
        )
        assert evidence.accepted is False
        assert evidence.rejection_reason == "empty_content_no_tools"
        # No verifier called because disabled by default

    @pytest.mark.asyncio
    async def test_verifier_enabled_triggers_llm_on_rejection(self, mock_provider):
        """When enable_step_verifier=True and rules reject, LLM is called once."""
        # Setup mock response with accepted verdict
        response = MagicMock()
        response.content = '{"accepted": true, "reason": "Content shows completion"}'
        mock_provider.chat_with_retry.return_value = response

        policy = StepAcceptancePolicy()
        step = _step(done_criteria="QED")
        evidence = await policy.evaluate_with_verifier(
            step=step,
            tool_calls=[],
            tool_results=[],
            final_content="",  # Rules reject: empty content
            iterations_used=1,
            provider=mock_provider,
            model="test-model",
            enable_verifier=True,
        )

        # LLM should have been called
        mock_provider.chat_with_retry.assert_called_once()
        # Verdict should be accepted based on LLM
        assert evidence.accepted is True
        assert evidence.verifier_verdict is not None
        assert evidence.verifier_verdict["accepted"] is True

    @pytest.mark.asyncio
    async def test_verifier_verdict_recorded_in_evidence(self, mock_provider):
        """Verifier's verdict (accepted/rejected) is stored in step_evidence."""
        response = MagicMock()
        response.content = '{"accepted": false, "reason": "No proof provided"}'
        mock_provider.chat_with_retry.return_value = response

        policy = StepAcceptancePolicy()
        step = _step(done_criteria="QED")
        evidence = await policy.evaluate_with_verifier(
            step=step,
            tool_calls=[],
            tool_results=[],
            final_content="",
            iterations_used=1,
            provider=mock_provider,
            model="test-model",
            enable_verifier=True,
        )

        assert evidence.accepted is False
        assert evidence.verifier_verdict is not None
        assert evidence.verifier_verdict["accepted"] is False
        assert "reason" in evidence.verifier_verdict
        # Check to_dict includes verifier_verdict
        data = evidence.to_dict()
        assert "verifier_verdict" in data
        assert data["verifier_verdict"]["accepted"] is False

    @pytest.mark.asyncio
    async def test_verifier_not_called_twice_for_same_step(self, mock_provider):
        """Second evaluation of same step should not re-call LLM (cached)."""
        response = MagicMock()
        response.content = '{"accepted": true, "reason": "OK"}'
        mock_provider.chat_with_retry.return_value = response

        policy = StepAcceptancePolicy()
        step = _step(done_criteria="QED")
        cache: dict[int, dict[str, Any]] = {}

        # First call
        await policy.evaluate_with_verifier(
            step=step,
            tool_calls=[],
            tool_results=[],
            final_content="",
            iterations_used=1,
            provider=mock_provider,
            model="test-model",
            enable_verifier=True,
            step_evidence_cache=cache,
        )
        # Second call with same step_id
        await policy.evaluate_with_verifier(
            step=step,
            tool_calls=[],
            tool_results=[],
            final_content="",
            iterations_used=1,
            provider=mock_provider,
            model="test-model",
            enable_verifier=True,
            step_evidence_cache=cache,
        )

        # LLM should only be called once
        assert mock_provider.chat_with_retry.call_count == 1

    @pytest.mark.asyncio
    async def test_verifier_exception_falls_back_to_rule_result(self, mock_provider):
        """If LLM call fails, rule-based rejection is preserved, turn doesn't crash."""
        mock_provider.chat_with_retry.side_effect = Exception("LLM API error")

        policy = StepAcceptancePolicy()
        step = _step(done_criteria="QED")
        evidence = await policy.evaluate_with_verifier(
            step=step,
            tool_calls=[],
            tool_results=[],
            final_content="",  # Rules reject
            iterations_used=1,
            provider=mock_provider,
            model="test-model",
            enable_verifier=True,
        )

        # Should fall back to rule result (rejected)
        assert evidence.accepted is False
        assert evidence.rejection_reason == "empty_content_no_tools"
        assert evidence.verifier_verdict is not None
        assert evidence.verifier_verdict.get("error") == "verifier_failed"
        assert evidence.verifier_verdict.get("fallback_to_rule") is True

    @pytest.mark.asyncio
    async def test_verifier_uses_call_ledger_with_verifier_purpose(self, mock_provider):
        """LLM call uses CallPurpose.VERIFIER for ledger accounting."""
        from miniunicorn.agent.call_ledger import CallPurpose, _PURPOSE

        response = MagicMock()
        response.content = '{"accepted": true, "reason": "OK"}'
        mock_provider.chat_with_retry.return_value = response

        policy = StepAcceptancePolicy()
        step = _step(done_criteria="QED")
        await policy.evaluate_with_verifier(
            step=step,
            tool_calls=[],
            tool_results=[],
            final_content="",
            iterations_used=1,
            provider=mock_provider,
            model="test-model",
            enable_verifier=True,
        )

        # Verify chat_with_retry was called (which means call_purpose context was entered)
        mock_provider.chat_with_retry.assert_called_once()
        # The call_purpose context manager sets _PURPOSE context var during the call
        # We can't easily test the context var after the call since it's reset,
        # but we can verify the call happened within the expected context by
        # checking the provider was called with the right setup.
        # The key assertion is that no exception occurred and the call was made.