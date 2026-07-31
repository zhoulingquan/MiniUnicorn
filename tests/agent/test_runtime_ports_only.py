"""Task 3 — Agent Core must own its ports and never import Runtime."""

import ast
from pathlib import Path

from miniunicorn.agent.ports import EffectiveToolPolicy, build_tool_execution_request


def test_tool_request_builder_is_agent_owned() -> None:
    request = build_tool_execution_request(
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="read_file",
        arguments={"path": "README.md"},
        policy=EffectiveToolPolicy(
            effect_class="READ",
            risk_class="LOW",
            idempotency_mode="REPLAY_SAFE",
            approval_policy="NEVER",
            recovery_policy="REPLAY",
            concurrency_scope="NONE",
        ),
    )
    assert request.task_id == "task-1"
    assert len(request.arguments_hash) == 64
    assert len(request.idempotency_key) == 64


def test_agent_source_has_no_runtime_import() -> None:
    root = Path("miniunicorn/agent")
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("miniunicorn.runtime"):
                violations.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("miniunicorn.runtime"):
                        violations.append(f"{path}:{node.lineno}")
    assert violations == []


def test_agent_source_has_no_sqlite3_import() -> None:
    root = Path("miniunicorn/agent")
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "sqlite3":
                violations.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlite3":
                        violations.append(f"{path}:{node.lineno}")
    assert violations == []
