# Embedding and Three-Worker Completion Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the approved local CPU embedding path persist and recall vectors through `memory.db`, and make the production supervised runtime start one Control Plane plus exactly three Workers, execute durable tasks, and recover correctly after the task-owning Worker is killed.

**Architecture:** First restore deterministic release gates and durable recovery semantics, then finish the planned façade extractions without changing public imports or protocol shapes, then complete optional-dependency packaging, and finally prove the two user-visible outcomes with real processes and a real local embedding model. SQLite remains the only task authority; `memory.db` remains a rebuildable derived index.

**Tech Stack:** Python 3.11+, asyncio, SQLite/WAL, pytest/pytest-asyncio, FastEmbed + ONNX Runtime, sqlite-vec, multiprocessing spawn, TypeScript/React/Vite/Vitest, Node.js ESM, Ruff 0.15.21 from `uv.lock`, Hatchling, uv, Docker.

## Global Constraints

- Execute in `D:\MyProject\MiniUnicorn-worktrees\full-remediation` on branch `codex/full-remediation`; do not modify the original checkout's untracked `.workbuddy/`, `webui/package-lock.json`, `webui/public/favicon_decoded.png`, or `webui/public/logo_decoded.png`.
- Preserve `lightweight` and `supervised` modes, the one-Control-Plane plus exactly-three-Worker production default, public Python imports, Channel class names, configuration keys, WebUI event fields, and the Runtime SQLite schema.
- Do not restore `AgentLoop._dispatch`, `AgentLoop._active_tasks`, `TurnDispatcher.dispatch`, or `TurnDispatcher.process_direct`; do not create another in-memory execution authority.
- Keep session history authoritative. `memory/memory.db` is a derived vector index and may safely degrade to `NoOpVectorStore` when optional vector dependencies are unavailable.
- Every behavior change follows red-green-refactor. Confirm the named test fails for the stated reason before changing production code.
- Do not hide a required result with `xfail`, `skip`, timing-only sleeps, or a relaxed assertion. A fault test that cannot resolve the lease owner must fail with a diagnostic.
- Keep Provider, tool, Session, Outbox, and task execution on their existing single durable paths. Ambiguous non-idempotent effects remain `OUTCOME_UNKNOWN` or `WAITING_USER`.
- Apply the locked Ruff formatter only to repository-owned Python files; keep generated protocol semantics strict while normalizing CRLF/LF for comparison.
- Prefix repository Python commands with `py -m uv run` on Windows. If `py` is unavailable, substitute the interpreter used to create the repository environment and record the substitution in the evidence report.
- Commit after every task. Do not combine behavioral, mechanical formatting, structural extraction, packaging, and evidence updates in one commit.

---

**Reading order:** Execute Tasks 0–27 in the physical order below. Do not advance past a stage checkpoint while any command in that checkpoint is red.

## Execution Index

| Stage | Tasks | Green checkpoint |
|---|---:|---|
| A — release and recovery gates | 0–8 | lock, protocol, core, recovery, lint/format |
| B1 — AgentRunner boundary | 9–12 | runner façade and all Agent tests |
| B2 — WebUI boundary | 13–14 | frontend protocol/lint/test/build |
| B3 — Channel boundary | 15–18 | Channel and architecture suites |
| C — packaging | 19–22 | wheel/sdist/minimal/documents installs |
| D — real operational proof | 23–27 | embedding, 3 Workers, load, soak, final matrix |

The executor must complete stages in order. Within a stage, do not start the next task until the focused command is green and the task commit exists.

### Prior Four-Batch Plan Mapping

| Prior task | Remediation task | Disposition |
|---:|---:|---|
| 15 | 9 | AgentRunner characterization and target boundaries |
| 16 | 10 | runner types and Provider request extraction |
| 17 | 11 | tool execution extraction |
| 18 | 12 | ReAct controller extraction and façade gate |
| 19 | 13 | WebUI stream reducer extraction |
| 20 | 14 | activity parsing/rendering split |
| 21 | 15 | WebSocket outbound extraction |
| 22 | 16–17 | Feishu rendering and media split |
| 23 | 18 | Weixin crypto/API/media split |
| 24 | 18 Step 5 | Batch 3 whole-system checkpoint |
| 25 | 19 | documents extra and actionable error |
| 26 | 20, 22 | artifact, minimal-install, Docker, and package checkpoint |
| 27 | 21 | malformed source metadata fallback |
| 28 | 27 | final verification, release notes, and handoff |

Task 27 writes the commit hash and verification command back to every mapped prior checkbox; this table is routing information, not completion evidence.

## Locked File and Interface Map

- `miniunicorn/agent/runner.py`: public `AgentRunSpec`, `AgentRunResult`, and `AgentRunner` façade only after extraction; target at most 450 physical lines.
- `miniunicorn/agent/runner_types.py`: shared dataclasses/enums for model, tool, and loop collaborators; no Provider or registry ownership.
- `miniunicorn/agent/runner_model.py`: request construction, Provider calls, retries, usage normalization.
- `miniunicorn/agent/runner_tools.py`: tool batching, gateway/direct execution, result normalization, security classification.
- `miniunicorn/agent/runner_control.py`: ReAct iteration transitions and finalization; no public façade replacement.
- `webui/src/hooks/stream-state.ts`: immutable state/cursor/buffer types and initial-state helpers.
- `webui/src/hooks/stream-reducer.ts`: pure event-to-state transitions; no socket, timer, DOM, or React imports.
- `webui/src/hooks/useMiniunicornStream.ts`: sockets, animation frames, subscriptions, and effects; target below 650 lines.
- `webui/src/components/thread/activity/`: activity types, parsers, and focused row/group components; original `AgentActivityCluster.tsx` remains the import façade and is at most 500 lines.
- `miniunicorn/channels/websocket/outbound.py`: WebSocket event serialization and fan-out only.
- `miniunicorn/channels/feishu/rendering.py`, `miniunicorn/channels/feishu/media.py`: Feishu pure rendering and SDK media operations.
- `miniunicorn/channels/weixin/crypto.py`, `miniunicorn/channels/weixin/api_client.py`, `miniunicorn/channels/weixin/media.py`: Weixin AES helpers, HTTP/auth client, and upload/download operations.
- `miniunicorn/utils/document.py`: lazy parser dispatch and actionable missing-backend exception.
- `miniunicorn/runtime/sqlite/execution_store.py`: durable Provider attempt identity; no schema change.
- `tests/runtime/support/supervised.py`: test-only helpers that resolve and terminate the actual lease-owning Worker.
- `scripts/verify_embedding_memory.py`: opt-in real embedding/index/recall evidence command.
- `scripts/runtime_soak.py`: deterministic supervised soak evidence command.

---

## Stage A: Release and Recovery Gates

### Task 0: Freeze the Baseline and Evidence Directory

**Files:**
- Create: `docs/superpowers/evidence/2026-08-03-remediation-baseline.md`
- Modify: none

**Interfaces:**
- Consumes: approved design at `docs/superpowers/specs/2026-08-03-embedding-three-worker-completion-remediation-design.md`.
- Produces: an immutable list of baseline commit, known failures, tool versions, and protected untracked paths.

- [ ] **Step 1: Verify the isolated branch and protected checkout**

Run:

```powershell
git branch --show-current
git status --short
git -C D:\MyProject\MiniUnicorn status --short
```

Expected: current branch is `codex/full-remediation`; worktree has no changes before the evidence file; original checkout shows only the four protected untracked paths listed in Global Constraints.

- [ ] **Step 2: Record exact tool and source versions**

Run:

```powershell
git rev-parse HEAD
py --version
py -m uv --version
node --version
npm --version
py -m uv run ruff --version
```

Expected: every command exits 0. Copy the literal output into the evidence file; do not paraphrase versions.

- [ ] **Step 3: Capture the known red baseline**

Run:

```powershell
py -m uv lock --check
py -m uv run pytest -m core -q
py -m uv run pytest tests/runtime/test_runtime_fault_injection.py::TestSupervisedWorkerKillRecovery::test_task_recovers_after_worker_kill -q
Push-Location webui; npm run check:protocol; Pop-Location
```

Expected at the pre-remediation commit: stale lock failure; core failures referencing removed `_dispatch`/`_active_tasks`; the recovery test may pass or fail because it kills an arbitrary Worker; protocol check reports stale generated types on CRLF checkout. Record each exit code and the first causal error line.

- [ ] **Step 4: Write the baseline evidence**

Use this exact section structure and replace the prose instructions with the literal command output collected in Steps 1–3:

```markdown
# Remediation Baseline

- Source commit: paste the 40-character value printed by `git rev-parse HEAD`
- Branch: `codex/full-remediation`
- Protected original-checkout files: `.workbuddy/`, `webui/package-lock.json`, `webui/public/favicon_decoded.png`, `webui/public/logo_decoded.png`

## Tool Versions

Paste the six version-command output lines here.

## Known Red Gates

| Command | Exit | Causal line |
|---|---:|---|
| `py -m uv lock --check` | 1 | paste the first causal lock error line |
| `py -m uv run pytest -m core -q` | 1 | paste the first removed-API failure line |
| `npm run check:protocol` | 1 | `generated agent event types are stale` |
```

- [ ] **Step 5: Commit**

```powershell
git add -f docs/superpowers/evidence/2026-08-03-remediation-baseline.md
git commit -m "docs: record remediation baseline"
```

Expected: one documentation-only commit.

---

### Task 1: Make Generated Protocol Checks Line-Ending Independent

**Files:**
- Create: `webui/scripts/generated-file-check.mjs`
- Create: `webui/scripts/generated-file-check.test.mjs`
- Modify: `webui/scripts/generate-agent-events.mjs`
- Modify: `webui/package.json`

**Interfaces:**
- Produces: `normalizeGeneratedText(value: string): string` and `generatedTextMatches(current: string, generated: string): boolean`.
- Preserves: any semantic schema/TypeScript difference still makes `npm run check:protocol` fail.

- [ ] **Step 1: Add a failing Node test for LF/CRLF equality and semantic inequality**

```javascript
import assert from "node:assert/strict";
import test from "node:test";
import { generatedTextMatches, normalizeGeneratedText } from "./generated-file-check.mjs";

test("normalizes CRLF and one trailing newline", () => {
  assert.equal(normalizeGeneratedText("a\r\nb\r\n"), "a\nb\n");
  assert.equal(generatedTextMatches("a\r\nb\r\n", "a\nb\n"), true);
});

test("does not hide semantic drift", () => {
  assert.equal(generatedTextMatches("export type A = 1;\r\n", "export type A = 2;\n"), false);
});
```

Add script:

```json
"test:generated-check": "node --test scripts/generated-file-check.test.mjs"
```

- [ ] **Step 2: Run the new test and confirm the missing-module failure**

Run: `Push-Location webui; npm run test:generated-check; Pop-Location`

Expected: FAIL because `generated-file-check.mjs` does not exist.

- [ ] **Step 3: Implement the normalization helper and use it in check mode**

```javascript
export function normalizeGeneratedText(value) {
  return `${value.replace(/\r\n/g, "\n").trimEnd()}\n`;
}

export function generatedTextMatches(current, generated) {
  return normalizeGeneratedText(current) === normalizeGeneratedText(generated);
}
```

In `generate-agent-events.mjs`, import `generatedTextMatches`, keep generated output normalized to LF, and replace `current !== normalized` with `!generatedTextMatches(current, normalized)`. Do not normalize any token other than CRLF and terminal whitespace already removed by the generator.

- [ ] **Step 4: Verify both helper and real generator**

Run:

```powershell
Push-Location webui
npm run test:generated-check
npm run check:protocol
Pop-Location
```

Expected: both commands PASS without rewriting `webui/src/generated/agent-events.ts`.

- [ ] **Step 5: Prove semantic drift still fails**

Temporarily change one generated type token, run `npm run check:protocol`, and expect exit 1 with `generated agent event types are stale`; restore only that temporary test edit with `git restore webui/src/generated/agent-events.ts`, then rerun the command and expect PASS.

- [ ] **Step 6: Commit**

```powershell
git add webui/scripts/generated-file-check.mjs webui/scripts/generated-file-check.test.mjs webui/scripts/generate-agent-events.mjs webui/package.json
git commit -m "fix: make generated protocol checks newline safe"
```

---

### Task 2: Migrate Unified-Session Tests off Removed AgentLoop Authority

**Files:**
- Modify: `tests/session/test_unified_session.py`
- Create: `tests/runtime/test_unified_session_control.py`
- Read: `miniunicorn/agent/loop.py`
- Read: `miniunicorn/agent/turn_dispatcher.py`

**Interfaces:**
- Consumes: `AgentLoop._effective_session_key(msg: InboundMessage) -> str` and `TurnDispatcher.process_message(msg: InboundMessage, *, emit_final: bool = True) -> None`.
- Preserves: hard-cutover assertions that `_dispatch`, `_active_tasks`, `TurnDispatcher.dispatch`, and `TurnDispatcher.process_direct` are absent.

- [ ] **Step 1: Replace stale routing setup with the current key seam**

For tests that only assert session routing, replace calls to `_dispatch(msg)` with direct assertions:

```python
assert loop._effective_session_key(msg) == UNIFIED_SESSION_KEY
```

For override cases:

```python
msg.session_key_override = "explicit-session"
assert loop._effective_session_key(msg) == "explicit-session"
```

Update the module/class/test docstrings to say `effective session key` or `TurnDispatcher.process_message`; remove historical positive references to `_dispatch` while retaining hard-cutover negative assertions elsewhere.

- [ ] **Step 2: Replace behavior tests with the current dispatcher entry point**

Where the test needs actual message processing, inject a mock `loop.core_dispatcher.process_message` and call:

```python
await loop.core_dispatcher.process_message(msg, emit_final=True)
loop.core_dispatcher.process_message.assert_awaited_once_with(msg, emit_final=True)
```

Do not add forwarding methods to `AgentLoop`.

- [ ] **Step 3: Replace `_active_tasks` command assertions with current subagent behavior**

For the three stale `/stop` cases, set the existing subagent mock and assert the effective unified key is used:

```python
loop.subagents.cancel_by_session = AsyncMock(return_value=2)
key = loop._effective_session_key(msg)
ctx = CommandContext(msg=msg, session=None, key=key, raw="/stop", loop=loop)
result = await cmd_stop(ctx)
loop.subagents.cancel_by_session.assert_awaited_once_with(UNIFIED_SESSION_KEY)
assert result.content == "Stopped 2 subagent(s)."
assert not hasattr(loop, "_active_tasks")
```

Delete all creation/insertion of raw `asyncio.Task` objects. The command deliberately cancels only in-process subagents after the hard cutover.

- [ ] **Step 4: Add a durable root-task cancellation test**

In `tests/runtime/test_unified_session_control.py`, use the existing Runtime fixtures:

```python
@pytest.mark.asyncio
async def test_unified_session_root_task_cancel_is_durable(
    store, sample_scope, make_inbound_envelope
):
    service = TaskService(store)
    envelope = make_inbound_envelope(
        sample_scope,
        session_key=UNIFIED_SESSION_KEY,
        channel_message_id="unified-cancel-1",
    )
    handle = await service.submit(envelope)
    result = await service.control(TaskControlRequest(
        task_id=handle.task_id,
        kind="CANCEL",
        dedup_key="cancel-unified-1",
        payload_blob_id=None,
        requested_by="test-user",
        requested_at_ms=1_000_002,
    ))
    assert result.status == "APPENDED"
    task = store.read_task(handle.task_id)
    assert task is not None
    assert task.session_key == UNIFIED_SESSION_KEY
    assert task.state == "CANCELLED"
```

- [ ] **Step 5: Run the focused files**

Run: `py -m uv run pytest tests/session/test_unified_session.py tests/runtime/test_unified_session_control.py -q`

Expected: all tests PASS and no reference to removed fields remains.

- [ ] **Step 6: Run the cutover guard and commit**

Run:

```powershell
py -m uv run pytest tests/runtime/test_lightweight_host.py -k "hard_cutover or legacy" -q
rg -n "\._dispatch|_active_tasks|process_direct" tests/session/test_unified_session.py
```

Expected: pytest PASS; `rg` has no matches. Then:

```powershell
git add tests/session/test_unified_session.py tests/runtime/test_unified_session_control.py
git commit -m "test: migrate unified sessions to durable dispatch"
```

---

### Task 3: Migrate WebUI Turn Tests to the Current Dispatch Seam

**Files:**
- Modify: `tests/session/test_webui_turns.py`
- Read: `miniunicorn/agent/turn_dispatcher.py`

**Interfaces:**
- Consumes: `TurnDispatcher.process_message(msg, *, emit_final=True)`.
- Preserves: WebUI session-key, final-event, and transcript behavior under the existing runtime authority.

- [ ] **Step 1: Change one stale test and observe the current failure disappear**

Replace `await loop._dispatch(msg)` with:

```python
await loop.core_dispatcher.process_message(msg, emit_final=True)
```

Keep all existing event/transcript assertions unchanged.

- [ ] **Step 2: Run the changed test**

Run: `py -m uv run pytest tests/session/test_webui_turns.py -x -q`

Expected before all replacements: the next stale `_dispatch` call fails; this proves the migration is incremental rather than a production compatibility shim.

- [ ] **Step 3: Replace every remaining stale call**

Use the same `loop.core_dispatcher.process_message(msg, emit_final=True)` call. Rename the local `_dispatch_execute` helper to `_execute_for_message`. If a test intentionally suppresses the final event, pass `emit_final=False` explicitly and retain its assertion.

- [ ] **Step 4: Verify focused and core suites**

Run:

```powershell
py -m uv run pytest tests/session/test_webui_turns.py -q
py -m uv run pytest -m core -q
```

Expected: both PASS; the former baseline of 9 core failures is eliminated without restoring legacy APIs.

- [ ] **Step 5: Commit**

```powershell
git add tests/session/test_webui_turns.py
git commit -m "test: migrate webui turns to current dispatcher"
```

---

### Task 4: Add an Explicit Provider-Recovery Identity Error

**Files:**
- Modify: `miniunicorn/runtime/contracts.py`
- Modify: `tests/runtime/test_wp4_provider_tool_gateway.py`

**Interfaces:**
- Produces: `RecoveryIdentityMismatch(RuntimeError)` with fields `task_id`, `logical_call_id`, `attempt_no`, `existing`, and `requested`.
- Does not change SQLite behavior yet; Task 5 consumes this error at the durable attempt boundary.

- [ ] **Step 1: Add a failing exception-contract test**

```python
def test_recovery_identity_mismatch_exposes_diagnostics() -> None:
    exc = RecoveryIdentityMismatch(
        task_id="task-1",
        logical_call_id="model-call-1",
        attempt_no=1,
        existing={"provider_name": "a", "model_name": "m", "request_hash": "h1"},
        requested={"provider_name": "a", "model_name": "m", "request_hash": "h2"},
    )
    assert exc.task_id == "task-1"
    assert exc.logical_call_id == "model-call-1"
    assert exc.attempt_no == 1
    assert exc.existing["request_hash"] == "h1"
    assert exc.requested["request_hash"] == "h2"
    assert "task-1/model-call-1/1" in str(exc)
```

- [ ] **Step 2: Run and confirm import failure**

Run: `py -m uv run pytest tests/runtime/test_wp4_provider_tool_gateway.py::test_recovery_identity_mismatch_exposes_diagnostics -q`

Expected: FAIL because `RecoveryIdentityMismatch` is absent.

- [ ] **Step 3: Implement the diagnostic exception**

```python
class RecoveryIdentityMismatch(RuntimeError):
    def __init__(
        self,
        *,
        task_id: str,
        logical_call_id: str,
        attempt_no: int,
        existing: dict[str, str],
        requested: dict[str, str],
    ) -> None:
        self.task_id = task_id
        self.logical_call_id = logical_call_id
        self.attempt_no = attempt_no
        self.existing = existing
        self.requested = requested
        super().__init__(
            f"provider attempt identity mismatch for {task_id}/"
            f"{logical_call_id}/{attempt_no}: existing={existing!r}, requested={requested!r}"
        )
```

Place it beside `StaleLeaseError` and `SessionCommitMismatchError` in `contracts.py`; do not put a durable-store error in `models.py`.

- [ ] **Step 4: Verify and commit**

Run: `py -m uv run pytest tests/runtime/test_wp4_provider_tool_gateway.py::test_recovery_identity_mismatch_exposes_diagnostics -q`

Expected: PASS. Then:

```powershell
git add miniunicorn/runtime/contracts.py tests/runtime/test_wp4_provider_tool_gateway.py
git commit -m "feat: add provider recovery identity error"
```

---

### Task 5: Make Provider-Attempt Begin Idempotent

**Files:**
- Modify: `miniunicorn/runtime/sqlite/execution_store.py`
- Modify: `tests/runtime/test_wp4_provider_tool_gateway.py`

**Interfaces:**
- Consumes: `begin_model_attempt(claim: TaskClaim, value: ModelAttemptWrite) -> str`.
- Consumes: `RecoveryIdentityMismatch` from Task 4.
- Preserves: lease validation occurs inside the same `BEGIN IMMEDIATE` transaction before reuse or insertion; no schema migration.

- [ ] **Step 1: Add the failing same-identity reuse test**

Create a task and valid claim using `_claim_running_task`, then:

```python
value = ModelAttemptWrite(
    logical_call_id="model-call-1",
    attempt_no=1,
    provider_name="custom",
    model_name="stub-model",
    request_hash="sha256:abc",
    started_at_ms=now_ms,
)
first = store.begin_model_attempt(claim, value)
second = store.begin_model_attempt(claim, value)
assert second == first
events = [e for e in store.list_events(claim.task_id) if e.event_type == "MODEL_STARTED"]
assert len(events) == 1
```

- [ ] **Step 2: Run and confirm the unique-constraint failure**

Run: `py -m uv run pytest tests/runtime/test_wp4_provider_tool_gateway.py::test_begin_model_attempt_reuses_same_durable_identity -q`

Expected: FAIL with `sqlite3.IntegrityError` for `(task_id, logical_call_id, attempt_no)`.

- [ ] **Step 3: Add mismatch, stale-lease, and terminal-state cases**

Add three tests:

1. repeat with a copied `ModelAttemptWrite` whose `request_hash="sha256:different"`; expect `RecoveryIdentityMismatch`;
2. replace/expire the claim before an identical repeat; expect `StaleLeaseError`;
3. after completing one attempt, and separately after failing one attempt, repeat the identical begin; expect the original id and exactly one `MODEL_STARTED` event.

Use the existing `finish_model_attempt` and failure helper in the same store test module rather than writing SQL to change attempt state.

- [ ] **Step 4: Query durable identity after lease validation**

Inside the current `BEGIN IMMEDIATE` block and after `self._validate_lease(claim, now_ms=value.started_at_ms)`, add:

```python
existing = self._conn.execute(
    """
    SELECT model_attempt_id, provider_name, model_name, request_hash, state
    FROM model_attempts
    WHERE task_id=? AND logical_call_id=? AND attempt_no=?
    """,
    (claim.task_id, value.logical_call_id, value.attempt_no),
).fetchone()
```

Build `existing_identity` and `requested_identity` dictionaries from `provider_name`, `model_name`, and `request_hash`. If they differ, raise `RecoveryIdentityMismatch`. If they match and state is `STARTED`, `COMPLETED`, or `FAILED`, commit and return the existing `model_attempt_id` without appending an event.

- [ ] **Step 5: Preserve new-identity insertion exactly**

For no existing row, retain the generated id, plain `INSERT`, single `MODEL_STARTED` append, commit, and return. Do not use `INSERT OR IGNORE`; it would suppress identity diagnostics.

- [ ] **Step 6: Run focused and fault suites**

```powershell
py -m uv run pytest tests/runtime/test_wp4_provider_tool_gateway.py -q
py -m uv run pytest tests/runtime/test_runtime_fault_injection.py -q
```

Expected: Provider-attempt tests PASS. The Worker-kill case may still be timing-sensitive until Tasks 6–7.

- [ ] **Step 7: Commit**

```powershell
git add miniunicorn/runtime/sqlite/execution_store.py tests/runtime/test_wp4_provider_tool_gateway.py
git commit -m "fix: reuse durable provider attempt identities"
```

---

### Task 6: Resolve the Actual Lease-Owning Worker in Tests

**Files:**
- Create: `tests/runtime/support/supervised.py`
- Create: `tests/runtime/test_supervised_support.py`
- Modify: `miniunicorn/runtime/supervisor.py`

**Interfaces:**
- Produces production read-only method `Supervisor.child_record(child_id: str) -> _ChildRecord | None`.
- Produces test helper `wait_for_task_owner(store, task_id, *, timeout_s) -> tuple[TaskRecord, str]` and `terminate_worker(supervisor, worker_id, *, timeout_s=2.0) -> int`.
- Avoids direct test access to `supervisor._children`.

- [ ] **Step 1: Add failing helper tests**

Test that `wait_for_task_owner` polls until `TaskRecord.state == "RUNNING"` and `leased_by` is non-empty, then returns that exact owner. Test timeout with a queued task and assert:

```python
with pytest.raises(AssertionError, match="lease owner was not resolved"):
    await wait_for_task_owner(store, task_id, timeout_s=0.05)
```

Test `terminate_worker` rejects `worker-404` with `AssertionError("no live supervised Worker named worker-404")`.

- [ ] **Step 2: Run and confirm imports fail**

Run: `py -m uv run pytest tests/runtime/test_supervised_support.py -q`

Expected: FAIL because the support module and public lookup method do not exist.

- [ ] **Step 3: Add the read-only supervisor lookup**

```python
def child_record(self, child_id: str) -> _ChildRecord | None:
    """Return the current supervised child record for diagnostics/fault injection."""
    return self._children.get(child_id)
```

Do not expose the whole mutable mapping.

- [ ] **Step 4: Implement deterministic test helpers**

```python
async def wait_for_task_owner(store, task_id: str, *, timeout_s: float):
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = store.read_task(task_id)
        if last is not None and last.state == "RUNNING" and last.leased_by:
            return last, last.leased_by
        await asyncio.sleep(0.05)
    raise AssertionError(f"lease owner was not resolved for {task_id}; last={last!r}")

def terminate_worker(supervisor, worker_id: str, *, timeout_s: float = 2.0) -> int:
    record = supervisor.child_record(worker_id)
    assert record is not None and record.role == "worker" and record.process is not None
    assert record.process.is_alive(), f"no live supervised Worker named {worker_id}"
    pid = record.process.pid
    record.process.terminate()
    record.process.join(timeout_s)
    if record.process.is_alive():
        record.process.kill()
        record.process.join(timeout_s)
    return pid
```

- [ ] **Step 5: Verify and commit**

Run: `py -m uv run pytest tests/runtime/test_supervised_support.py tests/runtime/test_wp6_supervised.py -q`

Expected: PASS. Then:

```powershell
git add miniunicorn/runtime/supervisor.py tests/runtime/support/supervised.py tests/runtime/test_supervised_support.py
git commit -m "test: expose deterministic supervised worker lookup"
```

---

### Task 7: Kill the Task Owner and Prove Recovery

**Files:**
- Modify: `tests/runtime/test_runtime_fault_injection.py`
- Test: `tests/runtime/test_runtime_fault_injection.py`

**Interfaces:**
- Consumes: `wait_for_task_owner(store, task_id, timeout_s=10.0)`, `terminate_worker(supervisor, owner)`, and idempotent Provider-attempt behavior from Task 5.
- Produces: deterministic owner-kill recovery assertion with no recovery-path skip.

- [ ] **Step 1: Replace arbitrary Worker selection**

Replace the 20-iteration poll and `worker_records[0]` selection with:

```python
task, owner = await wait_for_task_owner(
    test_store,
    handle.task_id,
    timeout_s=10.0,
)
assert task.leased_by == owner
killed_pid = terminate_worker(resources.host.supervisor, owner)
assert killed_pid > 0
```

Delete branches that skip when the task is queued, completed, or failed before the kill. The helper's diagnostic failure is the correct result when the scenario was not established.

- [ ] **Step 2: Strengthen postconditions**

After `wait_terminal`, assert:

```python
assert snapshot.state == "COMPLETED"
final_task = test_store.read_task(handle.task_id)
assert final_task is not None
assert final_task.root_attempt_count >= 2
assert 1 <= len(stub.requests) <= 2
```

Also query durable events/attempt rows using existing store methods and assert one logical Provider decision and no duplicate terminal/final-reply effect.

- [ ] **Step 3: Run the exact test six times**

Run:

```powershell
1..6 | ForEach-Object {
  py -m uv run pytest tests/runtime/test_runtime_fault_injection.py::TestSupervisedWorkerKillRecovery::test_task_recovers_after_worker_kill -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: 6/6 PASS; every run resolves a non-empty owner and terminates that owner.

- [ ] **Step 4: Run the complete runtime suite**

Run: `py -m uv run pytest tests/runtime -q`

Expected: all Runtime tests PASS, with only environment/feature skips already present before this task.

- [ ] **Step 5: Commit**

```powershell
git add tests/runtime/test_runtime_fault_injection.py
git commit -m "test: kill the task-owning worker during recovery"
```

---

### Task 8: Synchronize Lock Metadata and Establish the Stage A Checkpoint

**Files:**
- Modify: `uv.lock`
- Modify: repository-owned Python files changed by Tasks 2–7 if the formatter changes them

**Interfaces:**
- Produces: lock metadata matching `pyproject.toml` version `0.3.0` and Windows `tzdata>=2025.2` development resolution.
- Preserves: dependency layout is provisional until Task 20 performs the final documents-extra lock update.

- [ ] **Step 1: Regenerate and verify the lock**

Run:

```powershell
py -m uv lock
py -m uv lock --check
```

Expected: both exit 0; the local project entry is version `0.3.0`, and the Windows dev marker includes `tzdata`.

- [ ] **Step 2: Apply the locked formatter in one mechanical pass**

Run:

```powershell
py -m uv run ruff format miniunicorn tests scripts hatch_build.py
py -m uv run ruff format --check miniunicorn tests scripts hatch_build.py
py -m uv run ruff check miniunicorn tests scripts hatch_build.py
```

Expected: format and lint checks PASS. Review `git diff --stat` and confirm only repository-owned Python plus `uv.lock` changed.

- [ ] **Step 3: Run Stage A checkpoint**

Run:

```powershell
py -m uv lock --check
py -m uv run pytest -m core -q
py -m uv run pytest tests/runtime -q
Push-Location webui
npm run test:generated-check
npm run check:protocol
Pop-Location
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 4: Commit mechanical changes**

```powershell
git add uv.lock miniunicorn tests scripts hatch_build.py
git commit -m "chore: synchronize lock and python formatting"
```

If a listed path has no change, Git ignores it. This commit must not contain semantic edits.

---

## Stage B1: AgentRunner Boundaries

### Task 9: Characterize AgentRunner and Freeze the Target Boundaries

**Files:**
- Create: `tests/agent/test_runner_boundaries.py`
- Create: `tests/agent/test_runner_characterization.py`
- Read: `miniunicorn/agent/runner.py`

**Interfaces:**
- Consumes: existing `AgentRunner.run(spec) -> AgentRunResult` behavior.
- Produces: executable limits: `runner.py <= 450` lines after Task 12 and every method in `runner_control.py <= 200` lines.

- [ ] **Step 1: Inventory behavior before moving code**

Run:

```powershell
rg -n "^    (async )?def |^class " miniunicorn/agent/runner.py
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
py -m uv run pytest $runnerTests -q
```

Expected: existing runner tests PASS. Save the method list in the Task 9 commit message body so later reviews can distinguish moves from behavior changes.

- [ ] **Step 2: Add characterization for the public façade**

Add assertions for the current constructor and result types:

```python
def test_runner_public_surface_is_stable():
    assert str(inspect.signature(AgentRunner)) == "(provider: 'LLMProvider')"
    assert "spec" in inspect.signature(AgentRunner.run).parameters
    assert AgentRunSpec.__name__ == "AgentRunSpec"
    assert AgentRunResult.__name__ == "AgentRunResult"
```

In `test_runner_characterization.py`, add focused cases not already covered for: a no-tool final answer, one direct tool call, one gateway tool call, Provider error placeholder, budget stop, injected message, and finalization retry. Reuse current fixtures and assert exact messages/events/usage.

- [ ] **Step 3: Freeze public imports and numeric targets without making the branch red**

```python
RUNNER_FACADE_LINE_LIMIT = 450
RUNNER_CONTROL_METHOD_LINE_LIMIT = 200

def test_runner_public_imports_are_stable():
    from miniunicorn.agent.runner import AgentRunResult, AgentRunSpec, AgentRunner
    assert AgentRunner.__name__ == "AgentRunner"
    assert AgentRunSpec.__name__ == "AgentRunSpec"
    assert AgentRunResult.__name__ == "AgentRunResult"
```

The numeric constants are consumed by Task 12, which adds the failing end-state assertions immediately before completing the final extraction. This task must leave the suite green.

- [ ] **Step 4: Run and verify characterization is green**

Run:

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
py -m uv run pytest tests/agent/test_runner_boundaries.py tests/agent/test_runner_characterization.py $runnerTests -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the red boundary contract**

```powershell
git add tests/agent/test_runner_boundaries.py tests/agent/test_runner_characterization.py
git commit -m "test: freeze agent runner boundaries"
```

---

### Task 10: Extract Runner Types and Model Requests

**Files:**
- Create: `miniunicorn/agent/runner_types.py`
- Create: `miniunicorn/agent/runner_model.py`
- Modify: `miniunicorn/agent/runner.py`
- Test: `tests/agent/test_runner_model.py`
- Test: `tests/agent/test_runner_boundaries.py`

**Interfaces:**
- Produces: `ModelRequester(provider_getter: Callable[[], LLMProvider])` and re-exported `AgentRunSpec`/`AgentRunResult` from `miniunicorn.agent.runner`.
- Preserves mutable `AgentRunner.provider`; `ModelRequester` resolves the Provider through its getter for every call.
- Preserves existing monkeypatch points `runner._request_model` and `runner._request_finalization_retry` as one-return-statement delegates.

- [ ] **Step 1: Add failing model-client tests**

Define the expected result shape in the test:

```python
runner = AgentRunner(provider_a)
await runner.run(one_response_spec)
runner.provider = provider_b
await runner.run(second_response_spec)
assert provider_a.call_count == 1
assert provider_b.call_count == 1
```

Add cases for request kwargs parity, streaming callbacks, retry, cancellation, and Provider exception. Compare exact kwargs captured by the existing fake Provider.

- [ ] **Step 2: Run and confirm import failure**

Run: `py -m uv run pytest tests/agent/test_runner_model.py -q`

Expected: FAIL because `runner_model` does not exist.

- [ ] **Step 3: Create shared dataclasses without changing public identity**

Move the existing dataclass definitions into `runner_types.py`, then in `runner.py` import and re-export them. Preserve field names/defaults byte-for-byte and preserve caller imports through ordinary re-exports.

In `runner.py`, import and re-export with `__all__ = ["AgentRunner", "AgentRunResult", "AgentRunSpec"]`. Do not rename fields or change defaults.

- [ ] **Step 4: Move model methods as one behavior-preserving unit**

Create:

```python
class ModelRequester:
    def __init__(self, provider_getter: Callable[[], LLMProvider]) -> None:
        self._provider_getter = provider_getter

    @property
    def provider(self) -> LLMProvider:
        return self._provider_getter()
```

Move the exact request/stream/retry/timeout behavior into `request()` and `request_finalization()`. Construct with `self._model_requester = ModelRequester(lambda: self.provider)`.

Move the bodies of `_build_request_kwargs`, `_request_model`, `_request_finalization_retry`, `_usage_dict`, `_accumulate_usage`, and `_merge_usage` into this class. Pass callbacks/configuration explicitly; do not import `AgentRunner` from the collaborator.

- [ ] **Step 5: Delegate from the façade and verify**

Keep these façade delegates and replace only their bodies:

```python
async def _request_model(self, spec, messages, hook, context):
    return await self._model_requester.request(spec, messages, hook, context)

async def _request_finalization_retry(self, spec, messages):
    return await self._model_requester.request_finalization(spec, messages)
```

Run:

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
py -m uv run pytest tests/agent/test_runner_model.py tests/agent/test_runner_characterization.py $runnerTests -q
py -m uv run pytest tests/architecture -q
```

Expected: all behavior tests PASS; only later structural size gates may remain red.

- [ ] **Step 6: Commit**

```powershell
git add miniunicorn/agent/runner.py miniunicorn/agent/runner_types.py miniunicorn/agent/runner_model.py tests/agent/test_runner_model.py tests/agent/test_runner_boundaries.py
git commit -m "refactor: extract runner model client"
```

---

### Task 11: Extract Tool Execution and Result Normalization

**Files:**
- Create: `miniunicorn/agent/runner_tools.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/runner_types.py`
- Create: `tests/agent/test_runner_tools.py`

**Interfaces:**
- Produces: `ToolBatchResult(results, events, fatal_error)` in `runner_types.py` and `ToolExecutor.execute(spec, tool_calls, external_lookup_counts, workspace_violation_counts) -> ToolBatchResult`.
- Owns: current `_execute_tools`, `_run_tool`, `_run_tool_via_gateway`, policy/violation classification, normalization, result budget, history snipping, and batching.
- Preserves `_execute_tools` and directly monkeypatched static helpers as thin façade delegates.

- [ ] **Step 1: Add failing parity tests for direct, gateway, and violation paths**

Use current fake registry/gateway fixtures. Assert exact normalized content and metric/event payloads:

```python
result = await executor.execute(spec, calls, {}, {})
assert len(result.results) == 1
assert result.results[0]["tool_call_id"] == calls[0]["id"]
assert result.fatal_error is None
```

Add parallel-safe batching, sequential unsafe batching, SSRF soft payload, workspace violation, gateway lease fencing, result truncation, and tool exception cases.

- [ ] **Step 2: Run and confirm import failure**

Run: `py -m uv run pytest tests/agent/test_runner_tools.py -q`

Expected: FAIL because `runner_tools` does not exist.

- [ ] **Step 3: Define the explicit result type**

```python
@dataclass(slots=True)
class ToolBatchResult:
    results: list[Any]
    events: list[dict[str, str]]
    fatal_error: Exception | None = None
```

Keep the checkpoint emitter as an injected constructor dependency and pass `spec` plus violation counters explicitly. Do not retain the whole Runner or make registry/gateway state module-global.

- [ ] **Step 4: Move tool methods and delegate**

Move the full existing bodies of the named tool methods. Preserve ordering and exact exception-to-payload behavior. Keep `_execute_tools` as:

```python
async def _execute_tools(
    self, spec, tool_calls, external_lookup_counts, workspace_violation_counts
):
    batch = await self._tool_executor.execute(
        spec, tool_calls, external_lookup_counts, workspace_violation_counts
    )
    return batch.results, batch.events, batch.fatal_error
```

- [ ] **Step 5: Verify tool, runner, and security suites**

Run:

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
py -m uv run pytest tests/agent/test_runner_tools.py tests/agent/test_runner_characterization.py $runnerTests -q
py -m uv run pytest tests/security tests/architecture -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```powershell
git add miniunicorn/agent/runner.py miniunicorn/agent/runner_tools.py miniunicorn/agent/runner_types.py tests/agent/test_runner_tools.py
git commit -m "refactor: extract runner tool execution"
```

---

### Task 12: Extract ReAct Control Flow and Close the Runner Boundary

**Files:**
- Create: `miniunicorn/agent/runner_control.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/runner_types.py`
- Modify: `tests/agent/test_runner_boundaries.py`
- Create: `tests/agent/test_runner_control.py`

**Interfaces:**
- Produces: `RunLoopState`, `IterationAction`, and `RunController.run(spec) -> AgentRunResult`.
- Preserves: `AgentRunner.run(spec)` as the public call; it delegates to `RunController`. The controller receives the façade so existing monkeypatch points remain effective.

- [ ] **Step 1: Add red state-transition and end-state boundary tests**

Cover final answer, tool continuation, budget stop, cancellation, max-iteration finalization, injected-message continuation, Provider retry, and checkpoint failure. Expected enum:

```python
class IterationAction(Enum):
    CONTINUE = auto()
    BREAK = auto()

@dataclass(slots=True)
class RunLoopState:
    messages: list[dict[str, Any]]
    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    last_call_usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    stop_reason: str = "completed"
    tool_events: list[dict[str, str]] = field(default_factory=list)
    external_lookup_counts: dict[str, int] = field(default_factory=dict)
    workspace_violation_counts: dict[str, int] = field(default_factory=dict)
    empty_content_retries: int = 0
    length_recovery_count: int = 0
    had_injections: bool = False
    injection_cycles: int = 0
    plan: Any | None = None
    planner: Any | None = None
    planner_task_text: str | None = None
    planner_tools_summary: str | None = None
    reflection: Any | None = None
```

The state must contain exactly per-run mutable data and must not own a Provider, registry, callback, session object, or second task authority.

In the same red step, add `test_runner_facade_is_at_most_450_lines`, `test_runner_collaborator_modules_exist`, and the AST-based 200-line method test using the constants frozen in Task 9.

- [ ] **Step 2: Run and confirm missing module**

Run: `py -m uv run pytest tests/agent/test_runner_control.py -q`

Expected: FAIL importing `runner_control`.

- [ ] **Step 3: Move control flow behind explicit collaborators**

Create `RunController(owner: AgentRunner, reflection_supervisor)`. Split the current loop into setup, prepare iteration, consume model response, execute tools, consume final response, and finish exhausted phases. Pass mutable per-run data through `RunLoopState`, not shared controller state.

The façade becomes structurally equivalent to:

```python
class AgentRunner:
    def __init__(self, provider: LLMProvider):
        self._model_requester = ModelRequester(lambda: self.provider)

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        return await self._run_controller.run(spec)
```

Keep the existing constructor-created governor, injection, checkpoint, registry, gateway, and reflection dependencies. `RunController` calls the façade delegates so existing extension/monkeypatch seams remain compatible.

- [ ] **Step 4: Enforce line and method limits with AST**

Extend boundary tests:

```python
def test_runner_control_methods_are_at_most_200_lines(repo_root: Path):
    source = (repo_root / "miniunicorn/agent/runner_control.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    oversized = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
        and node.end_lineno - node.lineno + 1 > 200
    }
    assert oversized == {}
```

- [ ] **Step 5: Run complete runner checkpoint**

Run:

```powershell
py -m uv run pytest tests/agent/test_runner_boundaries.py tests/agent/test_runner_control.py tests/agent/test_runner_model.py tests/agent/test_runner_tools.py -q
py -m uv run pytest tests/agent -q
py -m uv run pytest tests/architecture -q
```

Expected: all PASS; `runner.py <= 450`; no collaborator imports `AgentRunner`.

- [ ] **Step 6: Commit**

```powershell
git add miniunicorn/agent/runner.py miniunicorn/agent/runner_control.py miniunicorn/agent/runner_types.py tests/agent/test_runner_boundaries.py tests/agent/test_runner_control.py
git commit -m "refactor: extract runner control flow"
```

---

## Stage B2: WebUI Boundaries

### Task 13: Extract Pure Stream State and Reducer

**Files:**
- Create: `webui/src/hooks/stream-state.ts`
- Create: `webui/src/hooks/stream-reducer.ts`
- Create: `webui/src/tests/stream-reducer.test.ts`
- Modify: `webui/src/hooks/useMiniunicornStream.ts`
- Modify: `webui/src/tests/useMiniunicornStream.test.tsx`

**Interfaces:**
- Produces: `StreamState`, `StreamAction`, `createInitialStreamState(chatId)`, and `reduceStream(state, action) -> StreamState`.
- Preserves: hook public return type, generated event field names, ordering, delta batching, placeholder merging, file-edit deduplication, goal state, and latency behavior.

- [ ] **Step 1: Add reducer tests for existing high-risk transitions**

Define actions with current generated payload types:

```typescript
const next = reduceStream(state, {
  type: "reasoning_delta",
  chatId: "chat-1",
  text: "thinking",
  receivedAt: 100,
});
expect(next.messages.at(-1)?.reasoning).toBe("thinking");
```

Add cases for `reasoning_end`, answer delta, `stream_end`, `turn_end`, tool progress, file-edit placeholder upgrade, interrupted pre-tool text, media attachment, session switch, and goal state restore.

- [ ] **Step 2: Run and confirm missing-module failure**

Run: `Push-Location webui; npm test -- --run src/tests/stream-reducer.test.ts; Pop-Location`

Expected: FAIL importing `stream-reducer`.

- [ ] **Step 3: Define state without React or browser dependencies**

```typescript
export interface StreamState {
  chatId: string;
  messages: UIMessage[];
  streaming: boolean;
  runStartedAt: number | null;
  goalState: GoalStateWsPayload | undefined;
  cursor: ActiveAssistantCursor | null;
}

export function createInitialStreamState(
  chatId: string,
  messages: UIMessage[] = [],
): StreamState {
  return { chatId, messages, streaming: false, runStartedAt: null, goalState: undefined, cursor: null };
}
```

Move `StreamBuffer`, `ActiveAssistantCursor`, pure message helpers, and immutable merge helpers out of the hook.

- [ ] **Step 4: Implement pure actions and keep effects in the hook**

`reduceStream` may call only pure helpers. Keep WebSocket subscribe/unsubscribe, `requestAnimationFrame`, timers, refs, callbacks, and React state synchronization in `useMiniunicornStream.ts`. Dispatch actions after effect data is ready.

- [ ] **Step 5: Verify behavior and size**

Add:

```typescript
it("keeps the stream hook below 650 physical lines", () => {
  const source = readFileSync(resolve(process.cwd(), "src/hooks/useMiniunicornStream.ts"), "utf8");
  expect(source.split(/\r?\n/).length).toBeLessThan(650);
});
```

Run:

```powershell
Push-Location webui
npm test -- --run src/tests/stream-reducer.test.ts src/tests/useMiniunicornStream.test.tsx
npm run lint
npm run build
Pop-Location
```

Expected: all PASS and hook is below 650 lines.

- [ ] **Step 6: Commit**

```powershell
git add webui/src/hooks/stream-state.ts webui/src/hooks/stream-reducer.ts webui/src/hooks/useMiniunicornStream.ts webui/src/tests/stream-reducer.test.ts webui/src/tests/useMiniunicornStream.test.tsx
git commit -m "refactor: extract webui stream reducer"
```

---

### Task 14: Split Activity Parsing from Rendering

**Files:**
- Create: `webui/src/components/thread/activity/types.ts`
- Create: `webui/src/components/thread/activity/trace-format.ts`
- Create: `webui/src/components/thread/activity/cli-runs.ts`
- Create: `webui/src/components/thread/activity/mcp-runs.ts`
- Create: `webui/src/components/thread/activity/file-edits.ts`
- Create: `webui/src/components/thread/activity/ActivityTraceTimeline.tsx`
- Create: `webui/src/components/thread/activity/CliRunGroup.tsx`
- Create: `webui/src/components/thread/activity/McpRunGroup.tsx`
- Create: `webui/src/components/thread/activity/FileEditGroup.tsx`
- Create: `webui/src/components/thread/activity/AnimatedNumber.tsx`
- Create: `webui/src/tests/activity-parsers.test.ts`
- Modify: `webui/src/components/thread/AgentActivityCluster.tsx`
- Modify: `webui/src/tests/agent-activity-cluster.test.tsx`

**Interfaces:**
- Produces pure parsers `describeTraceLine`, `collectCliRuns`, `collectMcpRuns`, `summarizeFileEdits` and focused presentational components.
- Preserves: `AgentActivityCluster` export and all visual/i18n/accessibility behavior.

- [ ] **Step 1: Add parser tests before moving functions**

Cover public URL versus private hostname, shell redaction, MCP preset parsing, CLI chronological merge, file-edit success-over-failure merge, delete labeling, pathless pending removal, and zero-diff suppression. Import the functions from the new path so the test is initially red.

- [ ] **Step 2: Move pure types and parsers**

Move `ActivityCounts`, `FileEditSummary`, `CliRunSummary`, `McpRunSummary`, trace description/URL/shell helpers, and collection/merge functions into the responsibility-named files. Export exact typed signatures, for example:

```typescript
export function collectCliRuns(messages: UIMessage[]): CliRunSummary[];
export function collectMcpRuns(messages: UIMessage[]): McpRunSummary[];
export function summarizeFileEdits(edits: UIFileEdit[], active: boolean): FileEditSummary[];
```

Parser modules must not import React, DOM APIs, or i18n hooks.

- [ ] **Step 3: Move focused views without changing markup**

Move the trace timeline, CLI, MCP, file-edit groups, and animated/rolling number helpers with their exact existing class names, ARIA attributes, translation keys, and child ordering. Keep disclosure/scroll/auto-collapse orchestration in `AgentActivityCluster.tsx`.

- [ ] **Step 4: Add and satisfy the façade size test**

```typescript
it("keeps AgentActivityCluster at or below 500 physical lines", () => {
  const source = readFileSync(
    resolve(process.cwd(), "src/components/thread/AgentActivityCluster.tsx"),
    "utf8",
  );
  expect(source.split(/\r?\n/).length).toBeLessThanOrEqual(500);
});
```

- [ ] **Step 5: Run full frontend checkpoint and commit**

Run:

```powershell
Push-Location webui
npm test -- --run src/tests/activity-parsers.test.ts src/tests/agent-activity-cluster.test.tsx
npm run check:protocol
npm run lint
npm test
npm run build
Pop-Location
```

Expected: all commands PASS. Then:

```powershell
git add webui/src/components/thread/AgentActivityCluster.tsx webui/src/components/thread/activity webui/src/tests/activity-parsers.test.ts webui/src/tests/agent-activity-cluster.test.tsx
git commit -m "refactor: split agent activity components"
```

---

## Stage B3: Channel Boundaries

### Task 15: Extract WebSocket Outbound Emission

**Files:**
- Create: `miniunicorn/channels/websocket/outbound.py`
- Create: `tests/channels/test_websocket_outbound.py`
- Modify: `miniunicorn/channels/websocket/channel.py`
- Create: `tests/architecture/test_completion_file_boundaries.py`

**Interfaces:**
- Produces: `WebSocketOutboundEmitter` with `send_agent_event`, `send_message`, `send_reasoning_delta`, `send_reasoning_end`, `send_delta`, `send_turn_end`, and goal/session/model update methods.
- Preserves: `WebSocketChannel` public methods and exact wire events.

- [ ] **Step 1: Add red serialization/fan-out tests**

Construct fake connections and transcript sink. Assert exact JSON for answer, reasoning, turn end, goal status/state, session update, and runtime model update; assert one broken connection does not prevent delivery to another and is logged through the supplied logger callback.

- [ ] **Step 2: Define dependencies explicitly**

```python
class WebSocketOutboundEmitter:
    def __init__(
        self,
        *,
        connections_for_chat: Callable[[str], Iterable[Any]],
        safe_send: Callable[[Any, str], Awaitable[None]],
        rewrite_markdown_images: Callable[[str], str],
        append_transcript: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._connections_for_chat = connections_for_chat
        self._safe_send = safe_send
        self._rewrite_markdown_images = rewrite_markdown_images
        self._append_transcript = append_transcript
```

No lifecycle, HTTP routing, authentication, or inbound envelope logic belongs in this service.

- [ ] **Step 3: Move outbound bodies and leave delegators**

Each original public method delegates with unchanged signature. Keep connection cleanup/lifecycle in the Channel. Do not change event field names or protocol version.

- [ ] **Step 4: Add the `<1450` line gate and verify**

Add `test_websocket_channel_is_below_1450_lines` to the new architecture file, then run:

```powershell
py -m uv run pytest tests/channels/test_websocket_outbound.py tests/channels/test_websocket_channel.py tests/channels/test_websocket_integration.py -q
py -m uv run pytest tests/architecture -q
```

Expected: PASS; `channel.py < 1450` physical lines.

- [ ] **Step 5: Commit**

```powershell
git add miniunicorn/channels/websocket/outbound.py miniunicorn/channels/websocket/channel.py tests/channels/test_websocket_outbound.py tests/architecture
git commit -m "refactor: extract websocket outbound emission"
```

---

### Task 16: Extract Feishu Rendering

**Files:**
- Create: `miniunicorn/channels/feishu/rendering.py`
- Create: `tests/channels/test_feishu_rendering_service.py`
- Modify: `miniunicorn/channels/feishu/channel.py`

**Interfaces:**
- Produces: `strip_markdown_formatting`, `parse_markdown_table`, `build_card_elements`, `split_elements_by_table_limit`, `split_headings`, `detect_message_format`, `markdown_to_post`, `fallback_text_chunks`, and tool-hint formatting.
- Preserves: current class methods on `FeishuChannel` as compatibility delegators where tests/extensions call them.

- [ ] **Step 1: Add red pure-rendering parity tests**

Move representative current assertions to the new module import path: headings, tables at the platform limit, code fences, mentions, post payload, interactive fallback chunks, and tool hints containing code blocks.

- [ ] **Step 2: Move pure functions without SDK state**

Export typed functions. Example compatibility delegator:

```python
@classmethod
def _strip_md_formatting(cls, text: str) -> str:
    return strip_markdown_formatting(text)
```

Do not import the Feishu SDK in `rendering.py`.

- [ ] **Step 3: Verify rendering suites**

Run:

```powershell
py -m uv run pytest tests/channels/test_feishu_rendering_service.py tests/channels/test_feishu_markdown_rendering.py tests/channels/test_feishu_post_content.py tests/channels/test_feishu_table_split.py tests/channels/test_feishu_tool_hint_code_block.py -q
```

Expected: PASS with byte-for-byte payload parity.

- [ ] **Step 4: Commit**

```powershell
git add miniunicorn/channels/feishu/rendering.py miniunicorn/channels/feishu/channel.py tests/channels/test_feishu_rendering_service.py
git commit -m "refactor: extract feishu rendering"
```

---

### Task 17: Extract Feishu Media Operations

**Files:**
- Create: `miniunicorn/channels/feishu/media.py`
- Create: `tests/channels/test_feishu_media_service.py`
- Modify: `miniunicorn/channels/feishu/channel.py`
- Modify: `tests/architecture/test_completion_file_boundaries.py`

**Interfaces:**
- Produces: `FeishuMediaService.upload_image`, `upload_file`, `download_image`, `download_file`, `download_and_save`, and `safe_media_filename`.
- Preserves: existing SDK client, thread offloading, path-safety rules, and Channel method signatures.

- [ ] **Step 1: Add red service tests**

Use fake SDK clients to assert request shapes, return keys, download bytes, sanitized traversal filenames, extension preservation, and failure-to-`None` behavior identical to current Channel methods.

- [ ] **Step 2: Implement injected SDK service**

```python
class FeishuMediaService:
    def __init__(self, *, client: Any, media_dir: Path) -> None:
        self._client = client
        self._media_dir = media_dir

    @staticmethod
    def safe_media_filename(filename: str | None, fallback: str) -> str:
        candidate = Path(filename or fallback).name
        return candidate if candidate not in {"", ".", ".."} else fallback
```

Keep synchronous SDK calls synchronous inside the service; the Channel retains `asyncio.to_thread` ownership where it currently exists.

- [ ] **Step 3: Delegate and verify all Feishu tests**

Run:

```powershell
py -m uv run pytest tests/channels -k feishu -q
py -m uv run pytest tests/architecture -q
```

Expected: PASS; `miniunicorn/channels/feishu/channel.py < 1500` physical lines.

- [ ] **Step 4: Commit**

```powershell
git add miniunicorn/channels/feishu/media.py miniunicorn/channels/feishu/channel.py tests/channels/test_feishu_media_service.py tests/architecture
git commit -m "refactor: extract feishu media service"
```

---

### Task 18: Extract Weixin Crypto, API, and Media Services

**Files:**
- Create: `miniunicorn/channels/weixin/crypto.py`
- Create: `miniunicorn/channels/weixin/api_client.py`
- Create: `miniunicorn/channels/weixin/media.py`
- Create: `tests/channels/test_weixin_crypto.py`
- Create: `tests/channels/test_weixin_api_client.py`
- Create: `tests/channels/test_weixin_media.py`
- Modify: `miniunicorn/channels/weixin/channel.py`
- Modify: `tests/architecture/test_completion_file_boundaries.py`

**Interfaces:**
- Produces crypto functions `parse_aes_key`, `encrypt_aes_ecb`, `decrypt_aes_ecb`, `pkcs7_unpad_safe`; `WeixinApiClient.get/post/get_with_base`; and `WeixinMediaService.download/send_file`.
- Preserves module-level underscored crypto imports from `channel.py` through aliases, HTTP headers/auth behavior, retry classification, media wire format, and `WeixinChannel` public API.

- [ ] **Step 1: Add red crypto known-answer tests**

Move current round-trip cases and add fixed vectors for valid padding, invalid padding, invalid key length, and binary data. Import from `miniunicorn.channels.weixin.crypto`.

- [ ] **Step 2: Add red API/media tests**

With `httpx.MockTransport`, assert exact URL, headers, query/body, auth/no-auth, error propagation, retryable media classification, file extension, encrypted bytes, and upload message payload.

- [ ] **Step 3: Implement and preserve compatibility aliases**

In `channel.py`:

```python
from .crypto import (
    decrypt_aes_ecb as _decrypt_aes_ecb,
    encrypt_aes_ecb as _encrypt_aes_ecb,
    parse_aes_key as _parse_aes_key,
    pkcs7_unpad_safe as _pkcs7_unpad_safe,
)
```

Instantiate `WeixinApiClient` and `WeixinMediaService` from existing config/state; do not let either own polling, login state transitions, typing state, or message dispatch.

- [ ] **Step 4: Satisfy the `<1150` line gate**

Run:

```powershell
py -m uv run pytest tests/channels/test_weixin_crypto.py tests/channels/test_weixin_api_client.py tests/channels/test_weixin_media.py tests/channels/test_weixin_channel.py -q
py -m uv run pytest tests/channels -q
py -m uv run pytest tests/architecture -q
```

Expected: all PASS; `miniunicorn/channels/weixin/channel.py < 1150` physical lines.

- [ ] **Step 5: Run Stage B whole-system checkpoint**

Run:

```powershell
py -m uv run pytest tests/agent tests/channels tests/architecture -q
Push-Location webui
npm run check:protocol
npm run lint
npm test
npm run build
Pop-Location
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 6: Commit**

```powershell
git add miniunicorn/channels/weixin tests/channels/test_weixin_crypto.py tests/channels/test_weixin_api_client.py tests/channels/test_weixin_media.py tests/channels/test_weixin_channel.py tests/architecture/test_completion_file_boundaries.py
git commit -m "refactor: extract weixin channel services"
```

---

## Stage C: Packaging and Optional Documents

### Task 19: Define the Documents Extra and Actionable Missing-Backend Error

**Files:**
- Modify: `pyproject.toml`
- Modify: `miniunicorn/utils/document.py`
- Modify: `tests/test_document_parsing.py`
- Modify: `tests/test_api_attachment.py`
- Modify: `tests/test_context_documents.py`
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `docs/quick-start.md`

**Interfaces:**
- Produces: `MissingDocumentBackendError(extension: str, distribution: str)` whose message contains `pip install 'miniunicorn-ai[documents]'`.
- Produces extras: `documents = [pypdf, python-docx, openpyxl, python-pptx]`; existing `pdf` remains compatible by containing both its current `pymupdf` and `pypdf`.
- Preserves: lazy imports and text/image routing.

- [ ] **Step 1: Add red dependency-layout assertions**

Parse `pyproject.toml` and assert:

```python
base = set(project["dependencies"])
documents = set(project["optional-dependencies"]["documents"])
assert not any(name in " ".join(base) for name in ("pypdf", "python-docx", "openpyxl", "python-pptx"))
assert all(name in " ".join(documents) for name in ("pypdf", "python-docx", "openpyxl", "python-pptx"))
assert "pypdf" in " ".join(project["optional-dependencies"]["pdf"])
```

Also assert the `dev` extra directly contains all four document distributions and keeps its current `pymupdf` entry.

- [ ] **Step 2: Add red missing-backend tests for all four extensions**

Block each parser import in turn and assert:

```python
with pytest.raises(MissingDocumentBackendError) as exc:
    extract_text(document_path)
assert exc.value.extension == ".docx"
assert exc.value.distribution == "python-docx"
assert "miniunicorn-ai[documents]" in str(exc.value)
```

Use `.pdf`/`pypdf`, `.docx`/`python-docx`, `.xlsx`/`openpyxl`, and `.pptx`/`python-pptx` parametrization. Existing extraction failures after a backend imports remain returned/logged according to current caller contract; only absent packages use this explicit exception.

- [ ] **Step 3: Run and confirm current string-return behavior fails**

Run: `py -m uv run pytest tests/test_document_parsing.py -q`

Expected: FAIL because base dependencies still contain the backends and missing imports return parser-specific `not installed` strings.

- [ ] **Step 4: Implement the exception and dependency move**

```python
class MissingDocumentBackendError(RuntimeError):
    def __init__(self, extension: str, distribution: str) -> None:
        self.extension = extension
        self.distribution = distribution
        super().__init__(
            f"Reading {extension} files requires {distribution}. "
            "Install it with: pip install 'miniunicorn-ai[documents]'"
        )
```

Each lazy import raises this exception from the `ImportError` with the exact extension/distribution pair. Update high-level `extract_documents` callers only where they currently convert parser failures into user-visible attachment text; preserve an actionable message instead of silently dropping the file.

- [ ] **Step 5: Verify document behavior**

Run:

```powershell
py -m uv run pytest tests/test_document_parsing.py tests/test_api_attachment.py tests/test_context_documents.py tests/agent/test_document_extraction_toggle.py -q
```

Expected: PASS.

- [ ] **Step 6: Document exact install profiles**

Use these commands consistently in all three documentation files:

```bash
pip install miniunicorn-ai
pip install "miniunicorn-ai[documents]"
pip install -e ".[api,vector,pdf,documents,dev]"
```

Explain that `[documents]` supplies PDF/DOCX/XLSX/PPTX extraction and `[pdf]` remains the PDF-specific compatibility profile.

- [ ] **Step 7: Commit**

```powershell
git add pyproject.toml miniunicorn/utils/document.py tests/test_document_parsing.py tests/test_api_attachment.py tests/test_context_documents.py README.md README.en.md docs/quick-start.md
git commit -m "packaging: move document backends to an extra"
```

---

### Task 20: Verify Wheel, Sdist, Minimal, Documents, and Docker Installs

**Files:**
- Create: `tests/packaging/test_artifacts.py`
- Create: `scripts/verify_package_install.py`
- Modify: `Dockerfile`
- Modify: `.github/workflows/ci.yml`
- Modify: `uv.lock`
- Read: `hatch_build.py`

**Interfaces:**
- Produces: artifact inspection and isolated-install verifier; does not depend on the source checkout being importable.
- Preserves: bundled `miniunicorn/web/dist` assets and the CLI entry point.

- [ ] **Step 1: Add red artifact metadata tests**

Build into a temporary directory and inspect wheel/sdist archives. Assert wheel metadata excludes all four document backends from unconditional `Requires-Dist`, exposes the `documents` and `pdf` extras, contains `miniunicorn/web/dist/index.html`, and contains no repository tests or local caches.

Expected helper signature:

```python
def inspect_wheel(path: Path) -> dict[str, object]:
    return {
        "names": sorted(zipfile.ZipFile(path).namelist()),
        "metadata": metadata_text,
    }
```

- [ ] **Step 2: Add an isolated install verifier**

`scripts/verify_package_install.py` accepts `--artifact`, `--extra {base,documents,pdf}`, and `--work-dir`. It creates a venv, installs the artifact using `python -m pip`, runs imports from a temporary directory outside the repository, and returns non-zero on any missing expected/forbidden dependency.

Base assertions:

```python
assert importlib.util.find_spec("miniunicorn") is not None
assert importlib.util.find_spec("pypdf") is None
assert importlib.util.find_spec("docx") is None
assert importlib.util.find_spec("openpyxl") is None
assert importlib.util.find_spec("pptx") is None
```

Documents assertions require `pypdf`, `docx`, `openpyxl`, and `pptx`. PDF assertions require both `fitz` (PyMuPDF) and `pypdf`, while asserting DOCX/XLSX/PPTX are absent.

- [ ] **Step 3: Run red package tests**

Run: `py -m uv run pytest tests/packaging/test_artifacts.py -q`

Expected: FAIL because dependency metadata still needs final lock/build/CI wiring or the verifier does not exist.

- [ ] **Step 4: Keep Docker and CI document-capable**

Change the Docker install target to the local package with the documents extra, preserving all other flags. Add CI commands that build wheel and sdist, run the base verifier, run the documents verifier, and inspect bundled WebUI assets. The base verification environment must not inherit repository dev dependencies.

- [ ] **Step 5: Regenerate the final lock and verify artifacts**

Run:

```powershell
py -m uv lock
py -m uv lock --check
$distTarget = 'D:\MyProject\MiniUnicorn-worktrees\full-remediation\dist-remediation'
$expectedTarget = Join-Path (Resolve-Path .).Path 'dist-remediation'
if ([IO.Path]::GetFullPath($distTarget) -ne [IO.Path]::GetFullPath($expectedTarget)) {
  throw "refusing to remove unexpected artifact directory: $distTarget"
}
Remove-Item -LiteralPath $distTarget -Recurse -Force -ErrorAction SilentlyContinue
py -m uv build --out-dir dist-remediation
py -m uv run pytest tests/packaging/test_artifacts.py -q
py -m uv run --with twine twine check dist-remediation/*
```

Expected: the validation confirms the explicit target is under this worktree before removal; build, artifact tests, and Twine validation all pass.

- [ ] **Step 6: Run isolated base and documents installs**

Run:

```powershell
$wheel = (Get-ChildItem .\dist-remediation\miniunicorn_ai-*.whl | Select-Object -First 1).FullName
py -m uv run python scripts/verify_package_install.py --artifact $wheel --extra base --work-dir .package-smoke-base
py -m uv run python scripts/verify_package_install.py --artifact $wheel --extra documents --work-dir .package-smoke-documents
py -m uv run python scripts/verify_package_install.py --artifact $wheel --extra pdf --work-dir .package-smoke-pdf
py -m uv run --all-extras python -c "import pypdf, docx, openpyxl, pptx; print('all extras include documents')"
```

Expected: all isolated verifiers exit 0 outside the source tree, and the all-extras source profile prints `all extras include documents`.

- [ ] **Step 7: Run Docker smoke when Docker is available**

Run:

```powershell
docker version
docker build -t miniunicorn-remediation:local .
docker run --rm miniunicorn-remediation:local python -c "import pypdf, docx, openpyxl, pptx; import miniunicorn; print(miniunicorn.__version__)"
```

Expected when Docker is available: build and import smoke PASS. If `docker version` itself is unavailable, record `environment unavailable` with the literal error in final evidence; this is the only permitted environmental deferral and does not convert the gate to PASS.

- [ ] **Step 8: Commit**

```powershell
git add pyproject.toml uv.lock Dockerfile .github/workflows/ci.yml tests/packaging/test_artifacts.py scripts/verify_package_install.py
git commit -m "test: verify minimal and document package installs"
```

Do not add `dist-remediation/`.

---

### Task 21: Make Source Version Resolution Tolerate Bad Metadata

**Files:**
- Modify: `miniunicorn/__init__.py`
- Modify: `tests/test_package_version.py`

**Interfaces:**
- Preserves resolution order: valid source `pyproject.toml`, then installed distribution metadata, then `0.0.0+unknown`.
- Produces: missing, unreadable, malformed TOML, missing `[project]`, and non-string `project.version` all return `None` from `_read_pyproject_version()`.

- [ ] **Step 1: Add red parametrized source-metadata tests**

Monkeypatch `Path.exists`/`Path.read_text` or the module's resolved path seam to cover:

```python
pytest.param("[project\nversion='broken'", id="malformed-toml"),
pytest.param("[tool.example]\nvalue=1", id="missing-project"),
pytest.param("[project]\nname='x'", id="missing-version"),
pytest.param("[project]\nversion=3", id="non-string-version"),
```

Add an unreadable file case raising `OSError("denied")`. In every case, mock installed metadata to return `9.9.9` and assert `_resolve_version() == "9.9.9"`.

- [ ] **Step 2: Run and confirm exceptions escape today**

Run: `py -m uv run pytest tests/test_package_version.py -q`

Expected: malformed TOML and unreadable cases FAIL by raising.

- [ ] **Step 3: Implement narrow fallback handling**

```python
try:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
except (OSError, UnicodeError, tomllib.TOMLDecodeError):
    return None
version = data.get("project", {}).get("version")
return version if isinstance(version, str) and version else None
```

Do not swallow exceptions from unrelated later package initialization.

- [ ] **Step 4: Verify source and installed contexts**

Run:

```powershell
py -m uv run pytest tests/test_package_version.py -q
py -m uv run python -c "import miniunicorn; assert miniunicorn.__version__ == '0.3.0'; print(miniunicorn.__version__)"
```

Expected: tests PASS and source import prints `0.3.0`.

- [ ] **Step 5: Commit**

```powershell
git add miniunicorn/__init__.py tests/test_package_version.py
git commit -m "fix: tolerate malformed source version metadata"
```

---

### Task 22: Close the Packaging Stage

**Files:**
- Modify: `docs/superpowers/evidence/2026-08-03-remediation-baseline.md`
- Modify: none else unless a gate exposes a defect; fix exposed defects in a separate task-sized commit before repeating this checkpoint.

**Interfaces:**
- Produces: Stage C evidence table with literal commands, exit codes, artifact names, and Docker status.

- [ ] **Step 1: Run packaging and dependency gates from a clean source state**

Run:

```powershell
py -m uv lock --check
py -m uv run pytest tests/test_document_parsing.py tests/packaging tests/test_package_version.py -q
py -m uv build --out-dir dist-remediation
$wheel = (Get-ChildItem .\dist-remediation\miniunicorn_ai-*.whl | Select-Object -First 1).FullName
py -m uv run python scripts/verify_package_install.py --artifact $wheel --extra base --work-dir .package-smoke-base
py -m uv run python scripts/verify_package_install.py --artifact $wheel --extra documents --work-dir .package-smoke-documents
py -m uv run python scripts/verify_package_install.py --artifact $wheel --extra pdf --work-dir .package-smoke-pdf
```

Print `$wheel` before executing and confirm it resolves under `D:\MyProject\MiniUnicorn-worktrees\full-remediation\dist-remediation`. Resolve and validate each `.package-smoke-*` directory under this worktree before removing it after the test.

- [ ] **Step 2: Run neighboring full gates**

Run:

```powershell
py -m uv run pytest tests/architecture tests/agent tests/channels tests/runtime -q
Push-Location webui
npm run check:protocol
npm run lint
npm test
npm run build
Pop-Location
```

Expected: all PASS.

- [ ] **Step 3: Record evidence and commit**

Append `## Stage C Checkpoint` with command, exit, artifact path, and result rows. For Docker use only `PASS` or `ENVIRONMENT UNAVAILABLE:` followed by the exact `docker version` error.

```powershell
git add -f docs/superpowers/evidence/2026-08-03-remediation-baseline.md
git commit -m "docs: record packaging checkpoint"
```

---

## Stage D: Real Operational Proof

### Task 23: Prove Real CPU Embedding, Persistence, Recall, and Fallback

**Files:**
- Create: `scripts/verify_embedding_memory.py`
- Create: `tests/integration/test_embedding_memory_integration.py`
- Modify: `miniunicorn/agent/memory.py`
- Modify: `tests/providers/test_local_embedding.py` only if its marker needs registration
- Read: `miniunicorn/providers/local_embedding.py`
- Read: `miniunicorn/runtime/sqlite/vector_memory_store.py`

**Interfaces:**
- Consumes: `LocalEmbeddingProvider.embed(texts, model=None) -> list[list[float]]`, `create_vector_store(db_path, embedding_dim=512, model_id="BAAI/bge-small-zh-v1.5")`, `MemoryStore.index_text`, `VectorMemoryStore.index`, and `search`.
- Produces backward-compatible optional keyword arguments `MemoryStore.index_text(text, kind="history", metadata=None, importance=0.5, source_identity="", source_revision="", scope=None)` and forwards them to the vector store.
- Produces: JSON evidence containing model id, dimension, database path, row count, top recalled text, idempotent row count, and fallback status.

- [ ] **Step 1: Add integration tests with a deterministic fake embedding**

Use real sqlite-vec when installed, `MemoryStore`, and a fake local Provider that returns deterministic 512-dimensional vectors. Attach the real vector store, call `memory.set_embed_provider`, and call `await memory.index_text` for two scoped Chinese texts with fixed `source_identity` and `source_revision` pairs. Close/reopen the store, search using the matching query vector, and assert the correct text ranks first. Re-index the same identity/revision and assert the row count is unchanged. Add a monkeypatched sqlite-vec load failure and assert `NoOpVectorStore.enabled is False` without deleting authoritative input text.

- [ ] **Step 2: Run and observe the missing forwarding arguments**

Run: `py -m uv run --extra vector pytest tests/integration/test_embedding_memory_integration.py tests/agent/test_vector_memory_fingerprint.py -q`

Expected before the production change: the new integration test FAILS because `MemoryStore.index_text` does not accept/forward `source_identity`, `source_revision`, and `scope`. Existing fingerprint tests PASS.

- [ ] **Step 3: Extend the actual MemoryStore indexing seam**

Change the signature and forwarding call:

```python
async def index_text(
    self,
    text: str,
    kind: str = "history",
    metadata: dict | None = None,
    importance: float = 0.5,
    *,
    source_identity: str = "",
    source_revision: str = "",
    scope: dict[str, str] | None = None,
) -> None:
    embeddings = await self._embed_provider.embed([text], model=self._embed_model)
    if embeddings:
        self._vector_store.index(
            text,
            embeddings[0],
            kind=kind,
            metadata=metadata,
            importance=importance,
            source_identity=source_identity,
            source_revision=source_revision,
            scope=scope,
        )
```

Retain the current disabled/empty/provider guards and exception behavior around this core. Update archive/procedural callers that already have a cursor to pass stable source identity and `str(cursor)` revision; existing callers without an identity remain valid.

- [ ] **Step 4: Re-run deterministic integration**

Run: `py -m uv run --extra vector pytest tests/integration/test_embedding_memory_integration.py tests/agent/test_vector_memory_fingerprint.py tests/agent/test_upgrade_integration.py -q`

Expected: PASS with no required vector case skipped in the final `[vector]` environment.

- [ ] **Step 5: Implement the real verification script**

The script accepts `--workspace`, defaults model to `BAAI/bge-small-zh-v1.5`, creates `workspace/memory/memory.db`, embeds these exact texts:

```python
documents = [
    "MiniUnicorn 使用本地 CPU 嵌入保存长期记忆。",
    "三工作进程运行时负责持久任务执行。",
]
query = "本地嵌入如何保存记忆？"
```

Assert every vector has 512 finite values and norm within `1e-5` of 1.0. Attach the real vector store and real `LocalEmbeddingProvider` to a `MemoryStore`; index through `await memory.index_text` with scope `{"tenant_id": "local", "principal_id": "owner", "agent_id": "main", "workspace_id": "embedding-proof"}` plus deterministic `source_identity`/`source_revision`. Close/reopen, embed the query through the same Provider, search with the same scope, and require the first document at rank 1. Re-index through `MemoryStore` with the same source identity/revision and assert no duplicate row. Instantiate `NoOpVectorStore`, assert `enabled is False`, `index("authoritative text", [0.0] * 512) is None`, and `search([0.0] * 512) == []`, then record `fallback_status="safe-noop"`. Print and write `embedding-memory-evidence.json`.

- [ ] **Step 6: Run the existing real-model smoke**

Run:

```powershell
$env:MINIUNICORN_RUN_EMBEDDING_SMOKE='1'
py -m uv run --extra vector pytest tests/providers/test_local_embedding.py::TestRealModelSmoke -q
Remove-Item Env:MINIUNICORN_RUN_EMBEDDING_SMOKE
```

Expected: PASS; the approved model downloads/loads and returns one normalized 512-dimensional vector.

- [ ] **Step 7: Run the end-to-end persistence verifier**

Run:

```powershell
py -m uv run --extra vector python scripts/verify_embedding_memory.py --workspace .embedding-evidence-workspace
```

Expected: exit 0; `.embedding-evidence-workspace/memory/memory.db` exists; JSON reports `dimension=512`, `row_count=2`, `row_count_after_reindex=2`, and the first document as `top_text`.

- [ ] **Step 8: Commit code and tests, not generated evidence databases**

```powershell
git add miniunicorn/agent/memory.py scripts/verify_embedding_memory.py tests/integration/test_embedding_memory_integration.py tests/providers/test_local_embedding.py
git commit -m "test: prove embedding memory persistence and recall"
```

If `tests/providers/test_local_embedding.py` is unchanged, omit it. Keep the JSON for Task 27 but never add `memory.db` or model cache files.

---

### Task 24: Prove One Control Plane and Exactly Three Real Workers

**Files:**
- Modify: `tests/runtime/test_three_worker_acceptance.py`
- Modify: `tests/runtime/test_supervised_golden_flow.py`
- Create: `scripts/verify_three_worker_runtime.py`
- Read: `tests/runtime/support/openai_stub.py`

**Interfaces:**
- Consumes: `build_supervised_runtime(config)`, `resources.start/stop`, `resources.host.snapshot()`, `TaskService.submit/wait_terminal`.
- Produces: JSON evidence with one ready Control Plane, three distinct ready Worker ids/PIDs, completed task id, final durable state, Provider request count, and post-stop child liveness.

- [ ] **Step 1: Strengthen topology acceptance**

Assert snapshot contains exactly four children: one `role == "control"` and three `role == "worker"`; all are ready/alive; Worker ids equal `{"worker-0", "worker-1", "worker-2"}`; instance ids are distinct; `ready_workers() == 3`.

- [ ] **Step 2: Strengthen the real-turn golden flow**

After terminal completion, assert durable session has user then assistant content, exactly one final reply Outbox fact, exactly one logical Provider decision, and exactly one stub request for the no-tool response. After `resources.stop()`, assert the supervisor snapshot or captured child records report no alive child process.

- [ ] **Step 3: Run the two spawned-process tests**

Run:

```powershell
py -m uv run pytest tests/runtime/test_three_worker_acceptance.py tests/runtime/test_supervised_golden_flow.py -q
```

Expected: PASS with real production entrypoints, not process stubs.

- [ ] **Step 4: Implement and run the standalone verifier**

Reuse the process-safe local OpenAI stub and production config. The script must start the runtime, wait for exact topology, resolve each Worker id through `resources.host.supervisor.child_record(worker_id)` and record its live `process.pid`, submit `"three-worker-proof"`, wait for `COMPLETED`, query durable facts through a separate SQLite connection, stop in `finally`, verify no child survives, and write `three-worker-evidence.json`.

Run: `py -m uv run python scripts/verify_three_worker_runtime.py --workspace .three-worker-evidence-workspace --output three-worker-evidence.json`

Expected: exit 0 with `control_count=1`, `worker_count=3`, three distinct Worker ids, `task_state="COMPLETED"`, `provider_requests=1`, and `children_alive_after_stop=0`.

- [ ] **Step 5: Commit**

```powershell
git add tests/runtime/test_three_worker_acceptance.py tests/runtime/test_supervised_golden_flow.py scripts/verify_three_worker_runtime.py
git commit -m "test: prove the production three-worker topology"
```

---

### Task 25: Repeat Exact-Owner Crash Recovery and Decision Reuse

**Files:**
- Modify: `tests/runtime/test_runtime_fault_injection.py`
- Modify: `scripts/verify_three_worker_runtime.py`

**Interfaces:**
- Consumes: owner-resolution helpers from Task 6 and attempt reuse from Task 5.
- Produces: standalone crash mode `--kill-owner` and durable recovery evidence.

- [ ] **Step 1: Add crash mode to the verifier**

When `--kill-owner` is present, configure the stub with a bounded delayed first response, submit the task, resolve `leased_by`, record owner id/PID, terminate that exact process, wait for terminal completion, and query attempts/events/outbox. Output:

```json
{
  "killed_worker_id": "worker-1",
  "recovery_attempts": 2,
  "task_state": "COMPLETED",
  "logical_provider_decisions": 1,
  "duplicate_model_started_events": 0,
  "duplicate_final_replies": 0,
  "children_alive_after_stop": 0
}
```

The concrete worker id may differ; it must equal the task's recorded pre-kill `leased_by`.

- [ ] **Step 2: Run the exact recovery test repeatedly**

Run the six-pass PowerShell loop from Task 7. Expected: 6/6 PASS.

- [ ] **Step 3: Run standalone crash evidence**

Run:

```powershell
py -m uv run python scripts/verify_three_worker_runtime.py --workspace .three-worker-crash-workspace --output three-worker-crash-evidence.json --kill-owner
```

Expected: exit 0 and all JSON facts match the schema above.

- [ ] **Step 4: Run Runtime regression and commit**

Run: `py -m uv run pytest tests/runtime -q`

Expected: PASS. Then:

```powershell
git add tests/runtime/test_runtime_fault_injection.py scripts/verify_three_worker_runtime.py
git commit -m "test: prove exact-owner crash recovery"
```

---

### Task 26: Run 1,000-Task Load and 30-Minute Soak

**Files:**
- Modify: `scripts/runtime_soak.py`
- Create: `tests/runtime/test_runtime_soak_summary.py`
- Modify: `tests/runtime/test_runtime_load.py`

**Interfaces:**
- Produces final soak keys: `worker_ids`, `missing_terminal`, `missing_final_replies`, `duplicate_effects`, `same_session_overlaps`, `same_session_order_violations`, `stale_mutations`, `unresolved_sqlite_busy`, and `children_alive_after_shutdown`.

- [ ] **Step 1: Add red summary-contract tests**

Extract or test a pure final-summary validator:

```python
def assert_release_soak_summary(summary: dict[str, Any]) -> None:
    assert len(set(summary["worker_ids"])) == 3
    for key in (
        "missing_terminal", "missing_final_replies", "duplicate_effects",
        "same_session_overlaps", "same_session_order_violations",
        "stale_mutations", "unresolved_sqlite_busy", "children_alive_after_shutdown",
    ):
        assert summary[key] == 0, f"{key}={summary[key]}"
```

Feed one valid and parametrized invalid summaries; each invalid key must produce a diagnostic naming that key.

- [ ] **Step 2: Extend collection from durable facts**

Collect Worker ids from task leases/events, missing final replies from completed interactive tasks lacking final Outbox rows, stale mutations from fenced-event diagnostics, and unresolved SQLite busy from terminal error/event facts. Capture child liveness after shutdown. Do not infer success solely from submitted/completed counts.

- [ ] **Step 3: Run soak unit tests**

Run: `py -m uv run pytest tests/runtime/test_runtime_soak_summary.py -q`

Expected: PASS.

- [ ] **Step 4: Make cross-session concurrency a required load assertion**

Replace the existing conditional skip with:

```python
assert metrics["max_concurrent"] >= 3, (
    f"distinct concurrent sessions={metrics['max_concurrent']}, expected at least 3"
)
```

The load gate must fail, not skip, when three execution slots never overlap across distinct sessions.

- [ ] **Step 5: Run deterministic 1,000-task/100-session load**

Run:

```powershell
py -m uv run pytest tests/runtime/test_runtime_load.py -q
```

Expected: 2 tests PASS; `duplicates=0`, `terminal=1000`, bounded queue age, and `same_session_overlaps=0`. Record wall time.

- [ ] **Step 6: Run the full 30-minute supervised soak**

Run exactly:

```powershell
py -m uv run python scripts/runtime_soak.py --duration-minutes 30 --sessions 100 --rate-per-second 2 --seed 20260731 --output runtime-soak-release.json
```

Expected: exit 0; three distinct Worker ids; same-session overlap 0; missing terminal 0; missing final replies 0; duplicate effects 0; stale mutations 0; unresolved SQLite busy 0; children alive after shutdown 0.

- [ ] **Step 7: Validate the saved report and commit code/tests**

Run: `py -m uv run python -c "import json; from scripts.runtime_soak import assert_release_soak_summary; assert_release_soak_summary(json.load(open('runtime-soak-release.json', encoding='utf-8'))); print('soak evidence valid')"`

Expected: prints `soak evidence valid`. Then:

```powershell
git add scripts/runtime_soak.py tests/runtime/test_runtime_soak_summary.py tests/runtime/test_runtime_load.py
git commit -m "test: harden supervised soak evidence"
```

Keep `runtime-soak-release.json` for Task 27; do not commit Runtime databases.

---

### Task 27: Run the Final Matrix and Complete the Handoff Record

**Files:**
- Create: `docs/superpowers/evidence/2026-08-03-remediation-final.md`
- Create or modify: `docs/superpowers/plans/2026-07-29-four-batch-core-hardening.md`
- Modify: this plan only to check completed boxes as work lands
- Create: `docs/four-batch-hardening-release-notes.md`

**Interfaces:**
- Produces: one evidence-backed completion record from the same final commit.
- Records Tasks 15–28 of the prior four-batch plan as implemented or explicitly superseded by Tasks 9–22 here, with commit hashes.

- [ ] **Step 1: Verify no hidden incomplete-work markers**

Run:

```powershell
rg -n "xfail|pytest\.skip|describe\.skip|it\.skip|test\.skip" tests webui/src scripts
rg -n "_dispatch|_active_tasks|process_direct" miniunicorn tests
```

Expected: every remaining skip is an existing optional/environment gate and is listed with justification in final evidence; no required embedding, three-worker, recovery, packaging, load, or soak result is skipped. Removed task-authority names appear only in hard-cutover negative assertions/documentation.

- [ ] **Step 2: Run source and backend gates**

Run:

```powershell
py -m uv lock --check
py -m uv run python scripts/export_agent_event_schema.py --check
py -m uv run ruff format --check miniunicorn tests scripts hatch_build.py
py -m uv run ruff check miniunicorn tests scripts hatch_build.py
py -m uv run pytest tests/architecture -q
py -m uv run pytest -m core -q
py -m uv run pytest tests/runtime -q
py -m uv run pytest -q
git diff --check
```

Expected: every command exits 0. Record pass/skip counts and wall times literally.

- [ ] **Step 3: Run frontend gates**

Run:

```powershell
Push-Location webui
npm run test:generated-check
npm run check:protocol
npm run lint
npx tsc --noEmit
npm test
npm run build
Pop-Location
```

Expected: all PASS. Vite circular-chunk warnings may be recorded as advisory only if build exits 0 and no runtime test fails.

- [ ] **Step 4: Re-run the two user-visible proof commands**

Run the real embedding command from Task 23 and both normal/crash three-worker commands from Tasks 24–25 from the final commit. Expected: all exit 0; evidence JSON contains the required durable facts.

- [ ] **Step 5: Re-run artifact verification and inspect long-run evidence**

Run Task 22 package commands against freshly built artifacts. Validate `runtime-soak-release.json` from Task 26 and include its SHA-256:

```powershell
Get-FileHash runtime-soak-release.json -Algorithm SHA256
```

If any source changed after the 30-minute soak, rerun the full soak from the new final commit; a report from an older source state is not release evidence.

- [ ] **Step 6: Update prior-plan supersession mapping**

If the prior plan is absent in the isolated worktree, first copy the unchanged source document without editing the original checkout:

```powershell
$priorSource = 'D:\MyProject\MiniUnicorn\docs\superpowers\plans\2026-07-29-four-batch-core-hardening.md'
$priorTarget = 'D:\MyProject\MiniUnicorn-worktrees\full-remediation\docs\superpowers\plans\2026-07-29-four-batch-core-hardening.md'
if (!(Test-Path -LiteralPath $priorTarget)) {
  Copy-Item -LiteralPath $priorSource -Destination $priorTarget
}
if ((Get-FileHash $priorSource).Hash -ne (Get-FileHash $priorTarget).Hash) {
  throw 'prior plan copy does not match source before checklist edits'
}
```

For each prior Task 15–28, compute the implementing commit with `git log --format="%H %s" --grep="refactor: extract runner control flow" -1` (changing the grep text to that task's actual commit subject), then record one of these sentence forms with the returned hash and the exact command already run:

```markdown
- [x] Implemented by remediation Task 12, commit followed by the resolved 40-character hash, verified by the runner checkpoint command.
- [x] Superseded by remediation Task 20 because the document backends moved from base dependencies to the `documents` extra while Docker/dev remain document-capable, commit followed by the resolved 40-character hash, verified by the three isolated-install commands.
```

No task may be marked complete solely because this plan exists.

- [ ] **Step 7: Write final evidence**

Use sections: Source Commit, Protected Files Check, Gate Matrix, Embedding Facts, Three-Worker Facts, Crash-Recovery Facts, Packaging Artifacts, Load Facts, Soak Facts, Docker Result, Prior-Plan Mapping, Residual Advisory Warnings. Link or hash each generated JSON; do not include secrets, API keys, databases, or model-cache paths. Create `docs/four-batch-hardening-release-notes.md` with the same measured line counts, install profiles, compatibility boundaries, test counts/durations, and any evidence-backed deviations.

- [ ] **Step 8: Verify the original checkout was untouched**

Run: `git -C D:\MyProject\MiniUnicorn status --short`

Expected: exactly the original four protected untracked paths and no new tracked modification from this remediation.

- [ ] **Step 9: Commit the final documentation**

```powershell
git add docs/four-batch-hardening-release-notes.md
git add -f docs/superpowers/evidence/2026-08-03-remediation-final.md docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md
git add -f docs/superpowers/plans/2026-07-29-four-batch-core-hardening.md
git commit -m "docs: record embedding and three-worker completion"
```

The prior-plan path is required by the completion criteria and must not be omitted from this commit.

- [ ] **Step 10: Finish the branch without publishing it automatically**

Invoke `superpowers:finishing-a-development-branch`, present merge/PR/keep-worktree options, and wait for the user. Do not push, merge, open a PR, or remove the worktree without explicit instruction.

---

## Definition of Done

The implementation agent may report completion only when all checkboxes are checked and the final evidence proves, from one final source commit:

1. real `BAAI/bge-small-zh-v1.5` CPU inference returns normalized 512-dimensional vectors;
2. two entries persist in `memory/memory.db`, survive reopen, recall the matching Chinese memory at rank 1, and do not duplicate under the same source identity/revision;
3. supervised startup reports one ready Control Plane and exactly three distinct ready Workers;
4. a durable turn completes through a real spawned Worker and one local process-safe Provider stub request;
5. the Worker named by `TaskRecord.leased_by` is terminated, the task recovers, and no duplicate Provider decision or final reply appears;
6. all lock, format, lint, architecture, core, Runtime, full pytest, protocol, frontend, artifact, minimal-install, load, and soak gates pass;
7. Docker passes when available, or final evidence contains the literal environment-unavailable error without calling the gate successful;
8. Tasks 15–28 of the prior plan have commit-backed implementation or evidence-backed supersession records; and
9. the original checkout's unrelated untracked files remain unchanged.

## Handoff Command for the Implementing Agent

Give the next agent this exact instruction:

```text
Work in D:\MyProject\MiniUnicorn-worktrees\full-remediation on branch codex/full-remediation. Read docs/superpowers/specs/2026-08-03-embedding-three-worker-completion-remediation-design.md and docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md completely. Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Execute tasks strictly in numeric order, one red-green-refactor cycle and one commit per task. Update each checkbox only after its command passes. Never restore _dispatch/_active_tasks, never kill an arbitrary Worker, and do not claim completion without the real embedding, exact three-worker, owner-kill recovery, package, load, and 30-minute soak evidence from the final commit.
```
