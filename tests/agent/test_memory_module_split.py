"""W4-1 split acceptance tests: memory.py facade vs memory_store.py module.

Guards the pure-move refactor: the facade re-export keeps every original
symbol path working, the new module imports standalone, and the shared
constants keep a single definition site.
"""

import importlib
import importlib.util
import sys
from unittest.mock import MagicMock

import miniunicorn.agent
import miniunicorn.agent.memory as memory_facade
import miniunicorn.agent.memory_consolidator as memory_consolidator
import miniunicorn.agent.memory_store as memory_store


def test_facade_identity_store():
    """Facade re-exports resolve to the exact classes defined in memory_store."""
    assert memory_facade.MemoryStore is memory_store.MemoryStore
    assert memory_facade.WorkspaceMemoryRegistry is memory_store.WorkspaceMemoryRegistry


def test_memory_store_standalone_import():
    """memory_store.py imports standalone without circular-import symptoms."""
    module = importlib.import_module("miniunicorn.agent.memory_store")
    assert module is not None
    assert "miniunicorn.agent.memory_store" in sys.modules


def test_constants_live_in_store_module():
    """Shared constants are defined in memory_store and visible via the facade."""
    assert memory_store._HISTORY_ENTRY_HARD_CAP == 64_000
    assert memory_store._RAW_ARCHIVE_MAX_CHARS == 16_000
    assert memory_facade._HISTORY_ENTRY_HARD_CAP is memory_store._HISTORY_ENTRY_HARD_CAP
    assert memory_facade._RAW_ARCHIVE_MAX_CHARS is memory_store._RAW_ARCHIVE_MAX_CHARS


def test_agent_package_reexport_identity():
    """agent/__init__.py re-export chain through the facade stays intact."""
    assert miniunicorn.agent.MemoryStore is memory_store.MemoryStore


def test_memory_py_shrunk():
    """memory.py shrank to near-facade size; memory_store.py stayed bounded."""
    facade_path = importlib.util.find_spec("miniunicorn.agent.memory").origin
    store_path = importlib.util.find_spec("miniunicorn.agent.memory_store").origin
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
        memory_facade._ARCHIVE_SUMMARY_MAX_CHARS
        is memory_consolidator._ARCHIVE_SUMMARY_MAX_CHARS
    )


def test_memory_py_shrunk_further():
    """memory.py shrank again after the Consolidator extraction."""
    facade_path = importlib.util.find_spec("miniunicorn.agent.memory").origin
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
