# Embedding Memory Production Design

**Date:** 2026-08-04

**Status:** Approved for implementation planning

## 1. Goal

Turn the current local embedding prototype into a default-on, observable, rebuildable memory-retrieval feature for a single-process MiniUnicorn runtime.

The completed feature must:

- run `BAAI/bge-small-zh-v1.5` locally on CPU with 512-dimensional vectors;
- download and verify the model during the recommended installation flow;
- keep chat usable when dependencies, download, model load, inference, or the vector database fail;
- treat readable workspace files as the authoritative memory source;
- treat `memory/memory.db` as a disposable derived index;
- rebuild the complete index from authoritative files without losing memory;
- avoid repeatedly sending full long-term memory files to the chat LLM;
- record the source and revision of every indexed item;
- update conflicting explicit memories only after user confirmation;
- expose simple status and repair actions in the WebUI and CLI.

## 2. Non-goals

This work does not add:

- the three-Worker runtime or any multi-process coordination;
- a remote embedding API;
- GPU, CUDA, PyTorch, `sentence-transformers`, or an external vector database;
- a document knowledge-base ingestion feature;
- permanent deletion of memories or Git history rewriting;
- automatic modification of an existing memory after an unconfirmed semantic-conflict judgment;
- a full monitoring dashboard.

## 3. Product decisions

### 3.1 Distribution

The base Python package remains lightweight. The recommended MiniUnicorn installation installs the existing `vector` extra, including:

- `fastembed>=0.8.0,<0.9.0`;
- `sqlite-vec`.

The model is not embedded in the Python wheel or source repository. The recommended setup flow downloads `BAAI/bge-small-zh-v1.5` pinned to Hugging Face revision `7999e1d3359715c523056ef9478215996d62a620` into `~/.miniunicorn/models/`, validates its manifest, and runs a real 512-dimensional CPU self-test. The implementation may use FastEmbed's supported-model artifact mapping, but the local manifest must retain this upstream model revision and the SHA-256 of every runtime file actually loaded.

Download failure does not fail MiniUnicorn installation. It leaves embedding in a visible `not_ready` state, preserves normal chat, and offers retry through the CLI and WebUI.

### 3.2 Default behavior

`agents.defaults.vectorRecall` defaults to `true`. When model and index readiness checks pass, vector recall is active. When they do not pass, MiniUnicorn uses the bounded file-memory fallback and reports why vector recall is unavailable.

### 3.3 Source of truth

The authoritative memory remains in workspace files:

- `SOUL.md`;
- `USER.md`;
- `memory/MEMORY.md`;
- `memory/history.jsonl`;
- `memory/episodic.jsonl`;
- `memory/procedural.jsonl`;
- `memory/explicit.jsonl`.

`memory/memory.db` duplicates the searchable text, provenance metadata, and vectors for retrieval. It is not authoritative and may always be rebuilt.

## 4. Runtime architecture

The implementation is split into focused components.

### 4.1 `EmbeddingModelManager`

Owns model lifecycle, not inference policy. It provides:

- a pinned model manifest;
- local model-path resolution under `~/.miniunicorn/models/`;
- dependency detection;
- resumable or retryable setup;
- SHA-256 and model-revision validation;
- a real CPU self-test;
- normalized status values and actionable error details.

It never installs Python packages from inside a running gateway. The official installer installs dependencies; `miniunicorn embedding setup` downloads and validates model assets.

### 4.2 `LocalEmbeddingProvider`

Remains the single inference adapter. It consumes a validated local model path from `EmbeddingModelManager`, loads lazily, uses `asyncio.to_thread`, normalizes output, rejects non-finite or wrong-dimensional vectors, and returns an explicit result or typed failure to its service caller.

The provider continues to use CPU. Chat-provider switching never replaces or reconfigures it.

### 4.3 `MemorySourceCatalog`

Reads authoritative files and emits normalized `MemorySourceRecord` values. Each record contains:

```python
@dataclass(frozen=True)
class MemorySourceRecord:
    source_id: str
    source_type: str
    source_file: str
    source_revision: str
    content_hash: str
    text: str
    importance: float
    active: bool = True
    metadata: dict[str, object] = field(default_factory=dict)
```

Stable source identities are:

- `history:<cursor>` for `history.jsonl`;
- `procedural:<cursor>` for `procedural.jsonl`;
- `episodic:<event_id>` for new episodic records;
- `episodic:legacy:<line>:<hash-prefix>` for legacy episodic records;
- `explicit:<memory_id>` for the effective explicit-memory revision;
- `user:<heading-path>:<ordinal>` for `USER.md` chunks;
- `memory:<heading-path>:<ordinal>` for `MEMORY.md` chunks.

`SOUL.md` is deliberately excluded from vector recall. It is a bounded instruction file and remains part of every chat system prompt.

Markdown records are split on heading boundaries and then into bounded chunks. JSONL records preserve their existing cursor or event identity. Invalid records are reported in source status and skipped without preventing other sources from indexing.

### 4.4 `VectorIndexManager`

Owns `memory/memory.db`. Its schema records:

- schema version;
- model ID and pinned model revision;
- vector dimension;
- `source_id` with a unique constraint;
- source type, file, revision, and content hash;
- active flag, text copy, importance, metadata, and timestamps;
- sqlite-vec row linked one-to-one to the source row.

Incremental reconciliation compares the source revision and content hash. Unchanged records are not re-embedded. Changed records are re-embedded and upserted. Records no longer active in the authoritative effective view are excluded from retrieval without erasing the source history.

A full rebuild writes `memory.db.rebuilding`. After all records are embedded, the manager validates model fingerprint, row parity, vector dimension, finite values, and a sample query. Only then does it atomically replace `memory.db`. A pre-existing incompatible database is renamed to a timestamped local backup after the replacement succeeds; it is never deleted before success.

### 4.5 `MemoryRecallService`

Runs before each chat-provider call:

1. locally embed the current user query;
2. search `memory.db` for an over-fetched candidate set;
3. discard candidates below the configured similarity floor;
4. deduplicate identical content and current core-memory content;
5. rank primarily by semantic similarity, with bounded importance and recency adjustments;
6. select at most five records under a dedicated recall token budget;
7. format every result with source identity for tracing;
8. return a typed fallback reason when recall is unavailable.

No local file read or SQLite query consumes chat-LLM tokens. Tokens are consumed only for the bounded content actually inserted into the prompt.

### 4.6 Prompt memory policy

Every chat call includes:

- bounded `SOUL.md` instructions;
- an `Always` core extracted from `USER.md` and `MEMORY.md` under a fixed token budget;
- zero to five relevant records from `MemoryRecallService`.

`USER.md` and `MEMORY.md` are also indexed, but their full content is not sent on every turn. Existing files without an `Always` heading remain backward compatible: the builder takes a bounded leading core and indexes the remainder. The first Dream update after rollout may normalize the headings, but startup does not rewrite user files.

If embedding is unavailable, the fallback inserts a bounded combination of `USER.md` and `MEMORY.md`; it never injects unbounded files.

## 5. Explicit memory and conflict updates

### 5.1 Capture

Unambiguous commands and phrases create explicit memory proposals:

- `/remember <text>` and `/记住 <text>`;
- Chinese imperative phrases such as `记住`, `请记住`, `帮我记一下`, and `以后请一直`;
- English imperative phrases such as `remember that`, `please remember`, and `save this to memory`.

Negated or quoted phrases do not create a memory. Ambiguous natural language requests receive a confirmation question.

### 5.2 Append-only revision journal

`memory/explicit.jsonl` is append-only. A record contains:

```python
@dataclass(frozen=True)
class ExplicitMemoryRevision:
    memory_id: str
    revision: int
    raw_text: str
    normalized_fact: str
    scope: str | None
    created_at: str
    supersedes_revision: int | None = None
```

The latest valid revision for a `memory_id` is active. Older revisions remain inspectable and restorable but are excluded from ordinary recall.

### 5.3 Conflict flow

Before appending a new active fact:

1. vector retrieval finds semantically similar active candidates from `USER.md`, `MEMORY.md`, and `explicit.jsonl`;
2. a structured LLM judgment labels the relationship `duplicate`, `supplement`, `conflict`, or `unrelated`;
3. the LLM may propose wording but has no write authority;
4. a conflict response displays old text, new text, source, and timestamp;
5. the user selects `update`, `keep_existing`, or `both_with_scope`;
6. only `update` or a completed scoped clarification appends a new active revision.

MiniUnicorn never automatically overwrites a potentially conflicting memory.

## 6. Status and repair surfaces

### 6.1 Shared status contract

The CLI and WebUI consume one backend contract with four summaries:

- `model`: `not_downloaded`, `downloading`, `verifying`, `ready`, `corrupt`, or `failed`;
- `index`: `missing`, `building`, `ready`, `stale`, `corrupt`, or `failed`;
- `sources`: discovered, indexed, pending, stale, invalid, and inactive counts;
- `recall`: configured, active, fallback reason, last self-test, and last latency.

Detailed status additionally exposes model ID, revision, dimension, cache path, model bytes, index path, index bytes, last rebuild time, last error code, and an actionable message. It never exposes vector values or secrets.

### 6.2 CLI

The CLI adds:

```text
miniunicorn embedding setup
miniunicorn embedding status
miniunicorn embedding verify
miniunicorn embedding rebuild
```

Commands return non-zero on the requested operation's failure while leaving chat availability unchanged.

### 6.3 WebUI

The settings UI adds a Memory & Embedding section with four simple status cards. Expandable details show provenance and diagnostics. Supported actions are:

- retry model download;
- verify model;
- rebuild index;
- search memory and inspect the source file plus record identity.

Long-running actions report progress and cannot be started twice concurrently.

## 7. Error handling

The feature is fail-open for chat and fail-closed for vector use.

- Missing dependencies: show the recommended installation command; use bounded file fallback.
- Download failure: retain partial download only when the downloader can validate and resume it; otherwise quarantine it; allow retry.
- Model corruption or hash mismatch: do not load; report `corrupt`; allow verified re-download.
- Inference failure or non-finite output: do not write vectors; record a rate-limited error; use fallback for that turn.
- Missing or corrupt database: start a guarded background rebuild; use fallback until validation succeeds.
- Source parse failure: skip the invalid source record and surface its path and line without blocking valid sources.
- Disk-full or permission failure: stop the mutating operation, preserve authoritative files and the previous valid index, and show an actionable error.
- Application shutdown: cancel setup or rebuild cleanly; never swap an unvalidated database.

## 8. Migration and rollback

Existing `memory.db` files are inspected by fingerprint. A compatible database is incrementally reconciled. An incompatible database remains untouched until a complete replacement validates successfully, then becomes a timestamped backup.

Existing configuration values remain accepted. The new default for omitted `vectorRecall` is `true`; an explicit `false` continues to disable model setup at runtime and bypass recall.

Rollback is always available by setting:

```json
{"agents":{"defaults":{"vectorRecall":false}}}
```

Rollback does not alter authoritative memory files.

## 9. Security and privacy

- Embedding inference is local CPU only.
- Model assets are pinned and hash-verified before use.
- Model and index paths must resolve under the configured MiniUnicorn data directory or Agent workspace.
- Source paths returned by the API are workspace-relative; absolute paths remain in expandable local diagnostics only.
- No status endpoint returns raw vectors, credentials, or unrelated file contents.
- Conflict classification sends only the proposed fact and the small candidate set already eligible for the chat LLM; it does not send the full memory archive.

## 10. Verification gates

Release evidence must prove all of the following from a clean workspace:

1. Recommended installation includes FastEmbed and sqlite-vec.
2. Setup downloads or resolves the pinned model, verifies it, and produces one finite normalized 512-dimensional Chinese embedding on CPU.
3. Download failure does not prevent ordinary non-vector chat construction.
4. Two Chinese memories persist in `memory.db` across close and reopen.
5. A relevant Chinese query ranks the expected memory first above the configured threshold.
6. Re-indexing an unchanged source does not add a duplicate row.
7. Editing a source updates the existing source row and vector.
8. Removing `memory.db` triggers a complete, atomic rebuild from source files.
9. A killed or cancelled rebuild leaves the previous valid index usable.
10. A model or dimension mismatch cannot serve stale vectors.
11. `SOUL.md` remains bounded and always included; full `USER.md` and `MEMORY.md` are not injected on every turn.
12. Recall inserts no more than five unique records and remains inside its token budget.
13. Every result exposes source file, source ID, revision, and synchronization state.
14. Explicit-memory duplicates do not create new identities.
15. Potential conflicts require user confirmation before a new active revision is written.
16. Old explicit revisions remain inspectable but are absent from normal recall.
17. CLI and WebUI report the same model, index, source, and recall state.
18. Windows packaging, Python tests, frontend tests, lint, type-check, and production build pass.

## 11. Implementation boundary

All work is performed on the embedding-only history rooted at commit `f0adcf63`. The implementation must not cherry-pick three-Worker runtime modules, depend on runtime lease or journaling types, or move vector-memory authority into the later multi-process runtime store.
