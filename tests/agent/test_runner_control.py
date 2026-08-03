"""Red state-transition and boundary tests for RunController extraction (Task 12).

These tests verify that the ReAct control flow is extracted into a
``RunController`` collaborator (``runner_control.py``) with explicit
``RunLoopState`` and ``IterationAction`` types, while ``AgentRunner.run``
becomes a thin delegate.  Boundary tests enforce the line and method limits
frozen in ``test_runner_boundaries.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.runner import AgentRunner, AgentRunSpec
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import LLMResponse, ToolCallRequest
from tests.agent.conftest import FakeToolExecutionPort
from tests.agent.test_runner_boundaries import (
    RUNNER_CONTROL_METHOD_LINE_LIMIT,
    RUNNER_FACADE_LINE_LIMIT,
)

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _spec(
    messages: list[dict[str, Any]],
    *,
    tools: Any = None,
    tool_execution_port: Any = None,
    max_iterations: int = 3,
    **kwargs: Any,
) -> AgentRunSpec:
    if tools is None:
        tools = MagicMock()
        tools.get_definitions.return_value = []
    return AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        tool_execution_port=tool_execution_port,
        model="test-model",
        max_iterations=max_iterations,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Structural: types and collaborator exist
# ---------------------------------------------------------------------------


def test_runner_control_module_imports() -> None:
    """``runner_control`` module exists and exports the required symbols."""
    from miniunicorn.agent.runner_control import (
        IterationAction,
        RunController,
        RunLoopState,
    )

    assert IterationAction is not None
    assert RunController is not None
    assert RunLoopState is not None


def test_iteration_action_has_continue_and_break() -> None:
    from miniunicorn.agent.runner_control import IterationAction

    assert IterationAction.CONTINUE is not None
    assert IterationAction.BREAK is not None


def test_run_loop_state_has_required_fields() -> None:
    """``RunLoopState`` carries exactly the per-run mutable data."""
    from miniunicorn.agent.runner_control import RunLoopState

    state = RunLoopState(messages=[{"role": "user", "content": "hi"}])
    assert state.messages == [{"role": "user", "content": "hi"}]
    assert state.final_content is None
    assert state.tools_used == []
    assert state.usage == {"prompt_tokens": 0, "completion_tokens": 0}
    assert state.last_call_usage == {}
    assert state.error is None
    assert state.stop_reason == "completed"
    assert state.tool_events == []
    assert state.external_lookup_counts == {}
    assert state.workspace_violation_counts == {}
    assert state.empty_content_retries == 0
    assert state.length_recovery_count == 0
    assert state.had_injections is False
    assert state.injection_cycles == 0
    assert state.plan is None
    assert state.planner is None
    assert state.planner_task_text is None
    assert state.planner_tools_summary is None
    assert state.reflection is None


def test_run_controller_has_run_method() -> None:
    from miniunicorn.agent.runner_control import RunController

    assert hasattr(RunController, "run")


def test_agent_runner_has_run_controller() -> None:
    """``AgentRunner`` constructs a ``RunController`` collaborator."""
    from miniunicorn.agent.runner_control import RunController

    runner = AgentRunner(MagicMock())
    assert hasattr(runner, "_run_controller")
    assert isinstance(runner._run_controller, RunController)


# ---------------------------------------------------------------------------
# Boundary: line and method limits
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the repository root by walking up from this test file."""
    p = Path(__file__).resolve()
    while p.name != "tests" or p.parent.name != "mini-Unicorn":
        if p.parent == p:
            raise RuntimeError("Could not find repository root")
        p = p.parent
    return p.parent


def test_runner_facade_is_at_most_450_lines() -> None:
    repo_root = _repo_root()
    source = (repo_root / "miniunicorn/agent/runner.py").read_text(encoding="utf-8")
    line_count = len(source.splitlines())
    assert line_count <= RUNNER_FACADE_LINE_LIMIT, (
        f"runner.py is {line_count} lines; limit is {RUNNER_FACADE_LINE_LIMIT}"
    )


def test_runner_control_methods_are_at_most_200_lines() -> None:
    repo_root = _repo_root()
    source = (repo_root / "miniunicorn/agent/runner_control.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    oversized = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
        and node.end_lineno - node.lineno + 1 > RUNNER_CONTROL_METHOD_LINE_LIMIT
    }
    assert oversized == {}, f"Methods exceeding limit: {oversized}"


def test_runner_collaborator_modules_exist() -> None:
    repo_root = _repo_root()
    for name in ("runner_model.py", "runner_tools.py", "runner_control.py", "runner_types.py"):
        path = repo_root / "miniunicorn/agent" / name
        assert path.exists(), f"Collaborator module {name} does not exist"


def test_no_collaborator_imports_agent_runner() -> None:
    """Collaborator modules must not import ``AgentRunner`` (no circular dep)."""
    import miniunicorn.agent.runner_control as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "AgentRunner":
                    pytest.fail(f"runner_control.py imports AgentRunner at line {node.lineno}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "AgentRunner":
                    pytest.fail(f"runner_control.py imports AgentRunner at line {node.lineno}")


# ---------------------------------------------------------------------------
# Behavioral: state transitions through the public run() API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_answer_transition() -> None:
    """A no-tool response produces final_content and stop_reason='completed'."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="final answer", tool_calls=[], usage={})
    )
    runner = AgentRunner(provider)
    result = await runner.run(_spec([{"role": "user", "content": "hi"}]))
    assert result.final_content == "final answer"
    assert result.stop_reason == "completed"
    assert result.error is None


@pytest.mark.asyncio
async def test_tool_continuation_transition() -> None:
    """A tool call followed by a final answer completes after two iterations."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id="c1", name="noop", arguments={})],
                usage={},
            ),
            LLMResponse(content="done", tool_calls=[], usage={}),
        ]
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "do task"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
            max_iterations=5,
        )
    )
    assert result.final_content == "done"
    assert result.stop_reason == "completed"
    assert result.tools_used == ["noop"]


@pytest.mark.asyncio
async def test_budget_stop_transition() -> None:
    """Exceeding the turn budget stops the loop with stop_reason='budget_exceeded'."""
    from miniunicorn.agent.turn_budget import TurnBudget

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="working",
            tool_calls=[],
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )
    )
    budget = TurnBudget(max_input_tokens=1)
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            turn_budget=budget,
        )
    )
    assert result.stop_reason == "budget_exceeded"
    assert result.budget_exceeded is True


@pytest.mark.asyncio
async def test_max_iterations_transition() -> None:
    """Exhausting max_iterations produces stop_reason='max_iterations'."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="thinking",
            tool_calls=[ToolCallRequest(id="c1", name="noop", arguments={})],
            usage={},
        )
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "loop"}],
            tools=tools,
            tool_execution_port=FakeToolExecutionPort(tools),
            max_iterations=2,
        )
    )
    assert result.stop_reason == "max_iterations"


@pytest.mark.asyncio
async def test_injected_message_continuation() -> None:
    """An injected user message keeps the loop running instead of breaking."""
    injection_calls = {"n": 0}

    async def injection_callback():
        injection_calls["n"] += 1
        if injection_calls["n"] == 1:
            return [{"role": "user", "content": "injected follow-up"}]
        return []

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content="first answer", tool_calls=[], usage={}),
            LLMResponse(content="final answer", tool_calls=[], usage={}),
        ]
    )
    runner = AgentRunner(provider)
    result = await runner.run(
        _spec(
            [{"role": "user", "content": "hi"}],
            injection_callback=injection_callback,
            max_iterations=5,
        )
    )
    assert result.final_content == "final answer"
    assert result.had_injections is True
