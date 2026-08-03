"""Shared dataclass types for the agent runner (Task 10).

``AgentRunSpec`` and ``AgentRunResult`` are the public configuration and
result types for ``AgentRunner.run``. They live here so that collaborator
modules (``runner_model``, ``runner_tools``, ``runner_control``) can import
them without creating a circular dependency on ``runner.py``.

``runner.py`` re-exports both symbols so existing callers that import from
``miniunicorn.agent.runner`` continue to work unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from miniunicorn.agent.hook import AgentHook
from miniunicorn.agent.tools.registry import ToolRegistry

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: str | None = None
    # Optional ContextGovernor override. When None, AgentRunner uses a default
    # governor that reproduces the legacy hardcoded pipeline. Typed as Any to
    # avoid a circular import with miniunicorn.agent.context_governor.
    context_governor: Any | None = None
    # Optional per-turn budget; when exceeded, run() stops with
    # stop_reason="budget_exceeded". None = no budget tracking (legacy behavior).
    # Typed as Any to avoid a circular import with miniunicorn.agent.turn_budget.
    turn_budget: Any | None = None
    # Plan-and-Execute mode. When True, the runner first decomposes the task
    # into steps via a Planner LLM call, then executes each step via ReAct.
    # Failed steps trigger replan (up to planner_max_replans). Default False
    # preserves the legacy pure-ReAct behavior.
    use_planner: bool = False
    planner_model: str | None = None  # model for planning LLM calls; None = use spec.model
    planner_max_replans: int = 3
    # Reflection: when enabled, produce a "lesson learned" on failure or every
    # reflection_interval iterations, appended to memory/reflections.jsonl for
    # Dream to consolidate. Default False = no reflection overhead.
    enable_reflection: bool = False
    reflection_interval: int = 5  # periodic reflection every N iterations
    # Durable runtime ports (design §11.1, §20). When set, the Runner routes
    # every tool call through ``tool_execution_port`` (no bypass) and journals
    # each Provider attempt through ``turn_journal``. When None, the Runner
    # uses the legacy in-process path (no durable journaling). Typed as Any to
    # avoid a circular import with miniunicorn.agent.ports.
    tool_execution_port: Any | None = None
    turn_journal: Any | None = None
    provider_attempt_observer: Any | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    budget_exceeded: bool = False
    plan: Any | None = None  # Plan | None, populated when use_planner=True
    # Usage from the last LLM call in this run (not cumulative). Represents
    # the actual context window footprint at the end of the turn.
    last_call_usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ToolBatchResult:
    """Typed result of a batch of tool executions (Task 11).

    Wraps the ``(results, events, fatal_error)`` tuple previously returned
    by ``AgentRunner._execute_tools``. ``results`` is the list of raw tool
    contents (one per tool call, in order). ``events`` is the list of
    ``{name, status, detail}`` event dicts. ``fatal_error`` is the first
    exception encountered when ``fail_on_tool_error`` is set, or ``None``.
    """

    results: list[Any]
    events: list[dict[str, str]]
    fatal_error: Exception | None = None
