# MiniUnicorn Embedding and Three-Worker Completion Remediation Design

**Date:** 2026-08-03

**Status:** Approved for planning

## 1. Goal

Complete the outstanding `docs/superpowers` work without changing MiniUnicorn's
public product model. The final source state must demonstrate two user-visible
outcomes with real execution rather than source inspection alone:

1. the local CPU embedding provider can generate vectors, store them in
   `memory.db`, and recall matching content; and
2. supervised Runtime starts one Control Plane and exactly three Workers, then
   accepts, executes, recovers, and completes durable tasks correctly.

All backend, frontend, packaging, generated-contract, load, soak, and available
container gates must pass from the same final source state.

## 2. Constraints

- Preserve the current `lightweight` and `supervised` runtime modes.
- Preserve the Control Plane plus three-Worker production topology.
- Preserve existing public Python imports, WebUI event fields, Channel class
  names, configuration keys, and Runtime SQLite schema.
- Do not restore `_dispatch`, `_active_tasks`, or another process-local task
  authority removed by the hard cutover.
- Do not introduce a second Provider, tool, session, or Outbox execution path.
- Keep `memory.db` as a derived vector index; session history remains the
  authoritative memory source.
- Preserve unrelated user files and existing untracked files.

## 3. Selected Approach

Use staged full remediation. Each stage restores a green checkpoint before the
next structural change begins.

### Stage A: Restore reproducible release gates

- Synchronize `uv.lock` with `pyproject.toml`, including project version,
  Windows timezone data, and the final optional-dependency layout.
- Make generated TypeScript event checks line-ending independent while keeping
  semantic drift detection strict.
- Apply the locked Ruff formatter only to repository-owned Python scope.
- Migrate stale session tests from removed AgentLoop task authority to the
  RuntimeApplication/TaskService contract.
- Make the Worker-kill test identify and terminate the actual task lease owner.
- Make Provider-attempt recovery idempotent at its durable identity boundary so
  a recovered task cannot fail on a duplicate `STARTED` insert.

### Stage B: Complete the four-batch structural boundaries

- Keep `AgentRunner` as the public façade and move model request construction,
  tool execution, and ReAct control flow into `runner_model.py`,
  `runner_tools.py`, and `runner_control.py`.
- Keep `useMiniunicornStream` responsible for sockets and effects; move pure
  state transitions into `stream-state.ts` and `stream-reducer.ts`.
- Keep `AgentActivityCluster` at its original import path; move parsing and
  focused views into `components/thread/activity/`.
- Keep Channel public classes and wire formats stable while extracting
  WebSocket outbound emission, Feishu rendering/media, and Weixin
  crypto/API/media services.
- Enforce the original plan's façade and method-size gates with tests. A limit
  may change only when a written compatibility reason and a smaller ownership
  boundary are both present.

### Stage C: Complete packaging and optional document dependencies

- Remove `pypdf`, `python-docx`, `openpyxl`, and `python-pptx` from base wheel
  dependencies.
- Add `miniunicorn-ai[documents]` containing all four backends.
- Keep `miniunicorn-ai[pdf]` compatible.
- Keep development, all-extras, and Docker installations document-capable.
- Replace silent parser error strings with one actionable missing-backend error
  that includes the correct install command.
- Make source version resolution tolerate missing, unreadable, and malformed
  `pyproject.toml` and fall back to installed metadata.
- Add wheel/sdist/minimal-install tests that also verify bundled WebUI assets.

### Stage D: Prove embedding and three-Worker operation

- Run the real local CPU embedding smoke test with the approved model.
- Verify vector creation, stable dimensions, `memory.db` persistence, scoped
  recall, idempotent indexing, and safe NoOp fallback.
- Start supervised Runtime with a process-safe OpenAI-compatible stub, wait for
  one ready Control Plane and three ready Workers, and complete a durable turn.
- Prove same-session serialization, cross-session concurrency, exact
  Worker-owner crash recovery, Provider decision reuse, and clean shutdown.
- Run the deterministic 1,000-task/100-session load gate and the 30-minute
  supervised soak. Inspect durable facts, not only exit codes.

## 4. Data and Recovery Flow

### Embedding

Authoritative session/history text is converted to a local embedding, indexed
in workspace `memory.db`, and queried with tenant/agent/workspace scope. A
missing optional vector backend produces a NoOp store and never corrupts or
blocks the authoritative session data.

### Durable task recovery

TaskService writes ingress to SQLite before execution. A Worker claims a lease,
journals Provider/tool effects, and commits session/final Outbox state only
while its lease is valid. On Worker loss, the lease scanner requeues the task.
The replacement Worker reads the journal and reuses completed decisions. An
existing attempt identity is treated according to its durable state rather than
blindly inserted again.

## 5. Error Handling

- Generated-contract checks normalize only line endings; schema or TypeScript
  content differences still fail.
- Ambiguous non-idempotent external effects remain `OUTCOME_UNKNOWN` or
  `WAITING_USER`; remediation must not add automatic replay.
- Missing document backends raise an actionable error naming the required
  extra.
- A malformed source `pyproject.toml` never prevents package import.
- A fault test that cannot identify the lease owner fails explicitly instead
  of killing an arbitrary Worker or skipping the required scenario.

## 6. Testing Strategy

Every behavior change follows red-green-refactor:

1. add or strengthen the smallest failing test;
2. run it and confirm the expected failure;
3. implement one bounded change;
4. run focused and neighboring suites;
5. run the stage checkpoint before proceeding.

Final verification includes:

- Ruff lint and format, lockfile check, and whitespace check;
- architecture, core, Runtime, and full pytest suites;
- generated Python/TypeScript event contract checks;
- frontend lint, type-check, tests, and production build;
- wheel, sdist, minimal base install, documents extra, and import checks;
- real local embedding smoke;
- real spawned three-Worker golden and crash-recovery flows;
- 1,000-task load and 30-minute soak;
- Docker build/configuration smoke when Docker is available.

## 7. Completion Criteria

The remediation is complete only when all of the following are true:

- local embedding generates and recalls persisted vectors through `memory.db`;
- supervised Runtime reports and uses exactly three Workers;
- the actual task-owning Worker can be killed and the task still completes
  without duplicate Provider/tool effects;
- tasks 15-28 of the four-batch plan are implemented or have an explicit,
  evidence-backed supersession recorded in that plan;
- all required source, test, generated, packaging, load, and soak gates pass
  from the final commit;
- no incomplete-work `xfail` or skip hides a required outcome;
- every pre-existing tracked and untracked status entry recorded at the start remains unchanged unless its resolution is explicitly approved and documented.

## 8. Non-Goals

- New runtime modes or a new process topology.
- A new embedding model or remote embedding service.
- SQLite schema redesign.
- UI redesign or new Channel features.
- Restoring legacy in-process dispatch authority.
