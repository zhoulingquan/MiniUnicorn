"""Context governance service split out of ``AgentRunner`` (PR-5b).

``ContextGovernanceService`` owns the message-governance paths that used to
live on ``AgentRunner``: governing messages through the pluggable
``ContextGovernor`` pipeline, applying the tool-result character budget,
and snipping history to fit the context window.

The provider is read through ``runner.provider`` at call time so
``ProviderRegistry`` hot-switching keeps applying to in-flight turns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent.execution.recovery import _SNIP_SAFETY_BUFFER
from miniunicorn.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
)

if TYPE_CHECKING:
    from miniunicorn.agent.runner import AgentRunner, AgentRunSpec


class ContextGovernanceService:
    """Context governance for a single agent turn.

    Constructed with the host ``AgentRunner``; the provider is resolved
    through ``runner.provider`` at call time.
    """

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    async def govern_messages(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Context governance for this iteration; falls back to raw messages.

        Keep the persisted conversation untouched. Context governance may
        repair or compact historical messages for the model, but those
        synthetic edits must not shift the append boundary used later when
        the caller saves only the new turn. The governor runs an ordered list
        of ContextStrategy; the default pipeline reproduces the legacy
        hardcoded steps (drop_orphan -> backfill -> microcompact -> budget ->
        snip -> drop_orphan -> backfill) and falls back to minimal repair on
        failure. Spec-provided governors override the default.
        """
        try:
            from miniunicorn.agent.context_governor import GovernanceContext

            governor = self._runner.get_governor(spec)
            pressure = self.compute_pressure(spec, messages)
            ctx_gov = GovernanceContext(
                spec=spec,
                tools=spec.tools,
                provider=self._runner.provider,
                iteration=iteration,
                runner=self._runner,
                pressure=pressure,
            )
            governed = governor.govern(messages, ctx_gov)
            if pressure is not None:
                self._record_prompt_telemetry(spec, governed, pressure)
            return governed
        except Exception:
            logger.exception(
                "Context governance failed on turn {} for {}; using raw messages",
                iteration,
                spec.session_key or "default",
            )
            return messages

    _COMPACTION_MARKERS = (
        "result omitted from context",
        "[Tool result unavailable",
    )

    def _record_prompt_telemetry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        pressure: Any,
    ) -> None:
        from miniunicorn.agent import turn_telemetry

        telemetry = turn_telemetry.current()
        if telemetry is None:
            return
        telemetry.governance_pressure = pressure
        telemetry.prompt_components = self.compute_prompt_components(spec, messages)

    def compute_prompt_components(self, spec: AgentRunSpec, messages: list[dict[str, Any]]) -> Any:
        """Estimate per-component prompt tokens (approximate, telemetry-only)."""
        import json

        from miniunicorn.agent.turn_telemetry import PromptComponentTokens

        system_tokens = 0
        history_tokens = 0
        compacted_tokens = 0
        for message in messages:
            tokens = estimate_message_tokens(message)
            role = message.get("role")
            if role == "system":
                system_tokens += tokens
                continue
            history_tokens += tokens
            content = message.get("content")
            if (
                role == "tool"
                and isinstance(content, str)
                and any(marker in content for marker in self._COMPACTION_MARKERS)
            ):
                compacted_tokens += tokens
        tool_tokens = 0
        try:
            definitions = (
                spec.effective_tool_definitions
                if getattr(spec, "effective_tool_definitions", None) is not None
                else spec.tools.get_definitions()
            )
            if definitions:
                tool_tokens = len(json.dumps(definitions, ensure_ascii=False)) // 4
        except Exception:
            tool_tokens = 0
        return PromptComponentTokens(
            system_prompt=system_tokens,
            tool_definitions=tool_tokens,
            conversation_history=history_tokens - compacted_tokens,
            step_guidance=0,
            reflection_context=0,
            compacted_context=compacted_tokens,
            total_estimated=system_tokens + tool_tokens + history_tokens,
        )

    def compute_pressure(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> Any | None:
        """Estimate prompt pressure relative to the context window budget."""
        from miniunicorn.agent.context_governor import PressureLevel, PressureSignal

        token_limit = spec.context_window_tokens
        if not token_limit:
            return None

        estimated, _ = estimate_prompt_tokens_chain(
            self._runner.provider,
            spec.model,
            messages,
            spec.tools.get_definitions(),
        )
        if estimated <= 0:
            return None

        ratio = estimated / token_limit
        level = (
            PressureLevel.RED
            if ratio >= 0.8
            else (PressureLevel.YELLOW if ratio >= 0.5 else PressureLevel.GREEN)
        )
        return PressureSignal(estimated, token_limit, ratio, level)

    def apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        pressure_level: Any | None = None,
    ) -> list[dict[str, Any]]:
        from miniunicorn.agent.context_governor import PressureLevel
        from miniunicorn.utils.helpers import truncate_text

        max_chars = spec.max_tool_result_chars
        if pressure_level is PressureLevel.RED:
            max_chars = max_chars // 2
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._runner.tool_execution.normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if (
                pressure_level is PressureLevel.RED
                and isinstance(normalized, str)
                and len(normalized) > max_chars
            ):
                normalized = truncate_text(normalized, max_chars)
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(
            getattr(self._runner.provider, "generation", None), "max_tokens", 4096
        )
        max_output = (
            spec.max_tokens
            if isinstance(spec.max_tokens, int)
            else (provider_max_tokens if isinstance(provider_max_tokens, int) else 4096)
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        estimate, _ = estimate_prompt_tokens_chain(
            self._runner.provider,
            spec.model,
            messages,
            spec.tools.get_definitions(),
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        fixed_tokens, _ = estimate_prompt_tokens_chain(
            self._runner.provider,
            spec.model,
            system_messages,
            spec.tools.get_definitions(),
        )
        remaining_budget = max(0, budget - max(system_tokens, fixed_tokens))
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept
