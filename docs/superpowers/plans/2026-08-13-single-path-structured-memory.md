# Single-Path Structured Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the three runtime memory modes and make governed structured memory MiniUnicorn's only normal execution path while retaining explicit legacy import tooling.

**Architecture:** The existing repository, lifecycle, recall, journal, Dream extraction, and strict Reflection implementations remain. Mode plumbing and conditional branches are deleted; configuration only supplies tuning values, and legacy files become inert import sources.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, Ruff, append-only JSONL journal, filelock.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-13-single-path-structured-memory-design.md` exactly.
- Use test-first red/green cycles for every behavior change.
- Do not introduce embeddings, vector stores, or new dependencies.
- Do not weaken journal validation, checksums, file locking, fsync, identity checks, scope checks, or fail-closed behavior.
- Do not commit or push; the supervising agent owns integration.

---

### Task 1: Remove the public mode configuration

**Files:**
- Modify: `tests/config/test_structured_memory_config.py`
- Modify: `miniunicorn/config/schema.py`
- Modify: `docs/configuration.md`

**Interfaces:**
- Produces: `StructuredMemoryConfig` containing recall/lifecycle tuning only.

- [ ] Replace assertions that the default mode is `shadow` with `assert not hasattr(config, "mode")` and assert all existing tuning defaults.
- [ ] Add a parametrized test proving `StructuredMemoryConfig.model_validate({"mode": value})` raises `ValidationError` for `legacy`, `shadow`, and `governed`.
- [ ] Run `pytest tests/config/test_structured_memory_config.py -q`; verify the new tests fail because `mode` still exists or is accepted.
- [ ] Remove `mode: Literal[...]` and update the class docstring. Remove an unused `Literal` import only if no other schema uses it.
- [ ] Update configuration docs to show tuning fields without `mode` and state that governed memory is always enabled.
- [ ] Re-run the focused test and require all tests to pass.

### Task 2: Make the structured store unconditional

**Files:**
- Modify: `tests/agent/test_structured_memory_boundary.py`
- Modify: `tests/agent/test_memory_repository.py` or the closest existing store initialization test
- Modify: `miniunicorn/agent/memory.py`

**Interfaces:**
- `MemoryStore(workspace, structured_config=None)` normalizes to `StructuredMemoryConfig()`.
- `structured_repository`, `structured_lifecycle`, and `structured_recall` are initialized on construction.

- [ ] Add a test that constructs `MemoryStore(workspace)` and asserts the normalized config and all three structured components exist and repository health is healthy.
- [ ] Run that exact test and verify it fails under the current legacy deferral behavior.
- [ ] Normalize `None` to `StructuredMemoryConfig()` in `MemoryStore.__init__`, always create bundled structured files, and always build the stack.
- [ ] Remove lazy legacy construction and nullable fallbacks whose only purpose was mode-off operation; preserve degraded journal behavior.
- [ ] Replace conditional lock-timeout fallback with the normalized config value.
- [ ] Run the focused store/repository tests and require them to pass.

### Task 3: Make governed context injection unconditional

**Files:**
- Modify: `tests/agent/test_context_structured_memory.py`
- Modify: `miniunicorn/agent/context.py`

**Interfaces:**
- `ContextBuilder(..., structured_memory_config=None)` still uses normalized governed memory.
- `build_system_prompt(..., recall_query=...)` always recalls active structured facts outside light context.

- [ ] Convert governed-mode tests to construct the builder without a mode; keep active/candidate, exact-scope, default-user, subagent, budget, and degraded assertions.
- [ ] Replace shadow/legacy tests with a regression test that creates `USER.md`, `MEMORY.md`, and shared legacy memory, then proves none appear in the prompt while an active record does.
- [ ] Add or preserve a test that customized `POLICY.md` remains injected.
- [ ] Run the focused file and verify failures expose current legacy/shadow branches.
- [ ] Remove `_structured_mode` and `_audit_shadow_recall`; remove `governed`/`shadow` local flags.
- [ ] Always exclude `USER.md`, skip wholesale legacy memory blocks, and execute `_recall_section` whenever a recall query is present and context is not light.
- [ ] Keep recall auditing in `_recall_section`, rename governed-specific log/error wording to single-path structured-memory wording, and preserve content-free degraded diagnostics.
- [ ] Run `pytest tests/agent/test_context_structured_memory.py -q` and require all tests to pass.

### Task 4: Remove mode plumbing and make Reflection strict by construction

**Files:**
- Modify: `tests/agent/test_loop_structured_memory_mode.py` (rename to `test_loop_structured_memory.py` if useful)
- Modify: `tests/agent/test_runner_reflection.py`
- Modify: `tests/agent/test_reflection_structured.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/reflection.py`

**Interfaces:**
- `AgentRunSpec` has no `structured_memory_mode` field.
- `Reflection(provider, model, workspace)` always parses exact `{lesson}` JSON and emits stable IDs.

- [ ] Replace mode propagation tests with a test that captures `AgentRunSpec` and proves the obsolete attribute is absent.
- [ ] Update Reflection tests to omit `structured_mode=True` and prove free text/extra JSON keys are rejected while exact lesson JSON is persisted with `rfl_<32hex>`.
- [ ] Run these focused tests and verify failure against current optional behavior.
- [ ] Delete `AgentLoop.structured_memory_mode`, its assignment, and the `AgentRunSpec` field/constructor propagation.
- [ ] Remove the Reflection `structured_mode` argument and branches; always render the structured prompt, strictly parse, and write `reflection_id` plus `lesson`.
- [ ] Preserve cursor-safe rotation and durable append behavior unchanged.
- [ ] Run all three focused test files and require all tests to pass.

### Task 5: Make Dream structured-only

**Files:**
- Modify: `tests/agent/test_dream_structured_memory.py`
- Modify: any legacy Dream-path tests that now describe removed runtime behavior
- Modify: `miniunicorn/agent/memory.py`

**Interfaces:**
- `Dream.run()` always delegates to `_run_structured_batch()`.
- Dream tools never expose fact-file mutation.

- [ ] Add a test using a default `MemoryStore` that observes `Dream.run()` call the structured batch path without a mode.
- [ ] Add/preserve a test proving `EditFileTool` is not registered for Dream fact updates.
- [ ] Run the focused tests and verify they fail because the default still selects the legacy path.
- [ ] Remove `structured_mode` calculation, never register the fact-editing tool, and make `run()` call `_run_structured_batch()` directly.
- [ ] Delete dead legacy Dream consolidation code only when no remaining caller exists; retain shared helpers still used by migration or structured extraction.
- [ ] Run `pytest tests/agent/test_dream_structured_memory.py -q` plus all Dream tests and require all tests to pass.

### Task 6: Turn migration into optional import and remove startup gate

**Files:**
- Modify: `tests/agent/test_memory_migration.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/command/memory.py`

**Interfaces:**
- A loop starts regardless of migration-manifest presence.
- `/memory-migrate` behavior and durability contracts remain unchanged.
- `/memory-status` reports `governed` as a constant architecture label.

- [ ] Replace governed/shadow startup tests with tests proving both an empty workspace and a workspace containing unimported legacy files start successfully.
- [ ] Prove unimported legacy facts do not appear in context, then run explicit migration and prove resulting active eligible facts can be recalled according to lifecycle policy.
- [ ] Run startup/migration tests and verify the current migration gate fails them.
- [ ] Remove the migration-completion check from `AgentLoop.__init__`.
- [ ] Remove `allow_legacy` command-stack semantics if now redundant; structured commands always operate on the initialized stack.
- [ ] Change `/memory-status` mode output to a constant `governed` label (or `Architecture: governed`) while retaining import progress separately.
- [ ] Run the complete migration and memory-command test files, including two-process migration tests.

### Task 7: Remove residual mode surface and update documentation

**Files:**
- Modify: `docs/memory.md`
- Modify: `README.md` and `README.en.md` only if they reference the old modes
- Modify: all affected tests and comments found by the scan

**Interfaces:**
- Public documentation describes one memory architecture and an optional import command.

- [ ] Run `rg -n 'structured_memory_mode|structured memory mode|mode.*(legacy|shadow|governed)|legacy.*shadow.*governed|structuredMemory.*mode' miniunicorn tests docs README.md README.en.md` and classify every hit.
- [ ] Remove runtime/config/test references. Historical specs may remain, but current docs must not advertise modes.
- [ ] Update `docs/memory.md` diagrams, startup guidance, storage descriptions, diagnostics, and migration wording to match the design.
- [ ] Ensure examples show `structuredMemory` tuning without `mode`.
- [ ] Re-run the scan; require no live-code or current-documentation references to runtime modes, except rejection tests and historical archived specs.

### Task 8: Full verification and cleanup

**Files:**
- Modify only files necessary to fix regressions introduced by Tasks 1-7.

- [ ] Run `pytest tests/config/test_structured_memory_config.py tests/agent/test_context_structured_memory.py tests/agent/test_reflection_structured.py tests/agent/test_runner_reflection.py tests/agent/test_dream_structured_memory.py tests/agent/test_memory_migration.py tests/agent/test_memory_commands.py -q`.
- [ ] Run `pytest tests/agent tests/config -q` and record exact pass/fail totals. Reproduce and clearly separate any pre-existing Windows `printf` or external-network failures before deciding they are unrelated.
- [ ] Run `ruff check miniunicorn tests`.
- [ ] Run `git diff --check`.
- [ ] Run the residual mode scan from Task 7.
- [ ] Inspect `git diff --stat` and `git diff`; remove dead imports, misleading comments, and accidental unrelated changes.
- [ ] Do not commit, merge, push, or delete the worktree. Return a concise implementation report with commands, results, changed files, and any remaining failures.
