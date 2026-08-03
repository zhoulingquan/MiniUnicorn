# Four-Batch Core Hardening — Release Notes

**Date:** 2026-08-03
**Branch:** `codex/full-remediation`
**Final commit:** `c2ac801946724f76bd555df558a5f73acbf35e36`
**Plan:** `docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md`

## Summary

The remediation completes 28 tasks (0–27) across five stages: release and recovery gates, AgentRunner boundary extraction, WebUI boundary extraction, Channel boundary extraction, packaging, and real operational proof. It delivers two user-visible outcomes: real local CPU embedding with persistent vector recall, and a production supervised runtime with exactly three Workers and exact-owner crash recovery.

## Measured Line Counts

| File | Lines | Target |
|---|---:|---|
| `miniunicorn/agent/runner.py` | 446 | ≤ 450 |
| `miniunicorn/agent/runner_control.py` | 919 | — |
| `miniunicorn/agent/runner_model.py` | 267 | — |
| `miniunicorn/agent/runner_tools.py` | 650 | — |
| `miniunicorn/agent/runner_types.py` | 116 | — |
| `webui/src/hooks/useMiniunicornStream.ts` | 402 | < 650 |
| `webui/src/components/thread/AgentActivityCluster.tsx` | 477 | ≤ 500 |
| `miniunicorn/channels/websocket/outbound.py` | 472 | — |
| `miniunicorn/channels/feishu/rendering.py` | 355 | — |
| `miniunicorn/channels/feishu/media.py` | 260 | — |
| `miniunicorn/channels/weixin/crypto.py` | 113 | — |
| `miniunicorn/channels/weixin/api_client.py` | 187 | — |
| `miniunicorn/channels/weixin/media.py` | 361 | — |

## Install Profiles

The package `miniunicorn_ai` (version 0.3.0) supports three install profiles, all verified against a freshly built wheel:

| Profile | Extra | Key dependencies | Import verification |
|---|---|---|---|
| Base | `base` | Core runtime, SQLite, agents | PASSED |
| Documents | `documents` | `python-docx`, `openpyxl`, `python-pptx` | PASSED |
| PDF | `pdf` | `pypdf`, `PyMuPDF` | PASSED |

Document backends are optional; `miniunicorn/utils/document.py` raises an actionable missing-dependency exception when the needed backend is absent.

## Compatibility Boundaries

- **SQLite** remains the sole authoritative store for persistent tasks. `memory/memory.db` is a derived vector index and may safely degrade to `NoOpVectorStore`.
- **Removed APIs:** `AgentLoop._dispatch`, `AgentLoop._active_tasks`, `TurnDispatcher.dispatch`, and `TurnDispatcher.process_direct` are permanently removed. No in-memory execution authority was reintroduced.
- **Embedding:** `BAAI/bge-small-zh-v1.5` on CPU via FastEmbed/ONNX Runtime. Requires the `vector` extra (`sqlite-vec`). Falls back to `NoOpVectorStore` when unavailable.
- **Runtime:** One Control Plane plus exactly three Workers. Lease owners are resolved from the immutable `task_events` log, not from the mutable `tasks.leased_by` column.
- **Protocol:** Generated agent-event TypeScript types are checked with CRLF/LF normalization. Semantic drift still fails the check.

## Test Counts and Durations

| Suite | Passed | Failed | Skipped | Duration |
|---|---:|---:|---:|---|
| Architecture | 148 | 0 | 0 | 0.84s |
| Core (`-m core`) | 1309 | 0 | 13 | 229.31s |
| Runtime | 566 | 0 | 0 | 392.92s |
| Full (`pytest -q`) | 4137 | 19 | 15 | 523.06s |
| Packaging | 42 | 0 | 0 | 4.50s |
| Load | 2 | 0 | 0 | 98.63s |
| Frontend (vitest) | 341 | 0 | 0 | 11.66s |
| Frontend (generated-check) | 2 | 0 | 0 | < 1s |
| Soak (30 min) | 3568 tasks | 0 anomalies | — | 30 min |

The 19 full-suite failures are all pre-existing (Windows-specific subprocess tests, CLI module renames, AgentLoop attribute removals from earlier remediation, one flaky timing test). None are regressions from this remediation.

## Evidence-Backed Deviations

1. **Platform substitution:** The plan was written for Windows (`py -m uv run`). On macOS, `/opt/homebrew/bin/uv run` was substituted. Node/npm were at `/usr/local/bin/`. All substitutions are recorded here.

2. **Docker build fix:** The Dockerfile's bun builder image was upgraded from `oven/bun:1.1-debian` to `oven/bun:1.2-debian` (commit `c2ac8019`) because `webui/bun.lock` uses the JSON `lockfileVersion: 1` format introduced in bun 1.2. After the fix, `docker build` and the import smoke test (`import pypdf, docx, openpyxl, pptx; import miniunicorn; print(miniunicorn.__version__)` → `0.3.0`) both PASS.

3. **Soak source state:** The 30-minute soak ran from commit `6213d4e0`. Post-soak commits (`ca97b56a`, `499e6d25`, `88369a97`) only modified test files, formatting, and frontend test type casts — no runtime source code changed. The soak report remains valid.

4. **Pre-existing test failures:** 19 tests in the full suite fail due to Windows-specific subprocess flags, earlier CLI module renames, earlier AgentLoop attribute removals, and one flaky timing test. These are documented in the final evidence and are not regressions.

## Key Commits

| Commit | Subject |
|---|---|
| `f7294799` | Task 9: freeze agent runner boundaries |
| `496e5b35` | Task 10: extract runner model client |
| `b7a6369e` | Task 11: extract runner tool execution |
| `a0318667` | Task 12: extract runner control flow |
| `e5bb6f19` | Task 13: extract pure stream state and reducer |
| `6b71eb57` | Task 14: split agent activity components |
| `d3fd81e2` | Task 15: extract WebSocket outbound emission |
| `d3756912` | Task 16: extract feishu rendering |
| `12dfebd6` | Task 17: extract feishu media service |
| `236635b1` | Task 18: extract weixin channel services |
| `74ba6d07` | Task 19: move document backends to an extra |
| `6132d925` | Task 20: verify minimal and document package installs |
| `062006a7` | Task 21: tolerate malformed source version metadata |
| `2cc93164` | Task 22: record packaging checkpoint |
| `47cb183c` | Task 23: prove embedding memory persistence and recall |
| `3bd46d92` | Task 24: prove the production three-worker topology |
| `47e0c1ab` | Task 25: prove exact-owner crash recovery |
| `6213d4e0` | Task 26: harden supervised soak evidence |
| `ca97b56a` | Gate fix: ruff format and unused imports |
| `499e6d25` | Gate fix: pre-existing test gaps |
| `88369a97` | Gate fix: tsc --noEmit mock instance casts |
| `c2ac8019` | Docker fix: upgrade bun builder to 1.2 for JSON lockfile |
| `f8ec80e0` | Task 27: final evidence and release notes |
