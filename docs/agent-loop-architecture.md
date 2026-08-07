# Agent Loop Architecture

## AgentLoop is a facade

`AgentLoop` is a construction and lifecycle facade. It assembles dependencies,
manages provider/MCP lifecycle, exposes host capabilities (tools, commands,
workspace, context), and delegates turn execution to four narrow collaborators.
It must not duplicate collaborator algorithms; each compatibility method is a
thin delegate so existing monkeypatches and extensions continue to intercept
calls.

The facade is bounded by a 900-line physical limit enforced by
`tests/agent/test_agent_loop_structure.py`.

## Turn ownership

- Entry scheduling, active tasks, pending queues, and coordinator scopes belong
  to `TurnDispatcher`.
- State transitions, `TurnContext`, state traces, and `ProcessedTurn` belong to
  `TurnExecutor`.
- `AgentRunner` construction and invocation adaptation belong to
  `AgentRunAdapter`.
- Session history, checkpoint, pending-turn recovery, and outbound assembly
  belong to `TurnPersistence`.
- `AgentLoop` constructs these collaborators and retains thin compatibility
  methods; it must not duplicate their algorithms.

## Call flow

```text
TurnDispatcher.dispatch / process_direct
    -> AgentLoop._process_message              # legacy seam, instance-monkeypatchable
        -> TurnDispatcher.process_message
            -> AgentLoop._execute_message      # Batch 1 seam, instance-monkeypatchable
                -> TurnExecutor.execute
                    -> AgentLoop._state_*       # StateMixin handlers
```

`TurnDispatcher.process_message()` copies `ProcessedTurn.context` into the
currently bound `TurnRuntime`. If `_process_message()` is replaced by a test,
the replacement fully owns that call; the production path always completes the
runtime through this wrapper.

`TurnExecutor.execute()` drives the state machine from `RESTORE` to `DONE`
using `TURN_TRANSITIONS`. For system-channel messages it delegates to
`TurnExecutor.process_system_message()` and returns
`ProcessedTurn(outbound, None)`.

`AgentRunAdapter.run()` is the single thick bridge between the loop and
`AgentRunner`. State handlers and `TurnExecutor` call `self._host._run_agent_loop`
so monkeypatches on that method remain effective.

## Compatibility methods

The following `AgentLoop` methods are thin delegates preserved for backwards
compatibility. Each has exactly one non-docstring body statement (verified by
the structural tests):

| Method | Delegates to |
|--------|-------------|
| `run` | `TurnDispatcher.run` |
| `process_direct` | `TurnDispatcher.process_direct` |
| `_dispatch` | `TurnDispatcher.dispatch` |
| `_process_message` | `TurnDispatcher.process_message` |
| `_execute_message` | `TurnExecutor.execute` |
| `_process_system_message` | `TurnExecutor.process_system_message` |
| `_run_agent_loop` | `AgentRunAdapter.run` |
| `_assemble_outbound` | `TurnPersistence.assemble_outbound` |
| `_sanitize_persisted_blocks` | `TurnPersistence.sanitize_persisted_blocks` |
| `_save_turn` | `TurnPersistence.save_turn` |
| `_persist_subagent_followup` | `TurnPersistence.persist_subagent_followup` |
| `_set_runtime_checkpoint` | `TurnPersistence.set_runtime_checkpoint` |
| `_mark_pending_user_turn` | `TurnPersistence.mark_pending_user_turn` |
| `_clear_pending_user_turn` | `TurnPersistence.clear_pending_user_turn` |
| `_clear_runtime_checkpoint` | `TurnPersistence.clear_runtime_checkpoint` |
| `_restore_runtime_checkpoint` | `TurnPersistence.restore_runtime_checkpoint` |
| `_restore_pending_user_turn` | `TurnPersistence.restore_pending_user_turn` |
| `_cancel_active_tasks` | `TurnDispatcher.cancel_active_tasks` |

The task registries `_active_tasks` and `_pending_queues` are read-only
properties backed by `TurnDispatcher`; mutating the returned dictionaries is
still supported for `/stop` and tests.

## Cancellation and failure cleanup

`TurnDispatcher.cancel_active_tasks()` cancels every active task for a session
key, awaits subagent cancellation, and removes the session's pending queue.
The coordinator scope's `finally` block releases the session lock and global
permit regardless of outcome (success, exception, or cancellation).

On failure, `TurnDispatcher.dispatch()` persists a runtime checkpoint so the
next turn can restore the in-flight assistant message and completed tool
results. Pending user turns are closed with an interrupted-response marker if
the loop crashed after persisting only the user message.

## Where future changes belong

- **Telemetry instrumentation** belongs in `TurnDispatcher` (coordinator/dispatch
  scope) and should consume trace/context from `ProcessedTurn`/`TurnRuntime`.
  Do not put telemetry state back on `AgentLoop`.
- **Background supervision** may replace task creation inside `TurnDispatcher`,
  but `AgentLoop._dispatch` must remain the patchable facade entry point.
- **Runner phase extraction** (Batch 3) starts from `AgentRunAdapter`; do not
  move the adapter body back into `AgentLoop`.
- **Structural guard**: the 900-line facade limit and delegate/method size
  limits are enforced by `tests/agent/test_agent_loop_structure.py` and must
  remain passing.

## Downstream batches

- Original Batch 2 telemetry must instrument coordinator/dispatch scope in
  `TurnDispatcher`, and consume trace/context from `ProcessedTurn`/
  `TurnRuntime`; it must not put telemetry state back on `AgentLoop`.
- Original Batch 2 background supervision may replace task creation in
  `TurnDispatcher`, but `AgentLoop._dispatch` must remain the patchable facade.
- Original Batch 3 runner phase extraction starts from `AgentRunAdapter`; do not
  move the adapter body back into `AgentLoop`.
- Original Batch 4 acceptance retains the 900-line structural guard.
