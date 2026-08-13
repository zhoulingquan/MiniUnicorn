# Memory in MiniUnicorn

MiniUnicorn has one always-on governed memory architecture. Conversation history is compressed into an archive, Dream extracts structured proposals, deterministic lifecycle rules decide their status, and only eligible active records are recalled into a prompt.

Models never edit durable fact files or the journal directly.

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
                  structured/journal.jsonl
                              |
                              v
                 scoped lexical recall -> prompt
```

### Consolidator

When a conversation approaches the context limit, `Consolidator` summarizes the oldest safe slice and appends it to `memory/history.jsonl`. Each JSONL row has a stable cursor, timestamp, content, and available identity metadata. The archive is evidence for later extraction; it is not injected wholesale into normal prompts.

### Reflection

When enabled, Reflection asks for exact JSON in the form `{"lesson":"..."}`. Application code assigns a stable `rfl_<32 hex>` ID and durably appends the entry to `memory/reflections.jsonl`. Invalid or free-text model output is discarded.

### Dream

Dream runs on its configured cron schedule, on idle/backlog triggers, or via `/dream`. It reads unprocessed history and reflections, asks the model for strictly validated proposals, and passes those proposals to the deterministic lifecycle. It does not edit `USER.md`, `MEMORY.md`, `MEMORY_SHARED.md`, `SOUL.md`, or the structured journal directly.

New facts normally begin as candidates. Promotion to active follows lifecycle rules or an explicit memory command. Candidates never enter model context.

## Storage

```text
workspace/
├── AGENTS.md                    # Project instructions loaded as bootstrap context
├── SOUL.md                      # Persona/style bootstrap context
├── USER.md                      # Inert legacy import source
├── notes.md                     # Agent scratchpad folded into history
└── memory/
    ├── history.jsonl            # Consolidated conversation evidence
    ├── reflections.jsonl        # Structured lessons consumed by Dream
    ├── .dream_cursor            # History consumption cursor
    ├── .reflections_cursor      # Reflection consumption cursor
    ├── structured/
    │   ├── journal.jsonl        # Append-only transactions; source of truth
    │   ├── tags.json            # Controlled tag catalog
    │   ├── migration-v1.json    # Optional legacy-import progress
    │   └── recall-audit.jsonl   # Optional redacted local audit
    └── shared/
        └── POLICY.md             # Always injected only when customized
```

Older files such as `memory/MEMORY.md`, `memory/shared/MEMORY_SHARED.md`, `episodic.jsonl`, and `procedural.jsonl` remain inert import sources. Normal runtime does not inject or mutate them.

## Prompt rules

For a normal, non-light turn, MiniUnicorn:

- loads bootstrap identity/instructions while excluding `USER.md`;
- includes customized `memory/shared/POLICY.md`;
- recalls by the exact session, project, user, and shared scopes;
- injects only active hits within count and token budgets;
- never injects candidates or complete legacy files.

Light/heartbeat contexts skip recall. Subagents inherit their parent session and user identity. If the repository or recall path is degraded, recall fails closed: no fact content is injected, only a content-free diagnostic.

Recall is deterministic and lexical. It routes by stable ID, subject, controlled tags and aliases, then scores source strength, scope, importance, and freshness. C2 has no embedding model or vector database.

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

`structuredMemory` contains tuning only. There is no runtime mode switch; supplying a `mode` field is a configuration error.

Dream settings:

- `enabled` registers scheduled Dream processing.
- `cron` is a five-field cron expression; the default runs nightly at 03:00 in the agent timezone.
- `modelOverride` optionally selects a separate extraction model.
- `maxBatchSize` limits history entries per Dream batch.

## Lifecycle and provenance

Every record is created at most once per `(source_batch, content_hash)` pair. The repository rebuilds that map from the journal, so retries and concurrent workers converge on the same record. Dream evidence uses `history:<cursor>` and `reflection:<reflection_id>` references, and its batch ID derives from the exact evidence set.

Fact changes use append-only revisions with stable `mem_...` IDs. Promotion, revocation, correction, expiry, conflict blocking, and replacement are validated in application code under the repository lock.

## Optional legacy import

Legacy data is never imported automatically and never gates startup.

- `/memory-migrate --dry-run` scans legacy sources and performs zero writes.
- `/memory-migrate --apply` imports through the lifecycle without rewriting sources.
- Apply is file-locked, resumable, idempotent, and fail-closed.
- `memory/structured/migration-v1.json` records progress; it is import state, not a feature flag.

## Commands

| Command | What it does |
|---------|--------------|
| `/memory-status` | Show architecture, health, record counts, and optional import state |
| `/memory-list [status]` | List records, up to 20 |
| `/memory-show <id>` | Show revisions, evidence, and replacement chain |
| `/memory-promote <id> [--replace <active-id>]` | Promote a candidate |
| `/memory-revoke <id> <reason>` | Revoke a candidate or active record |
| `/memory-correct <subject>\|<slot>\|<statement>` | Create an explicit user correction |
| `/memory-migrate [--dry-run\|--apply]` | Optionally import legacy files |
| `/dream` | Process pending history/reflections now |
| `/dream-log [sha]` | Inspect a recorded Dream journal change |
| `/dream-restore [sha]` | Inspect or restore a recorded memory version |

If `/memory-status` reports `degraded`, do not edit the journal or infer missing facts. Restore the damaged tracked file from a known-good backup/version, restart, and check status again.

With `recallAuditEnabled`, the audit contains only timestamps, hashed scope keys, hit IDs/scores/reason categories, counts, token totals, and degraded/error codes. Query text, statements, and evidence are never written; only the newest 1000 rows are retained.
