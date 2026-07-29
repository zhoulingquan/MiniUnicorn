from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from miniunicorn.agent.loop import AgentLoop

LOOP_LINE_LIMIT = 900
COMPATIBILITY_METHODS = {
    "run",
    "process_direct",
    "_dispatch",
    "_process_message",
    "_execute_message",
    "_process_system_message",
    "_run_agent_loop",
    "_assemble_outbound",
    "_sanitize_persisted_blocks",
    "_save_turn",
    "_persist_subagent_followup",
    "_set_runtime_checkpoint",
    "_mark_pending_user_turn",
    "_clear_pending_user_turn",
    "_clear_runtime_checkpoint",
    "_restore_runtime_checkpoint",
    "_restore_pending_user_turn",
    "_cancel_active_tasks",
}


def _loop_path() -> Path:
    return Path(inspect.getsourcefile(AgentLoop) or "")


def _agent_loop_node() -> ast.ClassDef:
    tree = ast.parse(_loop_path().read_text(encoding="utf-8"))
    return next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentLoop"
    )


def test_agent_loop_keeps_compatibility_method_surface() -> None:
    missing = sorted(name for name in COMPATIBILITY_METHODS if not hasattr(AgentLoop, name))
    assert missing == []


@pytest.mark.xfail(
    strict=True,
    reason="removed after the four collaborators make AgentLoop a <=900 line facade",
)
def test_agent_loop_source_stays_under_facade_limit() -> None:
    assert len(_loop_path().read_text(encoding="utf-8").splitlines()) <= LOOP_LINE_LIMIT


def test_agent_loop_does_not_yet_construct_partial_collaborators(
    loop_factory,
) -> None:
    loop = loop_factory()
    partial = [
        name
        for name in (
            "_turn_executor",
            "_agent_run_adapter",
            "_turn_persistence",
            "_turn_dispatcher",
        )
        if hasattr(loop, name)
    ]
    assert partial == []
