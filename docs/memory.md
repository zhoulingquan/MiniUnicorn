# Memory in Erza

Erza has one always-on governed memory architecture. Conversation history is compressed into an archive, Dream extracts structured proposals, deterministic lifecycle rules decide their status, and only eligible active records are recalled into a prompt.

Models never edit durable fact files or the database directly.

## Data flow

```text
session messages
      |
      v
Consolidator -> memory/history.jsonl
                          |
reflections.jsonl -------+-> Dream proposals
                              |
                              v
                    deterministic lifecycle
                    candidate -> active/etc.
                              |
                              v
              memory/structured/memory.db
              (transactions, revisions, tags)
                              |
                              v
                 scoped lexical recall -> prompt
```

The SQLite database at `memory/structured/memory.db` is the single runtime fact store (design 2026-08-14, sections 7-8). The old `journal.jsonl` is legacy migration input only: it is read once by the automatic migrator when the database is first created, never written by the runtime, and left untouched forever after. The `audit/` directory is a derived, rebuildable JSONL export of the transaction table.

### Consolidator

When a conversation approaches the context limit, `Consolidator` summarizes the oldest safe slice and appends it to `memory/history.jsonl`. Each JSONL row has a stable cursor, timestamp, content, and available identity metadata. The archive is evidence for later extraction; it is not injected wholesale into normal prompts.

### Reflection

When enabled, Reflection asks for exact JSON in the form `{"lesson":"..."}`. Application code assigns a stable `rfl_<32 hex>` ID and durably appends the entry to `memory/reflections.jsonl`. Invalid or free-text model output is discarded.

### Dream

Dream runs on its configured cron schedule, on idle/backlog triggers, or via `/dream`. It reads unprocessed history and reflections, asks the model for strictly validated proposals, and passes those proposals to the deterministic lifecycle. It never writes `SOUL.md`, policy, or the structured database directly.

New facts normally begin as candidates. Promotion to active follows lifecycle rules or an explicit memory command. Candidates never enter model context.

## Storage

```text
workspace/
├── AGENTS.md                    # Project instructions loaded as bootstrap context
├── SOUL.md                      # Persona/style bootstrap context
├── notes.md                     # Agent scratchpad folded into history
└── memory/
    ├── history.jsonl            # Consolidated conversation evidence
    ├── reflections.jsonl        # Structured lessons consumed by Dream
    ├── .dream_cursor            # History consumption cursor
    ├── .reflections_cursor      # Reflection consumption cursor
    ├── structured/
    │   ├── memory.db            # SQLite fact database (single runtime source of truth)
    │   ├── memory.db-wal        # SQLite write-ahead log (runtime)
    │   ├── memory.db-shm        # SQLite shared memory (runtime)
    │   ├── tags.json            # Controlled tag catalog
    │   ├── recall-audit.jsonl   # Optional redacted local recall audit
    │   ├── audit/               # Rebuildable JSONL audit export (derived)
    │   ├── backups/             # Integrity-verified database snapshots
    │   └── recovery/            # Safety copies taken before a restore
    └── shared/
        └── POLICY.md             # Always injected only when customized
```

### SQLite tables

`memory.db` is created and owned by `erza/agent/memory_sqlite_schema.py` and accessed only through the repository (`erza/agent/memory_repository.py`):

- `memory_transactions` — one row per governed transaction; the append-only audit trail with checksums.
- `memory_revisions` — one row per record revision; `is_current = 1` marks the current revision of each record.
- `memory_tags` — tag membership per record revision.
- `memory_aliases` — normalized alias lookup per record revision.
- `memory_source_batches` — source-batch provenance per record.
- `memory_creation_keys` — the `(source_batch, content_hash) -> memory_id` idempotency map, written once at revision 1.
- `storage_meta` — schema version and the `audit_exported_seq` watermark.

No other code reads or writes the database; models never touch it with SQL.

### Legacy journal migration

On the first boot of a workspace that still has the old `journal.jsonl` (and no `memory.db`), the runtime imports the legacy transactions exactly once into a temporary database, verifies integrity, and installs it atomically as `memory.db` together with the manifest `storage-migration-v2.json`. The journal is never modified, rewritten, or truncated. A missing database after a completed migration fails closed (restore from a backup instead of re-importing). Once the database exists, the journal is never read at runtime again.

## Prompt rules

For a normal, non-light turn, Erza:

- loads `AGENTS.md` and `SOUL.md` as bootstrap instructions and identity;
- includes customized `memory/shared/POLICY.md`;
- recalls by the exact session, project, user, and shared scopes;
- injects only active hits within count and token budgets;
- never injects candidates or raw database contents.

Light/heartbeat contexts skip recall. Subagents inherit their parent session and user identity. If the repository or recall path is degraded, recall fails closed: no fact content is injected, only a content-free diagnostic.

Recall is deterministic and lexical. It routes by stable ID, subject, controlled tags and aliases, then scores source strength, scope, importance, and freshness. There is no embedding model, vector store, FAISS, or ChromaDB anywhere in the runtime: no vector indexes are persisted or queried, and no runtime configuration selects a vector backend.

## Configuration

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "enabled": true,
        "cron": "0 3 * * *",
        "modelOverride": null,
        "maxBatchSize": 20
      },
      "structuredMemory": {
        "recallTokenBudget": 2500,
        "maxRecallHits": 20,
        "lockTimeoutS": 5,
        "autoPromoteVerified": true,
        "minRepeatedEvidence": 2,
        "candidateTtlDays": 30,
        "recallAuditEnabled": false
      }
    }
  }
}
```

`structuredMemory` contains tuning only. There is no runtime mode switch, backend selector, or fallback store; supplying a `mode` field is a configuration error.

Dream settings:

- `enabled` registers scheduled Dream processing.
- `cron` is a five-field cron expression; the default runs nightly at 03:00 in the agent timezone.
- `modelOverride` optionally selects a separate extraction model.
- `maxBatchSize` limits history entries per Dream batch.

## Lifecycle and provenance

Every record is created at most once per `(source_batch, content_hash)` pair. The repository resolves that map from `memory_creation_keys`, so retries and concurrent workers converge on the same record. Dream evidence uses `history:<cursor>` and `reflection:<reflection_id>` references, and its batch ID derives from the exact evidence set.

Fact changes use append-only revisions with stable `mem_...` IDs. Promotion, revocation, correction, expiry, conflict blocking, and replacement are validated in application code inside the SQLite write transaction.

## Commands

| Command | What it does |
|---------|--------------|
| `/memory-status` | Show architecture, SQLite health, migration state, record counts, and audit lag |
| `/memory-list [status]` | List records, up to 20 |
| `/memory-show <id>` | Show revisions, evidence, and replacement chain |
| `/memory-promote <id> [--replace <active-id>]` | Promote a candidate |
| `/memory-revoke <id> <reason>` | Revoke a candidate or active record |
| `/memory-correct <subject>\|<slot>\|<statement>` | Create an explicit user correction |
| `/memory-log [<tx-id>]` | Show recent transactions (or one transaction's operations) |
| `/memory-backup` | Create an integrity-verified snapshot of the SQLite database |
| `/memory-restore <backup-id>` | Restore the database from one of its own verified backups (with a safety copy) |
| `/memory-export-audit [--rebuild]` | Export pending transactions to the JSONL audit, or rebuild it fully |
| `/dream` | Process pending history/reflections now |

## Degraded handling and recovery

The repository fails closed: if the database cannot be opened, fails the integrity check, or hits an unsupported schema version, `/memory-status` reports `degraded` and recalls/writes are disabled. Recovery is always via the database, never by hand-editing:

- `sqlite_open_error` / `integrity_error` — restore the latest `backups/` snapshot with `/memory-restore <backup-id>`.
- `migration_database_lost` — the migration completed before but `memory.db` is missing; restore from a backup (the legacy journal lacks post-migration facts, so it is not re-imported).
- audit export failures only grow `audit lag` — facts in `memory.db` never roll back; rebuild with `/memory-export-audit --rebuild` once the cause is fixed.

While degraded, recall injects a content-free diagnostic and no facts. Do not infer missing facts.

The audit export (`audit/*.jsonl`) lags the database by design: each export advances an `audit_exported_seq` watermark, and the lag shown by `/memory-status` is the number of transactions not yet materialized. Audit data never flows into the database; a rebuild replaces the audit from the transaction table.

With `recallAuditEnabled`, the recall audit contains only timestamps, hashed scope keys, hit IDs/scores/reason categories, counts, token totals, and degraded/error codes. Query text, statements, and evidence are never written; only the newest 1000 rows are retained.