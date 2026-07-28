# Memory Database Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename MiniUnicorn's internal vector-memory database from `vectors.db` to `memory.db` without adding backward-compatibility code.

**Architecture:** Keep the existing `VectorMemoryStore` implementation and SQLite schema unchanged. Change only the database path selected by `AgentLoop`, protect the behavior with a constructor-level wiring test, and document `memory.db` as an internal semantic-recall index.

**Tech Stack:** Python 3.11+, pathlib, pytest, unittest.mock, SQLite/sqlite-vec, Markdown

## Global Constraints

- The runtime path must be exactly `workspace/memory/memory.db`.
- Do not add migration, fallback, copy, or dual-file detection for `vectors.db`.
- Keep `miniunicorn/agent/vector_memory.py`, `VectorMemoryStore`, and `create_vector_store` unchanged.
- Do not change configuration keys, public Python APIs, embedding providers, embedding models, the SQLite schema, or retrieval behavior.
- Treat `memory.db` as the internal memory index, not an external document knowledge base.

---

### Task 1: Change and protect the runtime database path

**Files:**
- Modify: `tests/agent/test_upgrade_integration.py`
- Modify: `miniunicorn/agent/loop.py:345-351`

**Interfaces:**
- Consumes: `create_vector_store(db_path: Path, embedding_dim: int = 1536)` from `miniunicorn.agent.vector_memory`.
- Produces: `AgentLoop(..., vector_recall=True)` passes `<workspace>/memory/memory.db` to `create_vector_store`.

- [ ] **Step 1: Write the failing wiring test**

Add this test after the existing vector-store fallback tests in
`tests/agent/test_upgrade_integration.py`:

```python
def test_agent_loop_uses_memory_db_for_vector_recall(tmp_path, monkeypatch):
    """Vector recall stores its internal index at memory/memory.db."""
    from miniunicorn.agent import vector_memory as vm
    from miniunicorn.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    vector_store = MagicMock()
    create_vector_store = MagicMock(return_value=vector_store)
    monkeypatch.setattr(vm, "create_vector_store", create_vector_store)

    AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
        vector_recall=True,
    )

    create_vector_store.assert_called_once_with(tmp_path / "memory" / "memory.db")
```

- [ ] **Step 2: Run the focused test and verify that the old path fails**

Run:

```powershell
uv run pytest tests/agent/test_upgrade_integration.py::test_agent_loop_uses_memory_db_for_vector_recall -v
```

Expected: FAIL because the actual call ends in `memory/vectors.db`.

- [ ] **Step 3: Make the minimal runtime change**

In `miniunicorn/agent/loop.py`, change only the filename passed to the existing
factory:

```python
vector_store = create_vector_store(self.workspace / "memory" / "memory.db")
```

- [ ] **Step 4: Run the focused test and vector-memory tests**

Run:

```powershell
uv run pytest tests/agent/test_upgrade_integration.py::test_agent_loop_uses_memory_db_for_vector_recall tests/agent/test_upgrade_integration.py::test_vector_memory_noop_store_contract tests/agent/test_upgrade_integration.py::test_vector_memory_store_disabled_when_no_sqlite_vec tests/agent/test_upgrade_integration.py::test_create_vector_store_falls_back_to_noop -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit the runtime change**

```powershell
git add miniunicorn/agent/loop.py tests/agent/test_upgrade_integration.py
git commit -m "refactor(memory): rename vector index database"
```

### Task 2: Document the database role and verify the rename

**Files:**
- Modify: `docs/memory.md:65-84`

**Interfaces:**
- Consumes: the runtime path established by Task 1.
- Produces: user-facing documentation that identifies `memory/memory.db` as the optional internal semantic-recall index.

- [ ] **Step 1: Add `memory.db` to the workspace layout**

Change the `workspace/memory/` tree in `docs/memory.md` to include:

```text
    ├── memory.db        # Optional local SQLite index for semantic recall
```

Place it after `history.jsonl` and before the cursor files.

- [ ] **Step 2: Explain its scope**

Add this bullet to the explanation immediately below the tree:

```markdown
- `memory.db` indexes selected internal memories for semantic recall when vector recall is enabled; it is not an external document knowledge base.
```

- [ ] **Step 3: Verify there are no stale product references**

Run:

```powershell
rg -n --glob '!docs/superpowers/**' 'vectors\.db' miniunicorn tests docs README.md README.en.md
```

Expected: no matches and exit code 1 from `rg`.

- [ ] **Step 4: Run focused tests and static checks**

Run:

```powershell
uv run pytest tests/agent/test_upgrade_integration.py -v
uv run ruff check miniunicorn/agent/loop.py tests/agent/test_upgrade_integration.py
git diff --check
```

Expected: the test file passes, Ruff reports no errors, and `git diff --check`
prints no whitespace errors.

- [ ] **Step 5: Commit the documentation**

```powershell
git add docs/memory.md
git commit -m "docs(memory): document internal memory database"
```
