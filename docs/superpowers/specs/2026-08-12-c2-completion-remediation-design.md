# C2 Governed Structured Memory Completion Remediation Design

**Status:** Approved for implementation

## 1. Goal

Close every gap found in the post-merge C2 audit without changing the intended
architecture: the checksummed JSONL journal remains the single source of truth,
all mutations pass through the repository and lifecycle, recall remains lexical
and deterministic, and no vector or embedding implementation is introduced.

## 2. Compatibility constraints

- Existing `journal.jsonl` records are never rewritten or truncated.
- The canonical controlled-tag path is `memory/structured/tags.json`, matching
  the original C2 design. Existing workspaces containing only `TAGS.json` are
  migrated by copying it to the canonical path without overwriting either file.
- The canonical migration manifest is
  `memory/structured/migration-v1.json`. A legacy manifest at
  `memory/migration-v1.json` is read when the canonical file is absent and is
  durably copied to the canonical location on the next apply.
- `legacy` and `shadow` prompt behavior stays unchanged. `governed` remains
  gated on a complete migration manifest.
- No new runtime dependency or configuration field for vectors, embeddings,
  vector stores, or knowledge graphs is allowed.

## 3. Durable repository invariants

While holding `journal.lock`, every append first synchronizes the in-process
repository with the durable journal. Validation runs against a transaction-local
copy of current state, so operations in the same transaction see preceding
operations without publishing partial indexes.

Validation must enforce:

1. checksum, schema and controlled tags;
2. exact expected revisions and revision increments;
3. legal status transitions;
4. `validate_same_status_revision()` for same-status revisions;
5. terminal records cannot be revised;
6. after the complete transaction, every `conflict_key` has at most one active
   record.

An append, flush, fsync or post-write synchronization error marks the repository
degraded. The repository must not accept another write until an explicit rebuild
has replayed the durable journal successfully. A writer that discovers an
external append synchronizes it before validating its own expected revisions.

## 4. Lifecycle and evidence

`MemoryLifecycleError` derives from the project's typed `MemoryError`.
Idempotency is keyed by source batch plus content hash across both candidate and
active records:

- an existing candidate resumes post-ingest promotion/conflict processing;
- an already active identical result returns success without a new candidate;
- a candidate-create success followed by promotion failure is retryable;
- an active same-content merge changes only evidence, derived-from and time.

Verified file evidence is checked against the referenced workspace file and
excerpt when the reference is a resolvable file locator. Git and tool evidence
cannot be independently re-read by this layer and therefore require a digest
that matches the supplied excerpt; they remain auditable but do not gain a
stronger guarantee than their captured source permits.

Dream history evidence uses the persisted entry cursor (`_line`) in references,
not its position within the current batch.

## 5. Migration

The manifest is written through a sibling temporary file, flushed, fsynced and
atomically replaced. Progress is saved after every successfully committed item,
so a process crash cannot lose up to 99 completed imports. `completed_at` is set
only when scanning produced no issues and all importable items succeeded.

Slots are deterministic and specific: heading/tag plus a stable digest of the
normalized subject and statement. This prevents unrelated legacy bullets from
colliding. Procedural migration does not fabricate two evidence references to
the same source; legacy procedural facts remain candidates unless independent
evidence genuinely exists. Existing manifest entries remain valid and are not
re-imported.

## 6. Scope propagation and recall

Every normal turn constructs exact session, project, user and shared scopes:

- `session:` + the effective `Session.key`;
- current project scope key;
- `user:` + the original sender identity, or `user:default`;
- `shared:*`.

Subagent follow-ups inherit the parent turn's saved user identity and session
scope; the literal sender `subagent` never creates a new user scope. Context
APIs accept the effective session key and resolved user key explicitly.

Dream proposals may use session, project, user or shared hints. Their hint is
mapped only to one of the exact scopes supplied for the batch. Recall Why lines
report capped score contributions accurately, and token budgeting includes the
rendered section header and separators.

If governed recall is degraded, context contains the policy plus a concise
diagnostic and no structured fact records.

## 7. Hygiene and recall audit

`run_memory_hygiene(now=None)` calls lifecycle `expire_due()` whenever the
structured stack exists and returns `structured_expired`. It appends expiry
revisions and never truncates the structured journal.

When `recallAuditEnabled` is true, both shadow and governed recall append a
canonical JSON line containing only timestamp, SHA-256 hashes of scope keys,
hit IDs/scores/reason codes, counters and token count. It never contains query
text, statements or evidence. The audit log is atomically rotated to its most
recent 1000 lines and is excluded from Git tracking.

## 8. Commands and documentation

All mutation commands catch the project's typed memory errors. `/memory-correct`
requires three non-empty fields and records `command:<message_id>` evidence.
Status output reports canonical migration state and degraded diagnostics.

Configuration, memory behavior, management commands, recovery steps, scope
semantics, migration compatibility, audit redaction and the no-vector boundary
are documented. The memory skill instructs the agent to use stable IDs and
commands rather than editing governed files directly.

## 9. Acceptance

- Regression tests reproduce every audit finding before its fix.
- A repository-wide boundary test covers configuration, dependencies, runtime
  imports, candidate exclusion and absence of whole-file governed injection.
- Ruff passes for `erza` and `tests`.
- The focused C2 suite passes.
- The full test suite completes and passes; if the repository contains a known
  hanging test, it is identified with a per-file run rather than reported as a
  pass.
- `git diff --check` passes and the worktree contains only intended changes.
