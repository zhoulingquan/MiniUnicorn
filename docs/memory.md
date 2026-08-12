# Memory in MiniUnicorn

MiniUnicorn's memory is built on a simple belief: memory should feel alive, but it should not feel chaotic.

Good memory is not a pile of notes. It is a quiet system of attention. It notices what is worth keeping, lets go of what no longer needs the spotlight, and turns lived experience into something calm, durable, and useful.

That is the shape of memory in MiniUnicorn.

## The Design

MiniUnicorn does not treat memory as one giant file.

It separates memory into layers, because different kinds of remembering deserve different tools:

- `session.messages` holds the living short-term conversation.
- `memory/history.jsonl` is the running archive of compressed past turns.
- `SOUL.md`, `USER.md`, and `memory/MEMORY.md` are the durable knowledge files.
- `GitStore` records how those durable files change over time.

This keeps the system light in the moment, but reflective over time.

## The Flow

Memory moves through MiniUnicorn in two stages.

### Stage 1: Consolidator

When a conversation grows large enough to pressure the context window, MiniUnicorn does not try to carry every old message forever.

Instead, the `Consolidator` summarizes the oldest safe slice of the conversation and appends that summary to `memory/history.jsonl`.

This file is:

- append-only
- cursor-based
- optimized for machine consumption first, human inspection second

Each line is a JSON object:

```json
{"cursor": 42, "timestamp": "2026-04-03 00:02", "content": "- User prefers dark mode\n- Decided to use PostgreSQL"}
```

It is not the final memory. It is the material from which final memory is shaped.

### Stage 2: Dream

`Dream` is the slower, more thoughtful layer. It runs on a cron schedule by default and can also be triggered manually.

Dream reads:

- new entries from `memory/history.jsonl`
- the current `SOUL.md`
- the current `USER.md`
- the current `memory/MEMORY.md`

Then it works in two phases:

1. It studies what is new and what is already known.
2. It edits the long-term files surgically, not by rewriting everything, but by making the smallest honest change that keeps memory coherent.

This is why MiniUnicorn's memory is not just archival. It is interpretive.

## The Files

```text
workspace/
├── SOUL.md              # The bot's long-term voice and communication style
├── USER.md              # Stable knowledge about the user
├── notes.md             # The agent's persistent working scratchpad
└── memory/
    ├── MEMORY.md        # Project facts, decisions, and durable context
    ├── history.jsonl    # Append-only history summaries
    ├── episodic.jsonl   # Timestamped events retained across sessions
    ├── procedural.jsonl # Durable lessons distilled from experience
    ├── reflections.jsonl # Reflection input consumed by Dream
    ├── shared/
    │   ├── MEMORY_SHARED.md
    │   └── procedural_shared.jsonl
    ├── .cursor          # Consolidator write cursor
    ├── .dream_cursor    # Dream consumption cursor
    └── .git/            # Version history for long-term memory files
```

These files play different roles:

- `SOUL.md` remembers how MiniUnicorn should sound.
- `USER.md` remembers who the user is and what they prefer.
- `MEMORY.md` remembers what remains true about the work itself.
- `history.jsonl` remembers what happened on the way there.
- `episodic.jsonl` records events, while `procedural.jsonl` retains reusable lessons.
- `reflections.jsonl` feeds compact lessons into Dream for later consolidation.
- `shared/` holds facts and procedures that apply across sessions.
- `notes.md` is the agent's working scratchpad and is folded into history during consolidation.

## Why `history.jsonl`

The old `HISTORY.md` format was pleasant for casual reading, but it was too fragile as an operational substrate.

`history.jsonl` gives MiniUnicorn:

- stable incremental cursors
- safer machine parsing
- easier batching
- cleaner migration and compaction
- a better boundary between raw history and curated knowledge

You can still search it with familiar tools:

```bash
# grep
grep -i "keyword" memory/history.jsonl

# jq
cat memory/history.jsonl | jq -r 'select(.content | test("keyword"; "i")) | .content' | tail -20

# Python
python -c "import json; [print(json.loads(l).get('content','')) for l in open('memory/history.jsonl','r',encoding='utf-8') if l.strip() and 'keyword' in l.lower()][-20:]"
```

The difference is philosophical as much as technical:

- `history.jsonl` is for structure
- `SOUL.md`, `USER.md`, and `MEMORY.md` are for meaning

## Commands

Memory is not hidden behind the curtain. Users can inspect and guide it.

| Command | What it does |
|---------|--------------|
| `/dream` | Run Dream immediately |
| `/dream-log` | Show the latest Dream memory change |
| `/dream-log <sha>` | Show a specific Dream change |
| `/dream-restore` | List recent Dream memory versions |
| `/dream-restore <sha>` | Restore memory to the state before a specific change |

These commands exist for a reason: automatic memory is powerful, but users should always retain the right to inspect, understand, and restore it.

## Versioned Memory

After Dream changes long-term memory files, MiniUnicorn can record that change with `GitStore`.

This gives memory a history of its own:

- you can inspect what changed
- you can compare versions
- you can restore a previous state

That turns memory from a silent mutation into an auditable process.

## Configuration

Dream is configured under `agents.defaults.dream`:

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "intervalH": 2,
        "modelOverride": null,
        "maxBatchSize": 20,
        "maxIterations": 10
      }
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| `intervalH` | How often Dream runs, in hours |
| `modelOverride` | Optional Dream-specific model override |
| `maxBatchSize` | How many history entries Dream processes per run |
| `maxIterations` | The tool budget for Dream's editing phase |

In practical terms:

- `modelOverride: null` means Dream uses the same model as the main agent. Set it only if you want Dream to run on a different model.
- `maxBatchSize` controls how many new `history.jsonl` entries Dream consumes in one run. Larger batches catch up faster; smaller batches are lighter and steadier.
- `maxIterations` limits how many read/edit steps Dream can take while updating `SOUL.md`, `USER.md`, and `MEMORY.md`. It is a safety budget, not a quality score.
- `intervalH` is the normal way to configure Dream. Internally it runs as an `every` schedule, not as a cron expression.

Legacy note:

- Older source-based configs may still contain `dream.cron`. MiniUnicorn continues to honor it for backward compatibility, but new configs should use `intervalH`.
- Older source-based configs may still contain `dream.model`. MiniUnicorn continues to honor it for backward compatibility, but new configs should use `modelOverride`.

## Governed Structured Memory

Beyond the Markdown/JSONL layers above, MiniUnicorn offers a governed, journal-backed memory subsystem. In this mode, model output never edits memory files directly — Dream extracts candidate records, a deterministic lifecycle decides promotion, and a journal is the single source of truth.

### Modes

Set `agents.defaults.structuredMemory.mode` to one of:

| Mode | Behavior |
|------|----------|
| `legacy` | Markdown/JSONL files injected as before; structured stack is not used |
| `shadow` | Structured stack runs in parallel for validation, but context injection is unchanged; easy, safe rollback |
| `governed` | Context injects only POLICY + recall results; legacy files are no longer injected wholesale |

```json
{
  "agents": {
    "defaults": {
      "structuredMemory": {
        "mode": "shadow",
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

### Storage

```text
memory/
├── structured/
│   ├── journal.jsonl       # Append-only transactions; the single source of truth
│   ├── tags.json           # Controlled tag catalog (user.identity, project.decision, ...)
│   ├── migration-v1.json  # Crash-safe legacy migration progress
│   └── recall-audit.jsonl # Optional redacted local audit; not Git-tracked
├── shared/POLICY.md    # Always-injected policy (only if customized)
└── ...                 # Legacy files remain available for rollback
```

Candidate facts exist in the journal with `status=candidate` and never enter the model context. Promotion to `active` follows deterministic rules (verified source evidence, confidence threshold) or an explicit user action.

Recall is deterministic and lexical: it routes by stable ID, subject, controlled tags and aliases, then scores source strength, exact scope, importance and freshness. Normal turns search the exact session, current project, original user (or `user:default`) and `shared:*`; subagents inherit the parent session/user identity. No embedding model or vector database is used in C2.

### Idempotency and provenance

Every record is created at most once per `(source_batch, content_hash)` pair: the repository rebuilds this creation map from the journal itself (revision-1 transactions), so retries after a crash, a manual `/memory-promote`, an identical merge, or concurrent workers all resolve to the same record without any second ledger. Dream evidence refs are stable: `history:<cursor>` for the visible history rows and `reflection:<reflection_id>` for reflections, where the reflection ID is generated by the application (`rfl_<32 hex>`) and survives file pruning or line shifts; legacy reflection entries receive a deterministic `rfl_legacy_<hash>` ref. Dream batch IDs are derived from the exact set of evidence refs, so re-running with identical input reuses the same batch. Candidate same-status revisions may only add `blocked_by` conflicts, never remove or replace them.

### Migration

`/memory-migrate --dry-run` scans legacy files (`USER.md`, `MEMORY.md`, `procedural.jsonl`, shared files, `episodic.jsonl`) and reports what would be imported — with zero writes. `/memory-migrate --apply` imports atomically through the lifecycle, is idempotent, and never rewrites legacy files. The whole apply — source scan, state load, per-item import, progress saves and the final `completed_at` — runs under the `memory/structured/migration-v1.lock` file lock, so multiple workers or processes never corrupt the manifest; a competing apply either waits or reports a lock timeout, and a timed-out apply writes nothing. An interrupted apply resumes item-by-item from `memory/structured/migration-v1.json` (each item is saved with a unique sibling temp file plus fsync before `os.replace`). When a canonical manifest exists it always wins; the old `memory/migration-v1.json` location is still read when no canonical manifest exists, but a corrupt canonical manifest fails closed instead of trusting the legacy file. `completed_at` is written only after a clean scan with no failed items or unresolved issues. Governed mode refuses to start until migration is complete.

If `/memory-status` reports `degraded`, no governed facts are injected. Keep the append-only journal unchanged, inspect the reported code and last valid line, restore the damaged file from backup or memory Git history, then restart and re-run `/memory-status`. Switching temporarily to `shadow` or `legacy` preserves a rollback path while legacy files remain untouched.

With `recallAuditEnabled`, `recall-audit.jsonl` records only the timestamp, SHA-256 scope-key hashes, hit IDs/scores/reason categories, candidate and filtered counts, budget exclusions, token totals and any degraded/error codes. Query text, statements and evidence are never written, and only the most recent 1000 lines are retained.

### Commands

| Command | What it does |
|---------|--------------|
| `/memory-status` | Mode, journal health, status counts, migration state |
| `/memory-list [status]` | List records (ID/status/kind/scope/source/statement, max 20) |
| `/memory-show <id>` | All revisions, evidence and the replace chain |
| `/memory-promote <id> [--replace <active-id>]` | Promote a candidate; conflicts require an explicit `--replace` |
| `/memory-revoke <id> <reason>` | Revoke a candidate/active record |
| `/memory-correct <subject>\|<slot>\|<statement>` | Create an explicit user correction; all three fields are required and evidence uses the inbound message ID |
| `/memory-migrate [--dry-run\|--apply]` | Import legacy memory files |

Evidence excerpts in command output are truncated to 200 characters.

## In Practice

What this means in daily use is simple:

- conversations can stay fast without carrying infinite context
- durable facts can become clearer over time instead of noisier
- the user can inspect and restore memory when needed

Memory should not feel like a dump. It should feel like continuity.

That is what this design is trying to protect.
