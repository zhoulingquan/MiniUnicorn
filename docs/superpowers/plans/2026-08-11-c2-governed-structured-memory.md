# C2 Governed Structured Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build C2 governed structured memory for MiniUnicorn with candidate gating, atomic lifecycle transitions, deterministic non-vector recall, auditable evidence, safe migration, and a governed context mode.

**Architecture:** Markdown remains the source for identity and always-on policy, while a checksummed append-only JSONL transaction journal becomes the sole source of structured facts. Dream creates proposals, lifecycle code alone promotes or replaces records, and ContextBuilder injects only deterministic recall hits with reasons. A rebuildable in-process index provides speed without adding a database.

**Tech Stack:** Python 3.11+, Pydantic 2, filelock, tiktoken, pytest 9, dulwich GitStore, JSONL/Markdown

## Global Constraints

- Normative design: `docs/superpowers/specs/2026-08-11-c2-governed-structured-memory-design.md`.
- Do not add embedding, vector similarity, vector database, knowledge graph, or a generic retrieval-provider abstraction.
- Do not add a persistent database or network dependency.
- `memory/structured/journal.jsonl` is the sole source of structured memory truth; in-process indexes are rebuildable.
- Every model-produced fact starts as `candidate`; only lifecycle code may produce `active`.
- A failed append, fsync, validation, parse, or promotion must not advance the Dream cursor.
- A candidate, terminal, expired, or out-of-scope record must never appear in model context.
- Every injected hit must include stable ID, score, and a human-readable `Why` line.
- Preserve existing public methods and tests until their callers are migrated in the same task.
- Default rollout mode is exactly `shadow`.
- Use TDD and make the commit at the end of every task only after its focused test command passes.

---

## File map

### New production files

| File | Responsibility |
|---|---|
| `miniunicorn/agent/memory_models.py` | Enums, Pydantic models, canonical normalization/hash helpers, legal transitions |
| `miniunicorn/agent/memory_repository.py` | Locked checksummed transaction append, replay, health, current-record indexes |
| `miniunicorn/agent/memory_lifecycle.py` | Candidate ingestion, source validation, promotion, dedupe, conflict, revoke, expiry |
| `miniunicorn/agent/memory_recall.py` | Scope filtering, lexical Tag/alias routing, exact score, budget, prompt rendering |
| `miniunicorn/agent/memory_extraction.py` | Strict Dream JSON extraction parser and proposal-to-evidence resolution |
| `miniunicorn/agent/memory_migration.py` | Dry-run and idempotent import of legacy Markdown/JSONL |
| `miniunicorn/templates/memory/TAGS.json` | Bundled schema-v1 controlled Tag catalog |
| `miniunicorn/templates/memory/POLICY.md` | Empty explanatory policy template |

### Modified production files

| File | Change |
|---|---|
| `miniunicorn/agent/memory.py` | MemoryStore façade, Dream structured pipeline, Git tracked paths, hygiene expiry |
| `miniunicorn/agent/context.py` | Policy injection, RecallQuery construction, shadow/governed behavior |
| `miniunicorn/agent/loop.py` | Pass structured-memory config and exact session/project/user scope |
| `miniunicorn/agent/loop_builder.py` | Wire `AgentDefaults.structured_memory` from config |
| `miniunicorn/config/schema.py` | Add strict `StructuredMemoryConfig` |
| `miniunicorn/command/builtin.py` | Add inspect, promote, revoke, correct, and migrate commands |
| `miniunicorn/templates/agent/dream_phase1.md` | Require strict extraction JSON |
| `miniunicorn/templates/agent/dream_phase2.md` | Remove long-term fact editing authority; retain only non-memory skill work if still used |
| `miniunicorn/templates/agent/reflection_system.md` | Require one atomic lesson and no claim of formal-memory status |
| `miniunicorn/skills/memory/SKILL.md` | Explain C2 search, IDs, correction and policy boundaries |
| `docs/memory.md` | Replace legacy-only explanation with modes, schema, lifecycle and operations |
| `docs/configuration.md` | Document `structuredMemory` exact fields |
| `docs/chat-commands.md` | Document `/memory-*` commands |

### New tests

```text
tests/agent/test_memory_models.py
tests/agent/test_memory_repository.py
tests/agent/test_memory_lifecycle.py
tests/agent/test_memory_recall.py
tests/agent/test_memory_extraction.py
tests/agent/test_memory_migration.py
tests/agent/test_context_structured_memory.py
tests/agent/test_dream_structured_memory.py
tests/command/test_builtin_memory.py
tests/config/test_structured_memory_config.py
tests/agent/test_structured_memory_boundary.py
```

---

### Task 1: Freeze the schema, normalization, transitions, and bundled catalog

**Files:**

- Create: `miniunicorn/agent/memory_models.py`
- Create: `miniunicorn/templates/memory/TAGS.json`
- Create: `miniunicorn/templates/memory/POLICY.md`
- Test: `tests/agent/test_memory_models.py`

**Interfaces:**

- Consumes: Pydantic `BaseModel`, `ConfigDict`, `Field`, field/model validators.
- Produces: `MemoryRecord`, `MemoryTransaction`, `MemoryOperation`, `CandidateProposal`, `EvidenceRef`, `MemoryScope`, `TagCatalog`, `RecallQuery`, `RecallHit`, `RecallResult`, `RepositoryHealth`, typed memory exceptions, `normalize_text()`, `normalize_slot()`, `content_hash()`, `transaction_checksum()`, `assert_transition()`.

- [ ] **Step 1: Write failing schema and transition tests**

Create tests with these exact cases:

```python
def test_content_hash_is_stable_after_nfkc_and_whitespace_normalization():
    left = content_hash(MemoryKind.FACT, MemoryScope(kind=ScopeKind.PROJECT, key="project:x"), "Ａ", "db.primary", "  PostgreSQL\n")
    right = content_hash(MemoryKind.FACT, MemoryScope(kind=ScopeKind.PROJECT, key="project:x"), "A", "db.primary", "PostgreSQL")
    assert left == right


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("candidate", "active"),
        ("candidate", "superseded"),
        ("candidate", "revoked"),
        ("candidate", "expired"),
        ("active", "superseded"),
        ("active", "revoked"),
        ("active", "expired"),
    ],
)
def test_legal_status_transitions(old, new):
    assert_transition(old, new)


@pytest.mark.parametrize("old", ["superseded", "revoked", "expired"])
def test_terminal_status_cannot_return_active(old):
    with pytest.raises(InvalidMemoryTransition):
        assert_transition(old, "active")


def test_record_rejects_unknown_tag(tag_catalog, record_data):
    record = MemoryRecord.model_validate(record_data | {"tags": ["not.registered"]})
    with pytest.raises(UnknownMemoryTag):
        tag_catalog.validate_record(record)


def test_transaction_checksum_ignores_checksum_field(transaction):
    first = transaction_checksum(transaction)
    second = transaction_checksum(transaction.model_copy(update={"checksum_sha256": "f" * 64}))
    assert first == second
```

- [ ] **Step 2: Run the focused tests and confirm contract failures**

Run:

```powershell
pytest tests/agent/test_memory_models.py -q
```

Expected: collection fails because `miniunicorn.agent.memory_models` does not exist.

- [ ] **Step 3: Implement the complete schema contract**

Use these exact public types and constants; keep all models `extra="forbid"` and frozen:

```python
SCHEMA_VERSION = 1
MEMORY_ID_RE = re.compile(r"^mem_[0-9a-f]{32}$")
TX_ID_RE = re.compile(r"^mtx_[0-9a-f]{32}$")
SLOT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
TERMINAL_STATUSES = frozenset({MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED})
LEGAL_STATUS_TRANSITIONS = {
    MemoryStatus.CANDIDATE: frozenset({
        MemoryStatus.CANDIDATE,
        MemoryStatus.ACTIVE,
        MemoryStatus.SUPERSEDED,
        MemoryStatus.REVOKED,
        MemoryStatus.EXPIRED,
    }),
    MemoryStatus.ACTIVE: frozenset({
        MemoryStatus.ACTIVE,
        MemoryStatus.SUPERSEDED,
        MemoryStatus.REVOKED,
        MemoryStatus.EXPIRED,
    }),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.REVOKED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
}


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex}"


def new_transaction_id() -> str:
    return f"mtx_{uuid.uuid4().hex}"


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def normalize_match_text(value: str) -> str:
    return normalize_text(value).casefold()


def normalize_slot(value: str) -> str:
    normalized = normalize_match_text(value).replace(" ", ".")
    if not SLOT_RE.fullmatch(normalized):
        raise ValueError("invalid memory slot")
    return normalized


def assert_transition(old: MemoryStatus, new: MemoryStatus) -> None:
    if new not in LEGAL_STATUS_TRANSITIONS[old]:
        raise InvalidMemoryTransition(f"illegal memory transition: {old.value} -> {new.value}")
```

Implement the fields and limits verbatim from design sections 6 and 7. `content_hash()` must canonicalize `(kind.value, scope.model_dump(mode="json"), normalize_match_text(subject), normalize_slot(slot), normalize_text(statement))` with sorted compact JSON before SHA-256. `transaction_checksum()` must serialize `model_dump(mode="json", exclude={"checksum_sha256"})` using `sort_keys=True`, `separators=(",", ":")`, and `ensure_ascii=False`.

The bundled catalog must contain these initial canonical tags and aliases:

```json
{
  "schema_version": 1,
  "tags": [
    {"name": "architecture.memory", "aliases": ["agent memory", "memory system", "全局记忆", "记忆系统"]},
    {"name": "project.decision", "aliases": ["decision", "decided", "项目决策", "决定"]},
    {"name": "project.constraint", "aliases": ["constraint", "must", "约束", "必须"]},
    {"name": "project.requirement", "aliases": ["requirement", "需求"]},
    {"name": "project.fact", "aliases": ["project fact", "项目事实"]},
    {"name": "project.outcome", "aliases": ["outcome", "result", "结果"]},
    {"name": "user.identity", "aliases": ["user profile", "用户信息"]},
    {"name": "user.preference", "aliases": ["preference", "prefers", "偏好", "喜欢"]},
    {"name": "workflow.procedure", "aliases": ["procedure", "workflow", "流程", "步骤"]},
    {"name": "failure.lesson", "aliases": ["failure", "lesson", "失败", "教训"]},
    {"name": "tool.behavior", "aliases": ["tool", "工具"]},
    {"name": "entity.relationship", "aliases": ["relationship", "关系"]},
    {"name": "session.event", "aliases": ["event", "事件"]},
    {"name": "shared.fact", "aliases": ["shared", "global", "共享", "全局"]}
  ]
}
```

- [ ] **Step 4: Run schema tests and static lint**

Run:

```powershell
pytest tests/agent/test_memory_models.py -q
ruff check miniunicorn/agent/memory_models.py tests/agent/test_memory_models.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit the contract**

```powershell
git add miniunicorn/agent/memory_models.py miniunicorn/templates/memory/TAGS.json miniunicorn/templates/memory/POLICY.md tests/agent/test_memory_models.py
git commit -m "feat(memory): define governed memory schema"
```

---

### Task 2: Build the atomic journal repository and rebuildable indexes

**Files:**

- Create: `miniunicorn/agent/memory_repository.py`
- Test: `tests/agent/test_memory_repository.py`

**Interfaces:**

- Consumes: Task 1 models and helpers; `filelock.FileLock`.
- Produces:

```text
StructuredMemoryRepository(workspace: Path, *, lock_timeout_s: float = 5.0)
property workspace -> Path
property health -> RepositoryHealth
rebuild() -> RepositoryHealth
append_transaction(transaction: MemoryTransaction) -> None
get(memory_id: str) -> MemoryRecord | None
revisions(memory_id: str) -> tuple[MemoryRecord, ...]
current_records(status: MemoryStatus | None = None) -> tuple[MemoryRecord, ...]
active_for_conflict_key(key: str) -> MemoryRecord | None
candidate_records() -> tuple[MemoryRecord, ...]
candidate_ids_for_source(source_batch: str) -> frozenset[str]
```

- [ ] **Step 1: Write repository failure and replay tests**

Cover these exact tests:

```python
def test_multi_record_transaction_replays_atomically(repository, replacement_tx):
    repository.append_transaction(replacement_tx)
    rebuilt = StructuredMemoryRepository(repository.workspace)
    assert rebuilt.get("mem_" + "a" * 32).status == MemoryStatus.ACTIVE
    assert rebuilt.get("mem_" + "b" * 32).status == MemoryStatus.SUPERSEDED


def test_bad_checksum_stops_at_first_bad_line_and_disables_writes(repository, valid_line):
    repository.journal_path.write_text(valid_line + "\n" + valid_line.replace('"checksum_sha256":"', '"checksum_sha256":"0'), encoding="utf-8")
    health = repository.rebuild()
    assert health.state == "degraded"
    assert health.last_valid_line == 1
    with pytest.raises(RepositoryDegradedError):
        repository.append_transaction(make_transaction())


def test_append_failure_does_not_update_index(repository, transaction, monkeypatch):
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("disk full")))
    with pytest.raises(MemoryWriteError, match="disk full"):
        repository.append_transaction(transaction)
    assert repository.get(transaction.operations[0].record.id) is None


def test_expected_revision_conflict_rejects_second_writer(repository, create_tx):
    repository.append_transaction(create_tx)
    first = update_transaction(expected_revision=1, statement="first")
    second = update_transaction(expected_revision=1, statement="second")
    repository.append_transaction(first)
    with pytest.raises(MemoryRevisionConflict):
        repository.append_transaction(second)
```

Also test invalid JSON tail, unsupported schema, skipped revision, illegal status transition, duplicate operation ID, unknown Tag, lock timeout, empty log, stable ordering, scope/subject/tag/alias indexes, and valid empty lines.

- [ ] **Step 2: Run repository tests and verify they fail**

```powershell
pytest tests/agent/test_memory_repository.py -q
```

Expected: import failure for `memory_repository`.

- [ ] **Step 3: Implement repository replay and commit protocol**

Use this exact state shape:

```python
class StructuredMemoryRepository:
    def _clear_index(self) -> None:
        self._current: dict[str, MemoryRecord] = {}
        self._revision_history: dict[str, list[MemoryRecord]] = defaultdict(list)
        self._by_status: dict[MemoryStatus, set[str]] = defaultdict(set)
        self._by_scope: dict[tuple[ScopeKind, str], set[str]] = defaultdict(set)
        self._by_subject: dict[str, set[str]] = defaultdict(set)
        self._by_tag: dict[str, set[str]] = defaultdict(set)
        self._by_alias: dict[str, set[str]] = defaultdict(set)
        self._active_by_conflict: dict[str, str] = {}
        self._candidate_by_source: dict[str, set[str]] = defaultdict(set)
```

`rebuild()` must read line-by-line, validate JSON and checksum, validate `expected_revisions` against the replayed index, validate every record transition, apply all operations to a copied transaction-local view, then publish all operations to the main index together. At the first error set `RepositoryHealth(state="degraded", last_valid_line=line_number - 1, error_code=code)`, where code is exactly one of `invalid_json`, `unsupported_schema`, `checksum_mismatch`, `revision_conflict`, `invalid_transition`, `unknown_tag`, or `invalid_transaction`; log one structured error and stop.

`append_transaction()` must perform this sequence without reordering:

```python
with FileLock(str(self.lock_path), timeout=self.lock_timeout_s):
    self._require_healthy()
    validated = self._validate_against_current(transaction)
    line = self._canonical_transaction_line(validated)
    with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    self._publish_transaction(validated)
```

Translate `filelock.Timeout` to `MemoryLockTimeout`, revision mismatch to `MemoryRevisionConflict`, and all append/flush/fsync failures to `MemoryWriteError` while preserving the original message. Never catch these errors and return success.

- [ ] **Step 4: Verify repository behavior**

```powershell
pytest tests/agent/test_memory_repository.py -q
pytest tests/agent/test_memory_models.py tests/agent/test_memory_repository.py -q
ruff check miniunicorn/agent/memory_repository.py tests/agent/test_memory_repository.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit repository**

```powershell
git add miniunicorn/agent/memory_repository.py tests/agent/test_memory_repository.py
git commit -m "feat(memory): add atomic memory journal"
```

---

### Task 3: Implement governed lifecycle and fail-closed conflict handling

**Files:**

- Create: `miniunicorn/agent/memory_lifecycle.py`
- Test: `tests/agent/test_memory_lifecycle.py`

**Interfaces:**

- Consumes: repository, Task 1 models, clock injection.
- Produces:

```python
@dataclass(frozen=True)
class IngestResult:
    candidate_id: str
    final_status: MemoryStatus
    active_id: str | None
    transaction_ids: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True)
class LifecyclePolicy:
    auto_promote_verified: bool
    min_repeated_evidence: int
    candidate_ttl_days: int


@dataclass(frozen=True)
class IngestContext:
    actor: ActorKind
    reason: str
    source_batch: str
    scope: MemoryScope
    evidence_catalog: Mapping[str, EvidenceRef]
    now: datetime
```

```text
StructuredMemoryLifecycle repository and policy constructor dependencies:
ingest(proposal: CandidateProposal, context: IngestContext) -> IngestResult
promote(candidate_id: str, *, actor: ActorKind, reason: str, replace_id: str | None = None) -> IngestResult
revoke(memory_id: str, *, reason: str) -> MemoryRecord
expire_due(now: datetime) -> tuple[str, ...]
```

`LifecyclePolicy` is a frozen dataclass with `auto_promote_verified: bool`, `min_repeated_evidence: int`, and `candidate_ttl_days: int`. Task 5 maps `StructuredMemoryConfig` into this runtime policy so the lifecycle module does not import the configuration layer.

- [ ] **Step 1: Write the promotion matrix and conflict tests**

Use parameterized tests for every row in design section 8. Include these assertions:

```python
def test_inferred_candidate_never_auto_promotes(lifecycle, inferred_proposal, context):
    result = lifecycle.ingest(inferred_proposal, context)
    assert result.final_status == MemoryStatus.CANDIDATE
    assert lifecycle.repository.get(result.candidate_id).status == MemoryStatus.CANDIDATE


def test_correction_replaces_lower_rank_in_one_transaction(lifecycle, active_decision, correction_proposal, context):
    result = lifecycle.ingest(correction_proposal, context)
    old = lifecycle.repository.get(active_decision.id)
    new = lifecycle.repository.get(result.candidate_id)
    assert new.status == MemoryStatus.ACTIVE
    assert old.status == MemoryStatus.SUPERSEDED
    assert old.replacement_id == new.id
    assert new.supersedes == (old.id,)


def test_same_rank_conflict_stays_candidate_without_replace(lifecycle, active_decision, competing_decision, context):
    result = lifecycle.ingest(competing_decision, context)
    assert result.reason_code == "same_rank_requires_explicit_replace"
    assert lifecycle.repository.get(result.candidate_id).blocked_by == (active_decision.id,)


def test_revoke_does_not_resurrect_superseded_record(lifecycle, replacement_chain):
    lifecycle.revoke(replacement_chain.current_id, reason="user withdrew correction")
    assert lifecycle.repository.active_for_conflict_key(replacement_chain.conflict_key) is None
```

Test evidence dedupe, exact evidence threshold, file hash mismatch, same-content merge, active immutable fields, terminal refusal, expiry, and repository exception propagation.

- [ ] **Step 2: Run lifecycle tests and confirm failure**

```powershell
pytest tests/agent/test_memory_lifecycle.py -q
```

Expected: import failure for `memory_lifecycle`.

- [ ] **Step 3: Implement source classification, candidate-first ingest, and promotion**

Use this exact rank and threshold table:

```python
SOURCE_RANK = {
    SourceLevel.INFERRED: 1,
    SourceLevel.REPEATED_EXPERIENCE: 2,
    SourceLevel.VERIFIED: 3,
    SourceLevel.CONFIRMED_DECISION: 4,
    SourceLevel.EXPLICIT_CORRECTION: 5,
}


def can_auto_promote(record: MemoryRecord, config: LifecyclePolicy) -> bool:
    distinct = {(e.kind, e.ref, e.sha256) for e in record.evidence}
    if record.source_level is SourceLevel.EXPLICIT_CORRECTION:
        return any(e.kind in {EvidenceKind.USER_MESSAGE, EvidenceKind.MANUAL} for e in record.evidence)
    if record.source_level is SourceLevel.CONFIRMED_DECISION:
        return record.confidence >= 0.90 and any(e.kind in {EvidenceKind.USER_MESSAGE, EvidenceKind.MANUAL} for e in record.evidence)
    if record.source_level is SourceLevel.VERIFIED:
        return config.auto_promote_verified and record.confidence >= 0.80 and any(e.kind in {EvidenceKind.FILE, EvidenceKind.TOOL_RESULT, EvidenceKind.GIT} for e in record.evidence)
    if record.source_level is SourceLevel.REPEATED_EXPERIENCE:
        return record.confidence >= 0.85 and len(distinct) >= config.min_repeated_evidence
    return False
```

`ingest()` must always commit candidate revision 1 first. A retried `source_batch + proposal_index + content_hash` must return the existing candidate instead of creating another ID. Promotion uses a second transaction. If that transaction fails, propagate the error and leave candidate revision 1 intact.

Conflict promotion must construct all changed record snapshots first, put them into one `MemoryTransaction`, and call `append_transaction()` once. Do not loop over records and append separately.

- [ ] **Step 4: Verify lifecycle and repository together**

```powershell
pytest tests/agent/test_memory_lifecycle.py tests/agent/test_memory_repository.py -q
ruff check miniunicorn/agent/memory_lifecycle.py tests/agent/test_memory_lifecycle.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit lifecycle**

```powershell
git add miniunicorn/agent/memory_lifecycle.py tests/agent/test_memory_lifecycle.py
git commit -m "feat(memory): govern memory lifecycle"
```

---

### Task 4: Implement deterministic lexical recall with exact reasons and budget

**Files:**

- Create: `miniunicorn/agent/memory_recall.py`
- Test: `tests/agent/test_memory_recall.py`

**Interfaces:**

- Consumes: repository indexes, Tag catalog, `RecallQuery`, `tiktoken`.
- Produces:

```text
StructuredMemoryRecall(repository: StructuredMemoryRepository, tag_catalog: TagCatalog)
recall(query: RecallQuery) -> RecallResult
render_prompt(result: RecallResult) -> str
```

- [ ] **Step 1: Write filtering, score, determinism, and no-LLM tests**

Test every score component from design section 9.4 independently. Include:

```python
def test_candidate_and_other_user_scope_never_recalled(recall, seeded_records, query):
    result = recall.recall(query)
    ids = {hit.record.id for hit in result.hits}
    assert seeded_records.candidate.id not in ids
    assert seeded_records.other_user.id not in ids


def test_score_and_reason_are_exact(recall, project_decision, query_at_fixed_time):
    result = recall.recall(query_at_fixed_time.model_copy(update={"query_text": "请分析全局记忆架构决定"}))
    hit = next(h for h in result.hits if h.record.id == project_decision.id)
    assert hit.score == 105
    assert hit.reasons == (
        "tag=architecture.memory(+45)",
        "source=confirmed_decision(+20)",
        "scope=project(+10)",
        "importance=5(+20)",
        "freshness<=7d(+10)",
    )


def test_insertion_order_does_not_change_recall(make_recall, records, query):
    forward = make_recall(records).recall(query)
    reverse = make_recall(list(reversed(records))).recall(query)
    assert [h.record.id for h in forward.hits] == [h.record.id for h in reverse.hits]


def test_recall_never_calls_provider(recall, query, provider):
    recall.recall(query)
    provider.chat_with_retry.assert_not_called()
```

Also test ASCII word boundaries, CJK substring, subject precedence, canonical versus catalog/record alias, expired-at-query-time, exact ID, requested kind, oversized first hit skipping, max hits, degraded health, and formatted ID/Why.

- [ ] **Step 2: Run recall tests and confirm failure**

```powershell
pytest tests/agent/test_memory_recall.py -q
```

Expected: import failure for `memory_recall`.

- [ ] **Step 3: Implement the exact routing and scoring pipeline**

Use named constants rather than inline numbers:

```python
SCOPE_SCORE = {"session": 12, "project": 10, "user": 8, "shared": 6}
SOURCE_SCORE = {
    "explicit_correction": 25,
    "confirmed_decision": 20,
    "verified": 15,
    "repeated_experience": 10,
    "inferred": 0,
}
ROUTE_EXPLICIT_ID = 100
ROUTE_SUBJECT = 60
ROUTE_CANONICAL_TAG = 45
ROUTE_CATALOG_ALIAS = 35
ROUTE_RECORD_ALIAS = 30


def freshness_score(updated_at: datetime, now: datetime) -> tuple[int, str]:
    days = max(0, (now - updated_at).days)
    if days <= 7:
        return 10, "freshness<=7d(+10)"
    if days <= 30:
        return 7, "freshness<=30d(+7)"
    if days <= 90:
        return 4, "freshness<=90d(+4)"
    return 0, "freshness>90d(+0)"
```

Filter in the normative order before scoring. Route categories do not stack; take the highest category and only its permitted extra matches. Build the exact sort key `(-score, -source_rank, -importance, -updated_epoch, id)`. Encode each complete rendered hit with `cl100k_base`; if it does not fit, increment `excluded_by_budget` and continue. `render_prompt()` returns an empty string for zero hits and otherwise begins `# Recalled Memory (Deterministic)`.

- [ ] **Step 4: Verify recall**

```powershell
pytest tests/agent/test_memory_recall.py -q
pytest tests/agent/test_memory_models.py tests/agent/test_memory_repository.py tests/agent/test_memory_lifecycle.py tests/agent/test_memory_recall.py -q
ruff check miniunicorn/agent/memory_recall.py tests/agent/test_memory_recall.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit recall**

```powershell
git add miniunicorn/agent/memory_recall.py tests/agent/test_memory_recall.py
git commit -m "feat(memory): add deterministic memory recall"
```

---

### Task 5: Add strict configuration and wire the C2 façade in shadow mode

**Files:**

- Modify: `miniunicorn/config/schema.py`
- Modify: `miniunicorn/agent/memory.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/loop_builder.py`
- Test: `tests/config/test_structured_memory_config.py`
- Test: `tests/agent/test_memory_store.py`

**Interfaces:**

- Consumes: Tasks 1–4.
- Produces: `StructuredMemoryConfig`; `MemoryStore.structured_repository`, `.structured_lifecycle`, `.structured_recall`, `.read_shared_policy()`, `.recall_structured(query)`.

- [ ] **Step 1: Write config validation and façade wiring tests**

```python
def test_structured_memory_defaults_to_shadow():
    config = StructuredMemoryConfig()
    assert config.mode == "shadow"
    assert config.recall_token_budget == 2500
    assert config.max_recall_hits == 20


@pytest.mark.parametrize("field", ["embeddingModel", "vectorStore", "vectorSearch"])
def test_structured_memory_rejects_vector_fields(field):
    with pytest.raises(ValidationError):
        StructuredMemoryConfig.model_validate({field: "forbidden"})


def test_memory_store_tracks_structured_files_but_not_runtime_files(tmp_path):
    store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
    assert "memory/structured/journal.jsonl" in store.git._tracked_files
    assert "memory/shared/POLICY.md" in store.git._tracked_files
    assert "memory/structured/journal.lock" not in store.git._tracked_files
    assert "memory/structured/recall-audit.jsonl" not in store.git._tracked_files
```

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
pytest tests/config/test_structured_memory_config.py tests/agent/test_memory_store.py -q
```

Expected: tests fail because `StructuredMemoryConfig` and the new MemoryStore parameter do not exist.

- [ ] **Step 3: Implement config and runtime wiring**

Add this exact model before `AgentDefaults` and field on `AgentDefaults`:

```python
class StructuredMemoryConfig(Base):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    mode: Literal["legacy", "shadow", "governed"] = "shadow"
    recall_token_budget: int = Field(default=2500, ge=256, le=16_000)
    max_recall_hits: int = Field(default=20, ge=1, le=100)
    lock_timeout_s: float = Field(default=5.0, ge=0.1, le=30.0)
    auto_promote_verified: bool = True
    min_repeated_evidence: int = Field(default=2, ge=2, le=10)
    candidate_ttl_days: int = Field(default=30, ge=1, le=365)
    recall_audit_enabled: bool = False


class AgentDefaults(Base):
    structured_memory: StructuredMemoryConfig = Field(default_factory=StructuredMemoryConfig)
```

Add `structured_memory_config: StructuredMemoryConfig | None = None` to `AgentLoop.__init__`, a matching builder method, and `builder.with_structured_memory_config(defaults.structured_memory)` in `from_config()`. Pass it to `ContextBuilder`, and pass from ContextBuilder to `MemoryStore`.

MemoryStore must create the bundled catalog and policy atomically only when absent, then construct repository → lifecycle → recall. In `legacy` mode it may defer repository construction until a structured command is used; in `shadow` and `governed` it constructs immediately and logs health once.

Add the four structured files to `_WRITER_WHITELIST` and GitStore tracked files. Keep all existing legacy methods unchanged in this task.

- [ ] **Step 4: Verify config, façade, and existing store behavior**

```powershell
pytest tests/config/test_structured_memory_config.py tests/agent/test_memory_store.py -q
pytest tests/config/test_dream_config.py tests/agent/test_context_builder.py -q
ruff check miniunicorn/config/schema.py miniunicorn/agent/memory.py miniunicorn/agent/loop.py miniunicorn/agent/loop_builder.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit shadow-mode wiring**

```powershell
git add miniunicorn/config/schema.py miniunicorn/agent/memory.py miniunicorn/agent/loop.py miniunicorn/agent/loop_builder.py tests/config/test_structured_memory_config.py tests/agent/test_memory_store.py
git commit -m "feat(memory): wire governed memory shadow mode"
```

---

### Task 6: Add strict Dream extraction and candidate-first cursor semantics

**Files:**

- Create: `miniunicorn/agent/memory_extraction.py`
- Modify: `miniunicorn/agent/memory.py:1500` (`Dream`)
- Modify: `miniunicorn/templates/agent/dream_phase1.md`
- Modify: `miniunicorn/templates/agent/dream_phase2.md`
- Modify: `miniunicorn/templates/agent/reflection_system.md`
- Test: `tests/agent/test_memory_extraction.py`
- Test: `tests/agent/test_dream_structured_memory.py`
- Test: `tests/agent/test_dream.py`

**Interfaces:**

- Consumes: lifecycle and repository façade.
- Produces: `parse_extraction_batch(raw, evidence_catalog, tag_catalog) -> MemoryExtractionBatch`; `Dream._run_structured_batch()`.

- [ ] **Step 1: Write strict parser and Dream fail-closed tests**

```python
@pytest.mark.parametrize("raw", ["nothing new", "{}", '{"schema_version":1,"proposals":[],"extra":1}'])
def test_extraction_rejects_non_contract_output(raw, evidence_catalog, tag_catalog):
    with pytest.raises(MemoryExtractionError):
        parse_extraction_batch(raw, evidence_catalog, tag_catalog)


def test_valid_empty_batch_is_accepted(evidence_catalog, tag_catalog):
    parsed = parse_extraction_batch('{"schema_version":1,"proposals":[]}', evidence_catalog, tag_catalog)
    assert parsed.proposals == ()


async def test_dream_does_not_advance_cursor_when_any_ingest_fails(dream, store, lifecycle, mock_provider):
    store.append_history("one")
    store.append_history("two")
    mock_provider.chat_with_retry.return_value.content = valid_two_proposal_json()
    lifecycle.ingest.side_effect = [successful_ingest(), MemoryWriteError("disk full")]
    assert await dream.run() is False
    assert store.get_last_dream_cursor() == 0


async def test_valid_empty_batch_advances_cursor(dream, store, mock_provider):
    store.append_history("transient greeting only")
    mock_provider.chat_with_retry.return_value.content = '{"schema_version":1,"proposals":[]}'
    assert await dream.run() is True
    assert store.get_last_dream_cursor() == 1
```

Also test evidence ref lookup, exact excerpt verification, unknown Tag, excess fields, duplicate retry idempotency, reflection-only batch, governed Dream tool registry lacking fact-edit authority, and Git failure not reverting a durable journal write.

- [ ] **Step 2: Run extraction and Dream tests and confirm failure**

```powershell
pytest tests/agent/test_memory_extraction.py tests/agent/test_dream_structured_memory.py -q
```

Expected: import failure for `memory_extraction`.

- [ ] **Step 3: Implement parser, prompt contract, and Dream transaction flow**

The Phase 1 system prompt must demand this exact top-level shape and no Markdown fences:

```json
{
  "schema_version": 1,
  "proposals": [
    {
      "proposal_index": 0,
      "kind": "decision",
      "scope_hint": "project",
      "subject": "MiniUnicorn",
      "slot": "memory.retrieval.strategy",
      "statement": "Main uses deterministic structured recall.",
      "detail": "No embeddings are used.",
      "tags": ["architecture.memory", "project.decision"],
      "aliases": ["全局记忆"],
      "confidence": 1.0,
      "importance": 5,
      "evidence_refs": ["history:42"],
      "speech_act": "confirmed_decision",
      "expires_at": null
    }
  ]
}
```

`parse_extraction_batch()` may use `json_repair` only to repair syntax, then must validate the strict Pydantic model. Resolution replaces evidence ref strings with evidence objects from the catalog; a missing ref or excerpt mismatch rejects the whole extraction batch.

In shadow/governed mode Dream must call lifecycle for proposals in `proposal_index` order. It advances both cursors only after all ingest calls return and repository health remains healthy. On any exception it returns `False`, retains both cursors, does not compact away input, and logs `memory_dream_batch_failed` with error code but no evidence excerpt.

Remove `EditFileTool` from Dream's structured fact path. If the existing skill-creation phase remains enabled, give it only read plus skill-directory write tools and explicitly reject paths under `memory/`, `USER.md`, and `SOUL.md`.

- [ ] **Step 4: Verify Dream and legacy regressions**

```powershell
pytest tests/agent/test_memory_extraction.py tests/agent/test_dream_structured_memory.py tests/agent/test_dream.py -q
pytest tests/command/test_builtin_dream.py tests/config/test_dream_config.py -q
ruff check miniunicorn/agent/memory_extraction.py miniunicorn/agent/memory.py tests/agent/test_memory_extraction.py tests/agent/test_dream_structured_memory.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit Dream integration**

```powershell
git add miniunicorn/agent/memory_extraction.py miniunicorn/agent/memory.py miniunicorn/templates/agent/dream_phase1.md miniunicorn/templates/agent/dream_phase2.md miniunicorn/templates/agent/reflection_system.md tests/agent/test_memory_extraction.py tests/agent/test_dream_structured_memory.py tests/agent/test_dream.py
git commit -m "feat(memory): route dream through governed candidates"
```

---

### Task 7: Implement dry-run and idempotent legacy migration

**Files:**

- Create: `miniunicorn/agent/memory_migration.py`
- Test: `tests/agent/test_memory_migration.py`

**Interfaces:**

- Consumes: lifecycle, repository, legacy MemoryStore paths.
- Produces:

```python
@dataclass(frozen=True)
class MigrationReport:
    scanned: int
    importable: int
    imported: int
    skipped_existing: int
    rejected: tuple[MigrationIssue, ...]
    completed: bool
```

```text
LegacyMemoryMigrator(store: MemoryStore, lifecycle: StructuredMemoryLifecycle)
scan() -> MigrationReport
apply() -> MigrationReport
```

- [ ] **Step 1: Write zero-write dry-run, mapping, interruption, and idempotency tests**

```python
def test_dry_run_creates_no_files(migrator, workspace):
    report = migrator.scan()
    assert report.importable > 0
    assert not (workspace / "memory/structured/journal.jsonl").exists()
    assert not (workspace / "memory/structured/migration-v1.json").exists()


def test_markdown_bullets_become_independent_records(migrator, workspace):
    (workspace / "memory/MEMORY.md").write_text("# Decisions\n- Use PostgreSQL\n- No embeddings\n", encoding="utf-8")
    report = migrator.apply()
    records = migrator.repository.current_records(MemoryStatus.ACTIVE)
    assert report.imported == 2
    assert {r.statement for r in records} == {"Use PostgreSQL", "No embeddings"}


def test_apply_is_idempotent_after_partial_failure(migrator, monkeypatch):
    monkeypatch_repository_to_fail_on_batch(migrator.repository, batch_number=2)
    with pytest.raises(MemoryWriteError):
        migrator.apply()
    restore_repository(migrator.repository)
    second = migrator.apply()
    third = migrator.apply()
    assert second.completed is True
    assert third.imported == 0
    assert third.skipped_existing == second.scanned
```

Also test USER heading mapping, shared scope, procedural evidence, episodic-as-candidate, malformed JSONL report, long paragraph rejection, 100-operation batch size, and old-file byte-for-byte preservation.

- [ ] **Step 2: Run migration tests and confirm failure**

```powershell
pytest tests/agent/test_memory_migration.py -q
```

Expected: import failure for `memory_migration`.

- [ ] **Step 3: Implement deterministic scan/apply**

Use this exact idempotency key:

```python
def legacy_key(relative_path: str, source_locator: str, content: str) -> str:
    raw = f"{relative_path}\n{source_locator}\n{normalize_text(content)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

`scan()` must operate on an in-memory representation and never call repository/lifecycle constructors that create files. `apply()` loads an existing manifest or starts `{"schema_version": 1, "items": {}, "completed_at": null}`. After each successful batch, write the manifest using a sibling temporary file, flush, fsync, and `os.replace`. Do not mark an item until its journal transaction is durable. Do not modify any legacy source.

For imported active legacy records, actor=`migration`, evidence kind=`file` or the source JSONL kind, source level exactly as design section 14.1, and `derived_from=["legacy:" + legacy_key]`. Apply the heading/kind/Tag mapping table in design section 14.1 literally; unrecognized MEMORY headings map to `fact + project.fact`, and unrecognized USER headings map to `identity + user.identity`.

- [ ] **Step 4: Verify migration**

```powershell
pytest tests/agent/test_memory_migration.py -q
pytest tests/agent/test_memory_repository.py tests/agent/test_memory_lifecycle.py -q
ruff check miniunicorn/agent/memory_migration.py tests/agent/test_memory_migration.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit migration**

```powershell
git add miniunicorn/agent/memory_migration.py tests/agent/test_memory_migration.py
git commit -m "feat(memory): add safe legacy memory migration"
```

---

### Task 8: Switch ContextBuilder from global injection to governed recall

**Files:**

- Modify: `miniunicorn/agent/context.py`
- Modify: `miniunicorn/agent/loop.py`
- Test: `tests/agent/test_context_structured_memory.py`
- Test: `tests/agent/test_context_builder.py`
- Test: `tests/agent/test_context_prompt_cache.py`

**Interfaces:**

- Consumes: `MemoryStore.recall_structured()`, exact turn session/user/project identity.
- Produces: mode-correct prompt assembly and `ContextBuilder.last_recall_result` for diagnostics.

- [ ] **Step 1: Write mode, scope, candidate exclusion, and Why tests**

```python
def test_shadow_computes_but_does_not_inject_structured_hits(builder, seeded_active):
    prompt = builder.build_system_prompt(memory_query=matching_query())
    assert seeded_active.statement not in prompt
    assert builder.last_recall_result.hits[0].record.id == seeded_active.id


def test_governed_injects_policy_and_hit_reason_but_not_legacy_shared(builder, seeded_active):
    set_mode(builder, "governed")
    write_policy(builder.workspace, "Never expose secrets.")
    write_legacy_shared(builder.workspace, "Unrelated global fact")
    prompt = builder.build_system_prompt(memory_query=matching_query())
    assert "Never expose secrets." in prompt
    assert seeded_active.id in prompt
    assert "Why:" in prompt
    assert "Unrelated global fact" not in prompt


def test_governed_never_injects_candidate(builder, seeded_candidate):
    set_mode(builder, "governed")
    prompt = builder.build_system_prompt(memory_query=matching_query())
    assert seeded_candidate.id not in prompt
    assert seeded_candidate.statement not in prompt


def test_subagent_uses_parent_turn_scopes(loop, parent_session, subagent_message):
    messages = loop.build_for_test(parent_session, subagent_message)
    assert "user:subagent" not in messages[0]["content"]
```

- [ ] **Step 2: Run context tests and confirm failure**

```powershell
pytest tests/agent/test_context_structured_memory.py -q
```

Expected: tests fail because ContextBuilder has no structured query integration.

- [ ] **Step 3: Pass exact scope into RecallQuery and enforce mode behavior**

Add optional arguments without breaking existing callers:

```text
def build_system_prompt(
    self,
    skill_names: list[str] | None = None,
    channel: str | None = None,
    session_summary: str | None = None,
    workspace: Path | None = None,
    agent_override: SubagentDefinition | None = None,
    light_context: bool = False,
    memory_query: RecallQuery | None = None,
) -> str:
```

`build_messages()` constructs RecallQuery from `current_message`, `session_key`, `sender_id`, and resolved project path, then passes it to `build_system_prompt()`. Add `session_key: str | None = None`; AgentLoop must pass `session.key` in both existing call sites. For subagent messages, retain the parent user key from session metadata or `user:default`; never use literal sender `subagent`.

Mode implementation:

```python
if mode in {"legacy", "shadow"}:
    append_legacy_memory_sections()
if mode in {"shadow", "governed"} and memory_query is not None:
    result = self.memory.recall_structured(memory_query)
    self.last_recall_result = result
    if mode == "governed" and result.hits:
        parts.append((self._PRIORITY_MEMORY, self.memory.structured_recall.render_prompt(result)))
append_shared_policy_as_critical()
```

In governed mode, repository degraded health produces no facts and one concise diagnostic section; it must not fall back to legacy injection. Keep existing injection-budget behavior and add recall as `_PRIORITY_MEMORY`.

- [ ] **Step 4: Verify context and prompt-cache regressions**

```powershell
pytest tests/agent/test_context_structured_memory.py tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py -q
pytest tests/agent/test_loop_save_turn.py tests/agent/test_consolidator.py -q
ruff check miniunicorn/agent/context.py miniunicorn/agent/loop.py tests/agent/test_context_structured_memory.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit context integration**

```powershell
git add miniunicorn/agent/context.py miniunicorn/agent/loop.py tests/agent/test_context_structured_memory.py tests/agent/test_context_builder.py tests/agent/test_context_prompt_cache.py
git commit -m "feat(memory): inject governed deterministic recall"
```

---

### Task 9: Add user governance and migration commands

**Files:**

- Modify: `miniunicorn/command/builtin.py`
- Test: `tests/command/test_builtin_memory.py`

**Interfaces:**

- Consumes: MemoryStore façade, lifecycle, migrator, repository.
- Produces: `/memory-status`, `/memory-list`, `/memory-show`, `/memory-promote`, `/memory-revoke`, `/memory-correct`, `/memory-migrate`.

- [ ] **Step 1: Write parsing, output, conflict confirmation, and redaction tests**

```python
async def test_memory_status_reports_health_counts_and_migration(command_context):
    out = await cmd_memory_status(command_context)
    assert "Mode: shadow" in out.content
    assert "Health: healthy" in out.content
    assert "candidate:" in out.content
    assert "Migration:" in out.content


async def test_promote_same_rank_conflict_requires_replace(command_context, conflict):
    out = await cmd_memory_promote(command_context.with_args(conflict.candidate_id))
    assert f"--replace {conflict.active_id}" in out.content
    assert repository.get(conflict.active_id).status == MemoryStatus.ACTIVE


async def test_show_redacts_long_evidence_excerpt(command_context, record_with_long_excerpt):
    out = await cmd_memory_show(command_context.with_args(record_with_long_excerpt.id))
    assert len(extract_excerpt(out.content)) <= 201


async def test_correct_uses_explicit_correction_and_becomes_active(command_context):
    out = await cmd_memory_correct(command_context.with_args("MiniUnicorn|memory.retrieval.strategy|Do not use embeddings"))
    assert "active" in out.content
    assert "explicit_correction" in out.content
```

Test missing/invalid IDs, missing revoke reason, pipe parsing, mode legacy read-only restriction, migration dry-run/apply, list filter and 20-row limit, show revision chain, and command registration/palette.

- [ ] **Step 2: Run command tests and confirm failure**

```powershell
pytest tests/command/test_builtin_memory.py -q
```

Expected: imports or command dispatch assertions fail.

- [ ] **Step 3: Implement exact commands and safe output**

Register exact/prefix routes:

```python
router.exact("/memory-status", cmd_memory_status)
router.exact("/memory-list", cmd_memory_list)
router.prefix("/memory-list ", cmd_memory_list)
router.prefix("/memory-show ", cmd_memory_show)
router.prefix("/memory-promote ", cmd_memory_promote)
router.prefix("/memory-revoke ", cmd_memory_revoke)
router.prefix("/memory-correct ", cmd_memory_correct)
router.prefix("/memory-migrate ", cmd_memory_migrate)
```

All mutation handlers use actor `user`, require repository health, catch only typed memory errors, and return the typed error message without a traceback. `/memory-correct` parses exactly three non-empty `|`-separated fields, creates manual evidence referring to `command:<message_id>`, uses current project scope, and calls lifecycle ingest. `/memory-show` limits each evidence excerpt to 200 characters and never prints recall-audit query hashes.

- [ ] **Step 4: Verify commands and existing command suite**

```powershell
pytest tests/command/test_builtin_memory.py tests/command/test_builtin_dream.py tests/command/test_model_command.py -q
ruff check miniunicorn/command/builtin.py tests/command/test_builtin_memory.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Commit governance commands**

```powershell
git add miniunicorn/command/builtin.py tests/command/test_builtin_memory.py
git commit -m "feat(memory): add memory governance commands"
```

---

### Task 10: Add audit/hygiene integration, documentation, hard boundaries, and full verification

**Files:**

- Modify: `miniunicorn/agent/memory.py`
- Modify: `miniunicorn/skills/memory/SKILL.md`
- Modify: `docs/memory.md`
- Modify: `docs/configuration.md`
- Modify: `docs/chat-commands.md`
- Create: `tests/agent/test_structured_memory_boundary.py`
- Modify: relevant tests from Tasks 1–9 when final integration exposes a real mismatch

**Interfaces:**

- Consumes: all previous tasks.
- Produces: expiry from hygiene, optional redacted recall audit, current documentation, and a repository-wide no-vector boundary test.

- [ ] **Step 1: Write final boundary and hygiene tests**

```python
def test_main_memory_runtime_contains_no_vector_or_embedding_imports():
    forbidden_imports = ("faiss", "chromadb", "qdrant", "weaviate", "pinecone", "pgvector", "sentence_transformers")
    runtime_files = list(Path("miniunicorn/agent").glob("memory*.py")) + [Path("miniunicorn/agent/context.py")]
    text = "\n".join(path.read_text(encoding="utf-8").casefold() for path in runtime_files)
    for name in forbidden_imports:
        assert name not in text


def test_hygiene_expires_records_without_truncating_journal(store, due_active, old_candidate):
    original_lines = store.structured_repository.journal_path.read_text(encoding="utf-8").count("\n")
    result = store.run_memory_hygiene(now=fixed_now())
    assert result["structured_expired"] == 2
    assert store.structured_repository.get(due_active.id).status == MemoryStatus.EXPIRED
    assert store.structured_repository.get(old_candidate.id).status == MemoryStatus.EXPIRED
    assert store.structured_repository.journal_path.read_text(encoding="utf-8").count("\n") > original_lines


def test_recall_audit_omits_query_statement_and_excerpt(store, recall_query, recall_result):
    store.write_recall_audit(recall_query, recall_result)
    raw = store.structured_recall_audit_file.read_text(encoding="utf-8")
    assert recall_query.query_text not in raw
    assert recall_result.hits[0].record.statement not in raw
    assert recall_result.hits[0].record.evidence[0].excerpt not in raw
```

Boundary tests must also assert no vector/embedding configuration fields, no optional vector dependency in `pyproject.toml`, candidate exclusion in governed context, and no whole shared-fact injection.

- [ ] **Step 2: Run final focused tests and observe expected failures**

```powershell
pytest tests/agent/test_structured_memory_boundary.py -q
```

Expected: hygiene/audit/documentation integration assertions fail until Step 3.

- [ ] **Step 3: Complete hygiene, audit, and user documentation**

`run_memory_hygiene(now=None)` must call `structured_lifecycle.expire_due(now or utc_now())`, return `structured_expired`, and never truncate `journal.jsonl`. Optional recall audit writes canonical minimal JSON containing timestamp, hashed scope keys, hit IDs/scores/reason codes, filter counts and token count; rotate atomically to the last 1000 lines.

Update the memory skill so the agent references stable IDs, uses `/memory-correct` for explicit corrections, never edits journal/tags directly, and treats POLICY.md as policy-only. Update docs with the exact mode table, schema example, state machine, score table, migration commands, degraded recovery behavior, and the statement that main has no vector path.

- [ ] **Step 4: Run complete verification from a clean process**

Run in this exact order:

```powershell
ruff check miniunicorn tests
pytest tests/agent/test_memory_models.py tests/agent/test_memory_repository.py tests/agent/test_memory_lifecycle.py tests/agent/test_memory_recall.py tests/agent/test_memory_extraction.py tests/agent/test_memory_migration.py tests/agent/test_context_structured_memory.py tests/agent/test_dream_structured_memory.py tests/command/test_builtin_memory.py tests/config/test_structured_memory_config.py tests/agent/test_structured_memory_boundary.py -q
pytest -q
git diff --check
git status --short
```

Expected:

- Ruff exits 0.
- Focused C2 suite passes.
- Full pytest suite passes with no unexpected skips.
- `git diff --check` prints nothing.
- `git status --short` lists only the files changed by Tasks 1–10.

- [ ] **Step 5: Commit final integration and documentation**

```powershell
git add miniunicorn/agent/memory.py miniunicorn/skills/memory/SKILL.md docs/memory.md docs/configuration.md docs/chat-commands.md tests/agent/test_structured_memory_boundary.py
git commit -m "docs(memory): document governed structured memory"
```

---

## Implementation checkpoints

Stop for review after each checkpoint; do not begin the next checkpoint with failing tests.

| Checkpoint | Tasks | Reviewer must verify |
|---|---|---|
| A — Durable core | 1–2 | schema is strict; replay is fail-closed; multi-record transaction is atomic |
| B — Governance | 3 | source matrix, conflict ordering, correction priority, terminal behavior |
| C — Retrieval | 4 | exact scoring, scope isolation, no LLM/network path, Why output |
| D — Runtime shadow | 5–6 | default shadow leaves prompt behavior unchanged; Dream cursor cannot skip failed memory |
| E — Migration and context | 7–8 | dry-run is zero-write; apply is idempotent; governed mode removes global shared injection |
| F — User control | 9–10 | inspect/correct/revoke flow, redaction, docs, full tests, no vector boundary |

## Rollout procedure after merge

1. Start with `agents.defaults.structuredMemory.mode = "shadow"`.
2. Run `/memory-migrate --dry-run`; resolve every rejected item or explicitly leave it in legacy storage.
3. Run `/memory-migrate --apply` and verify `/memory-status` reports healthy and completed.
4. Exercise representative tasks and inspect shadow recall logs for scope, Tag and score correctness.
5. Run `/memory-list candidate`; promote or revoke high-impact unresolved candidates.
6. Move only true cross-session behavior rules into `memory/shared/POLICY.md`; keep facts in structured records.
7. Change mode to `governed`.
8. Verify representative user, project, session and shared queries display the expected ID and Why line.
9. Keep legacy files unchanged for at least one release so rollback to `shadow` or `legacy` remains immediate.

## Rollback conditions

Switch back to `shadow` immediately if any of these occurs:

- repository health becomes degraded;
- a candidate or terminal record appears in prompt context;
- a scope isolation test or production observation shows cross-user/project leakage;
- Dream advances a cursor after a failed structured write;
- active uniqueness is violated for a conflict key;
- recall output lacks a stable ID or Why line.

Rollback changes only the mode. Do not delete journal lines, migration manifests, Git history, or legacy files.
