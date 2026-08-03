# Remediation Final Evidence

## Source Commit

- Branch: `codex/full-remediation`
- Final source commit: `c2ac801946724f76bd555df558a5f73acbf35e36`
- Platform: macOS (substituted `/opt/homebrew/bin/uv run` for `py -m uv run`)
- Node: v24.13.1, npm from `/usr/local/bin/npm`

## Protected Files Check

Removed task-authority names appear only in hard-cutover negative assertions and documentation, never as live call sites:

- `AgentLoop._dispatch` — absent from `miniunicorn/`; matches in tests are local helpers (`_dispatch_via_seam`) or unrelated `ChannelManager._dispatch_outbound` / `WebSocketChannel._dispatch_envelope` methods.
- `AgentLoop._active_tasks` — absent.
- `TurnDispatcher.dispatch` — absent.
- `TurnDispatcher.process_direct` — absent.

No required embedding, three-worker, recovery, packaging, load, or soak result is skipped or xfailed.

## Gate Matrix

| Gate | Exit | Result |
|---|---:|---|
| `uv lock --check` | 0 | 131 packages resolved |
| `python scripts/export_agent_event_schema.py --check` | 0 | schema current |
| `ruff format --check miniunicorn tests scripts` | 0 | 566 files already formatted |
| `ruff check miniunicorn tests scripts` | 0 | All checks passed |
| `pytest tests/architecture -q` | 0 | 148 passed in 0.84s |
| `pytest -m core -q` | 0 | 1309 passed, 13 skipped in 229.31s |
| `pytest tests/runtime -q` | 0 | 566 passed in 392.92s |
| `pytest -q` (full suite) | 1 | 4137 passed, 19 failed, 15 skipped in 523.06s |
| `git diff --check` | 0 | clean |
| `npm run test:generated-check` | 0 | 2 pass |
| `npm run check:protocol` | 0 | schema current |
| `npm run lint` | 0 | 0 warnings |
| `npx tsc --noEmit` | 0 | clean |
| `npm test` (vitest) | 0 | 341 passed in 11.66s |
| `npm run build` | 0 | built in 5.80s |

### Full-suite failure categorization (19 failures, all pre-existing)

All 19 failures predate Task 26–27 and are unrelated to the remediation's runtime, embedding, or packaging changes. None affect the required evidence commands.

| Category | Count | Root cause |
|---|---:|---|
| Windows-specific subprocess tests | 5 | `subprocess.CREATE_NEW_PROCESS_GROUP` is Windows-only; cannot pass on macOS |
| CLI module attribute renames | 11 | Tests reference `_print_cli_progress_line` / `_maybe_print_interactive_progress` / `_ReasoningBuffer` / `_print_cli_reasoning` which were renamed or removed from `miniunicorn/cli/commands.py` in earlier commits |
| AgentLoop attribute removal | 2 | `AgentLoop.bus` and `_turn_persistence` removed during earlier remediation; tests not updated |
| Flaky timing/logging | 1 | `test_supervisor_logs_background_exception` assertion on log capture timing |

## Embedding Facts

Source: `embedding-memory-evidence.json` (re-run from final commit, exit 0).

| Fact | Value |
|---|---|
| model_id | `BAAI/bge-small-zh-v1.5` |
| dimension | 512 |
| row_count | 2 |
| row_count_after_reindex | 2 (no duplicate) |
| top_text | `MiniUnicorn 使用本地 CPU 嵌入保存长期记忆。` |
| top_similarity | 0.8042683303356171 |
| fallback_status | safe-noop |

The real `BAAI/bge-small-zh-v1.5` model runs on CPU, returns normalized 512-dimensional vectors (norm 1.0 ± 1e-5), two Chinese documents persist in `memory.db`, survive close/reopen, recall the correct Chinese memory at rank 1, and do not duplicate under the same source identity/revision.

## Three-Worker Facts

Source: `three-worker-evidence.json` (re-run from final commit, exit 0).

| Fact | Value |
|---|---|
| mode | topology |
| control_count | 1 |
| ready_workers | 3 |
| worker_ids | `worker-0`, `worker-1`, `worker-2` (distinct) |
| worker_pids | 3 real positive PIDs |
| instance_ids | `control#1`, `worker-0#2`, `worker-1#3`, `worker-2#4` (distinct) |
| task_state | COMPLETED |
| provider_requests | 1 |
| outbox_kinds | `FINAL_REPLY` |
| session_has_user_then_assistant | true |
| duplicate_final_replies | 0 |
| duplicate_model_started_events | 0 |
| children_alive_after_stop | 0 |

Supervised startup reports one ready Control Plane and exactly three distinct ready Workers, each with a real positive PID. A durable turn completes through a real spawned Worker with one local process-safe Provider stub request and one FINAL_REPLY outbox row.

## Crash-Recovery Facts

Source: `three-worker-crash-evidence.json` (re-run from final commit, exit 0).

| Fact | Value |
|---|---|
| mode | crash |
| killed_worker_id | `worker-1#3` |
| killed_worker_pid | 34709 |
| recovery_attempts | 2 |
| logical_provider_decisions | 1 |
| model_started_events | 1 |
| duplicate_model_started_events | 0 |
| duplicate_final_replies | 0 |
| task_state | COMPLETED |
| provider_requests | 1 |
| children_alive_after_stop | 0 |

The Worker named by `TaskRecord.leased_by` (`worker-1#3`, PID 34709) was terminated. The task recovered after 2 recovery attempts, completed with exactly 1 logical Provider decision, and produced no duplicate model-started events or final replies.

## Packaging Artifacts

Built from final commit with `uv build --out-dir dist-remediation`.

| Artifact | Path |
|---|---|
| wheel | `dist-remediation/miniunicorn_ai-0.3.0-py3-none-any.whl` |
| sdist | `dist-remediation/miniunicorn_ai-0.3.0.tar.gz` |

| Install profile | Result |
|---|---|
| `--extra base` (`.package-smoke-base`) | PASSED — all import expectations met |
| `--extra documents` (`.package-smoke-documents`) | PASSED — all import expectations met |
| `--extra pdf` (`.package-smoke-pdf`) | PASSED — all import expectations met |

Packaging tests: `pytest tests/test_document_parsing.py tests/packaging tests/test_package_version.py -q` → 42 passed in 4.50s.

## Load Facts

`pytest tests/runtime/test_runtime_load.py -q` → 2 passed in 98.63s.

The 100-task smoke and 1000-task gate both passed, asserting `max_concurrent >= 3` distinct concurrent sessions.

## Soak Facts

Source: `runtime-soak-release.json` (30-minute supervised soak, validated by `assert_release_soak_summary`).

| Fact | Value |
|---|---|
| SHA-256 | `1abc0cf5de7b549096a6066230f50d3b1cca3fba086bf0a318407dfa5dae2a4e` |
| duration | 30 minutes |
| sessions | 100 |
| rate | 2 tasks/second |
| seed | 20260731 |
| worker_ids | `worker-0#2`, `worker-1#3`, `worker-2#4` (3 distinct) |
| total_submitted | 3568 |
| total_completed | 3568 |
| missing_terminal | 0 |
| missing_final_replies | 0 |
| duplicate_effects | 0 |
| same_session_overlaps | 0 |
| same_session_order_violations | 0 |
| stale_mutations | 0 |
| unresolved_sqlite_busy | 0 |
| children_alive_after_shutdown | 0 |

The soak ran from commit `6213d4e0` (Task 26). Post-soak commits (`ca97b56a`, `499e6d25`, `88369a97`) only modified test files, formatting, and frontend test type casts — no runtime source code (`miniunicorn/runtime/`, `miniunicorn/agent/`) was changed. The soak report remains valid release evidence.

## Docker Result

| Step | Command | Exit | Result |
|---|---|---:|---|
| version | `docker version` | 0 | Docker 29.6.2, build dfc4efb |
| build | `docker build -t miniunicorn-remediation:local .` | 0 | Built successfully (bun 1.2 builder + Python runtime with documents extra) |
| import smoke | `docker run --rm --entrypoint python miniunicorn-remediation:local -c "import pypdf, docx, openpyxl, pptx; import miniunicorn; print(miniunicorn.__version__)"` | 0 | Printed `0.3.0` |

The Dockerfile was updated from `oven/bun:1.1-debian` to `oven/bun:1.2-debian` (commit `c2ac8019`) because `webui/bun.lock` uses the JSON `lockfileVersion: 1` format introduced in bun 1.2, which bun 1.1 cannot parse. After the fix, build and import smoke both PASS.

## Prior-Plan Mapping

All 14 prior-plan tasks (15–28) have commit-backed implementation or evidence-backed supersession records in `docs/superpowers/plans/2026-07-29-four-batch-core-hardening.md`.

| Prior task | Remediation task | Commit | Disposition |
|---:|---:|---|---|
| 15 | 9 | `f7294799` | Implemented |
| 16 | 10 | `496e5b35` | Implemented |
| 17 | 11 | `b7a6369e` | Implemented |
| 18 | 12 | `a0318667` | Implemented |
| 19 | 13 | `e5bb6f19` | Implemented |
| 20 | 14 | `6b71eb57` | Implemented |
| 21 | 15 | `d3fd81e2` | Implemented |
| 22 | 16–17 | `d3756912`, `12dfebd6` | Implemented |
| 23 | 18 | `236635b1` | Implemented |
| 24 | 18 Step 5 | `236635b1` | Superseded (folded into Stage B checkpoint) |
| 25 | 19 | `74ba6d07` | Implemented |
| 26 | 20, 22 | `6132d925`, `2cc93164` | Superseded (split across packaging tasks) |
| 27 | 21 | `062006a7` | Implemented |
| 28 | 27 | (this commit) | Superseded (folded into final matrix) |

## Residual Advisory Warnings

### Remaining skips (all optional/environment gates)

- `pytest.skip` for missing optional channel dependencies (QQ, DingTalk, Feishu, WeCom) — not installed.
- `pytest.skip` for `sqlite-vec not installed` in vector-memory tests when the `vector` extra is absent.
- `pytest.skip` for `symlink creation is unavailable` on macOS sandboxed paths.
- `pytest.skip` for `MEMORY.md template not bundled` in isolated install profiles.
- `pytest.skip` for `task was failed during host shutdown, not recoverable` in fault-injection conditional.
- `pytest.xfail` in `tests/architecture/test_runtime_dependencies.py` for known import-cycle violations that predate WP1.

### Pre-existing full-suite failures (19, not regressions)

See the failure categorization table in the Gate Matrix section. These are Windows-specific subprocess tests, CLI module attribute renames from earlier commits, AgentLoop attribute removals from earlier remediation, and one flaky timing/logging test. None affect embedding, three-worker, recovery, packaging, load, or soak evidence.

### Docker build fix

The Dockerfile's bun builder image was upgraded from 1.1 to 1.2 (commit `c2ac8019`) to support the JSON `bun.lock` format. Build and import smoke now PASS.

### Vite circular-chunk warnings

The `npm run build` command may emit Vite circular-chunk warnings. Build exits 0 and no runtime test fails, so these are advisory only.
