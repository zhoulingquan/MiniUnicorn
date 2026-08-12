# C2 Completion Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the merged C2 governed structured-memory implementation satisfy its original completion definition and survive cross-process and failure-path use.

**Architecture:** Preserve the append-only journal/lifecycle/lexical-recall design. Tighten repository validation and synchronization first, then repair lifecycle and migration semantics, propagate exact scopes, and finally integrate hygiene, audit, commands, documentation and hard boundary tests.

**Tech Stack:** Python 3.12, Pydantic, filelock, pytest, Ruff, GitStore, JSONL.

## Global Constraints

- Use TDD for every production behavior change: add one focused failing test, observe the expected failure, implement the minimum fix, and rerun it.
- Never rewrite or truncate `memory/structured/journal.jsonl`.
- Preserve legacy/shadow behavior and existing configuration aliases.
- Do not add vector, embedding, vector-store or knowledge-graph dependencies or runtime entry points.
- Keep all errors typed through `miniunicorn.agent.memory_models.MemoryError`.

---

### Task 1: Canonical paths and durable repository invariants

**Files:**
- Modify: `miniunicorn/agent/memory.py`
- Modify: `miniunicorn/agent/memory_repository.py`
- Modify: `miniunicorn/agent/memory_models.py`
- Modify: `tests/agent/test_memory_store.py`
- Modify: `tests/agent/test_memory_repository.py`

**Interfaces:**
- Consumes: `validate_same_status_revision(previous, current)` and `MemoryTransaction`.
- Produces: lock-local journal synchronization, transaction-local validation, active uniqueness and degraded-on-uncertain-write semantics.

- [ ] Add failing tests for canonical lowercase tags on case-sensitive paths, same-status active mutation, duplicate active conflict keys, two repository instances, and fsync failure health.
- [ ] Run only those tests and confirm each fails for the audited reason.
- [ ] Implement canonical path compatibility and repository synchronization/validation without publishing partial state.
- [ ] Run repository/store tests and Ruff on changed files.
- [ ] Commit the independently passing task.

### Task 2: Lifecycle typed errors and retry idempotency

**Files:**
- Modify: `miniunicorn/agent/memory_lifecycle.py`
- Modify: `tests/agent/test_memory_lifecycle.py`
- Modify: `tests/agent/test_dream_structured_memory.py`

**Interfaces:**
- Consumes: repository candidate/source and content lookup indexes.
- Produces: retry-safe `ingest()` for candidate-create/promotion split failures and active completion lookup.

- [ ] Add failing tests for project `MemoryError` inheritance, promotion-failure retry and already-active batch retry.
- [ ] Run the tests and verify their exact failures.
- [ ] Implement typed inheritance, resumable candidate processing and active idempotency; remove illegal active `status_reason` changes.
- [ ] Run lifecycle and Dream structured tests.
- [ ] Commit the independently passing task.

### Task 3: Evidence and stable Dream provenance

**Files:**
- Modify: `miniunicorn/agent/memory_lifecycle.py`
- Modify: `miniunicorn/agent/memory.py`
- Modify: `tests/agent/test_memory_lifecycle.py`
- Modify: `tests/agent/test_dream_structured_memory.py`

**Interfaces:**
- Consumes: `EvidenceRef.ref`, workspace root and history `_line` cursor.
- Produces: file-source evidence verification and globally stable Dream refs.

- [ ] Add failing tests that reject a mismatched referenced file and assert history refs use persisted line numbers across batches.
- [ ] Verify failures.
- [ ] Add workspace-aware verification and stable reference construction.
- [ ] Run affected tests and commit.

### Task 4: Crash-safe, lossless migration

**Files:**
- Modify: `miniunicorn/agent/memory_migration.py`
- Modify: `miniunicorn/agent/memory.py`
- Modify: `miniunicorn/command/memory.py`
- Modify: `tests/agent/test_memory_migration.py`
- Modify: `tests/agent/test_memory_commands.py`

**Interfaces:**
- Produces: canonical `memory/structured/migration-v1.json`, legacy-manifest fallback, per-item durable progress, truthful completion, collision-resistant slots.

- [ ] Add failing tests for canonical path, legacy fallback, fsync, per-item crash recovery, failed/issues completion gate, distinct slots and non-fabricated procedural evidence.
- [ ] Verify failures.
- [ ] Implement atomic durable manifest saves, compatibility load, truthful completion and deterministic slots/evidence.
- [ ] Run migration/command tests and commit.

### Task 5: Exact session and user scopes

**Files:**
- Modify: `miniunicorn/agent/context.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/memory.py`
- Modify: `miniunicorn/agent/memory_extraction.py`
- Modify: `tests/agent/test_context_structured_memory.py`
- Modify: `tests/agent/test_dream_structured_memory.py`
- Modify: `tests/agent/test_memory_extraction.py`

**Interfaces:**
- Produces: `build_messages(..., session_key, memory_user_key)` and four exact recall scopes; Dream scope-hint mapping to supplied scopes.

- [ ] Add failing tests for normal session/user recall, default user, and subagent parent-scope inheritance.
- [ ] Verify failures.
- [ ] Thread effective scope identity through both loop call sites and Dream ingestion.
- [ ] Run context, loop and Dream tests; commit.

### Task 6: Recall correctness and degraded diagnostics

**Files:**
- Modify: `miniunicorn/agent/memory_recall.py`
- Modify: `miniunicorn/agent/context.py`
- Modify: `tests/agent/test_memory_recall.py`
- Modify: `tests/agent/test_context_structured_memory.py`

**Interfaces:**
- Produces: cap-consistent Why reasons, section-aware token budgeting and governed degraded diagnostic rendering.

- [ ] Add failing tests for capped multi-tag/alias reasons, exact rendered-section budget and degraded prompt diagnosis.
- [ ] Verify failures, implement minimal corrections, run affected tests and commit.

### Task 7: Structured hygiene and redacted recall audit

**Files:**
- Modify: `miniunicorn/agent/memory.py`
- Modify: `miniunicorn/agent/context.py`
- Modify: `tests/agent/test_memory_store.py`
- Create: `tests/agent/test_structured_memory_boundary.py`

**Interfaces:**
- Produces: `run_memory_hygiene(now=None)`, `write_recall_audit(query, result)`, 1000-line rotation and no-Git audit file.

- [ ] Add failing expiry, audit redaction/rotation and governed-audit tests.
- [ ] Verify failures.
- [ ] Implement expiry integration and canonical redacted audit writes.
- [ ] Run store/context/boundary tests and commit.

### Task 8: Commands, docs and hard boundaries

**Files:**
- Modify: `miniunicorn/command/memory.py`
- Modify: `tests/agent/test_memory_commands.py`
- Modify: `docs/configuration.md`
- Modify: `docs/memory.md`
- Modify: `docs/chat-commands.md`
- Modify: `miniunicorn/skills/memory/SKILL.md`
- Modify: `tests/agent/test_structured_memory_boundary.py`

**Interfaces:**
- Produces: non-empty correction parsing, message-ID evidence, current documentation and repository-wide no-vector boundary enforcement.

- [ ] Add failing command and boundary assertions.
- [ ] Verify failures and implement command changes.
- [ ] Update all user/agent documentation to match runtime behavior.
- [ ] Run command/boundary tests, Ruff and commit.

### Task 9: Verification and review

**Files:**
- Modify only files required by verified failures.

- [ ] Run `ruff check miniunicorn tests` and resolve only findings in scope.
- [ ] Run the complete focused C2 test suite including `test_structured_memory_boundary.py`.
- [ ] Run `pytest -q`; if it exceeds the limit, identify the hanging file/test with per-directory and per-file runs, correct in-scope regressions, then rerun.
- [ ] Run `git diff --check` and `git status --short`.
- [ ] Review the complete diff against the remediation design and original C2 completion definition.
- [ ] Commit final verification-only corrections and report exact evidence.

