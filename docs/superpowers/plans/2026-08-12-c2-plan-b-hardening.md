# C2 Plan B Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make C2 retry-safe and concurrency-safe while preserving stable evidence provenance, exact user/session scope, crash-safe migration, and the no-vector boundary.

**Architecture:** Keep `journal.jsonl` as the only structured-memory source of truth. Rebuild immutable creation-idempotency and cumulative provenance indexes from journal transactions, perform conditional candidate creation under the repository lock, assign evidence IDs in application code, and serialize migration manifest updates under a separate file lock.

**Tech Stack:** Python 3.11+, Pydantic v2, `filelock`, JSONL, pytest/pytest-asyncio, Ruff, PowerShell-compatible commands.

## Global Constraints

- Normative design: `docs/superpowers/specs/2026-08-12-c2-plan-b-hardening-design.md`.
- Design baseline commit: `c87dad75` or a descendant containing that design unchanged.
- Do not change `SCHEMA_VERSION`; existing journals must replay without migration.
- Do not create a second persistent idempotency ledger or database.
- Do not add embedding, vector-search, vector-database, graph-memory dependencies, imports, config, or extension points.
- Preserve fail-closed behavior: candidates are never recalled; invalid evidence/scope never advances Dream cursors.
- Use `apply_patch` for source edits and preserve unrelated user changes.
- Follow TDD for every behavior: run the named test before implementation and record the expected failure, then implement and rerun it.
- After every task, run Ruff on touched source/tests before committing.
- Do not merge or push unless the user separately authorizes it.

## File Responsibility Map

| File | Responsibility in this plan |
|---|---|
| `miniunicorn/agent/memory_repository.py` | Rebuild cumulative provenance, enforce journal idempotency uniqueness, provide locked conditional creation |
| `miniunicorn/agent/memory_lifecycle.py` | Consume conditional creation result and resume deterministic lifecycle work |
| `miniunicorn/agent/memory_models.py` | Enforce monotonic candidate conflict metadata |
| `miniunicorn/agent/reflection.py` | Assign stable reflection IDs in application code and persist complete entries |
| `miniunicorn/agent/memory.py` | Normalize legacy reflection IDs, construct Dream evidence/prompt/batch IDs/scopes, share migration state helper |
| `miniunicorn/templates/agent/reflection_system.md` | Ask only for a lesson, never an ID |
| `miniunicorn/templates/agent/dream_phase1.md` | Describe visible evidence refs and dynamically allowed scopes |
| `miniunicorn/agent/memory_migration.py` | Lock manifest RMW, atomically save with unique temp, load canonical/legacy state consistently |
| `miniunicorn/command/memory.py` | Use compatible migration status and return usage for malformed shell syntax |
| `tests/agent/*` | Regression, concurrency, prompt-contract, recovery and boundary coverage |
| `docs/configuration.md`, `docs/memory.md` | Document exact audit and hardening semantics |

---

### Task 1: Preserve creation idempotency and cumulative source provenance on replay

**Files:**
- Modify: `miniunicorn/agent/memory_models.py`
- Modify: `miniunicorn/agent/memory_repository.py:64-334`
- Test: `tests/agent/test_memory_repository.py`

**Interfaces:**
- Consumes: `MemoryTransaction.source_batch`, `MemoryRecord.revision`, `MemoryRecord.content_hash`.
- Produces: `record_created_for(source_batch: str, content_hash: str) -> MemoryRecord | None` and cumulative `record_ids_for_source(source_batch: str) -> frozenset[str]`.
- Invariant: a non-empty `(source_batch, content_hash)` may identify only one revision-1 record in a healthy journal.

- [ ] **Step 1: Add failing provenance tests**

Add tests that create a candidate in batch A, publish a later revision with `source_batch=""`, and assert both the current and rebuilt Repository still resolve batch A:

```python
def test_creation_batch_survives_later_revision_and_rebuild(repository):
    created = MemoryRecord.model_validate(record_data())
    repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))
    promoted = created.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.ACTIVE,
            "updated_at": dt("2026-08-11T08:32:00Z"),
        }
    )
    repository.append_transaction(
        make_transaction(promoted, expected_revisions={created.id: 1}, source_batch="")
    )

    assert repository.record_created_for("dream:batch-a", created.content_hash).id == created.id
    assert repository.record_ids_for_source("dream:batch-a") == frozenset({created.id})

    rebuilt = StructuredMemoryRepository(repository.workspace)
    assert rebuilt.record_created_for("dream:batch-a", created.content_hash).id == created.id
    assert rebuilt.record_ids_for_source("dream:batch-a") == frozenset({created.id})
```

Add a second test proving a later batch is accumulated without replacing batch A:

```python
def test_record_source_batches_are_cumulative(repository):
    created = MemoryRecord.model_validate(record_data())
    repository.append_transaction(make_transaction(created, source_batch="dream:batch-a"))
    revised = created.model_copy(
        update={"revision": 2, "blocked_by": ("mem_" + "a" * 32,), "updated_at": dt("2026-08-11T08:32:00Z")}
    )
    repository.append_transaction(
        make_transaction(revised, expected_revisions={created.id: 1}, source_batch="dream:batch-b")
    )
    assert repository.record_ids_for_source("dream:batch-a") == frozenset({created.id})
    assert repository.record_ids_for_source("dream:batch-b") == frozenset({created.id})
```

- [ ] **Step 2: Run the tests and capture the old failure**

Run:

```powershell
python -m pytest -q tests/agent/test_memory_repository.py::test_creation_batch_survives_later_revision_and_rebuild tests/agent/test_memory_repository.py::test_record_source_batches_are_cumulative
```

Expected before implementation: the first assertion finds no record for batch A, and the cumulative assertion loses batch A after revision 2.

- [ ] **Step 3: Split current-record unindexing from immutable provenance publication**

In `_clear_index()` initialize:

```python
self._created_by_source_content: dict[tuple[str, str], str] = {}
self._source_batches_by_record: dict[str, set[str]] = defaultdict(set)
```

Keep `_unindex()` limited to current-state indexes. Remove its loop that discards IDs from `_records_by_source`. Replace the old source indexing in `_index()` with a new method called from `_publish_transaction()`:

```python
def _publish_source_provenance(self, record: MemoryRecord, source_batch: str) -> None:
    if not source_batch:
        return
    self._source_batches_by_record[record.id].add(source_batch)
    if record.revision != 1:
        return
    key = (source_batch, record.content_hash)
    existing_id = self._created_by_source_content.get(key)
    if existing_id is not None and existing_id != record.id:
        raise DuplicateMemoryIdempotencyKey(
            "duplicate idempotency key "
            f"source_batch={source_batch} content_hash={record.content_hash}: "
            f"{existing_id}, {record.id}"
        )
    self._created_by_source_content[key] = record.id
```

Call it after the transaction has passed validation and before/with normal publication. Implement accessors:

```python
def record_created_for(self, source_batch: str, content_hash: str) -> MemoryRecord | None:
    memory_id = self._created_by_source_content.get((source_batch, content_hash))
    return self._current.get(memory_id) if memory_id else None

def record_ids_for_source(self, source_batch: str) -> frozenset[str]:
    return frozenset(
        memory_id
        for memory_id, batches in self._source_batches_by_record.items()
        if source_batch in batches
    )
```

Do not use `_source_batches_by_record` for Lifecycle idempotency; only revision-1 creation mapping is authoritative.

- [ ] **Step 4: Make duplicate creation keys degrade replay deterministically**

Add the typed model error next to the other repository transition errors:

```python
class DuplicateMemoryIdempotencyKey(InvalidMemoryTransition):
    """Two record IDs claim the same source-batch/content creation key."""
```

Validate creation-key uniqueness in `_validate_against_current()` using a projected copy of `_created_by_source_content`, including multiple operations in the same transaction. Raise `DuplicateMemoryIdempotencyKey`. In `_replay_line()`, catch that exact subclass before `InvalidMemoryTransition` and set health code `duplicate_idempotency_key`; do not inspect exception text.

Add a test that manually writes two valid transactions with different IDs and the same batch/hash, rebuilds, and asserts:

```python
assert rebuilt.health.state == "degraded"
assert rebuilt.health.error_code == "duplicate_idempotency_key"
assert rebuilt.current_records() == ()
```

- [ ] **Step 5: Run Repository tests and lint**

```powershell
python -m pytest -q tests/agent/test_memory_repository.py
python -m ruff check miniunicorn/agent/memory_models.py miniunicorn/agent/memory_repository.py tests/agent/test_memory_repository.py
```

Expected: all Repository tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add miniunicorn/agent/memory_models.py miniunicorn/agent/memory_repository.py tests/agent/test_memory_repository.py
git commit -m "fix(memory): preserve source creation provenance"
```

---

### Task 2: Add locked conditional creation to the Repository

**Files:**
- Modify: `miniunicorn/agent/memory_repository.py:166-252`
- Test: `tests/agent/test_memory_repository.py`

**Interfaces:**
- Consumes: the Task 1 `record_created_for()` index.
- Produces: `append_create_if_absent(transaction: MemoryTransaction) -> tuple[MemoryRecord, bool]`.
- Guarantees: synchronization, lookup and append occur under the same `journal.lock` acquisition.

- [ ] **Step 1: Add a stale-instance regression test**

Construct two Repository instances before either writes. Build two revision-1 records with the same batch/content hash and distinct IDs. Call conditional create through instance A then B:

```python
def test_append_create_if_absent_deduplicates_stale_repository_instances(workspace):
    first_repo = StructuredMemoryRepository(workspace)
    second_repo = StructuredMemoryRepository(workspace)
    first = MemoryRecord.model_validate(record_data(memory_id=new_memory_id()))
    second = MemoryRecord.model_validate(
        record_data(memory_id=new_memory_id(), content_hash=first.content_hash)
    )

    first_current, first_created = first_repo.append_create_if_absent(
        make_transaction(first, source_batch="dream:stable-batch")
    )
    second_current, second_created = second_repo.append_create_if_absent(
        make_transaction(second, source_batch="dream:stable-batch")
    )

    assert first_created is True
    assert second_created is False
    assert second_current.id == first_current.id
    assert len(StructuredMemoryRepository(workspace).current_records()) == 1
```

- [ ] **Step 2: Add contract rejection tests**

Parameterize transactions with empty batch, two operations, revision 2, or nonzero expected creation revision. Each must raise `MemoryWriteError` before appending and leave journal unchanged.

- [ ] **Step 3: Run new tests and verify they fail**

```powershell
python -m pytest -q tests/agent/test_memory_repository.py -k "create_if_absent"
```

Expected before implementation: `AttributeError` because `append_create_if_absent` does not exist.

- [ ] **Step 4: Factor the locked append primitive**

Extract the body shared by ordinary append and conditional create without nesting `FileLock` acquisitions:

```python
def _append_validated_locked(self, transaction: MemoryTransaction) -> None:
    validated = self._validate_against_current(transaction)
    line = self._canonical_transaction_line(validated)
    try:
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        self._degrade(
            self._health.last_valid_line or 0,
            "write_uncertain",
            f"journal durability is uncertain: {exc}",
        )
        raise MemoryWriteError(str(exc)) from exc
    self._publish_transaction(validated)
```

Both public methods must translate `FileLockTimeout` to `MemoryLockTimeout` and preserve existing typed errors.

- [ ] **Step 5: Implement conditional creation under one lock**

Use a dedicated validator for the four contract rules. The public method structure must be:

```python
def append_create_if_absent(
    self, transaction: MemoryTransaction
) -> tuple[MemoryRecord, bool]:
    self._validate_create_transaction_shape(transaction)
    record = transaction.operations[0].record
    try:
        with FileLock(str(self.lock_path), timeout=self.lock_timeout_s):
            self._require_healthy()
            self._synchronize_locked()
            existing = self.record_created_for(transaction.source_batch, record.content_hash)
            if existing is not None:
                return existing, False
            self._append_validated_locked(transaction)
            return record, True
    except FileLockTimeout as exc:
        raise MemoryLockTimeout(
            f"journal lock timeout after {self.lock_timeout_s}s"
        ) from exc
```

Do not perform the existing lookup before acquiring the lock.

- [ ] **Step 6: Add a real multiprocess smoke test**

Define a module-level worker so Windows `spawn` can import it. Use `multiprocessing.get_context("spawn")`, one start event, one result queue, and two processes. Each worker constructs its own Repository and calls conditional create with the same batch/hash but its own ID. Assert both exit codes are zero, exactly one returned `created=True`, both returned the same record ID, and rebuilt journal contains one record.

Use a 10-second join timeout and terminate only a still-alive test child in test cleanup to prevent a hung test suite.

- [ ] **Step 7: Run tests and commit**

```powershell
python -m pytest -q tests/agent/test_memory_repository.py
python -m ruff check miniunicorn/agent/memory_repository.py tests/agent/test_memory_repository.py
git add miniunicorn/agent/memory_repository.py tests/agent/test_memory_repository.py
git commit -m "fix(memory): make candidate creation atomic"
```

---

### Task 3: Route Lifecycle ingestion through atomic conditional creation

**Files:**
- Modify: `miniunicorn/agent/memory_lifecycle.py:144-204,261-266`
- Test: `tests/agent/test_memory_lifecycle.py`
- Test: `tests/agent/test_dream_structured_memory.py`

**Interfaces:**
- Consumes: `StructuredMemoryRepository.append_create_if_absent()`.
- Produces: retry-safe `ingest()` with no lock-external deduplication lookup.

- [ ] **Step 1: Add manual-promotion retry regression**

```python
def test_retry_original_batch_after_manual_promotion_returns_same_active(
    lifecycle, inferred_proposal, context
):
    first = lifecycle.ingest(inferred_proposal, context)
    lifecycle.promote(first.candidate_id, actor=ActorKind.USER, reason="approve")

    retry = lifecycle.ingest(inferred_proposal, context)

    assert retry.candidate_id == first.candidate_id
    assert retry.active_id == first.candidate_id
    assert retry.final_status is MemoryStatus.ACTIVE
    assert retry.reason_code == REASON_EXISTING
    assert retry.transaction_ids == ()
    assert len(lifecycle.repository.current_records()) == 1
```

- [ ] **Step 2: Add stale-Lifecycle instance regression**

Create two Repository/Lifecycle objects before ingestion. Ingest the same inferred proposal/context through both and assert the same ID, one record, and no extra revision on the second call.

- [ ] **Step 3: Add identical-merge retry regression**

Create an active identical fact from another batch, ingest the proposal from batch B so it is merged/superseded, then retry batch B. Assert record count and revision counts do not change and the result deterministically references the already-created terminal record plus its active replacement.

- [ ] **Step 4: Run the three regressions before implementation**

```powershell
python -m pytest -q tests/agent/test_memory_lifecycle.py -k "manual_promotion or stale_lifecycle or identical_merge_retry"
```

Expected: duplicate IDs/records or missing deterministic active replacement.

- [ ] **Step 5: Replace lock-external lookup in `ingest()`**

Remove `_find_existing_record()`. Build the proposal record and create transaction as today, then:

```python
current, created = self.repository.append_create_if_absent(create_tx)
if not created:
    return self._resume_existing(current, context, now)
return self._apply_after_ingest(record, context, now, create_tx.tx_id)
```

Implement a focused helper:

```python
def _resume_existing(
    self, record: MemoryRecord, context: IngestContext, now: datetime
) -> IngestResult:
    if record.status is MemoryStatus.CANDIDATE:
        return self._apply_after_ingest(record, context, now, "")
    active_id = record.id if record.status is MemoryStatus.ACTIVE else None
    if record.status is MemoryStatus.SUPERSEDED and record.replacement_id:
        replacement = self.repository.get(record.replacement_id)
        if replacement is not None and replacement.status is MemoryStatus.ACTIVE:
            active_id = replacement.id
    return IngestResult(
        candidate_id=record.id,
        final_status=record.status,
        active_id=active_id,
        transaction_ids=(),
        reason_code=REASON_EXISTING,
    )
```

Do not choose among cumulative source records or iterate a set.

- [ ] **Step 6: Verify lifecycle/Dream tests and commit**

```powershell
python -m pytest -q tests/agent/test_memory_lifecycle.py tests/agent/test_dream_structured_memory.py
python -m ruff check miniunicorn/agent/memory_lifecycle.py tests/agent/test_memory_lifecycle.py tests/agent/test_dream_structured_memory.py
git add miniunicorn/agent/memory_lifecycle.py tests/agent/test_memory_lifecycle.py tests/agent/test_dream_structured_memory.py
git commit -m "fix(memory): resume lifecycle ingestion idempotently"
```

---

### Task 4: Assign stable Reflection IDs in application code

**Files:**
- Modify: `miniunicorn/agent/reflection.py:40-219`
- Modify: `miniunicorn/templates/agent/reflection_system.md:1-20`
- Modify: `miniunicorn/agent/memory.py:631-700`
- Create: `tests/agent/test_reflection_structured.py`
- Test: `tests/agent/test_memory_store.py`

**Interfaces:**
- Produces: `new_reflection_id() -> str`, `reflection_evidence_id(entry: Mapping[str, Any]) -> str`.
- ID format: new `rfl_[0-9a-f]{32}`; legacy fallback `rfl_legacy_[0-9a-f]{24}`.

- [ ] **Step 1: Add structured Reflection output-contract test**

Mock Provider response as `{"lesson":"Verify exact evidence IDs."}`. Assert the persisted JSONL entry contains a program-generated ID matching `rfl_[0-9a-f]{32}`, and that the system prompt does not ask the model for `reflection_id` or line numbers.

- [ ] **Step 2: Add prune-and-append uniqueness test**

Generate one reflection, mark it consumed, prune, generate another, and assert the two persisted/returned IDs differ even though the second entry again occupies physical line 1.

- [ ] **Step 3: Add deterministic legacy fallback test**

For a legacy reflection dict without a valid new ID, call `reflection_evidence_id()` twice and after JSONL prune/reload. Assert the same `rfl_legacy_<24 hex>` is returned. Change the lesson and assert the ID changes.

- [ ] **Step 4: Run tests and verify old behavior fails**

```powershell
python -m pytest -q tests/agent/test_reflection_structured.py tests/agent/test_memory_store.py -k "reflection_id or reflection_evidence"
```

Expected: the current template requires `R<line>`, and IDs repeat after pruning.

- [ ] **Step 5: Implement application-owned IDs**

At module level in `reflection.py`:

```python
_REFLECTION_ID_RE = re.compile(r"^rfl_[0-9a-f]{32}$")

def new_reflection_id() -> str:
    return f"rfl_{uuid.uuid4().hex}"
```

Change structured parsing to validate only exact `{"lesson": <non-empty string>}`. Generate the ID after parsing and store it in the entry. `_append_reflection()` must return success/failure or raise a typed local error so `reflect()` never reports success when persistence failed.

In `memory.py`, add:

```python
def reflection_evidence_id(entry: Mapping[str, Any]) -> str:
    raw = str(entry.get("reflection_id") or "")
    if re.fullmatch(r"rfl_[0-9a-f]{32}", raw):
        return raw
    canonical = json.dumps(
        {key: value for key, value in entry.items() if key != "_line"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"rfl_legacy_{digest}"
```

Do not use `_line` in the digest.

- [ ] **Step 6: Replace the template contract**

The structured branch must show exactly:

```text
{"lesson":"When a source identifier is required, copy the exact identifier shown in the input."}
```

State that the application assigns the stable ID and forbid extra keys.

- [ ] **Step 7: Run Reflection/store tests and commit**

```powershell
python -m pytest -q tests/agent/test_reflection_structured.py tests/agent/test_memory_store.py
python -m ruff check miniunicorn/agent/reflection.py miniunicorn/agent/memory.py tests/agent/test_reflection_structured.py tests/agent/test_memory_store.py
git add miniunicorn/agent/reflection.py miniunicorn/agent/memory.py miniunicorn/templates/agent/reflection_system.md tests/agent/test_reflection_structured.py tests/agent/test_memory_store.py
git commit -m "fix(memory): assign stable reflection evidence ids"
```

---

### Task 5: Make the Dream prompt and parser share exact evidence and scope contracts

**Files:**
- Modify: `miniunicorn/agent/memory.py:2203-2334`
- Modify: `miniunicorn/templates/agent/dream_phase1.md:1-43`
- Test: `tests/agent/test_dream_structured_memory.py`
- Test: `tests/agent/test_memory_extraction.py`

**Interfaces:**
- Consumes: Task 4 `reflection_evidence_id()`.
- Produces: `_dream_source_batch(evidence_refs: Iterable[str]) -> str` and one shared `scope_by_hint` used for both prompt rendering and parser validation.

- [ ] **Step 1: Strengthen the persisted-history cursor test**

After cursor 1 has been consumed and cursor 2 is pending, capture Provider call messages and assert user prompt contains `[history:2 |`, does not relabel it as `history:1`, and a response citing `history:2` succeeds.

- [ ] **Step 2: Add exact reflection-ref prompt test**

Write a reflection with `reflection_id="rfl_0123456789abcdef0123456789abcdef"`; assert the prompt displays `[reflection:rfl_0123456789abcdef0123456789abcdef | ...]` and the resulting record carries that exact evidence ref.

- [ ] **Step 3: Add stable Dream source-batch tests**

Call the helper with identical refs in different iteration order and assert the same `dream:<24 hex>` value. Add/remove one ref and assert a different value. In an ingestion retry, assert both transactions use the stable batch and no duplicate record appears.

- [ ] **Step 4: Add dynamic-scope prompt tests**

For one history entry with exact session/user identity, assert system prompt lists `project, shared, session, user`. For mixed users or an entry missing identity, assert `user` is absent. For mixed sessions, assert `session` is absent. Include a reflection-only batch without `user_key` and assert user scope is absent.

- [ ] **Step 5: Run the prompt-contract tests before implementation**

```powershell
python -m pytest -q tests/agent/test_dream_structured_memory.py -k "prompt or source_batch or identity_scope"
```

Expected: visible refs are missing, template forbids identity scopes, or cursor-based batch IDs differ from the required evidence hash.

- [ ] **Step 6: Build evidence lines with exact refs**

Render history and reflection lines as:

```python
history_lines.append(
    f"[{ref} | {entry.get('timestamp', '')}] "
    f"{truncate_text(content, self._HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
)
reflection_lines.append(
    f"[reflection:{reflection_id} | {entry.get('timestamp', '')}] "
    f"({entry.get('trigger', 'unknown')}) {content}"
)
```

Use the same strings as keys in `evidence_catalog`.

- [ ] **Step 7: Compute scope before the Provider call and render it dynamically**

Move `scope_by_hint` construction above `render_template()`. Derive identity from every evidence-bearing input, not history alone. Pass:

```python
allowed_scope_hints=", ".join(kind.value for kind in scope_by_hint)
```

to the template. Replace the hard-coded prohibition with:

```text
- Allowed scope_hint values for this batch: {{ allowed_scope_hints }}.
- Choose the narrowest accurate allowed scope. Never output a value absent from this list.
```

Continue passing `set(scope_by_hint)` into `parse_extraction_batch()`.

- [ ] **Step 8: Use an evidence-set-derived batch ID**

```python
def _dream_source_batch(evidence_refs: Iterable[str]) -> str:
    canonical = "\n".join(sorted(set(evidence_refs)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"dream:{digest}"
```

Call it with `evidence_catalog.keys()`. The no-input early return means canonical is never empty during a Dream run.

- [ ] **Step 9: Run Dream/extraction tests and commit**

```powershell
python -m pytest -q tests/agent/test_dream_structured_memory.py tests/agent/test_memory_extraction.py
python -m ruff check miniunicorn/agent/memory.py miniunicorn/agent/memory_extraction.py tests/agent/test_dream_structured_memory.py tests/agent/test_memory_extraction.py
git add miniunicorn/agent/memory.py miniunicorn/templates/agent/dream_phase1.md tests/agent/test_dream_structured_memory.py tests/agent/test_memory_extraction.py
git commit -m "fix(memory): align Dream evidence and scope contracts"
```

---

### Task 6: Serialize and durably save migration state

**Files:**
- Modify: `miniunicorn/agent/memory_migration.py:350-518`
- Test: `tests/agent/test_memory_migration.py`

**Interfaces:**
- Produces: `load_migration_state(workspace: Path) -> MigrationState`.
- New runtime file: `memory/structured/migration-v1.lock`.

- [ ] **Step 1: Add canonical/legacy helper tests**

Cover four cases:

1. neither file exists → empty state;
2. only completed legacy file exists → completed state;
3. both exist → canonical wins;
4. canonical exists but is corrupt while legacy says completed → canonical returns incomplete/fail-closed and does not fall back.

- [ ] **Step 2: Add migration lock contention test**

Acquire `migration-v1.lock` manually, construct `MemoryMigration` with a short timeout, call `apply()`, and assert typed `MemoryLockTimeout`. Assert journal and canonical manifest remain unchanged.

- [ ] **Step 3: Add unique-temp and directory-fsync tests**

Monkeypatch the temp-name generator or inspect paths opened during two saves. Assert paths differ, both are siblings of the manifest, `os.replace` receives the current save's temp, file fsync occurs before replace, and supported directory fsync occurs after replace.

- [ ] **Step 4: Run new migration tests before implementation**

```powershell
python -m pytest -q tests/agent/test_memory_migration.py -k "load_migration_state or lock_contention or unique_temp or directory_fsync"
```

Expected: helper/lock are absent and the current fixed `.tmp` path is reused.

- [ ] **Step 5: Implement the shared loader**

```python
def load_migration_state(workspace: Path) -> MigrationState:
    canonical = Path(workspace) / MIGRATION_STATE_FILE
    legacy = Path(workspace) / LEGACY_MIGRATION_STATE_FILE
    if canonical.exists():
        return MigrationState.load(canonical)
    return MigrationState.load(legacy)
```

Keep corrupt canonical fail-closed through `MigrationState.load()` returning incomplete.

- [ ] **Step 6: Implement unique atomic save**

Use `tempfile.NamedTemporaryFile` with `delete=False`, `dir=path.parent`, and prefix based on `path.name`. Close it only after JSON write, flush and file fsync. Replace the target, then on POSIX open `path.parent` with `os.open(path.parent, os.O_RDONLY)` and fsync/close it. Guard directory fsync for unsupported platforms without suppressing file write/replace failures. Finally unlink only this invocation's remaining temp.

- [ ] **Step 7: Lock the complete apply RMW cycle**

Add `lock_timeout_s` to `MemoryMigration.__init__`, defaulting to repository timeout when present and otherwise 5 seconds. Structure `apply()` as:

```python
try:
    with FileLock(str(self.lock_path), timeout=self.lock_timeout_s):
        return self._apply_locked()
except FileLockTimeout as exc:
    raise MemoryLockTimeout(
        f"migration lock timeout after {self.lock_timeout_s}s"
    ) from exc
```

Move scan, state load, item loop, per-item state save and final completion save into `_apply_locked()` so no state decision occurs outside the lock.

- [ ] **Step 8: Add two-process migration verification**

Use spawn-safe workers that each build their own Repository/Lifecycle/Migration and wait on a shared start event. After both exit, assert one canonical manifest contains every scanned legacy key, the journal has one creation per legacy key, and both reports are successful or one cleanly reports all items skipped.

- [ ] **Step 9: Run migration/repository tests and commit**

```powershell
python -m pytest -q tests/agent/test_memory_migration.py tests/agent/test_memory_repository.py
python -m ruff check miniunicorn/agent/memory_migration.py tests/agent/test_memory_migration.py
git add miniunicorn/agent/memory_migration.py tests/agent/test_memory_migration.py
git commit -m "fix(memory): serialize migration manifest updates"
```

---

### Task 7: Unify migration status across startup and commands

**Files:**
- Modify: `miniunicorn/agent/memory.py:347-375`
- Modify: `miniunicorn/command/memory.py:83-108`
- Test: `tests/agent/test_memory_migration.py`
- Test: `tests/agent/test_memory_commands.py`

**Interfaces:**
- Consumes: Task 6 `load_migration_state()`.
- Produces: identical migration-completed answer in migrator, AgentLoop startup gate and `/memory-status`.

- [ ] **Step 1: Add old-manifest startup test**

Write only `memory/migration-v1.json` with a valid `completed_at`, configure governed mode, and assert `AgentLoop` constructs successfully.

- [ ] **Step 2: Add old-manifest status test**

With the same legacy-only state, dispatch `/memory-status` and assert it reports the exact completion timestamp rather than pending.

- [ ] **Step 3: Run both tests before implementation**

```powershell
python -m pytest -q tests/agent/test_memory_migration.py::TestGovernedStartupGate::test_governed_loop_accepts_completed_legacy_manifest tests/agent/test_memory_commands.py::TestStatus::test_status_reads_completed_legacy_manifest
```

Expected: startup raises migration-required and status reports pending.

- [ ] **Step 4: Replace direct loads with the shared helper**

In `MemoryStore.migration_completed()`:

```python
from miniunicorn.agent.memory_migration import load_migration_state
return load_migration_state(self.workspace).completed_at is not None
```

In `cmd_memory_status()`, call the same helper. Remove now-unused imports of `MIGRATION_STATE_FILE` and `MigrationState`.

- [ ] **Step 5: Run migration/command tests and commit**

```powershell
python -m pytest -q tests/agent/test_memory_migration.py tests/agent/test_memory_commands.py
python -m ruff check miniunicorn/agent/memory.py miniunicorn/command/memory.py tests/agent/test_memory_migration.py tests/agent/test_memory_commands.py
git add miniunicorn/agent/memory.py miniunicorn/command/memory.py tests/agent/test_memory_migration.py tests/agent/test_memory_commands.py
git commit -m "fix(memory): unify migration completion lookup"
```

---

### Task 8: Enforce monotonic `blocked_by` and handle malformed command quoting

**Files:**
- Modify: `miniunicorn/agent/memory_models.py:669-703`
- Modify: `miniunicorn/command/memory.py:64-222`
- Test: `tests/agent/test_memory_repository.py`
- Test: `tests/agent/test_memory_commands.py`

**Interfaces:**
- Candidate same-status revisions may add but never remove `blocked_by` IDs.
- `_split_args_or_usage(ctx, usage) -> tuple[list[str] | None, OutboundMessage | None]` safely handles `shlex.split` syntax errors.

- [ ] **Step 1: Add blocked-by removal/replacement tests**

Start with candidate revision 1 blocked by active X. Submit revision 2 that clears it and another that replaces X with Y. Both must raise `InvalidMemoryTransition` and leave revision history unchanged. Add a positive test where revision 2 retains X and adds Y.

- [ ] **Step 2: Add malformed-quote command tests**

Parameterize:

```python
(
    (cmd_memory_show, '"', "/memory-show <id>"),
    (cmd_memory_promote, '"', "/memory-promote <id> [--replace <active-id>]"),
    (cmd_memory_revoke, '" reason', "/memory-revoke <id> <reason>"),
)
```

Assert each handler returns an `OutboundMessage` containing its usage and does not raise.

- [ ] **Step 3: Run tests before implementation**

```powershell
python -m pytest -q tests/agent/test_memory_repository.py -k "blocked_by" tests/agent/test_memory_commands.py -k "malformed_quote"
```

Expected: raw transaction is accepted and commands raise `ValueError: No closing quotation`.

- [ ] **Step 4: Add `blocked_by` to the candidate superset fields**

Change only the candidate branch:

```python
if previous.status is MemoryStatus.CANDIDATE:
    allowed = _SAME_STATUS_CANDIDATE_FIELDS
    must_superset = ("evidence", "derived_from", "blocked_by")
```

Do not add it to active revisions.

- [ ] **Step 5: Add a narrow command argument helper**

```python
def _split_args_or_usage(
    ctx: CommandContext, usage: str
) -> tuple[list[str] | None, OutboundMessage | None]:
    try:
        return shlex.split(ctx.args), None
    except ValueError:
        return None, _usage(ctx, usage)
```

Use it only in show/promote/revoke and immediately return the usage reply when provided. Do not add `except ValueError` to `_requires_stack`.

- [ ] **Step 6: Run tests and commit**

```powershell
python -m pytest -q tests/agent/test_memory_repository.py tests/agent/test_memory_commands.py
python -m ruff check miniunicorn/agent/memory_models.py miniunicorn/command/memory.py tests/agent/test_memory_repository.py tests/agent/test_memory_commands.py
git add miniunicorn/agent/memory_models.py miniunicorn/command/memory.py tests/agent/test_memory_repository.py tests/agent/test_memory_commands.py
git commit -m "fix(memory): protect conflict audit metadata"
```

---

### Task 9: Strengthen the no-vector runtime boundary and correct documentation

**Files:**
- Modify: `tests/agent/test_structured_memory_boundary.py:127-154`
- Modify: `docs/configuration.md:1550-1560`
- Modify: `docs/memory.md`

**Interfaces:**
- Produces: AST-based runtime import denylist and accurate operator documentation.

- [ ] **Step 1: Replace the narrow boundary test with AST import scanning**

Collect the exact runtime files listed in the design, including all `miniunicorn/agent/memory_*.py`. Parse each with `ast.parse()`, collect roots from `ast.Import` and `ast.ImportFrom`, and assert disjointness from:

```python
FORBIDDEN_VECTOR_IMPORTS = {
    "annoy",
    "chromadb",
    "faiss",
    "hnswlib",
    "lancedb",
    "pinecone",
    "pymilvus",
    "qdrant_client",
    "sentence_transformers",
    "weaviate",
}
```

Keep dependency scanning for both regular and optional dependency tables in `pyproject.toml`.

- [ ] **Step 2: Prove the boundary test detects a runtime import**

Extract import collection to a test-local helper accepting source strings. Assert `collect_import_roots("import lancedb\n") == {"lancedb"}` and that checking it against the denylist fails. This verifies the test mechanism without modifying production files.

- [ ] **Step 3: Correct recall-audit documentation**

State that audit contains timestamp, hashed scope keys, hit IDs/scores/reason categories, counts, token use and degraded/error codes. State explicitly that it excludes raw query text, memory statements and evidence excerpts. Document creation-idempotency, stable reflection IDs and migration lock at operator level in `docs/memory.md`.

- [ ] **Step 4: Run boundary tests and docs checks**

```powershell
python -m pytest -q tests/agent/test_structured_memory_boundary.py
python -m ruff check tests/agent/test_structured_memory_boundary.py
git diff --check
```

- [ ] **Step 5: Commit Task 9**

```powershell
git add tests/agent/test_structured_memory_boundary.py docs/configuration.md docs/memory.md
git commit -m "test(memory): enforce no-vector runtime boundary"
```

---

### Task 10: Run layered verification and review the complete patch

**Files:**
- Review: all files changed since the stable design baseline `c87dad75`; exclude this plan document when assessing implementation scope
- Modify only if verification finds a defect; any fix requires its own regression test and commit.

**Interfaces:**
- Produces: evidence that Plan B satisfies its design without regressing the existing agent suite.

- [ ] **Step 1: Check repository state and diff scope**

```powershell
git status --short
git log --oneline c87dad75..HEAD
git diff --stat c87dad75..HEAD
git diff --check c87dad75..HEAD
```

Expected: only planned files changed, one intentional commit per completed task, and no whitespace errors.

- [ ] **Step 2: Run focused C2 tests**

```powershell
python -m pytest -q tests/agent/test_memory_repository.py tests/agent/test_memory_lifecycle.py tests/agent/test_memory_migration.py tests/agent/test_memory_extraction.py tests/agent/test_dream_structured_memory.py tests/agent/test_reflection_structured.py tests/agent/test_memory_store.py tests/agent/test_memory_commands.py tests/agent/test_context_structured_memory.py tests/agent/test_structured_memory_boundary.py
```

Expected: zero failures, zero errors, zero hangs.

- [ ] **Step 3: Run all Agent tests**

```powershell
python -m pytest -q tests/agent
```

Expected: zero code-caused failures. For any failure suspected to be environmental or baseline, run the exact failing test against an untouched `main` worktree and record both commands and outputs before classifying it.

- [ ] **Step 4: Run static and formatting gates**

```powershell
python -m ruff check miniunicorn tests
git diff --check c87dad75..HEAD
```

Expected: Ruff prints `All checks passed!`; diff check has no output.

- [ ] **Step 5: Re-run the two original duplication scenarios manually**

Run small temporary-workspace scripts for:

1. create candidate → manual promote → retry original batch;
2. construct two repositories before either write → ingest same batch/content through both.

For both, assert one current record or one creation lineage as defined by the test, stable ID, and no unexpected revision on retry. Save the command/output in the implementation handoff.

- [ ] **Step 6: Review invariants directly from the final diff**

Confirm all of the following by code inspection:

- lookup and conditional create are inside one journal lock;
- revision publication never removes historical source provenance;
- no set iteration chooses an idempotency result;
- Dream prompt and parser use the same allowed scope set;
- every Dream evidence ref appears verbatim in the model input;
- Reflection IDs are application-generated and independent of line number;
- migration scan/state RMW is inside one migration lock;
- canonical corruption never falls back to legacy completion;
- no new vector dependency or runtime import exists.

- [ ] **Step 7: Create a final verification commit only if needed**

If verification required code changes, commit the regression test and fix together:

```powershell
git add <only-the-files-changed-by-the-verification-fix>
git commit -m "fix(memory): close Plan B verification gap"
```

If no changes were needed, do not create an empty commit.

## Handoff Report Required From the Implementing Agent

The implementing Agent must return:

1. final branch name and HEAD SHA;
2. ordered commit list since `c87dad75`, identifying the plan-document commit separately from implementation commits;
3. files changed;
4. focused test count and result;
5. complete `tests/agent` result;
6. Ruff and `git diff --check` results;
7. outputs from the manual retry and stale-instance reproductions;
8. any failures reproduced on `main`, with exact commands;
9. confirmation that nothing was merged or pushed unless separately authorized.
