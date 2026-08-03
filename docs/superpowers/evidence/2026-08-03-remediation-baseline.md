# Remediation Baseline

- Source commit: f787ca1823f598e6f3c5e185c3c3f9f5c949d085
- Branch: `codex/full-remediation`
- Protected original-checkout files: `.workbuddy/`, `webui/package-lock.json`, `webui/public/favicon_decoded.png`, `webui/public/logo_decoded.png`
- Platform: macOS (adapted from Windows/PowerShell plan; LF line endings, no CRLF issue)
- Pre-existing working-tree modifications preserved: 6 deleted old `docs/superpowers/` docs, modified `uv.lock`

## Tool Versions

```
git rev-parse HEAD → f787ca1823f598e6f3c5e185c3c3f9f5c949d085
.venv/bin/python --version → Python 3.13.9
uv --version → uv 0.7.6 (Homebrew 2025-05-19)
node --version → v24.13.1
npm --version → 11.11.0
.venv/bin/ruff --version → ruff 0.15.21
```

## Known Red Gates

| Command | Exit | Causal line |
|---|---:|---|
| `uv lock --check` | 0 | PASS — lock already in sync (differs from plan's expected stale-lock failure) |
| `uv run pytest -m core -q` | 1 | `AttributeError: 'AgentLoop' object has no attribute '_dispatch'` (8 failures) and `'AgentLoop' object has no attribute '_active_tasks'` (1 failure) plus `test_final_conflict_enters_retry_wait` RUNNING != RETRY_WAIT |
| `npm run check:protocol` | 0 | PASS — macOS uses LF, no CRLF drift (differs from plan's expected stale-types failure) |

### Core test failure detail (10 failed, 1222 passed, 11 skipped)

1. `tests/session/test_unified_session.py::TestUnifiedSessionDispatch::test_unified_session_rewrites_key_to_unified_default` — `AttributeError: 'AgentLoop' object has no attribute '_dispatch'`
2. `tests/session/test_unified_session.py::TestUnifiedSessionDispatch::test_unified_session_different_channels_share_same_key` — `AttributeError: 'AgentLoop' object has no attribute '_dispatch'`
3. `tests/session/test_unified_session.py::TestUnifiedSessionDispatch::test_unified_session_disabled_preserves_original_key` — `AttributeError: 'AgentLoop' object has no attribute '_dispatch'`
4. `tests/session/test_unified_session.py::TestUnifiedSessionDispatch::test_unified_session_respects_existing_override` — `AttributeError: 'AgentLoop' object has no attribute '_dispatch'`
5. `tests/session/test_unified_session.py::TestStopCommandWithUnifiedSession::test_active_tasks_use_effective_key_in_unified_mode` — `AttributeError: 'AgentLoop' object has no attribute '_active_tasks'`
6. `tests/session/test_unified_session.py::TestStopCommandWithUnifiedSession::test_stop_command_finds_task_in_unified_mode` — `AttributeError: 'AgentLoop' object has no attribute '_active_tasks'`
7. `tests/session/test_unified_session.py::TestStopCommandWithUnifiedSession::test_stop_command_uses_effective_key_without_session_override` — `AttributeError: 'AgentLoop' object has no attribute '_active_tasks'`
8. `tests/session/test_unified_session.py::TestStopCommandWithUnifiedSession::test_stop_command_cross_channel_in_unified_mode` — `AttributeError: 'AgentLoop' object has no attribute '_active_tasks'`
9. `tests/session/test_webui_turns.py::test_concurrent_turns_emit_own_context_usage` — `AttributeError: 'AgentLoop' object has no attribute '_dispatch'`
10. `tests/runtime/test_lightweight_host.py::TestSessionRevisionConflict::test_final_conflict_enters_retry_wait` — `AssertionError: assert 'RUNNING' == 'RETRY_WAIT'`

### Fault injection baseline

`uv run pytest tests/runtime/test_runtime_fault_injection.py::TestSupervisedWorkerKillRecovery::test_task_recovers_after_worker_kill -q` → 1 passed in 4.67s (kills an arbitrary Worker; Task 7 will make it kill the actual lease owner).

## Environment Notes

- `uv` installed at `/opt/homebrew/bin/uv` (not in default shell PATH; commands must prepend `/opt/homebrew/bin:/usr/local/bin` to PATH).
- `node`/`npm` at `/usr/local/bin/`.
- Project `.venv` uses Python 3.13.9 with ruff 0.15.21 pre-installed.
- `webui/node_modules` was installed via `npm install` during baseline capture; `webui/package-lock.json` is a new untracked file preserved as a protected artifact.
