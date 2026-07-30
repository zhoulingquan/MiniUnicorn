from __future__ import annotations

import ast
import inspect
from pathlib import Path

from miniunicorn.agent.loop import AgentLoop

LOOP_LINE_LIMIT = 900
COMPATIBILITY_METHODS = {
    "_process_message",
    "_execute_message",
    "_process_system_message",
    "_run_agent_loop",
    "_assemble_outbound",
    "_sanitize_persisted_blocks",
    "_save_turn",
    "_persist_subagent_followup",
}

DELEGATE_METHODS = COMPATIBILITY_METHODS

COLLABORATOR_LINE_LIMITS = {
    "turn_executor.py": 340,
    "agent_run_adapter.py": 420,
    "turn_persistence.py": 450,
    "turn_dispatcher.py": 200,
}


def _loop_path() -> Path:
    return Path(inspect.getsourcefile(AgentLoop) or "")


def _agent_loop_node() -> ast.ClassDef:
    tree = ast.parse(_loop_path().read_text(encoding="utf-8"))
    return next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentLoop"
    )


def _method_nodes() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in _agent_loop_node().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _body_statement_count(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    """Count non-docstring statements in a method body."""
    return sum(
        not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Constant)
        for statement in node.body
    )


def test_agent_loop_keeps_compatibility_method_surface() -> None:
    missing = sorted(name for name in COMPATIBILITY_METHODS if not hasattr(AgentLoop, name))
    assert missing == []


def test_agent_loop_source_stays_under_facade_limit() -> None:
    assert len(_loop_path().read_text(encoding="utf-8").splitlines()) <= LOOP_LINE_LIMIT


def test_agent_loop_compatibility_methods_are_thin_delegates() -> None:
    methods = _method_nodes()
    assert {
        name: _body_statement_count(methods[name])
        for name in DELEGATE_METHODS
        if _body_statement_count(methods[name]) > 1
    } == {}


def test_agent_loop_has_no_large_non_constructor_method() -> None:
    oversized = {
        name: node.end_lineno - node.lineno + 1
        for name, node in _method_nodes().items()
        if name != "__init__" and node.end_lineno - node.lineno + 1 > 120
    }
    assert oversized == {}


def test_collaborators_stay_within_module_and_method_limits() -> None:
    root = Path("miniunicorn/agent")
    oversized_modules: dict[str, int] = {}
    oversized_methods: dict[str, int] = {}
    for filename, limit in COLLABORATOR_LINE_LIMITS.items():
        source = (root / filename).read_text(encoding="utf-8")
        if len(source.splitlines()) > limit:
            oversized_modules[filename] = len(source.splitlines())
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = node.end_lineno - node.lineno + 1
                if span > 240:
                    oversized_methods[f"{filename}:{node.name}"] = span
    assert oversized_modules == {}
    assert oversized_methods == {}


def test_agent_loop_constructs_all_four_collaborators(loop_factory) -> None:
    from miniunicorn.agent.agent_run_adapter import AgentRunAdapter
    from miniunicorn.agent.turn_dispatcher import TurnDispatcher
    from miniunicorn.agent.turn_executor import TurnExecutor
    from miniunicorn.agent.turn_persistence import TurnPersistence

    loop = loop_factory()
    assert isinstance(loop._turn_executor, TurnExecutor)
    assert isinstance(loop._agent_run_adapter, AgentRunAdapter)
    assert isinstance(loop._turn_persistence, TurnPersistence)
    assert isinstance(loop._turn_dispatcher, TurnDispatcher)
    assert loop._turn_executor.host is loop
    assert loop._agent_run_adapter.host is loop
    assert loop._turn_persistence.host is loop
    assert loop._turn_dispatcher.host is loop


def test_agent_loop_collaborator_modules_do_not_import_loop_at_runtime() -> None:
    for path in (
        Path("miniunicorn/agent/turn_executor.py"),
        Path("miniunicorn/agent/agent_run_adapter.py"),
        Path("miniunicorn/agent/turn_persistence.py"),
        Path("miniunicorn/agent/turn_dispatcher.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and "miniunicorn.agent.loop" in ast.unparse(node)
        ]
        assert imports == []
