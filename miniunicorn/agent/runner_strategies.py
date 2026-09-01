"""Built-in ContextStrategy implementations and governance primitives.

This module hosts both the strategy classes used by ContextGovernor and the
pure-function governance implementations (``drop_orphan_tool_results``,
``backfill_missing_tool_results``, ``microcompact``) that those strategies
delegate to. The runner-bound strategies (``ApplyToolResultBudgetStrategy``
and ``SnipHistoryStrategy``) delegate to the public methods of the
``ContextGovernanceService`` service via ``GovernanceContext._runner``'s
``_context_governance`` attribute, so they no longer reach into AgentRunner
private methods (circular-stitch fix, PR-5b). The ``ContextStrategy``
Protocol and the ``miniunicorn.context_strategies`` entry-point contract
are unchanged.
"""

from __future__ import annotations

from typing import Any

from miniunicorn.agent.context_governor import GovernanceContext, PressureLevel
from miniunicorn.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Governance constants (moved here from runner.py to keep governance logic
# self-contained in this module).
# ---------------------------------------------------------------------------
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset(
    {
        "read_file",
        "exec",
        "grep",
        "find_files",
        "web_search",
        "web_fetch",
        "list_dir",
        "list_exec_sessions",
    }
)
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"


# ---------------------------------------------------------------------------
# Governance implementation functions (moved from AgentRunner staticmethods).
# ---------------------------------------------------------------------------


def drop_orphan_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop tool results that have no matching assistant tool_call earlier in the history."""
    declared: set[str] = set()
    updated: list[dict[str, Any]] | None = None
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                if updated is None:
                    updated = [dict(m) for m in messages[:idx]]
                continue
        if updated is not None:
            updated.append(dict(msg))

    if updated is None:
        return messages
    return updated


def backfill_missing_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Insert synthetic error results for orphaned tool_use blocks."""
    declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
    fulfilled: set[str] = set()
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    name = ""
                    func = tc.get("function")
                    if isinstance(func, dict):
                        name = func.get("name", "")
                    declared.append((idx, str(tc["id"]), name))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                fulfilled.add(str(tid))

    missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
    if not missing:
        return messages

    updated = list(messages)
    offset = 0
    for assistant_idx, call_id, name in missing:
        insert_at = assistant_idx + 1 + offset
        while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
            insert_at += 1
        updated.insert(
            insert_at,
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            },
        )
        offset += 1
    return updated


def microcompact(
    messages: list[dict[str, Any]],
    tools: ToolRegistry | None = None,
) -> list[dict[str, Any]]:
    """Replace old compactable tool results with one-line summaries.

    A tool result is compactable if the tool declares compactable=True OR
    the tool name is in the legacy ``_COMPACTABLE_TOOLS`` whitelist, AND
    the tool's importance is below 1.0 (never drop critical results).
    """
    compactable_indices: list[int] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        name = msg.get("name")
        if not name:
            continue
        # Prefer tool metadata when available; fall back to legacy whitelist
        tool = tools.get(name) if tools else None
        if tool is not None:
            if not tool.compactable or tool.importance >= 1.0:
                continue
        elif name not in _COMPACTABLE_TOOLS:
            continue
        compactable_indices.append(idx)

    if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
        return messages

    stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
    updated: list[dict[str, Any]] | None = None
    for idx in stale:
        msg = messages[idx]
        content = msg.get("content")
        if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
            continue
        name = msg.get("name", "tool")
        summary = f"[{name} result omitted from context]"
        if updated is None:
            updated = [dict(m) for m in messages]
        updated[idx]["content"] = summary

    return updated if updated is not None else messages


# ---------------------------------------------------------------------------
# Strategy classes
# ---------------------------------------------------------------------------


class _RunnerBoundStrategy:
    """Base for strategies that delegate to AgentRunner's existing methods."""

    name = "base"

    def _runner(self, ctx: GovernanceContext) -> Any:
        runner = getattr(ctx, "_runner", None)
        if runner is None:
            raise RuntimeError("GovernanceContext._runner not set")
        return runner

    def apply(self, messages: list[dict[str, Any]], ctx: GovernanceContext) -> list[dict[str, Any]]:
        raise NotImplementedError


class DropOrphanStrategy(_RunnerBoundStrategy):
    name = "drop_orphan_tool_results"

    def apply(self, messages, ctx):
        return drop_orphan_tool_results(messages)


class BackfillMissingStrategy(_RunnerBoundStrategy):
    name = "backfill_missing_tool_results"

    def apply(self, messages, ctx):
        return backfill_missing_tool_results(messages)


class MicrocompactStrategy(_RunnerBoundStrategy):
    name = "microcompact"

    def apply(self, messages, ctx):
        # P2-T1: skip compaction when pressure is GREEN (no compaction needed).
        if ctx.pressure is not None and ctx.pressure.level is PressureLevel.GREEN:
            return messages
        return microcompact(messages, ctx.tools)


class ApplyToolResultBudgetStrategy(_RunnerBoundStrategy):
    name = "apply_tool_result_budget"

    def apply(self, messages, ctx):
        pressure_level = ctx.pressure.level if ctx.pressure else None
        return self._runner(ctx).context_governance.apply_tool_result_budget(
            ctx.spec, messages, pressure_level=pressure_level
        )


class SnipHistoryStrategy(_RunnerBoundStrategy):
    name = "snip_history"

    def apply(self, messages, ctx):
        if ctx.pressure is not None and ctx.pressure.level is PressureLevel.GREEN:
            return messages
        return self._runner(ctx).context_governance.snip_history(ctx.spec, messages)
