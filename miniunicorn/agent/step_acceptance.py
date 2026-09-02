"""Deterministic step evidence acceptance policy (P1-T3) with LLM verifier fallback (T5).

Evaluates whether a plan step has produced sufficient evidence to be marked
COMPLETED. Primary acceptance is rule-based; when rules return rejected and
enable_step_verifier is True, an LLM call is made as a fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from miniunicorn.agent.planner import PlanStep, effective_evidence_level


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """单次工具调用的结构化观察，验收证据的最小单元。"""

    tool_name: str
    arguments: dict[str, Any]
    status: str
    result_excerpt: str
    step_id: int | None = None
    receipt: dict[str, Any] | None = None
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        data = {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "status": self.status,
            "result_excerpt": self.result_excerpt,
            "step_id": self.step_id,
            "occurred_at": self.occurred_at,
        }
        if self.receipt is not None:
            data["receipt"] = self.receipt
        return data


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
    observations: list[dict[str, Any]] = field(default_factory=list)
    evidence_level: str = "text"
    evidence_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "step_id": self.step_id,
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "final_content": self.final_content,
            "iterations_used": self.iterations_used,
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "evidence_level": self.evidence_level,
            "evidence_digest": self.evidence_digest,
        }
        if self.verifier_verdict is not None:
            data["verifier_verdict"] = self.verifier_verdict
        if self.observations:
            data["observations"] = list(self.observations)
        return data


def observations_digest(observations: list[ToolObservation]) -> str:
    """观察序列规范化哈希：只取影响判定的稳定字段。

    排序后哈希，使并发批次下到达顺序不同的一组观察得到同一个 digest；
    不含 result_excerpt / occurred_at / arguments，避免缓存永远失效。
    """
    items = sorted(
        (
            o.tool_name,
            o.status,
            o.receipt.get("digest") if o.receipt else None,
            o.receipt.get("target") if o.receipt else None,
        )
        for o in observations
    )
    return sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class StepAcceptancePolicy:
    """Deterministic step acceptance with optional LLM verifier fallback."""

    def evaluate(
        self,
        step: PlanStep,
        observations: list[ToolObservation] | None,
        final_content: str | None,
        iterations_used: int,
    ) -> StepEvidence:
        """Rule-based evaluation only (no LLM)."""
        obs = list(observations or [])
        accepted = self._is_accepted(step, obs, final_content)
        return StepEvidence(
            step_id=step.id,
            tool_calls=[],
            tool_results=[],
            final_content=final_content,
            iterations_used=iterations_used,
            accepted=accepted,
            rejection_reason=None if accepted else self._rejection_reason(step, obs, final_content),
            observations=[o.to_dict() for o in obs],
            evidence_level=effective_evidence_level(step),
            evidence_digest=observations_digest(obs),
        )

    async def evaluate_with_verifier(
        self,
        step: PlanStep,
        observations: list[ToolObservation] | None,
        final_content: str | None,
        iterations_used: int,
        *,
        provider: Any,
        model: str,
        enable_verifier: bool,
        step_evidence_cache: dict[tuple[int, str], dict[str, Any]] | None = None,
    ) -> StepEvidence:
        """Evaluate with optional LLM verifier fallback.

        The verifier is a rescue channel for exactly one rejection reason
        (``done_criteria_not_met``); hard failures such as a missing receipt
        never reach the LLM. Verdicts are cached under
        ``(step_id, observations_digest)``, so a step that produced new
        evidence is judged again instead of reusing a stale verdict.
        """
        obs = list(observations or [])
        obs_dicts = [o.to_dict() for o in obs]
        digest = observations_digest(obs)
        cache_key = (step.id, digest)

        # Check cache first
        if step_evidence_cache is not None and cache_key in step_evidence_cache:
            cached = step_evidence_cache[cache_key]
            return StepEvidence(
                step_id=step.id,
                tool_calls=[],
                tool_results=[],
                final_content=final_content,
                iterations_used=iterations_used,
                accepted=cached["accepted"],
                rejection_reason=cached.get("rejection_reason"),
                verifier_verdict=cached.get("verifier_verdict"),
                observations=obs_dicts,
                evidence_level=effective_evidence_level(step),
                evidence_digest=digest,
            )

        # Rule-based evaluation
        rule_accepted = self._is_accepted(step, obs, final_content)
        rule_rejection = None if rule_accepted else self._rejection_reason(step, obs, final_content)

        # Only a criteria mismatch is rescuable — a step with no receipt must
        # not be talked into passing.
        rescuable = rule_rejection == "done_criteria_not_met"

        if rule_accepted or not enable_verifier or not rescuable:
            evidence = StepEvidence(
                step_id=step.id,
                tool_calls=[],
                tool_results=[],
                final_content=final_content,
                iterations_used=iterations_used,
                accepted=rule_accepted,
                rejection_reason=rule_rejection,
                observations=obs_dicts,
                evidence_level=effective_evidence_level(step),
                evidence_digest=digest,
            )
            if step_evidence_cache is not None:
                step_evidence_cache[cache_key] = {
                    "accepted": evidence.accepted,
                    "rejection_reason": evidence.rejection_reason,
                    "verifier_verdict": evidence.verifier_verdict,
                    "observations": obs_dicts,
                }
            return evidence

        # Rules rejected, rescue allowed and verifier enabled -> call LLM
        verifier_verdict = await self._call_verifier_llm(
            provider=provider,
            model=model,
            step=step,
            observations=obs,
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
            tool_calls=[],
            tool_results=[],
            final_content=final_content,
            iterations_used=iterations_used,
            accepted=accepted,
            rejection_reason=rejection_reason,
            verifier_verdict=verifier_verdict,
            observations=obs_dicts,
            evidence_level=effective_evidence_level(step),
            evidence_digest=digest,
        )

        # A failed verifier must not poison the cache: the rule fallback is a
        # degraded answer, not a considered one.
        if step_evidence_cache is not None and verifier_verdict.get("error") is None:
            step_evidence_cache[cache_key] = {
                "accepted": evidence.accepted,
                "rejection_reason": evidence.rejection_reason,
                "verifier_verdict": evidence.verifier_verdict,
                "observations": obs_dicts,
            }
        return evidence

    async def _call_verifier_llm(
        self,
        provider: Any,
        model: str,
        step: PlanStep,
        observations: list[ToolObservation],
        final_content: str | None,
    ) -> dict[str, Any] | None:
        """Call LLM to verify step completion. Returns verdict dict or None on failure."""
        from miniunicorn.ledger import CallPurpose, call_purpose

        prompt = self._build_verifier_prompt(step, observations, final_content)

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
        observations: list[ToolObservation],
        final_content: str | None,
    ) -> str:
        """Build the prompt for the LLM verifier from the real evidence.

        Observations are rendered one per line with their receipt (or an
        explicit "no receipt" marker). Full arguments and result excerpts are
        left out deliberately: they bloat the prompt and widen the injection
        surface without helping the verdict.
        """
        lines = [
            f"Step: {step.action}",
            f"Done criteria: {step.done_criteria or 'N/A'}",
            f"Evidence level: {effective_evidence_level(step)}",
            f"Tool observations ({len(observations)}):",
        ]
        for o in observations:
            if o.receipt is None:
                lines.append(f"  - {o.tool_name}(status={o.status}, no receipt)")
                continue
            committed = o.receipt.get("committed")
            files = o.receipt.get("files") or []
            if files:
                for entry in files:
                    lines.append(
                        f"  - {o.tool_name}(target={entry.get('path')}, "
                        f"committed={committed}, digest={str(entry.get('digest'))[:12]}...)"
                    )
            else:
                lines.append(
                    f"  - {o.tool_name}(target={o.receipt.get('target')}, "
                    f"committed={committed}, digest={str(o.receipt.get('digest'))[:12]}...)"
                )
        lines.extend(
            [
                "",
                f"Final content: {final_content or '(empty)'}",
                "",
                "The step HAS produced verified side effects (receipts above). "
                "Judge ONLY whether those actions plus the content satisfy the done criteria.",
                'Respond with JSON: {"accepted": true|false, "reason": "..."}',
            ]
        )
        return "\n".join(lines)

    def _is_accepted(
        self,
        step: PlanStep,
        observations: list[ToolObservation],
        final_content: str | None,
    ) -> bool:
        if effective_evidence_level(step) == "tool":
            # A committed receipt is necessary — text alone can never satisfy a
            # tool-level step, which is what closes the forgery loophole.
            if not any(
                o.receipt is not None and o.receipt.get("committed") is True for o in observations
            ):
                return False
            # The side effect is proven. A declared criteria still has to be
            # met; that is the one gap the verifier is allowed to rescue.
            if step.done_criteria:
                return bool(final_content and step.done_criteria.lower() in final_content.lower())
            return True
        if final_content and final_content.strip():
            if step.done_criteria:
                return step.done_criteria.lower() in final_content.lower()
            return True
        return False

    def _rejection_reason(
        self,
        step: PlanStep,
        observations: list[ToolObservation],
        final_content: str | None,
    ) -> str:
        if effective_evidence_level(step) == "tool":
            if not observations:
                # No tool ran at all.
                return "no_tool_receipt"
            if not any(o.receipt for o in observations):
                # Tools ran but none produced a trusted side effect.
                return "no_tool_receipt"
            # A receipt exists; only the criteria is in question, which the
            # verifier may rescue.
            return "done_criteria_not_met"
        if not final_content or not final_content.strip():
            if not observations:
                return "empty_content_no_tools"
            return "empty_content_with_tools"
        if step.done_criteria and step.done_criteria.lower() not in final_content.lower():
            return "done_criteria_not_met"
        return "unknown"
