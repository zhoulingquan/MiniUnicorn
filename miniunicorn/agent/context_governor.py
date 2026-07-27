"""Pluggable context governance for AgentRunner.

Strategies are applied in order before each LLM call to keep the prompt
within the model's context window while preserving conversation integrity.

Built-in strategies mirror the legacy hardcoded pipeline in AgentRunner.
Third-party strategies can register via the ``miniunicorn.context_strategies``
entry point group.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from miniunicorn.agent.tools.registry import ToolRegistry


@runtime_checkable
class ContextStrategy(Protocol):
    """A single context governance step.

    Implementations receive the current message list and a context object
    providing access to the run spec, tool registry, and provider, and must
    return a (possibly new) message list ready for the next strategy.
    """

    name: str

    def apply(
        self,
        messages: list[dict[str, Any]],
        ctx: "GovernanceContext",
    ) -> list[dict[str, Any]]:
        """Transform messages for the next pipeline stage."""
        ...


class GovernanceContext:
    """Read-only view passed to each ContextStrategy.

    Holds the spec-level fields a strategy needs without exposing the whole
    AgentRunSpec (which would couple strategies to runner internals). The
    owning ``AgentRunner`` is stashed on ``_runner`` so built-in strategies
    can delegate to its existing private methods.
    """

    __slots__ = ("spec", "tools", "provider", "iteration", "_runner")

    def __init__(
        self,
        spec: Any,
        tools: ToolRegistry,
        provider: Any,
        iteration: int,
        runner: Any | None = None,
    ) -> None:
        self.spec = spec
        self.tools = tools
        self.provider = provider
        self.iteration = iteration
        self._runner = runner


class ContextGovernor:
    """Orchestrates an ordered list of ContextStrategy.

    Default behavior reproduces the legacy AgentRunner pipeline:
    drop_orphan -> backfill_missing -> microcompact -> token_budget -> snip_history
    -> drop_orphan -> backfill_missing (cleanup pass).

    On failure the governor resets to the original messages and applies a
    minimal repair (drop_orphan + backfill); if that also fails the raw
    messages are returned. This preserves the legacy resilience contract.
    """

    BUILTIN_PIPELINE = (
        "drop_orphan_tool_results",
        "backfill_missing_tool_results",
        "microcompact",
        "apply_tool_result_budget",
        "snip_history",
        # cleanup pass after snip
        "drop_orphan_tool_results",
        "backfill_missing_tool_results",
    )

    def __init__(self, strategies: list[ContextStrategy] | None = None) -> None:
        if strategies is None:
            strategies = self._load_default_strategies()
        # Index by name for the fallback lookup
        self._by_name: dict[str, ContextStrategy] = {s.name: s for s in strategies}
        self._strategies: list[ContextStrategy] = strategies

    @classmethod
    def _load_default_strategies(cls) -> list[ContextStrategy]:
        """Load built-in strategies plus any registered plugins."""
        # Imported lazily to avoid circular import with AgentRunner
        from miniunicorn.agent.runner_strategies import (
            ApplyToolResultBudgetStrategy,
            BackfillMissingStrategy,
            DropOrphanStrategy,
            MicrocompactStrategy,
            SnipHistoryStrategy,
        )

        builtins: list[ContextStrategy] = [
            DropOrphanStrategy(),
            BackfillMissingStrategy(),
            MicrocompactStrategy(),
            ApplyToolResultBudgetStrategy(),
            SnipHistoryStrategy(),
        ]
        # Merge plugin strategies (by name; plugin never overrides builtin)
        builtin_names = {s.name for s in builtins}
        for ep in entry_points(group="miniunicorn.context_strategies"):
            try:
                strategy_cls = ep.load()
                strategy = strategy_cls()
                if strategy.name not in builtin_names:
                    builtins.append(strategy)
                else:
                    logger.warning(
                        "Context strategy plugin {} skipped: name {} conflicts with builtin",
                        ep.name,
                        strategy.name,
                    )
            except Exception:
                logger.exception("Failed to load context strategy plugin: %s", ep.name)
        return builtins

    def get(self, name: str) -> ContextStrategy | None:
        return self._by_name.get(name)

    def govern(
        self,
        messages: list[dict[str, Any]],
        ctx: GovernanceContext,
    ) -> list[dict[str, Any]]:
        """Run the full pipeline. Returns the governed message list.

        On any exception, resets to the original messages and applies a
        minimal repair (drop_orphan + backfill) to preserve the legacy
        resilience contract. If the minimal repair also fails, the raw
        messages are returned.
        """
        result = messages
        try:
            for strategy in self._strategies:
                result = strategy.apply(result, ctx)
            return result
        except Exception:
            logger.exception(
                "Context governance failed on turn {} for {}; applying minimal repair",
                ctx.iteration,
                getattr(ctx.spec, "session_key", None) or "default",
            )
            # Reset to the original messages — partially applied synthetic
            # edits must not leak into the model request when governance fails.
            result = messages
            try:
                fallback = self._by_name.get("drop_orphan_tool_results")
                if fallback is not None:
                    result = fallback.apply(result, ctx)
                fallback2 = self._by_name.get("backfill_missing_tool_results")
                if fallback2 is not None:
                    result = fallback2.apply(result, ctx)
                return result
            except Exception:
                logger.exception(
                    "Minimal context repair failed on turn {}; using raw messages",
                    ctx.iteration,
                )
                return messages
