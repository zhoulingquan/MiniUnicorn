"""W3-1: ``AgentLoop.__init__`` assembly split into eight ordered ``_init_*`` phases.

The split is a pure-move refactor. These tests pin the structural invariants:

1. ``__init__`` stays a thin guard + ordered phase-call facade (line budget).
2. Property-setter routing converges on ``_provider_registry`` (provider /
   model / context_window_tokens read/write the same registry values).
3. The eight ``_init_*`` phase methods are called in assembly order.
4. The injected-vs-fallback dual path still adopts an injected ``McpRuntime``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

from miniunicorn.agent.loop import AgentLoop, AgentLoopConfig
from miniunicorn.agent.planning_policy import PlanningMode, PlanningPolicy
from miniunicorn.bus.queue import MessageBus
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.tools.mcp_runtime import McpRuntime
from tests.agent.conftest import make_loop, make_provider

# Assembly order is the core invariant of the W3-1 split.
PHASE_METHODS = [
    "_init_command_layer",
    "_init_provider_layer",
    "_init_execution_limits",
    "_init_policy_and_workspace",
    "_init_session_layer",
    "_init_subagent_layer",
    "_init_resource_layer",
    "_init_turn_orchestrator",
]


def _test_provider() -> MagicMock:
    """Unspec'd provider mock, matching the convention used by existing agent tests."""
    return make_provider(spec=False)


def _direct_loop(tmp_path: Path, **config_fields) -> AgentLoop:
    """Direct-construct a loop from an explicit ``AgentLoopConfig`` bundle."""
    cfg = AgentLoopConfig(
        provider=_test_provider(),
        model="test-model",
        context_window_tokens=128_000,
        **config_fields,
    )
    return AgentLoop(bus=MessageBus(), workspace=tmp_path, config=cfg)


def test_init_stays_below_70_lines() -> None:
    """``__init__`` must remain a guard + ordered phase calls, not regress."""
    source = inspect.getsource(AgentLoop.__init__)
    line_count = len(source.strip().splitlines())
    assert line_count < 70


def test_setter_routing_converges_on_provider_registry(tmp_path: Path) -> None:
    """provider/model/context_window_tokens read and write ``_provider_registry``."""
    loop = make_loop(tmp_path, provider=_test_provider())

    registry = loop._provider_registry
    assert registry is not None
    assert loop.provider is registry.provider
    assert loop.model == registry.model
    assert loop.context_window_tokens == registry.context_window_tokens
    assert loop.model == "test-model"
    assert loop.context_window_tokens == 128_000
    # The setters routed into the registry instead of the fallback attributes.
    assert "_provider" not in loop.__dict__
    assert "_model" not in loop.__dict__
    assert "_context_window_tokens" not in loop.__dict__


def test_fast_tier_uses_default_fast_max_tool_iterations(tmp_path: Path) -> None:
    loop = make_loop(tmp_path, provider=_test_provider())

    assert loop.max_iterations == AgentDefaults().fast_max_tool_iterations


def test_managed_policy_uses_default_managed_max_tool_iterations(tmp_path: Path) -> None:
    loop = _direct_loop(tmp_path, planning_policy=PlanningPolicy(mode=PlanningMode.MANAGED))

    assert loop.max_iterations == AgentDefaults().managed_max_tool_iterations


def test_tool_result_chars_derived_from_tokens(tmp_path: Path) -> None:
    loop = _direct_loop(tmp_path, max_tool_result_tokens=1000)

    assert loop.max_tool_result_tokens == 1000
    assert loop.max_tool_result_chars == 4000  # tokens * 4


def test_explicit_planning_policy_wins(tmp_path: Path) -> None:
    policy = PlanningPolicy(
        mode=PlanningMode.MANAGED,
        planner_model="planner-x",
        planner_max_replans=7,
    )
    loop = _direct_loop(tmp_path, planning_policy=policy)

    assert loop.planning_policy is policy
    assert loop.use_planner is True
    assert loop.planner_model == "planner-x"
    assert loop.planner_max_replans == 7


def test_init_calls_phase_methods_in_order(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    originals = {name: getattr(AgentLoop, name) for name in PHASE_METHODS}

    def _wrap(name: str, original):
        def wrapper(self, *args, **kwargs):
            calls.append(name)
            return original(self, *args, **kwargs)

        return wrapper

    for name in PHASE_METHODS:
        monkeypatch.setattr(AgentLoop, name, _wrap(name, originals[name]))

    make_loop(tmp_path, provider=_test_provider())

    assert calls == PHASE_METHODS


def test_injected_mcp_runtime_is_adopted(tmp_path: Path) -> None:
    runtime = McpRuntime({})
    loop = _direct_loop(tmp_path, mcp_runtime=runtime)

    assert loop._mcp_runtime is runtime


def test_provider_none_raises_type_error(tmp_path: Path) -> None:
    cfg = AgentLoopConfig(provider=None)
    try:
        AgentLoop(bus=MessageBus(), workspace=tmp_path, config=cfg)
    except TypeError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("expected TypeError when provider is missing")
