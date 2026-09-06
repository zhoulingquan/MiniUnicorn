# Lean ReAct Kernel Correctness Design

**Date:** 2026-08-21

**Status:** Approved by the user's instruction to implement the supplied audit direction autonomously.

## Decision

Erza will converge on a small ReAct execution kernel with orthogonal planning, context, safety, budget, and recovery policies. We will not implement the older `DIRECT / GUIDED / PLANNED` router. The default remains FAST ReAct; managed planning remains opt-in until the correctness substrate is trustworthy.

The work is deliberately split into independently shippable slices:

1. **P0 correctness substrate (this implementation):** configuration propagation, turn-wide model-call accounting, explicit planner outcomes, correct replan limits/fallback behavior, full planner input, and a context-governor pipeline that matches its declaration.
2. **P1 managed execution:** deterministic FAST/MANAGED selection, durable plan state, step evidence acceptance, no-progress detection, per-tool intent/result checkpoints, terminal-only reflection, and bounded sustained-goal time slices.
3. **P2 efficiency:** pressure-driven context governance, prompt-component telemetry, lower ordinary-turn ceilings, and schema-cropping benchmarks.

P1 must not begin until P0 metrics and tests are green. This prevents new adaptive behavior from being built on miswired configuration and incomplete accounting.

## Alternatives Considered

### One-shot kernel rewrite

This produces the cleanest end state fastest on paper, but combines configuration, accounting, planning semantics, checkpoint durability, and routing changes in one regression surface. It is rejected because failure attribution and rollback would be poor.

### Configuration-only hotfix

This is the smallest change, but it would activate planner and reflection paths whose usage and fallback behavior are currently unreliable. It is rejected because it can make configured deployments more expensive or less stable while appearing to fix them.

### Staged vertical slices (chosen)

This fixes the full correctness path from YAML to runtime behavior and telemetry before changing routing. Each task has a focused test and can be reviewed or reverted independently.

## P0 Architecture

### 1. Configuration ownership

`AgentDefaults` remains the serialized schema. `AgentLoopBuilder.from_config()` copies every execution-policy field into `AgentLoopConfig`; `AgentLoop.__init__()` reads only the supplied bundle, using `AgentDefaults()` solely for values omitted by direct legacy constructors.

The fields are:

- `use_planner`
- `planner_model`
- `planner_max_replans`
- `enable_reflection`
- `reflection_interval`
- `max_input_tokens_per_turn`
- `max_cost_per_turn_usd`

This preserves the current public YAML keys and direct-construction compatibility.

### 2. Turn-wide CallLedger

A new `erza.agent.call_ledger` module owns call accounting. A ledger is bound with `contextvars`, so concurrent sessions do not share totals. The provider retry entry points record each completed response exactly once. Call sites set a purpose scope:

- `executor`
- `planner`
- `replan`
- `reflection`
- `compact`
- `finalization`
- `memory`
- `tool`
- `unclassified`

`CallLedger` stores ordered call records, cumulative usage, last-call usage, and purpose totals. If a `TurnBudget` is attached, recording a response also accumulates its usage and updates the exceeded reason. The ledger is passive during a call: it does not estimate or reserve tokens in P0. P1 will add pre-call reservation after prompt-component token estimates use one reliable unit.

`TurnOrchestrator.process_turn()` binds one ledger around COMPACT through RESPOND. Direct `AgentRunner.run()` calls create and bind a local ledger when none exists. This covers production turns and isolated runner tests without leaking state.

All Erza-owned LLM calls must use a retry entry point so they pass through this accounting boundary. Specialized tools are tagged `tool`; consolidation and summarization are tagged `compact` or `memory`. Unknown extensions are still counted as `unclassified`.

### 3. Planner outcomes

Planner parsing no longer disguises invalid output as a valid one-step plan. `PlannerResult` contains:

- `plan: Plan`
- `status: PlannerStatus` (`valid` or `fallback`)
- `error_code: str | None`

Fallback error codes are stable machine-readable strings such as `provider_error`, `missing_json`, `invalid_json`, `missing_steps`, and `invalid_steps`. The runner enables MANAGED execution only for `valid`. A fallback explicitly degrades to FAST ReAct and remains visible in logs and tests.

The planner receives the complete latest user text. Context governance remains responsible for controlling the overall model prompt; the arbitrary 500-character slice is removed.

### 4. Replan invariants

`max_replans=N` permits exactly N provider replan attempts. The counter increments when an attempt starts, not before the eligibility check. A valid replacement inherits `max_replans` and `replan_count`, and preserves completed-step history without relying on colliding step IDs.

An invalid or failed replan does not return the old plan with a FAILED current step. It returns an explicit fallback result, and the runner degrades to FAST ReAct after appending the tool failure result. Exhausting the configured limit ends the managed plan with `stop_reason="plan_failed"`.

### 5. Context governance correctness

The actual default strategy list must match `BUILTIN_PIPELINE`, including the post-snip cleanup pass. Duplicate cleanup stages are separate strategy instances and run in declared order. Plugin strategies remain appended after the complete built-in pipeline and cannot override built-in names.

### 6. Compatibility and observability

FAST ReAct remains the default. No new classifier, verifier LLM call, dynamic routing key, or configuration rename is introduced in P0. Existing `AgentRunResult.usage` becomes the turn ledger total, so planner, replan, reflection, compact, and finalization usage are no longer invisible. `last_call_usage` remains the final provider call footprint.

## Error Handling

- Ledger recording must never replace the provider response with an accounting exception. Malformed usage values are ignored field-by-field.
- Context-local binding is reset in `finally` blocks on success, error, timeout, and cancellation.
- Planner provider errors and malformed responses produce explicit fallback results.
- Budget exhaustion detected after planner/compact work prevents another executor call and returns `budget_exceeded`.
- Existing provider retry and cancellation semantics remain unchanged.

## Test Strategy

Every behavior change follows red-green-refactor:

- A formal `from_config()` integration test proves all seven fields reach `AgentLoop` and the generated `AgentRunSpec`/`TurnBudget`.
- Ledger unit tests prove purpose totals, last usage, malformed usage handling, budget accumulation, and context isolation.
- Provider integration tests prove streaming and non-streaming calls record once, including retry behavior.
- Runner tests prove initial planning usage is included, a planner-only budget overrun prevents executor work, and direct runs do not leak ledgers.
- Planner tests cover every status/error code, full task text, exact replan limits, replacement inheritance, and fallback-to-FAST behavior.
- Context-governor tests assert the exact built-in invocation order including the cleanup pass.
- Targeted tests run after each task; the complete Python suite and Ruff run at the final gate.

## Deferred P1/P2 Contract

P1 will introduce `PlanningPolicy`, `ProgressPolicy`, durable `PlanSnapshot`, and `StepAcceptancePolicy` as separate modules. It may consume P0 ledger telemetry but must not add classification LLM calls. `execute_plan` will be deprecated in favor of a delegation-oriented name only after compatibility usage is measured. These are explicitly out of the P0 patch so the first deployment changes correctness, not product behavior.
