"""Frozen boundary contracts for AgentRunner refactoring (Task 9).

These constants and tests freeze the public surface and line-count
targets that Tasks 10-12 must satisfy. The numeric limits are consumed
by Task 12's end-state assertions; this task only declares them so the
suite stays green during extraction.
"""

from __future__ import annotations

import inspect

from miniunicorn.agent.runner import (
    AgentRunner,
    AgentRunResult,
    AgentRunSpec,
)

# Target limits consumed by Task 12's final extraction assertions.
RUNNER_FACADE_LINE_LIMIT = 450
RUNNER_CONTROL_METHOD_LINE_LIMIT = 200


def test_runner_public_imports_are_stable() -> None:
    """The three public symbols remain importable from ``runner``."""
    assert AgentRunner.__name__ == "AgentRunner"
    assert AgentRunSpec.__name__ == "AgentRunSpec"
    assert AgentRunResult.__name__ == "AgentRunResult"


def test_runner_public_surface_is_stable() -> None:
    """Constructor and run signature match the frozen contract."""
    assert str(inspect.signature(AgentRunner)) == "(provider: 'LLMProvider')"
    assert "spec" in inspect.signature(AgentRunner.run).parameters


def test_runner_facade_line_limit_constant() -> None:
    """The facade line limit constant is declared for Task 12."""
    assert RUNNER_FACADE_LINE_LIMIT == 450


def test_runner_control_method_line_limit_constant() -> None:
    """The control method line limit constant is declared for Task 12."""
    assert RUNNER_CONTROL_METHOD_LINE_LIMIT == 200
