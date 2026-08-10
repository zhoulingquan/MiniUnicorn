# MiniUnicorn Main Without Embedding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all embedding and vector-memory functionality, reservations, configuration, dependencies, tests, UI copy, and product documentation from `main` while preserving MiniUnicorn's structured-memory pipeline.

**Architecture:** Collapse memory back to one structured path: conversation history flows into `history.jsonl`, Consolidator/Reflection/Dream update durable files, and `ContextBuilder` always injects structured memory and recent history. Remove the parallel vector path at every boundary instead of replacing it with a flag, NoOp object, adapter, or compatibility shim. Preserve old on-disk databases as inert user files by ensuring the runtime has no code path that knows about or opens them.

**Tech Stack:** Python 3.11+, Pydantic 2, pytest/pytest-asyncio, Hatchling/build, uv lock format, React/Vite i18n, Markdown

## Global Constraints

- Work only on `main`; do not checkout, rebase, delete, rewrite, merge, cherry-pick into, or force-push either enhancement branch.
- Record `origin/codex/embedding-no-worker-latest` and `origin/codex/embedding-memory-production` commit IDs before and after implementation; they must remain unchanged by this work.
- Remove functionality instead of retaining NoOp stores, feature flags, compatibility imports, empty methods, dormant constructor arguments, or future interfaces.
- Do not delete, rename, migrate, read, or write an existing user `memory.db`.
- Preserve `SOUL.md`, `USER.md`, `MEMORY.md`, `history.jsonl`, Consolidator, Dream, Reflection, episodic/procedural/shared memory, notes/scratchpad, and GitStore audit/restore.
- Preserve model-context metadata such as `max_position_embeddings`; it is unrelated to memory embedding.
- Existing Git history may contain removed functionality. The final tracked tree and freshly built wheel/sdist must not expose it.
- Do not touch pre-existing ignored or untracked files. Only tracked product files and files created by this plan may be edited or deleted.
- Temporary boundary tests and this design/plan may name the removed feature during implementation; delete them in the final cleanup commit so the final tracked tree contains no feature-specific reservations or internal guidance.
- Use `apply_patch` for source/document edits and file deletions. Do not run broad formatters or mechanical repository-wide rewrites.

## File Structure and Responsibilities

**Delete runtime modules:**

- `miniunicorn/agent/vector_memory.py` — SQLite vector store and NoOp fallback.
- `miniunicorn/agent/tools/recall.py` — semantic memory search tool.
- `miniunicorn/providers/embedding.py` — separate embedding-provider wrapper.

**Modify runtime boundaries:**

- `miniunicorn/config/schema.py` — remove feature settings and make relevant unknown settings fail generically.
- `miniunicorn/providers/base.py` — remove the embedding method from the provider contract.
- `miniunicorn/providers/openai_compat_provider.py` — remove the embeddings endpoint implementation.
- `miniunicorn/agent/memory.py` — keep only structured storage, hygiene, Consolidator, and Dream behavior.
- `miniunicorn/agent/context.py` — always assemble structured memory and recent history.
- `miniunicorn/agent/loop.py` — remove vector initialization and query generation.
- `miniunicorn/agent/loop_builder.py` — stop accepting or forwarding removed settings.
- `miniunicorn/agent/_mcp_lifecycle.py` — construct tools without a memory-search dependency.
- `miniunicorn/agent/tools/context.py` — remove the memory-search-only tool context field.

**Modify tests and release surfaces:**

- `tests/config/test_config_boundaries.py` — permanent generic strict-schema tests.
- `tests/main_product_boundary_test.py` — temporary TDD boundary checks; delete before final handoff.
- `tests/agent/test_upgrade_integration.py` — remove vector-store tests while preserving planner, subagent, budget, Reflection, and context-governor coverage.
- `webui/src/i18n/locales/en/common.json` and `webui/src/i18n/locales/zh-CN/common.json` — remove the deleted tool description.
- `pyproject.toml` and `uv.lock` — remove the optional dependency group and resolved package.
- `README.md`, `README.en.md`, and `docs/memory.md` — describe structured memory only.
- `docs/superpowers/plans/2026-07-28-memory-db-rename.md` and `docs/superpowers/specs/2026-07-28-memory-db-rename-design.md` — remove tracked obsolete internal guidance dedicated to the deleted product path.
- `docs/superpowers/specs/2026-07-28-comprehensive-code-review-remediation-design.md` — remove only Package A and its embedding/vector cross-references; preserve the unrelated Packages B–E.
- `docs/superpowers/specs/2026-08-10-main-without-embedding-design.md` and this plan — retain during execution, then delete from the final tracked tree after they have served as historical decision records.

---

### Task 1: Remove Configuration Surface and Enforce Generic Unknown-Field Errors

**Files:**

- Create: `tests/config/test_config_boundaries.py`
- Modify: `miniunicorn/config/schema.py`
- Modify: `miniunicorn/config/loader.py`

**Interfaces:**

- Consumes: Pydantic `AgentDefaults` and `ProvidersConfig` model validation.
- Produces: `AgentDefaults` rejects unknown settings; `ProvidersConfig` continues accepting dictionary-shaped custom providers but rejects scalar extras without recognizing any retired field by name.

- [ ] **Step 1: Write failing generic schema tests**

```python
import pytest
from pydantic import ValidationError

from miniunicorn.config.schema import AgentDefaults, ProviderConfig, ProvidersConfig


def test_agent_defaults_reject_unknown_setting() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentDefaults.model_validate({"unsupportedFeature": True})


def test_custom_provider_requires_object_shape() -> None:
    with pytest.raises(ValidationError, match="must be an object"):
        ProvidersConfig.model_validate({"customScalar": "not-an-object"})


def test_custom_provider_object_remains_supported() -> None:
    providers = ProvidersConfig.model_validate(
        {"teamGateway": {"apiKey": "secret", "apiBase": "https://example.test/v1"}}
    )
    custom = providers.__pydantic_extra__["teamGateway"]
    assert isinstance(custom, ProviderConfig)
    assert custom.api_key == "secret"
```

- [ ] **Step 2: Run the tests and verify the first two fail for the expected reasons**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/config/test_config_boundaries.py -v
```

Expected: `test_agent_defaults_reject_unknown_setting` fails because the field is silently ignored; `test_custom_provider_requires_object_shape` fails because the scalar is currently retained as an allowed extra; the object-shaped custom provider test passes.

- [ ] **Step 3: Make `AgentDefaults` strict and make custom provider extras uniformly typed**

Add this class-level configuration directly below the `AgentDefaults` docstring:

```python
model_config = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    extra="forbid",
)
```

Delete the `vector_recall` and `embedding_model` fields from `AgentDefaults`. Delete `embedding_provider`, `embedding_model`, `embedding_api_base`, and `embedding_api_key` plus their explanatory comments from `ProvidersConfig`.

Replace the body of `ProvidersConfig._coerce_extra_providers` with the generic shape check below; do not enumerate any retired setting names:

```python
extras = self.__pydantic_extra__ or {}
for name, value in list(extras.items()):
    if name in _BUILTIN_PROVIDER_NAMES:
        raise ValueError(f"自定义 provider 名 '{name}' 与内置 provider 冲突")
    if isinstance(value, dict):
        extras[name] = ProviderConfig.model_validate(value)
    elif not isinstance(value, ProviderConfig):
        raise ValueError(f"custom provider {name!r} must be an object")
return self
```

`tests/config/test_config_migration.py` already requires the unrelated legacy `memoryWindow` key to load and disappear on save. Preserve that behavior explicitly in `_migrate_config` before schema validation:

```python
agents = result.get("agents", {})
defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
if isinstance(defaults, dict):
    defaults.pop("memoryWindow", None)
    defaults.pop("memory_window", None)
```

- [ ] **Step 4: Run focused and migration configuration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/config/test_config_boundaries.py tests/config/test_config_migration.py tests/config/test_env_interpolation.py tests/config/test_model_presets.py -q
```

Expected: all selected tests pass. The explicit `memoryWindow` migration preserves existing unrelated compatibility without weakening strict validation or adding exceptions for the removed feature.

- [ ] **Step 5: Commit the configuration boundary**

```powershell
git add miniunicorn/config/schema.py miniunicorn/config/loader.py tests/config/test_config_boundaries.py
git commit -m "refactor: remove vector memory configuration"
```

---

### Task 2: Remove Embedding From the Provider Contract

**Files:**

- Create: `tests/main_product_boundary_test.py`
- Modify: `miniunicorn/providers/base.py`
- Modify: `miniunicorn/providers/openai_compat_provider.py`
- Delete: `miniunicorn/providers/embedding.py`

**Interfaces:**

- Consumes: existing chat, streaming, retry, transcription, and provider lifecycle APIs.
- Produces: provider classes expose generation functionality only; no dedicated embedding module or method remains.

- [ ] **Step 1: Write the failing temporary provider-boundary test**

```python
import importlib.util

from miniunicorn.providers.base import LLMProvider
from miniunicorn.providers.openai_compat_provider import OpenAICompatProvider


def test_provider_contract_has_no_embedding_api() -> None:
    assert "embed" not in LLMProvider.__dict__
    assert "embed" not in OpenAICompatProvider.__dict__
    assert importlib.util.find_spec("miniunicorn.providers.embedding") is None
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_provider_contract_has_no_embedding_api -v
```

Expected: FAIL because both classes expose `embed` and the dedicated provider module exists.

- [ ] **Step 3: Remove the provider API and implementation**

In `miniunicorn/providers/base.py`, delete the complete concrete method beginning with:

```python
async def embed(
    self,
    texts: list[str],
    model: str = "text-embedding-3-small",
) -> list[list[float]]:
```

The class should proceed directly from `get_default_model()` to its next non-embedding member.

In `miniunicorn/providers/openai_compat_provider.py`, delete the complete method beginning with the same signature, including the `client.embeddings.create(...)` call and exception handling. Delete `miniunicorn/providers/embedding.py` in full with `apply_patch`.

- [ ] **Step 4: Run the boundary and provider regression suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_provider_contract_has_no_embedding_api tests/providers -q
```

Expected: all tests pass; chat, streaming, retry, response parsing, and transcription tests remain green.

- [ ] **Step 5: Commit the provider cleanup**

```powershell
git add miniunicorn/providers/base.py miniunicorn/providers/openai_compat_provider.py miniunicorn/providers/embedding.py tests/main_product_boundary_test.py
git commit -m "refactor: remove embedding provider API"
```

---

### Task 3: Reduce MemoryStore, Consolidator, and Dream to Structured Storage

**Files:**

- Modify: `tests/main_product_boundary_test.py`
- Modify: `miniunicorn/agent/memory.py`
- Test: `tests/agent/test_memory_store.py`
- Test: `tests/agent/test_dream.py`
- Test: `tests/command/test_builtin_dream.py`

**Interfaces:**

- Consumes: `MemoryStore` file APIs, `Consolidator`, `Dream`, Reflection files, and GitStore.
- Produces: structured history and memory files remain unchanged; hygiene returns only file-layer counts; archive and Dream no longer call a secondary index.

- [ ] **Step 1: Append a failing temporary structured-memory source boundary**

```python
import inspect

from miniunicorn.agent.memory import Consolidator, Dream, MemoryStore


def test_structured_memory_has_no_vector_side_channel() -> None:
    source = "\n".join(
        inspect.getsource(obj) for obj in (MemoryStore, Consolidator, Dream)
    )
    forbidden = (
        "_vector_store",
        "_embed_provider",
        "_embed_model",
        "attach_vector_store",
        "set_embed_provider",
        "index_text",
        "vec_decayed",
        "vec_archived",
    )
    assert not [token for token in forbidden if token in source]
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_structured_memory_has_no_vector_side_channel -v
```

Expected: FAIL and report the current vector-store fields, methods, or indexing calls.

- [ ] **Step 3: Remove only vector-specific state and side effects from `memory.py`**

Delete:

- `_vector_store`, `_embed_provider`, and `_embed_model` initialization;
- `attach_vector_store`, `set_embed_provider`, `vector_store`, and `index_text`;
- vector-specific comments after the structured append methods;
- the vector decay/archive block in `run_memory_hygiene`;
- the archive-summary `await self.store.index_text(...)` block in `Consolidator`;
- the procedural indexing loop in `Dream`.

After the edit, `run_memory_hygiene` must have this structured-only result shape:

```python
result = {
    "episodic": self.prune_episodic_if_needed(),
    "procedural": self.prune_procedural_if_needed(),
    "reflections": self.prune_reflections_if_needed(),
    "shared_procedural": self.prune_shared_procedural_if_needed(),
}
return result
```

Keep `append_history`, `append_episodic`, `append_procedural`, shared-memory operations, cursor advancement, file compaction, notes cleanup, Dream edits, Reflection consumption, and GitStore commits unchanged.

- [ ] **Step 4: Run structured-memory and Dream tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_structured_memory_has_no_vector_side_channel tests/agent/test_memory_store.py tests/agent/test_dream.py tests/command/test_builtin_dream.py -q
```

Expected: all selected tests pass. Dream still advances cursors only on success, Consolidator still writes `history.jsonl`, and GitStore restore tests remain green.

- [ ] **Step 5: Commit the structured-memory cleanup**

```powershell
git add miniunicorn/agent/memory.py tests/main_product_boundary_test.py
git commit -m "refactor: keep memory store structured only"
```

---

### Task 4: Make ContextBuilder Always Use Structured Memory

**Files:**

- Modify: `tests/main_product_boundary_test.py`
- Modify: `miniunicorn/agent/context.py`
- Test: `tests/agent/test_context_builder.py`
- Test: `tests/agent/test_context_prompt_cache.py`

**Interfaces:**

- Consumes: `MemoryStore.get_memory_context()`, `read_shared_memory()`, `read_notes()`, and `read_unprocessed_history()`.
- Produces: `build_system_prompt(...)` and `build_messages(...)` no longer accept retrieval parameters and always assemble the structured path.

- [ ] **Step 1: Append a failing ContextBuilder signature test**

```python
def test_context_builder_accepts_only_structured_memory_inputs() -> None:
    import inspect

    from miniunicorn.agent.context import ContextBuilder

    for method in (ContextBuilder.build_system_prompt, ContextBuilder.build_messages):
        params = inspect.signature(method).parameters
        assert "query_embedding" not in params
        assert "vector_recall" not in params
```

- [ ] **Step 2: Run the signature test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_context_builder_accepts_only_structured_memory_inputs -v
```

Expected: FAIL because both parameters exist on both methods.

- [ ] **Step 3: Remove the alternate retrieval branches**

Delete `query_embedding` and `vector_recall` from both method signatures and from the call between `build_messages` and `build_system_prompt`.

Replace the memory branch with the existing structured path only:

```python
memory = self.memory.get_memory_context()
if memory and not self._is_template_content(
    self.memory.read_memory(), "memory/MEMORY.md"
):
    parts.append((self._PRIORITY_MEMORY, f"# Memory\n\n{memory}"))
```

Replace the history branch with the existing recent-history path only:

```python
entries = self.memory.read_unprocessed_history(
    since_cursor=self.memory.get_last_dream_cursor()
)
if entries:
    capped = entries[-self._MAX_RECENT_HISTORY :]
    history_text = "\n".join(
        f"- [{entry['timestamp']}] {entry['content']}" for entry in capped
    )
    history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
    parts.append((self._PRIORITY_HISTORY, "# Recent History\n\n" + history_text))
```

Keep shared memory, notes, bootstrap files, skills, session summaries, runtime lines, prompt-cache ordering, and injection-budget priorities unchanged.

- [ ] **Step 4: Run context assembly regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_context_builder_accepts_only_structured_memory_inputs tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py -q
```

Expected: all selected tests pass; structured memory, shared memory, notes, and recent history remain visible in the assembled prompt.

- [ ] **Step 5: Commit the single context path**

```powershell
git add miniunicorn/agent/context.py tests/main_product_boundary_test.py
git commit -m "refactor: use structured memory context only"
```

---

### Task 5: Remove AgentLoop, Builder, MCP, Tool, and WebUI Reservations

**Files:**

- Modify: `tests/main_product_boundary_test.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/loop_builder.py`
- Modify: `miniunicorn/agent/_mcp_lifecycle.py`
- Modify: `miniunicorn/agent/tools/context.py`
- Delete: `miniunicorn/agent/vector_memory.py`
- Delete: `miniunicorn/agent/tools/recall.py`
- Modify: `tests/agent/test_upgrade_integration.py`
- Modify: `webui/src/i18n/locales/en/common.json`
- Modify: `webui/src/i18n/locales/zh-CN/common.json`

**Interfaces:**

- Consumes: `AgentLoop`, `AgentLoopBuilder.from_config`, `ToolContext`, ToolLoader discovery, and WebUI tool descriptions.
- Produces: one AgentLoop construction path with no database initialization or query embedding; ToolLoader cannot discover a recall tool; WebUI has no stale tool copy.

- [ ] **Step 1: Append failing runtime surface and disk-inertness tests**

```python
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.agent.loop_builder import AgentLoopBuilder
from miniunicorn.agent.tools.context import ToolContext
from miniunicorn.bus.queue import MessageBus
from miniunicorn.providers.base import LLMProvider


def _provider() -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    return provider


def test_runtime_constructors_have_no_vector_reservations() -> None:
    import inspect
    import importlib.util

    loop_params = inspect.signature(AgentLoop).parameters
    assert "vector_recall" not in loop_params
    assert "embedding_model" not in loop_params
    assert not hasattr(AgentLoopBuilder, "with_vector_recall")
    assert not hasattr(AgentLoopBuilder, "with_embedding_model")
    assert "memory_store" not in ToolContext.__dataclass_fields__
    assert importlib.util.find_spec("miniunicorn.agent.vector_memory") is None
    assert importlib.util.find_spec("miniunicorn.agent.tools.recall") is None


def test_agent_startup_does_not_create_a_database(tmp_path: Path) -> None:
    AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )
    assert list(tmp_path.rglob("*.db")) == []


def test_agent_startup_leaves_an_existing_database_untouched(tmp_path: Path) -> None:
    legacy = tmp_path / "memory" / "memory.db"
    legacy.parent.mkdir(parents=True)
    original = b"user-owned-legacy-data"
    legacy.write_bytes(original)

    AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )

    assert legacy.read_bytes() == original
```

- [ ] **Step 2: Run the runtime surface test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_runtime_constructors_have_no_vector_reservations -v
```

Expected: FAIL because constructor arguments, builder methods, tool context state, and modules still exist. The two disk tests may already pass with the old flag disabled; they protect the post-removal lifecycle behavior.

- [ ] **Step 3: Remove orchestration and construction plumbing**

In `miniunicorn/agent/loop.py`:

- remove `vector_recall` and `embedding_model` from `AgentLoop.__init__`;
- delete `_vector_recall`, `_embedding_model`, conditional vector-store creation, and its provider attachment;
- delete `_compute_query_embedding`;
- remove the two query-computation calls;
- remove `query_embedding=` and `vector_recall=` from both `ContextBuilder.build_messages` calls.

In `miniunicorn/agent/loop_builder.py`, delete both `with_*` methods and these two lines from `from_config`:

```python
builder.with_vector_recall(defaults.vector_recall)
builder.with_embedding_model(defaults.embedding_model)
```

In `miniunicorn/agent/_mcp_lifecycle.py`, remove `_vector_recall` from the mixin attribute documentation and remove this constructor argument:

```python
memory_store=self.context.memory if self._vector_recall else None,
```

In `miniunicorn/agent/tools/context.py`, delete the `memory_store` dataclass field and its recall-specific comment.

- [ ] **Step 4: Delete runtime modules and obsolete integration tests**

Delete `miniunicorn/agent/vector_memory.py` and `miniunicorn/agent/tools/recall.py` with `apply_patch`.

In `tests/agent/test_upgrade_integration.py`:

- remove the vector-store and recall bullets from the module docstring;
- remove the `NoOpVectorStore` and `VectorMemoryStore` import;
- delete the complete section containing `test_vector_memory_noop_store_contract`, `test_vector_memory_store_disabled_when_no_sqlite_vec`, `test_create_vector_store_falls_back_to_noop`, and `test_agent_loop_uses_memory_db_for_vector_recall`;
- renumber only the human-readable section comments; keep all planner, delegation, TurnBudget, Reflection, ContextGovernor, registry, and full-chain tests.

- [ ] **Step 5: Remove the deleted tool from WebUI source and rebuild the bundled UI**

Remove only these two properties from their respective `toolDescriptions` objects:

```json
"recall": "Search past memories and conversation summaries"
```

```json
"recall": "搜索过去的记忆和对话摘要"
```

Run:

```powershell
Push-Location webui
npm run build
Pop-Location
```

Expected: TypeScript and Vite build succeed; `emptyOutDir: true` replaces stale hashed assets in `miniunicorn/web/dist` so the release artifact cannot retain the removed copy.

- [ ] **Step 6: Run orchestration, tool-loader, integration, and UI checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py tests/agent/test_upgrade_integration.py tests/agent/test_loop_tool_context.py tests/tools/test_tool_loader.py -q
Push-Location webui
npm run lint
npm test
Pop-Location
```

Expected: all Python and WebUI checks pass. ToolLoader does not log an import failure because the deleted module is no longer discoverable.

- [ ] **Step 7: Commit runtime and UI cleanup**

```powershell
git add miniunicorn/agent/loop.py miniunicorn/agent/loop_builder.py miniunicorn/agent/_mcp_lifecycle.py miniunicorn/agent/tools/context.py miniunicorn/agent/vector_memory.py miniunicorn/agent/tools/recall.py tests/main_product_boundary_test.py tests/agent/test_upgrade_integration.py webui/src/i18n/locales/en/common.json webui/src/i18n/locales/zh-CN/common.json
git commit -m "refactor: remove vector memory runtime"
```

The rebuilt `miniunicorn/web/dist` is intentionally ignored; do not force-add generated assets. Its clean rebuild is required immediately before packaging in Task 8.

---

### Task 6: Remove Optional Dependency and Lockfile Entries

**Files:**

- Modify: `tests/main_product_boundary_test.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**

- Consumes: PEP 621 project metadata and the uv lockfile.
- Produces: no vector optional extra and no resolved `sqlite-vec` distribution.

- [ ] **Step 1: Append a failing dependency metadata test**

```python
from pathlib import Path
import tomllib


def test_project_metadata_has_no_vector_extra_or_sqlite_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    extras = metadata["project"]["optional-dependencies"]
    assert "vector" not in extras
    assert "sqlite-vec" not in (root / "uv.lock").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the dependency test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_project_metadata_has_no_vector_extra_or_sqlite_dependency -v
```

Expected: FAIL because `pyproject.toml` defines the extra and `uv.lock` resolves the package.

- [ ] **Step 3: Remove the extra and regenerate the lockfile**

Delete this entire table from `pyproject.toml`:

```toml
vector = [
    "sqlite-vec>=0.1.0",
]
```

The current workstation does not expose a global `uv` executable. Install uv as an uncommitted development tool in the existing virtual environment, then update the lock without changing project dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install "uv>=0.8,<1.0"
.\.venv\Scripts\uv.exe lock
```

Expected: lock resolution succeeds and removes the project extra marker, dependency reference, and `sqlite-vec` package record. Do not add uv to `pyproject.toml`.

- [ ] **Step 4: Verify dependency metadata**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py::test_project_metadata_has_no_vector_extra_or_sqlite_dependency -v
Select-String -Path pyproject.toml,uv.lock -Pattern 'sqlite-vec|sqlite_vec|extra == ''vector'''
```

Expected: pytest passes; `Select-String` returns no matches.

- [ ] **Step 5: Commit metadata cleanup**

```powershell
git add pyproject.toml uv.lock tests/main_product_boundary_test.py
git commit -m "build: remove vector memory dependency"
```

---

### Task 7: Align Product and Tracked Internal Documentation

**Files:**

- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `docs/memory.md`
- Delete: `docs/superpowers/plans/2026-07-28-memory-db-rename.md`
- Delete: `docs/superpowers/specs/2026-07-28-memory-db-rename-design.md`
- Modify: `docs/superpowers/specs/2026-07-28-comprehensive-code-review-remediation-design.md`

**Interfaces:**

- Consumes: the structured-only runtime delivered by Tasks 1-6.
- Produces: README, memory guide, and tracked internal guidance describe only behavior present on `main`.

- [ ] **Step 1: Run the documentation boundary scan and capture the expected failures**

Run:

```powershell
git grep -n -E 'agent/vector_memory|\[vector\]|sqlite-vec|vectorRecall|memory/memory\.db|memory\.db|embeddingModel|embeddingProvider|embeddingApiBase|embeddingApiKey' -- README.md README.en.md docs
git grep -n -E 'self.*recall|recall.*memory retrieval|自省.*recall' -- README.md README.en.md
```

Expected: matches appear in both READMEs, `docs/memory.md`, and the three tracked obsolete internal documents.

- [ ] **Step 2: Update both READMEs without weakening structured memory documentation**

In both module tables, make the memory row point only to `agent/memory.py`.

Change the built-in tool count from `25` to `24` and remove `recall` from the introspection/self tool row. Keep web retrieval tools unchanged.

Delete the optional vector-retrieval paragraph in each README. Keep and, where needed, clarify this structured data flow:

```text
session.messages
  -> Consolidator -> memory/history.jsonl
  -> Reflection / Dream
  -> SOUL.md / USER.md / memory/MEMORY.md
     episodic.jsonl / procedural.jsonl / shared memory / notes.md
  -> ContextBuilder
```

- [ ] **Step 3: Update `docs/memory.md` to match the retained files**

Remove the `memory.db` line from the workspace tree and its explanatory bullet. Add the retained structured files to the tree:

```text
    ├── episodic.jsonl
    ├── procedural.jsonl
    ├── reflections.jsonl
    ├── shared/
    │   ├── MEMORY_SHARED.md
    │   └── procedural_shared.jsonl
    ├── .cursor
    ├── .dream_cursor
    └── .git/
```

Preserve the existing explanations of Consolidator, Dream, `history.jsonl`, commands, GitStore inspection, and restoration.

- [ ] **Step 4: Delete dedicated guidance and surgically clean the comprehensive design**

Delete `docs/superpowers/plans/2026-07-28-memory-db-rename.md` and `docs/superpowers/specs/2026-07-28-memory-db-rename-design.md` with `apply_patch`. Their history remains available through Git. Do not delete ignored files that are not tracked by Git.

In `docs/superpowers/specs/2026-07-28-comprehensive-code-review-remediation-design.md`:

- delete the complete `Package A: Local Embedding` section;
- remove Package A from Program Structure, dependency allowances, testing strategy, execution order, rollback, and exclusions;
- remove cross-references that make backend or frontend packages depend on Package A;
- preserve the complete substantive content of Packages B–E, including their tests and guardrails;
- adjust only numbering and connective prose needed for the remaining document to read consistently.

The final file must contain no embedding/vector-memory guidance, but this cleanup must not discard unrelated backend, frontend settings, lint, accessibility, or bundle design decisions.

- [ ] **Step 5: Re-run documentation scans**

Run:

```powershell
git grep -n -E 'agent/vector_memory|\[vector\]|sqlite-vec|vectorRecall|memory/memory\.db|memory\.db|embeddingModel|embeddingProvider|embeddingApiBase|embeddingApiKey' -- README.md README.en.md docs ':!docs/superpowers/specs/2026-08-10-main-without-embedding-design.md' ':!docs/superpowers/plans/2026-08-10-main-without-embedding.md'
git diff --check
```

Expected: no product/internal-document matches outside the temporary approved design and this implementation plan; `git diff --check` reports no whitespace errors.

- [ ] **Step 6: Commit documentation cleanup**

```powershell
git add README.md README.en.md docs/memory.md docs/superpowers/plans/2026-07-28-memory-db-rename.md docs/superpowers/specs/2026-07-28-memory-db-rename-design.md docs/superpowers/specs/2026-07-28-comprehensive-code-review-remediation-design.md
git commit -m "docs: describe structured memory only"
```

---

### Task 8: Remove Temporary Artifacts and Verify the Final Tree and Release Packages

**Files:**

- Delete: `tests/main_product_boundary_test.py`
- Delete: `docs/superpowers/specs/2026-08-10-main-without-embedding-design.md`
- Delete: `docs/superpowers/plans/2026-08-10-main-without-embedding.md`
- Verify: all modified runtime, tests, docs, WebUI source, generated WebUI dist, wheel, and sdist

**Interfaces:**

- Consumes: all previous task outputs.
- Produces: a clean, test-verified `main` tracked tree and freshly built release packages with no embedding/vector product surface.

- [ ] **Step 1: Run the temporary boundary suite one last time**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/main_product_boundary_test.py tests/config/test_config_boundaries.py -q
```

Expected: all temporary product-boundary and permanent generic schema tests pass.

- [ ] **Step 2: Delete feature-specific temporary tests and decision documents**

Delete the temporary boundary test, approved design spec, and this implementation plan with `apply_patch`. Do not remove `tests/config/test_config_boundaries.py`; its tests are generic configuration invariants and contain no retired feature names.

- [ ] **Step 3: Run exact tracked-tree scans with narrow false-positive handling**

Run:

```powershell
git grep -n -E 'vector_recall|vectorRecall|vector_memory|sqlite-vec|sqlite_vec|memory\.db|embeddingModel|embeddingProvider|embeddingApiBase|embeddingApiKey|embedding_model|embedding_provider|embedding_api_base|embedding_api_key|text-embedding|embeddings\.create|def embed\(' -- .
Test-Path miniunicorn/agent/vector_memory.py
Test-Path miniunicorn/agent/tools/recall.py
Test-Path miniunicorn/providers/embedding.py
```

Expected: `git grep` returns no matches and all three `Test-Path` calls print `False`.

Then review broad-language matches separately:

```powershell
git grep -n -i -E 'embedding|vector|recall' -- .
```

Expected allowed matches only include unrelated language such as `max_position_embeddings`, “embedding static secrets,” image “vector style,” or ordinary natural-language recall. There must be no memory-vector implementation, configuration, dependency, tool, UI description, or support claim.

- [ ] **Step 4: Run focused structured-memory regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/config/test_config_boundaries.py tests/agent/test_memory_store.py tests/agent/test_dream.py tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py tests/agent/test_upgrade_integration.py tests/command/test_builtin_dream.py -q
```

Expected: all selected tests pass, including structured memory, Consolidator/Dream, Reflection, shared memory, notes, and GitStore behavior.

- [ ] **Step 5: Run the full Python and WebUI verification suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check miniunicorn tests
Push-Location webui
npm run lint
npm test
npm run build
Pop-Location
```

Expected: pytest reports zero failures, Ruff reports no errors, WebUI lint/tests pass, and the final Vite build succeeds.

- [ ] **Step 6: Build fresh wheel and sdist outside the repository**

```powershell
$artifactDir = Join-Path ([System.IO.Path]::GetTempPath()) ("miniunicorn-main-clean-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $artifactDir | Out-Null
.\.venv\Scripts\python.exe -m build --outdir $artifactDir
Get-ChildItem -LiteralPath $artifactDir
```

Expected: one `.whl` and one `.tar.gz` are built successfully. Building outside the repository prevents stale `dist/` contents from being mistaken for current artifacts.

- [ ] **Step 7: Inspect archive names, text, and dependency metadata**

Run this read-only archive inspection using the `$artifactDir` from Step 6:

```powershell
@'
from pathlib import Path
import sys
import tarfile
import zipfile

root = Path(sys.argv[1])
blocked_names = (
    "vector_memory.py",
    "/tools/recall.py",
    "/providers/embedding.py",
)
blocked_text = (
    "sqlite-vec",
    "vectorRecall",
    "embeddingModel",
    "embeddingProvider",
    "embeddingApiBase",
    "embeddingApiKey",
    "text-embedding-3-small",
)

def check(name: str, payload: bytes) -> None:
    normalized = name.replace("\\", "/")
    assert not any(token in normalized for token in blocked_names), normalized
    if normalized.endswith((".py", ".md", ".toml", ".json", "METADATA")):
        text = payload.decode("utf-8", errors="ignore")
        found = [token for token in blocked_text if token in text]
        assert not found, f"{normalized}: {found}"

for archive in root.iterdir():
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                check(member.filename, zf.read(member))
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    handle = tf.extractfile(member)
                    check(member.name, handle.read() if handle else b"")

print("release archives clean")
'@ | .\.venv\Scripts\python.exe - $artifactDir
```

Expected: prints `release archives clean` and exits with code 0. The rebuilt WebUI bundle is included by Hatch and therefore covered by the archive scan where strings are represented in JSON/metadata; additionally run the next direct dist scan:

```powershell
rg -n -i 'search past memories|搜索过去的记忆|vectorRecall|sqlite-vec|text-embedding' miniunicorn/web/dist
```

Expected: no matches.

- [ ] **Step 8: Verify enhancement branches were untouched**

Run:

```powershell
git rev-parse origin/codex/embedding-no-worker-latest
git rev-parse origin/codex/embedding-memory-production
git status --short --branch
```

Expected: the two hashes equal the preflight values recorded before Task 1; only the intended `main` changes are present before the final commit.

- [ ] **Step 9: Commit final temporary-artifact cleanup**

```powershell
git add -A -- tests/main_product_boundary_test.py docs/superpowers/specs/2026-08-10-main-without-embedding-design.md docs/superpowers/plans/2026-08-10-main-without-embedding.md
git commit -m "chore: finalize structured-memory main"
```

- [ ] **Step 10: Perform post-commit evidence checks**

Run:

```powershell
git status --short --branch
git log --oneline --decorate -10
git diff origin/main...HEAD --stat
```

Expected: working tree is clean, `main` is ahead only by the reviewed cleanup commits, and the diff contains no changes to either enhancement branch.
