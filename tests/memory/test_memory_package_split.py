"""W4-1 split acceptance tests, extended by W7-1: memory package at top level.

Guards the pure-move refactors: the package facade re-export keeps every
original symbol path working, the store module imports standalone, the shared
constants keep a single definition site, and (since W7-1) the package lives at
``miniunicorn/memory/`` with zero ``miniunicorn.agent`` dependencies.
"""

import ast
import importlib
import importlib.util
import sys
from unittest.mock import MagicMock

import miniunicorn.agent
import miniunicorn.agent.dream_trigger as dream_trigger
import miniunicorn.memory as memory_facade
import miniunicorn.memory.consolidator as memory_consolidator
import miniunicorn.memory.dream as memory_dream
import miniunicorn.memory.jsonl_import as memory_jsonl_import
import miniunicorn.memory.store as memory_store


def test_facade_identity_store():
    """Facade re-exports resolve to the exact classes defined in memory_store."""
    assert memory_facade.MemoryStore is memory_store.MemoryStore
    assert memory_facade.WorkspaceMemoryRegistry is memory_store.WorkspaceMemoryRegistry


def test_memory_store_standalone_import():
    """memory_store.py imports standalone without circular-import symptoms."""
    module = importlib.import_module("miniunicorn.memory.store")
    assert module is not None
    assert "miniunicorn.memory.store" in sys.modules


def test_constants_live_in_store_module():
    """Shared constants are defined in memory_store and visible via the facade."""
    assert memory_store._HISTORY_ENTRY_HARD_CAP == 64_000
    assert memory_store._RAW_ARCHIVE_MAX_CHARS == 16_000
    assert memory_facade._HISTORY_ENTRY_HARD_CAP is memory_store._HISTORY_ENTRY_HARD_CAP
    assert memory_facade._RAW_ARCHIVE_MAX_CHARS is memory_store._RAW_ARCHIVE_MAX_CHARS


def test_agent_package_reexport_identity():
    """Since W7-1, agent/__init__.py no longer re-exports memory symbols."""
    assert not hasattr(miniunicorn.agent, "MemoryStore")
    assert "MemoryStore" not in miniunicorn.agent.__all__


def test_memory_py_shrunk():
    """memory.py shrank to near-facade size; memory_store.py stayed bounded."""
    facade_path = importlib.util.find_spec("miniunicorn.memory").origin
    store_path = importlib.util.find_spec("miniunicorn.memory.store").origin
    with open(facade_path, encoding="utf-8") as f:
        facade_lines = len(f.read().splitlines())
    with open(store_path, encoding="utf-8") as f:
        store_lines = len(f.read().splitlines())
    assert facade_lines < 1150
    assert store_lines < 1000


def test_facade_identity_consolidator():
    """Facade re-export resolves to the exact class defined in memory_consolidator."""
    assert memory_facade.Consolidator is memory_consolidator.Consolidator


def test_shared_constant_single_definition():
    """_RAW_ARCHIVE_MAX_CHARS is imported from memory_store, never redefined."""
    assert memory_consolidator._RAW_ARCHIVE_MAX_CHARS is memory_store._RAW_ARCHIVE_MAX_CHARS


def test_archive_summary_constant_identity():
    """Archive-summary constant lives in memory_consolidator, visible via the facade."""
    assert memory_consolidator._ARCHIVE_SUMMARY_MAX_CHARS == 8_000
    assert (
        memory_facade._ARCHIVE_SUMMARY_MAX_CHARS is memory_consolidator._ARCHIVE_SUMMARY_MAX_CHARS
    )


def test_memory_py_shrunk_further():
    """memory.py shrank again after the Consolidator extraction."""
    facade_path = importlib.util.find_spec("miniunicorn.memory").origin
    with open(facade_path, encoding="utf-8") as f:
        facade_lines = len(f.read().splitlines())
    assert facade_lines < 620


def test_consolidator_token_estimate_patchable_via_defining_module(monkeypatch):
    """estimate_message_tokens is patchable through the memory_consolidator namespace."""
    from types import SimpleNamespace

    calls: list[dict] = []
    monkeypatch.setattr(
        memory_consolidator,
        "estimate_message_tokens",
        lambda message: (calls.append(message), 999)[1],
    )
    consolidator = memory_consolidator.Consolidator(
        store=MagicMock(),
        provider=MagicMock(),
        model="test-model",
        sessions=MagicMock(),
        context_window_tokens=200,
        build_messages=lambda **kwargs: [],
        get_tool_definitions=lambda: [],
    )
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
        ],
        last_consolidated=0,
    )
    boundary = consolidator.pick_consolidation_boundary(session, tokens_to_remove=1)
    assert boundary == (1, 999)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# W4-3: Dream extraction + final pure-facade state
# ---------------------------------------------------------------------------


def test_final_facade_identity():
    """All 11 facade symbols resolve to the modules that define them."""
    assert memory_facade.MemoryStore is memory_store.MemoryStore
    assert memory_facade.WorkspaceMemoryRegistry is memory_store.WorkspaceMemoryRegistry
    assert memory_facade.Consolidator is memory_consolidator.Consolidator
    assert memory_facade.Dream is memory_dream.Dream
    assert memory_facade.count_pending_dream_entries is memory_dream.count_pending_dream_entries
    assert memory_facade.reflection_evidence_id is memory_dream.reflection_evidence_id
    assert memory_facade._dream_source_batch is memory_dream._dream_source_batch
    assert memory_facade._parse_datetime_loose is memory_dream._parse_datetime_loose
    assert memory_facade._HISTORY_ENTRY_HARD_CAP is memory_store._HISTORY_ENTRY_HARD_CAP
    assert memory_facade._RAW_ARCHIVE_MAX_CHARS is memory_store._RAW_ARCHIVE_MAX_CHARS
    assert (
        memory_facade._ARCHIVE_SUMMARY_MAX_CHARS is memory_consolidator._ARCHIVE_SUMMARY_MAX_CHARS
    )


def test_memory_py_is_pure_facade():
    """memory.py is a pure facade: at most 60 lines and zero class definitions."""
    from pathlib import Path

    facade_path = Path(importlib.util.find_spec("miniunicorn.memory").origin)
    src = facade_path.read_text(encoding="utf-8")
    assert len(src.splitlines()) <= 60
    assert "class " not in src


def test_jsonl_import_reexports():
    """The facade keeps the baseline jsonl-import namespace re-exports alive."""
    assert memory_facade.LegacyJournalImportError is memory_jsonl_import.LegacyJournalImportError
    assert memory_facade.migrate_legacy_journal is memory_jsonl_import.migrate_legacy_journal


def test_dream_helpers_moved_with_class():
    """reflection_evidence_id lives in memory_dream and still validates ids."""
    entry = {"reflection_id": "rfl_" + "a" * 32}
    assert memory_dream.reflection_evidence_id(entry) == "rfl_" + "a" * 32


def test_consumer_entry_points():
    """agent package and dream_trigger resolve Dream symbols to memory_dream."""
    assert not hasattr(miniunicorn.agent, "Dream")
    assert dream_trigger.count_pending_dream_entries is memory_dream.count_pending_dream_entries


def test_cold_import_loads_no_agent_modules():
    """Cold-importing the memory package must not pull in any agent module."""
    import subprocess

    code = (
        "import sys\n"
        "import miniunicorn.memory\n"
        "bad = [m for m in sys.modules if m == 'miniunicorn.agent' or m.startswith('miniunicorn.agent.')]\n"
        "print('\\n'.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_memory_package_is_agent_free():
    """AST scan: no module in miniunicorn/memory/ imports miniunicorn.agent."""
    from pathlib import Path

    package_root = Path(importlib.util.find_spec("miniunicorn.memory").origin).parent
    offenders: list[str] = []
    for path in sorted(package_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            if any(
                name == "miniunicorn.agent" or name.startswith("miniunicorn.agent.")
                for name in names
            ):
                offenders.append(path.name)
    assert offenders == []
