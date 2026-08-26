# In-Chat Commands

These commands work inside chat channels and interactive agent sessions:

| Command | Description |
|---------|-------------|
| `/new` | Stop current task and start a new conversation |
| `/stop` | Stop the current task |
| `/restart` | Restart the bot |
| `/status` | Show bot status |
| `/model` | Show the current model and available model presets |
| `/model <preset>` | Switch the runtime model preset for future turns |
| `/dream` | Run Dream memory consolidation now |
| `/memory-status` | Show governed memory architecture, database health and record counts |
| `/memory-list [status]` | List structured memory records (candidate/active/superseded/revoked/expired) |
| `/memory-show <id>` | Show one record: all revisions, evidence and the replace chain |
| `/memory-promote <id> [--replace <active-id>]` | Promote a candidate to active; conflicts require `--replace` |
| `/memory-revoke <id> <reason>` | Revoke a candidate or active record with a reason |
| `/memory-correct <subject>\|<slot>\|<statement>` | Create an explicit user correction; all three fields must be non-empty |
| `/memory-log [<tx-id>]` | Show recent transactions (or the operations of one transaction) |
| `/memory-backup` | Create an integrity-verified snapshot of the SQLite memory database |
| `/memory-restore <backup-id>` | Restore the database from one of its own backups (with a safety copy) |
| `/memory-export-audit [--rebuild]` | Export pending transactions to the JSONL audit, or rebuild it fully |
| `/help` | Show available in-chat commands |

Structured memory mutations are governed transactions in `memory/structured/memory.db` (the legacy `journal.jsonl` is migration input only). Use `/memory-show <id>` to inspect evidence and the replacement chain; never edit the database or runtime files under `memory/structured/` by hand. If `/memory-status` reports degraded health, governed recall injects a diagnostic but no memory facts until the database is recovered from a backup.

## Model Presets

Use `/model` to inspect the current runtime model:

```text
/model
```

The response shows the current model, the current preset, and the available preset names. `default` is always available and represents the model settings from `agents.defaults.*`.

To switch presets for future turns:

```text
/model fast
/model deep
/model default
```

Preset names come from the top-level `modelPresets` config. Switching is runtime-only: it does not rewrite `config.json`, and an in-progress turn keeps using the model it started with. See [Configuration: Model presets](./configuration.md#model-presets) for setup details.

## Periodic Tasks

The gateway wakes up every 30 minutes and checks `HEARTBEAT.md` in your workspace (`~/.miniunicorn/workspace/HEARTBEAT.md`). If the file has tasks, the agent executes them and delivers results to your most recently active chat channel.

**Setup:** edit `~/.miniunicorn/workspace/HEARTBEAT.md` (created automatically by `miniunicorn onboard`):

```markdown
## Periodic Tasks

- [ ] Check weather forecast and send a summary
- [ ] Scan inbox for urgent emails
```

The agent can also manage this file itself — ask it to "add a periodic task" and it will update `HEARTBEAT.md` for you.

> **Note:** The gateway must be running (`miniunicorn gateway`) and you must have chatted with the bot at least once so it knows which channel to deliver to.
