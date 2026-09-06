"""W1-2 Commit 2: SubagentManager ownership can move out of AgentLoop.

``AgentLoopConfig`` / ``AgentLoopBuilder`` now accept an injected
``SubagentManager`` (``with_subagent_manager``). When injected, the loop uses
that exact instance; when absent it falls back to self-constructing one (the
legal single-process form). These tests pin the identity contract so the
composition root can own the manager without the loop re-wrapping it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from erza.agent.loop_builder import AgentLoopBuilder
from erza.agent.subagent import SubagentManager
from erza.bus.queue import MessageBus


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


def _make_manager(tmp_path: Path) -> SubagentManager:
    return SubagentManager(
        provider=_provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=2000,
        model="test-model",
    )


def test_with_subagent_manager_injected_becomes_loop_subagents(tmp_path: Path) -> None:
    injected = _make_manager(tmp_path)
    loop = (
        AgentLoopBuilder(MessageBus(), _provider(), tmp_path)
        .with_subagent_manager(injected)
        .build()
    )

    assert loop.subagents is injected


def test_non_injected_loop_self_constructs_subagent_manager(tmp_path: Path) -> None:
    loop = AgentLoopBuilder(MessageBus(), _provider(), tmp_path).build()

    assert loop.subagents is not None
    assert isinstance(loop.subagents, SubagentManager)


def test_injected_manager_is_not_replaced_or_re_wrapped(tmp_path: Path) -> None:
    injected = _make_manager(tmp_path)
    loop = (
        AgentLoopBuilder(MessageBus(), _provider(), tmp_path)
        .with_subagent_manager(injected)
        .build()
    )

    assert loop.subagents is injected
    assert loop.subagents._turn_budget_factory is injected._turn_budget_factory
