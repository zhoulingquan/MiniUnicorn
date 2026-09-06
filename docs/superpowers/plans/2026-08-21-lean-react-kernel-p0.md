# Lean ReAct Kernel P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Erza's existing planning, reflection, context, and turn-budget mechanisms correct and fully accounted before adding adaptive routing.

**Architecture:** Propagate execution policy through `AgentLoopConfig`, bind a context-local `CallLedger` for the entire turn, account at provider retry boundaries, and make Planner/replan outcomes explicit. Preserve FAST ReAct as the default and avoid new routing behavior.

**Tech Stack:** Python 3.11+, asyncio, contextvars, dataclasses, Pydantic v2, pytest/pytest-asyncio, Ruff.

## Global Constraints

- Follow strict red-green-refactor for every production change; capture the failing and passing command output.
- Do not implement the old three-tier task router.
- Do not add a classifier or verifier LLM call.
- Preserve all existing YAML keys and direct `AgentLoop(...)` compatibility.
- Preserve provider retry, streaming, timeout, and cancellation semantics.
- Do not modify or stage `.trae-html-share-packages/` or `arch-eval-erza-vs-aniyaa/`.
- Do not begin P1 or P2 work in this plan.

---

### Task 1: Propagate execution-policy configuration

**Files:**
- Modify: `erza/agent/loop.py`
- Modify: `erza/agent/loop_builder.py`
- Create: `tests/agent/test_loop_execution_policy_config.py`

**Interfaces:**
- Produces: `AgentLoopConfig.use_planner: bool`, `planner_model: str | None`, `planner_max_replans: int`, `enable_reflection: bool`, `reflection_interval: int`, `max_input_tokens_per_turn: int | None`, and `max_cost_per_turn_usd: float | None`.
- Consumes: existing `Config.agents.defaults` fields and `AgentLoop._build_turn_budget()`.

- [x] **Step 1: Write the failing builder integration test**

Create a real `Config`, set distinct non-default values for all seven fields, call `AgentLoop.from_config(config, provider=fake_provider)`, and assert the loop attributes plus `TurnBudget(max_input_tokens=1234, max_cost_usd=0.25)`. Also monkeypatch `loop.runner.run` to capture the `AgentRunSpec` produced by `_run_agent_loop()` and assert the five planner/reflection fields and attached budget.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/agent/test_loop_execution_policy_config.py -q`

Expected: FAIL because `AgentLoopConfig` and `AgentLoopBuilder.from_config()` discard the configured fields.

- [x] **Step 3: Add fields and explicit builder methods**

Add the seven fields to `AgentLoopConfig`; add `with_use_planner`, `with_planner_model`, `with_planner_max_replans`, `with_enable_reflection`, `with_reflection_interval`, `with_max_input_tokens_per_turn`, and `with_max_cost_per_turn_usd`. Call them from `from_config()` with `defaults.<field>`.

In `AgentLoop.__init__`, use the bundle:

```python
self.use_planner = cfg.use_planner
self.planner_model = cfg.planner_model
self.planner_max_replans = cfg.planner_max_replans
self.enable_reflection = cfg.enable_reflection
self.reflection_interval = cfg.reflection_interval
self._max_input_tokens_per_turn = cfg.max_input_tokens_per_turn
self._max_cost_per_turn_usd = cfg.max_cost_per_turn_usd
```

- [x] **Step 4: Verify GREEN and regression scope**

Run: `python -m pytest tests/agent/test_loop_execution_policy_config.py tests/agent/test_max_messages_config.py tests/agent/test_loop_structured_memory_mode.py -q`

Expected: all tests pass.

### Task 2: Add a context-local CallLedger

**Files:**
- Create: `erza/agent/call_ledger.py`
- Modify: `erza/agent/turn_budget.py`
- Create: `tests/agent/test_call_ledger.py`

**Interfaces:**
- Produces: `CallPurpose`, `CallRecord`, `CallLedger`, `current_call_ledger()`, `bind_call_ledger()`, `reset_call_ledger()`, and `call_purpose()`.
- `CallLedger.record(*, model: str, usage: Mapping[str, Any] | None, finish_reason: str, purpose: CallPurpose | str | None = None) -> None`.
- `CallLedger.total_usage: dict[str, int]`, `last_call_usage: dict[str, int]`, `purpose_usage: dict[str, dict[str, int]]`, and `budget_exceeded_reason: str | None`.

- [x] **Step 1: Write failing ledger tests**

Cover cumulative totals, ordered purposes, last-call usage, malformed fields, explicit provider `cost_usd`, `TurnBudget` accumulation, nested `call_purpose()` restoration, concurrent-task context isolation, and token reset in a `finally` block.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/agent/test_call_ledger.py -q`

Expected: collection fails because `erza.agent.call_ledger` does not exist.

- [x] **Step 3: Implement the minimal ledger**

Use `ContextVar[CallLedger | None]` and `ContextVar[str]`; normalize numeric usage without throwing. `CallRecord` stores `purpose`, `model`, normalized `usage`, and `finish_reason`. Delegate budget math to `TurnBudget.accumulate()` and read `budget.check()` after each record; do not duplicate cost formulas.

- [x] **Step 4: Verify GREEN**

Run: `python -m pytest tests/agent/test_call_ledger.py tests/agent/test_turn_budget.py -q`

Expected: all tests pass.

### Task 3: Account every provider retry call exactly once

**Files:**
- Modify: `erza/providers/base.py`
- Modify: `erza/agent/execution/model_request.py`
- Modify: `erza/agent/planner.py`
- Modify: `erza/agent/reflection.py`
- Modify: `erza/agent/memory.py`
- Modify: `erza/agent/agent_generator.py`
- Modify: `erza/agent/tools/deep_research/tool.py`
- Modify: `erza/utils/evaluator.py`
- Modify: `tests/agent/tools/test_deep_research.py`
- Create: `tests/providers/test_call_ledger_integration.py`

**Interfaces:**
- Consumes: Task 2 ledger functions.
- Produces: retry-wrapped streaming and non-streaming responses automatically recorded once; call-site purposes set with `call_purpose(<purpose>)`.

- [x] **Step 1: Write failing provider tests**

Build a minimal real `LLMProvider` subclass whose first response is transient and second response succeeds. Assert `chat_with_retry` produces one ledger record, not one per retry attempt. Repeat for `chat_stream_with_retry` and assert no double record when the default stream fallback calls `chat()`.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/providers/test_call_ledger_integration.py -q`

Expected: FAIL because provider retry entry points do not record calls.

- [x] **Step 3: Instrument provider boundaries and tag purposes**

After `_run_with_retry()` returns, call the active ledger once with the final response's model/usage/finish reason. Wrap executor, finalization, planner, replan, reflection, compact/memory, evaluator, agent-generator, and deep-research calls in the appropriate `call_purpose()` scope. Replace Erza-owned direct `.chat()` calls with `.chat_with_retry()` only where their arguments and retry semantics remain equivalent.

- [x] **Step 4: Audit unaccounted call sites**

Run: `rg -n "\.chat\(" erza -g '*.py'`

Expected: only provider implementations, tests/examples, or explicitly documented non-LLM helpers remain; every product-layer LLM call uses a retry entry point.

- [x] **Step 5: Verify GREEN**

Run: `python -m pytest tests/providers/test_call_ledger_integration.py tests/providers/test_provider_retry.py tests/agent/test_runner_core.py tests/agent/tools/test_deep_research.py -q`

Expected: all tests pass.

### Task 4: Bind one ledger across the whole turn and direct runner runs

**Files:**
- Modify: `erza/agent/turn_orchestrator.py`
- Modify: `erza/agent/loop.py`
- Modify: `erza/agent/runner.py`
- Modify: `erza/agent/execution/recovery.py`
- Create: `tests/agent/test_turn_call_ledger.py`

**Interfaces:**
- `TurnDeps.build_turn_budget: Callable[[], TurnBudget | None]`.
- `TurnContext.call_ledger: CallLedger | None`.
- `AgentRunResult.usage` is the active ledger total; `last_call_usage` is its last record.

- [x] **Step 1: Write failing turn and runner tests**

Prove that compact + planner + executor usage is summed, planner-only budget exhaustion prevents the first executor call, direct concurrent `AgentRunner.run()` invocations do not share totals, and context binding resets after exceptions/cancellation.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/agent/test_turn_call_ledger.py -q`

Expected: FAIL because only executor responses currently reach `AgentRunResult.usage` and budget checks.

- [x] **Step 3: Bind ledger in the orchestrator and runner**

Create the production ledger before the state loop and bind/reset it in `try/finally`. Inject `build_turn_budget` through `TurnDeps`. In `AgentRunner.run()`, reuse an active ledger or bind a local one with `spec.turn_budget`. Before each executor iteration, stop with `budget_exceeded` if prior planner/compact calls exceeded the budget. Remove duplicate budget accumulation from runner paths; keep existing user-facing error content and hook notifications.

- [x] **Step 4: Make result usage ledger-backed**

Populate `AgentRunResult.usage` and `last_call_usage` from the ledger. Keep the existing `_TurnState` fields temporarily only where recovery helpers still require them; assert no call is counted twice.

- [x] **Step 5: Verify GREEN**

Run: `python -m pytest tests/agent/test_turn_call_ledger.py tests/agent/test_runner_core.py tests/agent/test_runner_errors.py tests/agent/test_runner_reflection.py tests/agent/test_turn_end_fields.py -q`

Expected: all tests pass.

### Task 5: Return explicit Planner results and preserve full task text

**Files:**
- Modify: `erza/agent/planner.py`
- Modify: `erza/agent/runner.py`
- Modify: `erza/agent/execution/planning.py`
- Modify: `tests/agent/test_upgrade_integration.py`
- Create: `tests/agent/test_planner_results.py`

**Interfaces:**
- Produces: `PlannerStatus(str, Enum)` with `VALID` and `FALLBACK`.
- Produces: `PlannerResult(plan: Plan, status: PlannerStatus, error_code: str | None)`.
- `Planner.create_plan(...) -> PlannerResult` and `Planner.replan(...) -> PlannerResult`.

- [x] **Step 1: Write failing parse-contract tests**

Assert valid JSON returns `VALID/None`; provider error returns `FALLBACK/provider_error`; missing JSON, invalid JSON, missing steps, and all-invalid steps return their stable codes. Assert a 2,000-character latest user message reaches the planner without truncation.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/agent/test_planner_results.py -q`

Expected: FAIL because Planner returns `Plan` and task extraction slices to 500 characters.

- [x] **Step 3: Implement result types and non-lossy extraction**

Return `PlannerResult` from all paths. A fallback still carries a one-step `Plan` for diagnostics, but `PlanningReflectionService.init_planner()` returns `(None, None, task_text, tools_summary)` unless status is `VALID`. Remove `[:500]` from string and text-block task extraction.

- [x] **Step 4: Update compatibility tests and callers**

Change existing tests/callers from `plan = await create_plan(...)` to `result = ...; plan = result.plan`, while asserting status where behavior matters. Do not add an implicit `__getattr__` compatibility proxy.

- [x] **Step 5: Verify GREEN**

Run: `python -m pytest tests/agent/test_planner_results.py tests/agent/test_upgrade_integration.py tests/agent/test_runner_core.py -q`

Expected: all tests pass.

### Task 6: Correct replan limits, inheritance, and fallback behavior

**Files:**
- Modify: `erza/agent/planner.py`
- Modify: `erza/agent/execution/recovery.py`
- Create: `tests/agent/test_replan_semantics.py`

**Interfaces:**
- Consumes: `PlannerResult` from Task 5.
- Produces: exactly `max_replans` provider attempts; fallback replans return FAST mode (`plan=None`) instead of a FAILED plan; exhausted plans return `plan_failed`.

- [x] **Step 1: Write failing boundary tests**

Parameterize `max_replans` as 0, 1, and 2. Assert 0/1/2 provider invocations respectively, attempt-time counter increments, and replacement plans retain both counters. Add tests where replan output is malformed or raises and assert recovery returns `("continue", None)` without a FAILED plan.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/agent/test_replan_semantics.py -q`

Expected: FAIL on the current increment-before-check off-by-one and old-plan fallback.

- [x] **Step 3: Implement exact-attempt semantics**

Check `plan.can_replan` before incrementing; increment immediately before the provider call. Copy `max_replans` and `replan_count` to valid replacements. Preserve completed steps as immutable history prepended to the replacement rather than matching new steps by ID.

- [x] **Step 4: Degrade invalid replans to FAST**

In `TurnRecoveryPolicy`, a valid `PlannerResult` continues managed execution; fallback logs its `error_code`, calls `hook.after_iteration`, and returns `("continue", None)`. When no attempts remain, retain the current `plan_failed` terminal behavior.

- [x] **Step 5: Verify GREEN**

Run: `python -m pytest tests/agent/test_replan_semantics.py tests/agent/test_runner_errors.py tests/agent/test_upgrade_integration.py -q`

Expected: all tests pass.

### Task 7: Make ContextGovernor execute its declared pipeline

**Files:**
- Modify: `erza/agent/context_governor.py`
- Modify: `tests/agent/test_context_governor.py` if present, otherwise create it.

**Interfaces:**
- Produces: default strategy names exactly equal `ContextGovernor.BUILTIN_PIPELINE` before plugin strategies.

- [x] **Step 1: Write the failing order test**

Patch strategy `apply()` methods to append their names and assert:

```python
[
    "drop_orphan_tool_results",
    "backfill_missing_tool_results",
    "microcompact",
    "apply_tool_result_budget",
    "snip_history",
    "drop_orphan_tool_results",
    "backfill_missing_tool_results",
]
```

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/agent/test_context_governor.py -q`

Expected: FAIL because `_load_default_strategies()` creates only five stages.

- [x] **Step 3: Load the exact built-in sequence**

Build factories keyed by strategy name and instantiate from `BUILTIN_PIPELINE`, allowing duplicate cleanup instances. Deduplicate only plugin names against the set of built-in names; do not deduplicate the built-in list.

- [x] **Step 4: Verify GREEN**

Run: `python -m pytest tests/agent/test_context_governor.py tests/agent/test_runner_governance.py -q`

Expected: all tests pass.

### Task 8: Documentation, compatibility audit, and final verification

**Files:**
- Modify: `docs/configuration.md`
- Modify: `docs/architecture/module-boundaries.md`
- Modify: `docs/superpowers/plans/2026-08-21-lean-react-kernel-p0.md` only to check completed boxes and append captured verification evidence.

**Interfaces:**
- Produces: documented FAST default, managed opt-in, complete budget accounting, fallback semantics, and P1 deferrals.

- [x] **Step 1: Update user and architecture documentation**

Document that `usePlanner` is a global managed-mode opt-in in P0, all turn model calls count against the configured limits, invalid planner output falls back to FAST, and dynamic FAST-to-MANAGED escalation is deferred.

- [x] **Step 2: Run focused P0 suite**

Run:

```powershell
python -m pytest tests/agent/test_loop_execution_policy_config.py tests/agent/test_call_ledger.py tests/providers/test_call_ledger_integration.py tests/agent/test_turn_call_ledger.py tests/agent/test_planner_results.py tests/agent/test_replan_semantics.py tests/agent/test_context_governor.py -q
```

Expected: all tests pass with zero warnings caused by this change.

- [x] **Step 3: Run all agent and provider tests**

Run: `python -m pytest tests/agent tests/providers -q`

Expected: all tests pass.

- [ ] **Step 4: Run the complete Python suite**

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Run lint and inspect the diff**

Run: `python -m ruff check erza tests`

Expected: exit 0.

Run: `git diff --check`

Expected: exit 0 with no whitespace errors.

Run: `git status --short`

Expected: only files listed by this plan are modified or created.

- [x] **Step 6: Record evidence**

Append the exact test counts, Ruff result, and any documented limitation under `## Verification Evidence` in this plan. Do not claim completion if any command is stale or failed.

## Verification Evidence

- Focused P0 suite: `74 passed in 1.21s`.
- Agent/provider suite: `1999 passed, 1 skipped, 1 xfailed, 2 warnings in 179.85s`.
- Exhaustive partition, excluding the two Windows asyncio files documented below:
  `3786 passed, 29 skipped, 1 xfailed, 172 warnings in 351.47s`.
- `tests/tools/test_exec_session_tools.py`: `18 passed, 2 warnings in 43.29s`.
- Changed/new Python files: Ruff exit 0 (`All checks passed!`).
- `git diff --check`: exit 0; only CRLF conversion warnings were printed.
- Product-layer `.chat(` audit: only provider fallback/base implementations remain.
- Independent code review: no remaining Critical or Important findings after two fix/review cycles.

Documented limitations:

- The monolithic `python -m pytest -q` run hangs on Windows in the pre-existing
  asyncio tool-test region. `tests/tools/test_mcp_probe.py::test_probe_returns_true_for_open_port`
  also hangs when run alone at `await writer.wait_closed()`; no P0 file imports or
  modifies that code. The non-hanging suite and exec-session file were therefore
  verified as the exhaustive partition above. Task 8 Step 4 remains unchecked.
- Full-repository Ruff has 8 pre-existing findings in files outside this plan:
  `_http_routes.py`, `config/schema.py`, `utils/progress_events.py`,
  `test_mcp_close_lifecycle.py`, `test_provider_hot_switch_isolation.py`,
  `test_turn_end_fields.py` (2), and `test_dependency_direction.py`. Task 8 Step 5
  remains unchecked; all changed/new Python files are clean.
- A final focused-suite rerun was blocked during pytest fixture setup by the
  Windows sandbox denying access to both the default and workspace-local temp
  roots; the previously completed focused run above remains the valid code
  result (`74 passed`).
