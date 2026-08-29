"""Deterministic step evidence acceptance policy (P1-T3) with LLM verifier fallback (T5).

Evaluates whether a plan step has produced sufficient evidence to be marked
COMPLETED. Primary acceptance is rule-based; when rules return rejected and
enable_step_verifier is True, an LLM call is made as a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from miniunicorn.agent.planner import PlanStep


@dataclass(frozen=True, slots=True)
class StepEvidence:
    """Structured evidence collected during a plan step's execution."""

    step_id: int
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    final_content: str | None
    iterations_used: int
    accepted: bool
    rejection_reason: str | None = None
    verifier_verdict: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "step_id": self.step_id,
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "final_content": self.final_content,
            "iterations_used": self.iterations_used,
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
        }
        if self.verifier_verdict is not None:
            data["verifier_verdict"] = self.verifier_verdict
        return data


class StepAcceptancePolicy:
    """Deterministic step acceptance with optional LLM verifier fallback."""

    def evaluate(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        final_content: str | None,
        iterations_used: int,
    ) -> StepEvidence:
        """Rule-based evaluation only (no LLM)."""
        accepted = self._is_accepted(step, tool_calls, final_content)
        return StepEvidence(
            step_id=step.id,
            tool_calls=list(tool_calls),
            tool_results=list(tool_results),
            final_content=final_content,
            iterations_used=iterations_used,
            accepted=accepted,
            rejection_reason=None
            if accepted
            else self._rejection_reason(step, tool_calls, final_content),
        )

    async def evaluate_with_verifier(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        final_content: str | None,
        iterations_used: int,
        *,
        provider: Any,
        model: str,
        enable_verifier: bool,
        step_evidence_cache: dict[int, dict[str, Any]] | None = None,
    ) -> StepEvidence:
        """Evaluate with optional LLM verifier fallback.

        If rules reject and enable_verifier is True, calls LLM once per step.
        Verdict is cached in step_evidence_cache to avoid duplicate calls.
        """
        # Check cache first
        if step_evidence_cache is not None and step.id in step_evidence_cache:
            cached = step_evidence_cache[step.id]
            return StepEvidence(
                step_id=step.id,
                tool_calls=list(tool_calls),
                tool_results=list(tool_results),
                final_content=final_content,
                iterations_used=iterations_used,
                accepted=cached["accepted"],
                rejection_reason=cached.get("rejection_reason"),
                verifier_verdict=cached.get("verifier_verdict"),
            )

        # Rule-based evaluation
        rule_accepted = self._is_accepted(step, tool_calls, final_content)
        rule_rejection = (
            None if rule_accepted else self._rejection_reason(step, tool_calls, final_content)
        )

        if rule_accepted or not enable_verifier:
            evidence = StepEvidence(
                step_id=step.id,
                tool_calls=list(tool_calls),
                tool_results=list(tool_results),
                final_content=final_content,
                iterations_used=iterations_used,
                accepted=rule_accepted,
                rejection_reason=rule_rejection,
            )
            if step_evidence_cache is not None:
                step_evidence_cache[step.id] = {
                    "accepted": evidence.accepted,
                    "rejection_reason": evidence.rejection_reason,
                    "verifier_verdict": evidence.verifier_verdict,
                }
            return evidence

        # Rules rejected and verifier enabled -> call LLM
        verifier_verdict = await self._call_verifier_llm(
            provider=provider,
            model=model,
            step=step,
            tool_calls=tool_calls,
            tool_results=tool_results,
            final_content=final_content,
        )

        # Use verifier verdict; on failure fall back to rule result
        if verifier_verdict is not None and verifier_verdict.get("accepted") is not None:
            accepted = bool(verifier_verdict["accepted"])
            rejection_reason = (
                None if accepted else (verifier_verdict.get("reason") or rule_rejection)
            )
        else:
            # Verifier failed or returned invalid result -> fall back to rule
            accepted = rule_accepted
            rejection_reason = rule_rejection
            verifier_verdict = {"error": "verifier_failed", "fallback_to_rule": True}

        evidence = StepEvidence(
            step_id=step.id,
            tool_calls=list(tool_calls),
            tool_results=list(tool_results),
            final_content=final_content,
            iterations_used=iterations_used,
            accepted=accepted,
            rejection_reason=rejection_reason,
            verifier_verdict=verifier_verdict,
        )

        if step_evidence_cache is not None:
            step_evidence_cache[step.id] = {
                "accepted": evidence.accepted,
                "rejection_reason": evidence.rejection_reason,
                "verifier_verdict": evidence.verifier_verdict,
            }
        return evidence

    async def _call_verifier_llm(
        self,
        provider: Any,
        model: str,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        final_content: str | None,
    ) -> dict[str, Any] | None:
        """Call LLM to verify step completion. Returns verdict dict or None on failure."""
        from miniunicorn.agent.call_ledger import CallPurpose, call_purpose

        prompt = self._build_verifier_prompt(step, tool_calls, tool_results, final_content)

        try:
            async with call_purpose(CallPurpose.VERIFIER):
                response = await provider.chat_with_retry(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": 'You are a strict step acceptance verifier. Respond ONLY with a JSON object: {"accepted": true|false, "reason": "..."}. No extra text.',
                        },
                        {"role": "user", "content": prompt},
                    ],
                    tools=None,
                    tool_choice=None,
                    temperature=0.0,
                )
        except Exception:
            return None

        content = (response.content or "").strip()
        if not content:
            return None

        # Parse JSON verdict
        import json
        import re

        # Try to extract JSON from response
        json_text = None
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if m:
            json_text = m.group(1)
        else:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                json_text = m.group(0)

        if not json_text:
            return None

        try:
            verdict = json.loads(json_text)
            if not isinstance(verdict, dict):
                return None
            if "accepted" not in verdict:
                return None
            return verdict
        except json.JSONDecodeError:
            return None

    def _build_verifier_prompt(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        final_content: str | None,
    ) -> str:
        """Build the prompt for the LLM verifier."""
        lines = [
            f"Step: {step.action}",
            f"Done criteria: {step.done_criteria or 'N/A'}",
            f"Tool calls: {len(tool_calls)}",
            f"Tool results: {len(tool_results)}",
            f"Final content: {final_content or '(empty)'}",
            "",
            "Does the final content satisfy the done criteria (if any) and demonstrate step completion?",
            'Respond with JSON: {"accepted": true|false, "reason": "..."}',
        ]
        return "\n".join(lines)

    def _is_accepted(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        final_content: str | None,
    ) -> bool:
        if final_content and final_content.strip():
            if step.done_criteria:
                return step.done_criteria.lower() in final_content.lower()
            return True
        return False

    def _rejection_reason(
        self,
        step: PlanStep,
        tool_calls: list[dict[str, Any]],
        final_content: str | None,
    ) -> str:
        if not final_content or not final_content.strip():
            if not tool_calls:
                return "empty_content_no_tools"
            return "empty_content_with_tools"
        if step.done_criteria and step.done_criteria.lower() not in final_content.lower():
            return "done_criteria_not_met"
        return "unknown"
