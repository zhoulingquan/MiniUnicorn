# Task 9 Report: 增加数据库日志、备份、恢复和审计命令

**Status: DONE**
**Commit:** `e354c1ab` `feat(memory): add sqlite audit backup and restore commands`
**Branch:** `feature/sqlite-memory-storage` (worktree `C:\MyProject\MiniUnicorn\.worktrees\sqlite-memory-storage`), parent `317c7cb9`

## Implementation summary

### `miniunicorn/agent/memory_backup.py` (new, Steps 1/4)

`MemoryBackupManager` with the brief's restore flow. Public results are frozen
Pydantic models: `BackupResult(backup_id, path, created_at,
last_transaction_seq, sha256)` and `RestoreResult(safety_backup_id,
safety_backup_path, restored_tx_seq)`. `MemoryBackupError(MemoryError)` with a
stable `code` (`backup_failed`, `restore_failed`, `invalid_backup_id`,
`invalid_backup`, `unsupported_schema_version`, `unsafe_backup_path`,
`backup_not_found`, `lock_timeout`).

- Backup id doubles as the canonical filename: `memory-<UTC>-<tx_seq>.db` with
  a `-<hex8>` uniqueness suffix, always resolved inside
  `memory/structured/backups/` (prefix + containment check).
- **create_backup**: `source.backup(destination)` (SQLite online backup API —
  never `shutil.copyfile`), integrity re-check on the snapshot, sha256 of the
  file bytes. Result read through the repository's own read path.
- **restore_backup** follows the brief's 8 steps exactly:
  1. Parse the id; canonical files only, inside `backups/`.
  2. Read-only open (`file:` URI, percent-encoded), verify `user_version == 1`,
     `PRAGMA integrity_check == "ok"`, `foreign_key_check` empty.
  3. Acquire `memory-maintenance.lock` (FileLock, `lock_timeout_s` from config).
  4. Safety backup via backup API into `recovery/<UTC>/memory-before-restore.db`
     (manifest `recovery/manifest.json` id = `recovery/<UTC>/memory-before-restore.db`).
  5. Copy the snapshot into the live connection with the backup API, then
     `PRAGMA wal_checkpoint(TRUNCATE)`.
  6. Quick check (integrity + foreign keys) and repository health refresh.
  7. `MemoryAuditExporter(repository).rebuild()` — audit identical to the
     restored database (manifest `database_last_tx_seq` == restored seq).
  8. Return safety-backup id + restored `last_transaction_seq`.
- Failure after step 4 keeps the safety backup (so the pre-restore database is
  never lost); failure before the swap leaves the database untouched.
  A failed restore never leaves the DB half-swapped: the copy happens inside
  one transaction, then the lock is released. Same single-process assumption
  as Tasks 5/6.

### `miniunicorn/command/memory.py` (Step 6)

- `/memory-status` extended (aligned with `RepositoryHealth` +
  `MemoryStorageStats`): `Backend: \`sqlite\``, `Schema: \`v1\``,
  `Transactions`, `Revisions`, `Current`, `Database size`,
  `Audit exported seq`, `Audit lag`, `Migration: \`not_needed\`` (or the
  actual migration state). Existing architecture/health/records lines kept.
- `/memory-log [<tx-id>]` — default: newest 20 **visible** transactions, one
  line each (`tx_id`, timestamp, actor, reason, touched record IDs), never
  evidence; heading shows the visible total. With `<tx-id>`: operations
  (`operation N: \`put\``) with status/scope and evidence excerpts truncated
  to 200 chars. Transactions whose records belong to other identities render
  identically to unknown ids (`No memory transaction with id ...`).
- `/memory-backup` — replies `## Memory backup created` with backup id,
  SHA-256, `Integrity: \`ok\``, transaction seq, size.
- `/memory-restore <backup-id>` — replies `## Memory restored` with the
  restored backup id, the automatic safety backup id and restored tx seq;
  `backup_not_found` renders `No memory backup with id \`...\`.`, malformed
  ids render the `invalid_backup_id` message. Errors pass through
  `MemoryBackupError.code` unchanged for diagnostics.
- `/memory-export-audit [--rebuild]` — `export_pending()` or `rebuild()`,
  replies with exported `Rows` and whether a rebuild ran.
- All new handlers go through `_requires_stack` + `_effective_store`
  (`WorkspaceScopeResolver`), so scoped workspaces back up/restore their own
  store (pinned by `TestDiagnosticScopeIsolation`). `MemoryBackupManager`
  imported lazily. Registered exact + prefix in `register_memory_commands`.

### `miniunicorn/command/builtin.py` (Step 6)

Deleted `/dream-log` and `/dream-restore` entirely: `BUILTIN_COMMAND_SPECS`
rows, router registrations (exact + prefix), `cmd_dream_log`,
`cmd_dream_restore`, and the four helpers they were the only consumers of
(`_extract_changed_files`, `_format_changed_files`, `_format_dream_log_content`,
`_format_dream_restore_list`) — removing the Git-SHA-diff dependency. `/dream`
itself unchanged; no alias added.

### Test files

- `tests/agent/test_memory_backup.py` (new) — brief Step 1 test verbatim plus
  the Step 2 set: legal roundtrip (safety backup, count/health/audit identical
  to backup), corrupt backup, wrong `user_version`, paths outside `backups/`
  (`../`, absolute, embedded traversal) + unknown id, and an injected copy
  failure proving the database and safety backup survive.
- `tests/agent/test_memory_commands.py` — status tests updated to the new
  fields; new `TestMemoryLog`, `TestMemoryBackup`, `TestMemoryRestore`,
  `TestMemoryExportAudit`, `TestDiagnosticScopeIsolation` (scoped workspaces
  cannot restore another workspace's backup; backups land in the effective
  workspace).
- `tests/command/test_builtin_dream.py` — deleted (its only content was the
  `/dream-log`/`/dream-restore` tests and the `_FakeStore.restore_memory_version`
  fixture; Step 6 requires their removal).
- `tests/command/test_router_dispatchable.py` — `/dream-log`/`/dream-restore`
  rows removed.

### Extra approved scope (same commit)

- `docs/chat-commands.md` — `/dream-log`/`/dream-restore` rows removed;
  `/memory-log`, `/memory-backup`, `/memory-restore`, `/memory-export-audit`
  rows added.
- `miniunicorn/agent/memory.py` — `_WRITER_WHITELIST` gained the Task 8
  runtime artifacts: `memory.db-wal`, `memory.db-shm`, `recovery/`,
  `memory-maintenance.lock`, `*.importing-*` (all `{"memory_store"}`).
  `_assert_writer_allowed` gained `fnmatchcase` so the importing-glob entry
  (`memory.db.importing-<token>` temp DBs from journal import, Task 4) is real
  rather than decorative. Main agent stays forbidden everywhere under
  `memory/structured/`.
- `tests/agent/test_memory_store.py` — whitelist tests extended to the new
  entries: memory-store allowance, no-warning children checks, and explicit
  `main_agent` violation warnings for wal/shm/importing/recovery/lock paths.

## Notes / decisions

- **One seed = one transaction.** The brief's Step 1 test asserts exactly one
  row in `memory_transactions`. A verified ingest auto-promotes in a **second**
  transaction, so the backup-test seed uses a non-verified fact (stays
  candidate) — one ingest, one transaction. The command tests' `_seed`
  (verified, auto-promotes) therefore counts 2 transactions, and its
  expectations reflect that.
- `docs/memory.md` lines 128-129 still reference `/dream-log`/`/dream-restore`:
  not touched here because Task 10 explicitly owns that cleanup (noted there).

## TDD evidence

**RED** (Step 3, before `memory_backup.py` existed and before command wiring):

```
uv run pytest tests/agent/test_memory_backup.py -q
ModuleNotFoundError: No module named 'miniunicorn.agent.memory_backup'
(6 failed)

uv run pytest tests/agent/test_memory_commands.py -q
FAILED TestStatus::test_status_shows_architecture_health_and_counts (new fields)
FAILED TestMemoryLog::* / TestMemoryBackup::* / TestMemoryRestore::* / TestMemoryExportAudit::* / TestDiagnosticScopeIsolation::*
(unhandled /memory-log and friends; missing status fields)
```

**GREEN** (Steps 4+6):

```
uv run pytest tests/agent/test_memory_backup.py tests/agent/test_memory_commands.py -q
57 passed in 7.11s
```

## Final test output

```
uv run pytest tests/agent/test_memory_backup.py tests/agent/test_memory_commands.py tests/agent/test_memory_store.py tests/command/test_router_dispatchable.py -q
150 passed, 1 skipped

uv run pytest tests/command -q
26 passed

Wider memory regression (full tests/agent):
1619 passed, 1 skipped, 5 failed
  - test_mcp_connection.py (3) — HuggingFace/ModelScope lookups time out in this
    environment (network), pre-existing, unrelated to memory
  - test_workspace_scope.py (2) — `printf` unavailable under PowerShell (mojibake
    stderr, exit code 1), Windows shell quirk, pre-existing; same 5 failures
    reported in the Task 8 run on the same machine
  - both files are untouched by this task (git diff empty)

uv run ruff check memory_backup.py memory.py command/memory.py command/builtin.py + 3 test files: All checks passed!
```

## Self-review findings

- Restore is copy-into-live + `wal_checkpoint(TRUNCATE)` inside the
  maintenance lock, matching the SQLite-online-backup requirement; a failed
  copy (injected `OSError`) leaves both the live database and the safety copy
  intact and the store healthy.
- The safety backup is written with the backup API too (not a raw file copy),
  so the recovery copy is always consistent even mid-WAL.
- `invalid_backup` (verification failure) is distinct from `invalid_backup_id`
  (parse) and `backup_not_found`; command rendering keeps the codes verbatim
  for diagnostics except `backup_not_found`, which gets the friendly
  `No memory backup with id ...` message.
- Log visibility reuses the same canonicalization as list/show
  (`_allowed_scopes`, `session:` fork-stripping, `user:default` fallback) and
  requires **every** record in a transaction to be visible — a mixed-scope
  transaction is withheld entirely, never partially revealed.
- Deleted only orphans created by this task: the four dream helper functions
  and the whole `test_builtin_dream.py` (which existed solely for the two
  removed commands).
- `_WRITER_WHITELIST` glob matching is scoped to `fnmatchcase` on the exact
  key list; with today's keys the only glob is `*.importing-*`, so no existing
  exact/prefix entry changes behavior (verified by the no-warning children
  tests).
