"""WP0 — Runtime dependency and module-boundary characterization.

These tests pin the invariant that Agent Core must not import SQLite or
multiprocessing implementations (design §6.17, §6.23, acceptance #23).
They also document the not-yet-existing runtime package as failing tests
marked for WP1 so the inventory is visible during migration.

No production behavior is changed by this file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[2] / "miniunicorn" / "agent"
_RUNTIME_PKG = Path(__file__).resolve().parents[2] / "miniunicorn" / "runtime"
_AGENT_PORTS = Path(__file__).resolve().parents[2] / "miniunicorn" / "agent" / "ports.py"

# Modules Agent Core may not import at runtime. The durable runtime lives in
# ``miniunicorn.runtime`` and IPC/multiprocessing primitives live in the
# supervisor layer. Agent Core must depend only on its own ports.
_FORBIDDEN_AGENT_IMPORTS = {
    "sqlite3",
    "multiprocessing",
    "miniunicorn.runtime",
}

# Current exceptions: modules under ``miniunicorn/agent/`` that import a
# forbidden module today and are scheduled for hardening by a later WP.
# Each entry maps the file name (relative to ``miniunicorn/agent/``) to the
# WP that will remove the violation. When that WP lands, remove the entry.
_KNOWN_VIOLATIONS: dict[str, str] = {
    # Embedding line: the provenance-aware sqlite-vec index and its compat
    # shim live in agent/ by design (they degrade to NoOp without sqlite-vec).
    "vector_index.py": "embedding line storage adapter (sqlite-vec)",
    "vector_memory.py": "embedding line storage adapter (sqlite-vec)",
}


def _iter_agent_py_files() -> list[Path]:
    """Yield every ``.py`` under ``miniunicorn/agent/`` (recursive)."""
    if not _AGENT_ROOT.exists():
        return []
    return sorted(_AGENT_ROOT.rglob("*.py"))


def _imports(tree: ast.AST) -> list[str]:
    """Return module names referenced by ``import`` / ``from ... import``."""

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def _forbidden_violations(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    imports = _imports(tree)
    violations: list[str] = []
    for forbidden in _FORBIDDEN_AGENT_IMPORTS:
        for imported in imports:
            if imported == forbidden or imported.startswith(forbidden + "."):
                violations.append(imported)
    return violations


# ---------------------------------------------------------------------------
# Current invariants (must pass today and after every WP)
# ---------------------------------------------------------------------------


class TestAgentCoreDependencyPurity:
    """Agent Core must not import SQLite, multiprocessing, or runtime impl."""

    def test_agent_directory_exists(self) -> None:
        assert _AGENT_ROOT.exists(), "miniunicorn/agent/ must exist"

    @pytest.mark.parametrize(
        "path",
        _iter_agent_py_files(),
        ids=lambda p: str(p.relative_to(_AGENT_ROOT.parent.parent)),
    )
    def test_agent_module_does_not_import_forbidden_runtimes(self, path: Path) -> None:
        relative = path.relative_to(_AGENT_ROOT).as_posix()
        violations = _forbidden_violations(path)
        if not violations:
            return
        if relative in _KNOWN_VIOLATIONS:
            pytest.xfail(_KNOWN_VIOLATIONS[relative])
        assert not violations, (
            f"{path.relative_to(_AGENT_ROOT.parent.parent)} imports forbidden runtime "
            f"modules: {violations}. Agent Core must depend only on its own ports."
        )

    def test_agent_loop_collaborators_do_not_import_loop_at_runtime(self) -> None:
        """Existing boundary: collaborators must not import ``AgentLoop``.

        This re-asserts the rule from ``test_agent_loop_structure`` so the
        runtime migration cannot silently regress it.
        """
        collaborators = [
            _AGENT_ROOT / "turn_executor.py",
            _AGENT_ROOT / "turn_dispatcher.py",
            _AGENT_ROOT / "turn_persistence.py",
            _AGENT_ROOT / "agent_run_adapter.py",
        ]
        for path in collaborators:
            assert path.exists(), f"missing collaborator: {path.name}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for imported in _imports(tree):
                assert "miniunicorn.agent.loop" not in imported, (
                    f"{path.name} imports miniunicorn.agent.loop at runtime"
                )


# ---------------------------------------------------------------------------
# Runtime package existence (WP1 landed)
# ---------------------------------------------------------------------------


class TestRuntimePackageCreated:
    """Asserts the runtime package and Agent-owned ports exist (design §10).

    These were xfails before WP1; they are now hard assertions so the
    package cannot be silently removed.
    """

    def test_runtime_package_exists(self) -> None:
        assert _RUNTIME_PKG.exists() and _RUNTIME_PKG.is_dir()

    def test_agent_ports_module_exists(self) -> None:
        assert _AGENT_PORTS.exists() and _AGENT_PORTS.is_file()

    def test_runtime_contracts_module_exists(self) -> None:
        assert (_RUNTIME_PKG / "contracts.py").exists()

    def test_runtime_models_module_exists(self) -> None:
        assert (_RUNTIME_PKG / "models.py").exists()


# ---------------------------------------------------------------------------
# Allowed dependency direction (design §7.2)
# ---------------------------------------------------------------------------


class TestDependencyDirection:
    """Document the allowed dependency direction from design §7.2.

    Today this is informational; after WP1 it becomes a hard constraint.
    """

    def test_runtime_contracts_must_not_import_agent_loop(self) -> None:
        """When ``miniunicorn/runtime/contracts.py`` exists, it must not
        import ``miniunicorn.agent.loop`` (only the ports module)."""
        runtime_contracts = _RUNTIME_PKG / "contracts.py"
        if not runtime_contracts.exists():
            pytest.skip("WP1: runtime/contracts.py not yet created")
        tree = ast.parse(runtime_contracts.read_text(encoding="utf-8"))
        for imported in _imports(tree):
            assert "miniunicorn.agent.loop" not in imported
            assert "miniunicorn.agent.runner" not in imported

    def test_runtime_store_must_not_import_agent_loop(self) -> None:
        """The SQLite Store façade and every responsibility mixin must
        not import ``miniunicorn.agent.loop`` or ``miniunicorn.agent.runner``.

        The store lives at ``miniunicorn/runtime/sqlite/`` (design §7.3,
        Task 12). After the internal split, every ``*_store.py`` module
        under that subpackage is inspected so a new mixin cannot silently
        reintroduce an Agent-loop dependency.
        """
        sqlite_pkg = _RUNTIME_PKG / "sqlite"
        assert sqlite_pkg.exists(), "miniunicorn/runtime/sqlite/ must exist"
        store_modules = sorted(sqlite_pkg.glob("*_store.py"))
        assert store_modules, "expected at least one *_store.py module"
        for module_path in store_modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for imported in _imports(tree):
                assert "miniunicorn.agent.loop" not in imported, (
                    f"{module_path.name} imports miniunicorn.agent.loop"
                )
                assert "miniunicorn.agent.runner" not in imported, (
                    f"{module_path.name} imports miniunicorn.agent.runner"
                )
