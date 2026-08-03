# MiniUnicorn Four-Batch Core Hardening Implementation Plan

> **Historical ledger — DO NOT EXECUTE:** Do not run any task, checkbox, command, skill instruction, branch action, or handoff prompt in this file. Only Task 27 of `docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md` may update the Task 15–28 evidence entries here.

> **Current status on `codex/full-remediation`:** This file is a tracked historical checklist and commit-evidence ledger. The authoritative executable plan is `docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md`; do not run this older plan independently.

**Goal:** Harden MiniUnicorn's security and per-turn concurrency semantics, establish a typed event/telemetry contract and fast CI gate, decompose the largest orchestration/UI/channel modules without behavior drift, and finally reduce the base installation surface.

**Architecture:** Preserve the current channel/API/SDK → `MessageBus` or `process_direct` → `AgentLoop` state machine → `AgentRunner` ReAct loop flow. Move mutable turn data into an explicitly bound `TurnRuntime`, retain session history and serialization at session scope, and retain the semaphore/provider/tool registry at process scope. Build protocol, telemetry, and characterization boundaries before extracting large modules. Packaging changes happen last so dependency movement cannot obscure behavioral regressions.

**Tech Stack:** Python 3.11–3.14, asyncio, `contextvars`, Pydantic 2, pytest/pytest-asyncio, Loguru, TypeScript 5, React 18, Vitest, Testing Library, Bun, GitHub Actions, Hatchling, uv

## Global Constraints

- Execute the batches strictly in order. A later batch may start only after every acceptance command in the previous batch passes.
- Preserve cross-session concurrency: different effective session keys may overlap up to `max_concurrent_requests`; the same effective session key remains serialized.
- A task waiting for its session lock must not consume a global concurrency permit.
- Every turn entry point, including `AgentLoop._dispatch()` and `AgentLoop.process_direct()`, must use the same concurrency coordinator and the same turn-runtime binding.
- Do not add a process-wide “current turn,” “current usage,” “current iteration,” or “last call usage” mutable field.
- Do not make provider rate limiting session-local. Provider quotas and global concurrency remain separate process-level controls.
- Preserve current event names and field names through Batch 2. Adding `protocol_version: 1` is allowed; removing legacy metadata flags is not.
- Batch 3 is an extraction refactor. Do not change prompts, retry counts, stop reasons, tool ordering, stream timing semantics, channel wire formats, or public imports while splitting files.
- Keep document extraction enabled in the default Docker distribution even after moving document libraries out of the base Python dependencies.
- Use TDD: add or strengthen a failing test, observe the intended failure, implement the smallest change, then run focused and neighboring tests.
- Commit after every task. Stage only the paths listed by that task; never use `git add .`.
- The checkout may contain unrelated user changes, including `pyproject.toml`. Preserve them. Before staging `pyproject.toml`, use `git diff -- pyproject.toml` and `git add -p pyproject.toml`.
- Do not stage `.workbuddy/`, `webui/public/*_decoded.png`, or unrelated lockfile changes.
- Use `uv run ruff format` only on files touched by the current task; do not bulk-format the repository.
- When a command fails, stop at that task, diagnose the failure, and update this plan’s checkbox notes before proceeding. Do not “accept” an unrelated failure without evidence.

## Target Ownership Model

| Scope | Owned state | Synchronization |
|---|---|---|
| Process | provider, tool registry, config snapshots, bus, global semaphore, telemetry sink | provider limiter plus global semaphore |
| Session | history, session metadata, pending injection queue, active tasks | one weakly-held lock per effective session key |
| Turn | turn ID, iteration, usage, last-call usage, trace, latency, budgets, hooks, telemetry metrics | `ContextVar[TurnRuntime]`, bound only inside the coordinator scope |

```mermaid
flowchart LR
    A["Channel / API / SDK"] --> B["TurnCoordinator.scope(session_key)"]
    B --> C["Session lock"]
    C --> D["Global semaphore"]
    D --> E["Bind TurnRuntime"]
    E --> F["AgentLoop state machine"]
    F --> G["AgentRunner phases"]
    G --> H["Provider and tools"]
    H --> I["Persist session-level result"]
    I --> J["Emit typed event and telemetry"]
    J --> K["Reset TurnRuntime"]
```

## Batch Dependency and Handoff Rules

| Batch | Purpose | Hard dependency | Rollback boundary |
|---|---|---|---|
| 1 | Security and concurrency correctness | current behavior baseline | revert Batch 1 commits only; no later API may depend on them yet |
| 2 | Typed contracts, telemetry, task supervision, fast CI | Batch 1 `TurnRuntime` and coordinator | keep Batch 1; revert protocol/telemetry commits independently |
| 3 | Behavioral decomposition | Batch 2 contracts and characterization gates | revert one extracted subsystem at a time |
| 4 | Packaging reduction | all runtime behavior stable | restore dependency declarations without reverting runtime refactors |

---

## Preflight: Protect the Existing Workspace and Record the Baseline

### Task 0: Establish a portable execution baseline

**Files:**
- Read only: `pyproject.toml`
- Read only: `.github/workflows/ci.yml`
- Inspect: the Git working tree with `git status`
- Do not modify source files in this task.

**Interfaces:**
- Consumes: the current repository checkout and this plan.
- Produces: an implementation branch whose baseline and unrelated changes are explicitly recorded.

- [ ] **Historical Step 1: superseded execution setup — do not run**

No active execution begins from this file. Close this historical ledger and follow `docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md` from its Cross-Computer Bootstrap section through Task 27.

- [ ] **Step 2: Record existing uncommitted changes**

Run:

```powershell
git status --short
git diff -- pyproject.toml
```

Expected: the executor can distinguish pre-existing user edits from plan edits. Save the output in the agent’s task notes; do not commit it.

- [ ] **Step 3: Verify the portable implementation branch**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location -LiteralPath $repoRoot
if ((git branch --show-current) -ne 'codex/full-remediation') {
  throw 'expected branch codex/full-remediation'
}
git status --short --untracked-files=all
```

Expected: the command runs from the repository root on `codex/full-remediation`; preserve every pre-existing status entry recorded by the authoritative plan's Task 0.

- [ ] **Step 4: Run the fast behavioral baseline**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest $runnerTests tests/agent/test_loop_runner_integration.py tests/agent/test_loop_progress.py tests/agent/test_loop_save_turn.py tests/session tests/providers -q
uv run ruff check miniunicorn tests
Set-Location webui
bun run lint
bun x tsc --noEmit
bun run test
Set-Location ..
```

Expected: Python focused suites pass; Ruff passes; frontend lint and type-check pass; Vitest reports 293 passing tests at the current baseline. Record pre-existing warning text separately because Batch 2 removes it.

- [ ] **Step 5: Confirm no baseline files were changed**

```powershell
git status --short
```

Expected: no new modifications caused by the baseline commands.

---

# Batch 1 — Security Boundaries and Turn-State Isolation

## Batch 1 Acceptance Contract

- Windows shell sandbox requests fail closed unless one explicit compatibility flag opts into unsandboxed fallback.
- A non-loopback API bind without a bearer key is rejected both from config and from a CLI `--host` override.
- Same-session work is serialized across bus and direct entry points.
- Different sessions overlap when the global concurrency limit permits.
- Usage, last-call usage, iteration, latency, hooks, and telemetry-ready trace data cannot leak between concurrent turns.

### Task 1: Make unsupported Windows shell sandboxing fail closed

**Files:**
- Modify: `tests/tools/test_exec_platform.py`
- Modify: `miniunicorn/agent/tools/shell.py`
- Modify: `docs/configuration.md`

**Interfaces:**
- Add config field: `ExecToolConfig.allow_unsandboxed_fallback: bool = False`.
- Add constructor argument: `ExecTool(..., allow_unsandboxed_fallback: bool = False)`.
- Preserve `_prepare_command(...) -> _PreparedCommand | str`; a fail-closed result is an `Error:` string and must be returned before `_spawn`.

- [ ] **Step 1: Replace the Windows permissive test with fail-closed coverage**

Add these cases next to the current `test_bwrap_skipped_on_windows` coverage in `tests/tools/test_exec_platform.py`:

```python
@pytest.mark.asyncio
async def test_sandbox_fails_closed_on_windows(tmp_path):
    with (
        patch("miniunicorn.agent.tools.shell._IS_WINDOWS", True),
        patch.object(ExecTool, "_spawn", new_callable=AsyncMock) as spawn,
        patch.object(ExecTool, "_guard_command", return_value=None),
    ):
        tool = ExecTool(working_dir=str(tmp_path), sandbox="bwrap")
        result = await tool.execute(command="echo safe")
    assert result.startswith("Error: sandbox 'bwrap' is not supported on Windows")
    assert "allow_unsandboxed_fallback" in result
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_windows_unsandboxed_fallback_runs(tmp_path):
    process = AsyncMock()
    process.communicate.return_value = (b"ok\n", b"")
    process.returncode = 0
    with (
        patch("miniunicorn.agent.tools.shell._IS_WINDOWS", True),
        patch.object(ExecTool, "_spawn", return_value=process) as spawn,
        patch.object(ExecTool, "_guard_command", return_value=None),
    ):
        tool = ExecTool(
            working_dir=str(tmp_path),
            sandbox="bwrap",
            allow_unsandboxed_fallback=True,
        )
        result = await tool.execute(command="echo safe")
    assert "ok" in result
    spawn.assert_awaited_once()
```

- [ ] **Step 2: Observe the security test fail**

```powershell
uv run pytest tests/tools/test_exec_platform.py -k "sandbox and windows" -v
```

Expected: the default fail-closed assertion fails because current code logs a warning and spawns unsandboxed.

- [ ] **Step 3: Add and plumb the compatibility flag**

Add the field to `ExecToolConfig`, pass it from `ExecTool.create()`, accept it in `ExecTool.__init__()`, and store it:

```python
class ExecToolConfig(Base):
    enable: bool = True
    timeout: int = Field(default=60, ge=0)
    path_append: str = ""
    sandbox: str = ""
    allow_unsandboxed_fallback: bool = False
    unshare_net: bool = False
    allowed_env_keys: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)
```

Use this exact fail-closed branch in `_prepare_command()`:

```python
if self.sandbox:
    if _IS_WINDOWS:
        if not self.allow_unsandboxed_fallback:
            return (
                f"Error: sandbox '{self.sandbox}' is not supported on Windows; "
                "refusing to run unsandboxed. Set "
                "tools.exec.allow_unsandboxed_fallback=true only if this risk is accepted."
            )
        logger.warning(
            "Sandbox '{}' is not supported on Windows; explicit unsandboxed fallback enabled",
            self.sandbox,
        )
    else:
        workspace = workspace_root or cwd
        command = wrap_command(
            self.sandbox,
            command,
            workspace,
            cwd,
            unshare_net=self.unshare_net,
        )
        cwd = str(Path(workspace).resolve())
```

- [ ] **Step 4: Update configuration documentation**

Document:

```yaml
tools:
  exec:
    sandbox: ""                       # Linux sandbox backend; empty disables it
    allow_unsandboxed_fallback: false # Windows-only legacy escape hatch
```

State explicitly that a configured sandbox is fail-closed on Windows by default and that enabling the fallback executes commands without OS sandbox isolation.

- [ ] **Step 5: Run focused and neighboring tests**

```powershell
uv run pytest tests/tools/test_exec_platform.py tests/tools/test_exec_tool.py -q
uv run ruff check miniunicorn/agent/tools/shell.py tests/tools/test_exec_platform.py
git diff --check
```

Expected: all selected tests pass; Ruff and whitespace checks print no errors.

- [ ] **Step 6: Commit the security boundary**

```powershell
git add miniunicorn/agent/tools/shell.py tests/tools/test_exec_platform.py docs/configuration.md
git commit -m "fix(exec): fail closed for unsupported Windows sandbox"
```

### Task 2: Reject unauthenticated public API binds

**Files:**
- Modify: `miniunicorn/config/schema.py`
- Modify: `miniunicorn/cli/commands.py`
- Create: `tests/config/test_api_security.py`
- Modify: `tests/cli/test_commands.py`
- Modify: `tests/agent/test_onboard_logic.py`
- Modify: `docs/configuration.md`
- Modify: `docs/openai-api.md`

**Interfaces:**
- Add `is_loopback_bind_host(host: str) -> bool`.
- Add `validate_api_bind_security(host: str, api_key: str, allow_insecure_public_bind: bool) -> None`.
- Add config field: `ApiConfig.allow_insecure_public_bind: bool = False`.
- Treat `localhost`, all IPv4 loopback addresses in `127.0.0.0/8`, and IPv6 loopback `::1` as local.

- [ ] **Step 1: Write configuration validation tests**

Create `tests/config/test_api_security.py`:

```python
import pytest
from pydantic import ValidationError

from miniunicorn.config.schema import ApiConfig, is_loopback_bind_host


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.2.3.4", "::1", "[::1]"])
def test_loopback_hosts_are_accepted_without_api_key(host):
    assert is_loopback_bind_host(host)
    assert ApiConfig(host=host).api_key == ""


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "api.example.test"])
def test_public_host_without_key_is_rejected(host):
    with pytest.raises(ValidationError, match="api_key"):
        ApiConfig(host=host)


def test_public_host_with_key_is_accepted():
    config = ApiConfig(host="0.0.0.0", api_key="secret")
    assert config.host == "0.0.0.0"


def test_explicit_insecure_override_is_accepted(caplog):
    config = ApiConfig(host="0.0.0.0", allow_insecure_public_bind=True)
    assert config.allow_insecure_public_bind is True
    assert "unauthenticated public bind explicitly enabled" in caplog.text
```

- [ ] **Step 2: Add a CLI override regression test**

In `tests/cli/test_commands.py`, patch `web.run_app` and assert `serve --host 0.0.0.0` exits before building or running the app when the loaded config has no key:

```python
def test_serve_host_override_rejects_unauthenticated_public_bind(
    monkeypatch,
    tmp_path,
):
    config_file = _write_instance_config(tmp_path)
    config = Config()
    seen: dict[str, object] = {}
    _patch_serve_runtime(monkeypatch, config, seen)

    result = runner.invoke(
        app,
        ["serve", "--config", str(config_file), "--host", "0.0.0.0"],
    )

    assert result.exit_code == 1
    assert "api_key" in result.output
    assert "api_app" not in seen
```

- [ ] **Step 3: Observe both failure modes**

```powershell
uv run pytest tests/config/test_api_security.py tests/cli/test_commands.py -k "public_bind or loopback_hosts" -v
```

Expected: collection or assertions fail because the helper/override do not exist and public bind currently only logs.

- [ ] **Step 4: Implement one shared validator**

At module scope in `miniunicorn/config/schema.py`:

```python
import ipaddress
import logging


def is_loopback_bind_host(host: str) -> bool:
    normalized = host.strip().strip("[]")
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_api_bind_security(
    host: str,
    api_key: str,
    allow_insecure_public_bind: bool,
) -> None:
    if is_loopback_bind_host(host) or api_key.strip():
        return
    if allow_insecure_public_bind:
        logging.getLogger(__name__).warning(
            "API unauthenticated public bind explicitly enabled for %s", host
        )
        return
    raise ValueError(
        f"API host {host!r} is not loopback and requires api.api_key; "
        "set api.allow_insecure_public_bind=true only to accept unauthenticated exposure"
    )
```

Update `ApiConfig`:

```python
class ApiConfig(Base):
    host: str = "127.0.0.1"
    port: int = 8900
    timeout: float = 120.0
    api_key: str = ""
    allow_insecure_public_bind: bool = False

    @model_validator(mode="after")
    def _validate_api_security(self) -> "ApiConfig":
        validate_api_bind_security(
            self.host,
            self.api_key,
            self.allow_insecure_public_bind,
        )
        return self
```

- [ ] **Step 5: Validate the effective CLI host**

Immediately after resolving the CLI overrides in `serve()`:

```python
try:
    validate_api_bind_security(
        host,
        api_cfg.api_key,
        api_cfg.allow_insecure_public_bind,
    )
except ValueError as exc:
    console.print(f"[red]Error: {exc}[/red]")
    raise typer.Exit(1) from exc
```

Import `validate_api_bind_security` through the normal config import section. This second call is required because Pydantic validated the configured host before Typer applied `--host`.

- [ ] **Step 6: Repair intentional public-bind fixtures**

In `tests/agent/test_onboard_logic.py`, change only fixtures that intentionally construct an unauthenticated public bind:

```python
ApiConfig(host="0.0.0.0", port=9999, allow_insecure_public_bind=True)
```

Do not set the override on ordinary production examples.

- [ ] **Step 7: Document secure and explicit-insecure forms**

Add examples:

```yaml
api:
  host: "0.0.0.0"
  api_key: "${MINIUNICORN_API_KEY}"
```

and, under a red/danger warning:

```yaml
api:
  host: "0.0.0.0"
  allow_insecure_public_bind: true
```

State that the CLI override is checked against the same rule.

- [ ] **Step 8: Verify and commit**

```powershell
uv run pytest tests/config/test_api_security.py tests/cli/test_commands.py tests/agent/test_onboard_logic.py -q
uv run ruff check miniunicorn/config/schema.py miniunicorn/cli/commands.py tests/config/test_api_security.py tests/cli/test_commands.py
git diff --check
git add miniunicorn/config/schema.py miniunicorn/cli/commands.py tests/config/test_api_security.py tests/cli/test_commands.py tests/agent/test_onboard_logic.py docs/configuration.md docs/openai-api.md
git commit -m "fix(api): require auth for public binds"
```

Expected: all selected tests and static checks pass.

### Task 3: Introduce an explicitly bound per-turn runtime

**Files:**
- Create: `miniunicorn/agent/turn_runtime.py`
- Create: `tests/agent/test_turn_runtime.py`
- Modify: `miniunicorn/agent/_state_machine.py`

**Interfaces:**
- `TurnRuntime` owns all mutable data that is meaningful only for one turn.
- `bind_turn_runtime()` and `reset_turn_runtime()` are token-based; nesting and exception cleanup must work.
- `require_turn_runtime()` raises a clear `RuntimeError` outside a bound turn.
- `TurnContext.usage` and `TurnContext.last_call_usage` carry the completed runner result into session persistence.

- [ ] **Step 1: Write the ContextVar lifecycle tests**

Create `tests/agent/test_turn_runtime.py`:

```python
import pytest

from miniunicorn.agent.turn_runtime import (
    TurnRuntime,
    bind_turn_runtime,
    current_turn_runtime,
    require_turn_runtime,
    reset_turn_runtime,
)


def test_turn_runtime_is_absent_by_default():
    assert current_turn_runtime() is None
    with pytest.raises(RuntimeError, match="No turn runtime is bound"):
        require_turn_runtime()


def test_turn_runtime_binding_is_nested_and_resettable():
    outer = TurnRuntime(turn_id="outer", session_key="ws:a")
    inner = TurnRuntime(turn_id="inner", session_key="ws:b")
    outer_token = bind_turn_runtime(outer)
    try:
        assert require_turn_runtime() is outer
        inner_token = bind_turn_runtime(inner)
        try:
            assert require_turn_runtime() is inner
        finally:
            reset_turn_runtime(inner_token)
        assert require_turn_runtime() is outer
    finally:
        reset_turn_runtime(outer_token)
    assert current_turn_runtime() is None
```

- [ ] **Step 2: Observe the missing-module failure**

```powershell
uv run pytest tests/agent/test_turn_runtime.py -v
```

Expected: FAIL during import because `turn_runtime.py` does not exist.

- [ ] **Step 3: Implement the complete runtime model**

Create `miniunicorn/agent/turn_runtime.py`:

```python
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

SESSION_LAST_USAGE_KEY = "last_usage"
SESSION_LAST_CALL_USAGE_KEY = "last_call_usage"


@dataclass(slots=True)
class TurnRuntime:
    turn_id: str
    session_key: str
    iteration: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    last_call_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int | None = None
    queue_wait_ms: int = 0
    stop_reason: str = ""
    state_durations_ms: dict[str, float] = field(default_factory=dict)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


_CURRENT_TURN: ContextVar[TurnRuntime | None] = ContextVar(
    "miniunicorn_current_turn",
    default=None,
)


def bind_turn_runtime(runtime: TurnRuntime) -> Token[TurnRuntime | None]:
    return _CURRENT_TURN.set(runtime)


def reset_turn_runtime(token: Token[TurnRuntime | None]) -> None:
    _CURRENT_TURN.reset(token)


def current_turn_runtime() -> TurnRuntime | None:
    return _CURRENT_TURN.get()


def require_turn_runtime() -> TurnRuntime:
    runtime = current_turn_runtime()
    if runtime is None:
        raise RuntimeError("No turn runtime is bound to the current task")
    return runtime
```

- [ ] **Step 4: Add result fields to the turn state**

In `TurnContext`, directly after `had_injections`:

```python
usage: dict[str, int] = field(default_factory=dict)
last_call_usage: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 5: Verify isolation and typing**

```powershell
uv run pytest tests/agent/test_turn_runtime.py tests/agent/test_state_machine.py -q
uv run ruff check miniunicorn/agent/turn_runtime.py miniunicorn/agent/_state_machine.py tests/agent/test_turn_runtime.py
```

Expected: selected tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit the turn-runtime primitive**

```powershell
git add miniunicorn/agent/turn_runtime.py miniunicorn/agent/_state_machine.py tests/agent/test_turn_runtime.py
git commit -m "refactor(agent): add isolated turn runtime"
```

### Task 4: Carry runner results through the turn and session

**Files:**
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/_state_machine.py`
- Modify: `miniunicorn/agent/turn_runtime.py`
- Modify: `miniunicorn/command/builtin.py`
- Modify: `tests/agent/test_loop_runner_integration.py`
- Modify: `tests/agent/test_loop_save_turn.py`
- Modify: `tests/cli/test_restart_command.py`

**Interfaces:**
- Add `AgentLoopRunResult`, a typed internal result replacing the five-value tuple from `_run_agent_loop()`.
- Carry `usage` and `last_call_usage` through `TurnContext`.
- Persist last completed session usage under `session.metadata["last_usage"]` and `session.metadata["last_call_usage"]`.
- `/status` reads the addressed session’s last completed usage.
- Keep the old loop fields temporarily in this task so each intermediate commit remains green; Task 6 removes them after every entry point has a bound runtime.

- [ ] **Step 1: Add result-plumbing and session-status tests**

In `tests/agent/test_loop_runner_integration.py`, make a fake runner return:

```python
AgentRunResult(
    final_content="done",
    messages=[{"role": "assistant", "content": "done"}],
    usage={"prompt_tokens": 101, "completion_tokens": 11},
    last_call_usage={"prompt_tokens": 77, "completion_tokens": 11},
)
```

Assert `_state_run()` copies both dictionaries into `TurnContext`.

In `tests/agent/test_loop_save_turn.py`, assert `_state_save()` writes copied dictionaries to session metadata. In `tests/cli/test_restart_command.py`, seed session metadata rather than `loop._last_usage` and assert `/status` shows that session’s values.

- [ ] **Step 2: Observe the missing result/session fields**

```powershell
uv run pytest tests/agent/test_loop_runner_integration.py tests/agent/test_loop_save_turn.py tests/cli/test_restart_command.py -k "usage or status" -v
```

Expected: the new assertions fail because `_run_agent_loop()` discards usage and `/status` reads loop-global state.

- [ ] **Step 3: Add the typed internal result**

Add to `miniunicorn/agent/turn_runtime.py`:

```python
@dataclass(slots=True)
class AgentLoopRunResult:
    final_content: str | None
    tools_used: list[str]
    messages: list[dict[str, Any]]
    stop_reason: str
    had_injections: bool
    usage: dict[str, int] = field(default_factory=dict)
    last_call_usage: dict[str, int] = field(default_factory=dict)
```

Return this type from `_run_agent_loop()` and change `_state_run()` to copy every field into `TurnContext`:

```python
ctx.final_content = result.final_content
ctx.tools_used = result.tools_used
ctx.all_messages = result.messages
ctx.stop_reason = result.stop_reason
ctx.had_injections = result.had_injections
ctx.usage = dict(result.usage)
ctx.last_call_usage = dict(result.last_call_usage)
```

- [ ] **Step 4: Persist completed session usage**

Before `SessionManager.save()` in `_state_save()`:

```python
ctx.session.metadata[SESSION_LAST_USAGE_KEY] = dict(ctx.usage)
ctx.session.metadata[SESSION_LAST_CALL_USAGE_KEY] = dict(ctx.last_call_usage)
```

Only write these keys after an actual runner result. Command shortcuts without an LLM call retain the previous completed usage.

- [ ] **Step 5: Move `/status` to session metadata**

Use:

```python
last_usage = dict(session.metadata.get(SESSION_LAST_USAGE_KEY) or {})
if ctx_est <= 0:
    ctx_est = last_usage.get("prompt_tokens", 0)
```

Pass `last_usage` to `build_status_content()`. Remove both `/status` reads of `loop._last_usage`.

- [ ] **Step 6: Verify and commit the green migration seam**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest tests/agent/test_loop_runner_integration.py tests/agent/test_loop_save_turn.py tests/cli/test_restart_command.py $runnerTests -q
uv run ruff check miniunicorn/agent/loop.py miniunicorn/agent/_state_machine.py miniunicorn/agent/turn_runtime.py miniunicorn/command/builtin.py
git diff --check
git add miniunicorn/agent/loop.py miniunicorn/agent/_state_machine.py miniunicorn/agent/turn_runtime.py miniunicorn/command/builtin.py tests/agent/test_loop_runner_integration.py tests/agent/test_loop_save_turn.py tests/cli/test_restart_command.py
git commit -m "refactor(agent): carry usage through turn results"
```

### Task 5: Unify direct and bus concurrency through `TurnCoordinator`

**Files:**
- Create: `miniunicorn/agent/turn_coordinator.py`
- Create: `tests/agent/test_turn_concurrency.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/turn_runtime.py`

**Interfaces:**
- `TurnCoordinator.scope(session_key: str, turn_id: str | None = None)` is an async context manager yielding `TurnRuntime`.
- Lock acquisition precedes semaphore acquisition.
- Locks remain weakly held to avoid unbounded growth.
- `_dispatch()` retains mid-turn injection queue behavior.
- `process_direct()` serializes same-session calls but never turns them into injected bus messages.

- [ ] **Step 1: Write coordinator ordering and cleanup tests**

Create `tests/agent/test_turn_concurrency.py` with:

```python
@pytest.mark.asyncio
async def test_same_session_is_serialized():
    coordinator = TurnCoordinator(max_concurrent_requests=2)
    active = 0
    peak = 0

    async def run_one():
        nonlocal active, peak
        async with coordinator.scope("ws:same"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(run_one(), run_one())
    assert peak == 1


@pytest.mark.asyncio
async def test_different_sessions_overlap():
    coordinator = TurnCoordinator(max_concurrent_requests=2)
    entered = asyncio.Event()
    count = 0

    async def run_one(key):
        nonlocal count
        async with coordinator.scope(key):
            count += 1
            if count == 2:
                entered.set()
            await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.gather(run_one("ws:a"), run_one("ws:b"))
    assert count == 2


@pytest.mark.asyncio
async def test_waiting_same_session_does_not_consume_global_permit():
    coordinator = TurnCoordinator(max_concurrent_requests=2)
    release_a = asyncio.Event()
    b_entered = asyncio.Event()

    async def first_a():
        async with coordinator.scope("ws:a"):
            await release_a.wait()

    async def second_a():
        async with coordinator.scope("ws:a"):
            return

    async def session_b():
        async with coordinator.scope("ws:b"):
            b_entered.set()

    task_a1 = asyncio.create_task(first_a())
    await asyncio.sleep(0)
    task_a2 = asyncio.create_task(second_a())
    await asyncio.sleep(0)
    task_b = asyncio.create_task(session_b())
    await asyncio.wait_for(b_entered.wait(), timeout=1)
    release_a.set()
    await asyncio.gather(task_a1, task_a2, task_b)


@pytest.mark.asyncio
async def test_runtime_is_reset_after_exception():
    coordinator = TurnCoordinator(max_concurrent_requests=1)
    with pytest.raises(RuntimeError, match="boom"):
        async with coordinator.scope("ws:a"):
            assert current_turn_runtime() is not None
            raise RuntimeError("boom")
    assert current_turn_runtime() is None
```

- [ ] **Step 2: Observe the missing-coordinator failure**

```powershell
uv run pytest tests/agent/test_turn_concurrency.py -v
```

Expected: import failure because `TurnCoordinator` does not exist.

- [ ] **Step 3: Implement the coordinator**

Create `miniunicorn/agent/turn_coordinator.py`:

```python
from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from miniunicorn.agent.turn_runtime import (
    TurnRuntime,
    bind_turn_runtime,
    reset_turn_runtime,
)


class TurnCoordinator:
    def __init__(self, max_concurrent_requests: int | None) -> None:
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._gate = (
            asyncio.Semaphore(max_concurrent_requests)
            if max_concurrent_requests and max_concurrent_requests > 0
            else None
        )

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    @asynccontextmanager
    async def scope(
        self,
        session_key: str,
        turn_id: str | None = None,
    ) -> AsyncIterator[TurnRuntime]:
        wait_started = time.monotonic()
        lock = self._lock_for(session_key)
        async with lock:
            if self._gate is not None:
                await self._gate.acquire()
            runtime = TurnRuntime(
                turn_id=turn_id or uuid.uuid4().hex,
                session_key=session_key,
                queue_wait_ms=int((time.monotonic() - wait_started) * 1000),
            )
            token = bind_turn_runtime(runtime)
            try:
                yield runtime
            finally:
                reset_turn_runtime(token)
                if self._gate is not None:
                    self._gate.release()
```

- [ ] **Step 4: Replace loop-owned locks and gate**

Construct one coordinator from the existing `max_concurrent_requests` value. Remove the loop’s direct lock/semaphore construction. If tests or `/stop` still need a lock dictionary during this batch, expose a read-only compatibility alias:

```python
self._turn_coordinator = TurnCoordinator(max_concurrent_requests)
self._session_locks = self._turn_coordinator.session_locks
```

Add a `session_locks` property returning the weak dictionary only if required by existing code; mark it internal and do not expose the semaphore.

- [ ] **Step 5: Route both entry points through the scope**

In `_dispatch()`, replace `async with lock, gate:` with:

```python
async with self._turn_coordinator.scope(session_key) as turn_runtime:
```

Keep pending queue publication and drain inside this scope. Add the internal
result and copy helper to `turn_runtime.py`:

```python
if TYPE_CHECKING:
    from miniunicorn.agent._state_machine import TurnContext
    from miniunicorn.bus.events import OutboundMessage


@dataclass(slots=True)
class ProcessedTurn:
    outbound: OutboundMessage | None
    context: TurnContext | None


def complete_turn_runtime(
    runtime: TurnRuntime,
    context: TurnContext | None,
) -> None:
    if context is None:
        return
    runtime.usage = dict(context.usage)
    runtime.last_call_usage = dict(context.last_call_usage)
    runtime.latency_ms = context.turn_latency_ms
    runtime.stop_reason = context.stop_reason
```

Rename the current state-machine implementation to
`_execute_message(...) -> ProcessedTurn`. A normal turn returns
`ProcessedTurn(ctx.outbound, ctx)`. The existing system-message branch returns
`ProcessedTurn(system_outbound, None)` because it does not construct a
`TurnContext`. Keep `_process_message(...) -> OutboundMessage | None` as a
compatibility wrapper:

```python
async def _process_message(self, *args, **kwargs) -> OutboundMessage | None:
    return (await self._execute_message(*args, **kwargs)).outbound
```

When constructing `TurnContext`, use `require_turn_runtime().turn_id` so the
state trace, telemetry, and coordinator all share one ID.

In `_dispatch()`, call the new internal method and populate the yielded
runtime before publishing the outbound response:

```python
result = await self._execute_message(
    msg,
    on_stream=on_stream,
    on_stream_end=on_stream_end,
    pending_queue=pending,
)
complete_turn_runtime(turn_runtime, result.context)
response = result.outbound
```

In `process_direct()`:

```python
effective_key = session_key
async with self._turn_coordinator.scope(effective_key) as turn_runtime:
    result = await self._execute_message(
        msg,
        session_key=effective_key,
        on_progress=on_progress,
        on_stream=on_stream,
        on_stream_end=on_stream_end,
        turn_hooks=hooks,
    )
    complete_turn_runtime(turn_runtime, result.context)
    return result.outbound
```

- [ ] **Step 6: Add entry-point behavioral tests**

Using a patched `_execute_message()` that records entry/exit:

- two `process_direct(..., session_key="sdk:a")` calls peak at one;
- `sdk:a` and `sdk:b` peak at two;
- a bus `_dispatch()` and a direct call using the same effective key peak at one;
- hooks passed to two concurrent direct calls remain attached to their own call;
- a cancelled turn releases both the session lock and global permit.

- [ ] **Step 7: Verify and commit**

```powershell
uv run pytest tests/agent/test_turn_concurrency.py tests/agent/test_loop_runner_integration.py -q
uv run ruff check miniunicorn/agent/turn_coordinator.py miniunicorn/agent/loop.py tests/agent/test_turn_concurrency.py
git diff --check
git add miniunicorn/agent/turn_coordinator.py miniunicorn/agent/turn_runtime.py miniunicorn/agent/loop.py tests/agent/test_turn_concurrency.py tests/agent/test_loop_runner_integration.py
git commit -m "refactor(agent): coordinate every turn entry point"
```

### Task 6: Remove shared turn fields and make turn-end/self reads concurrency-safe

**Files:**
- Modify: `miniunicorn/agent/_state_machine.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/tools/runtime_state.py`
- Modify: `miniunicorn/agent/tools/self.py`
- Modify: `tests/agent/test_loop_progress.py`
- Modify: `tests/agent/tools/test_self_tool.py`
- Create: `tests/session/test_webui_turns.py`
- Modify: `tests/agent/test_turn_concurrency.py`

**Interfaces:**
- Remove mutable loop fields `_last_usage`, `_last_call_usage`, `_current_iteration`, and `_pending_turn_latency_ms`.
- Update the bound runtime after each LLM usage update so self-inspection during a running turn sees its own cumulative usage.
- The compatibility self-tool keys `_current_iteration` and `_last_usage` remain read-only, but resolve through the current `TurnRuntime`.
- WebUI `turn_end.context_usage` comes from the completed turn’s context/runtime.

- [ ] **Step 1: Write cross-session turn-end and self-inspection regressions**

Add a test with two interleaved WebSocket turns whose fake runner results use distinct usage:

```python
usage_a = {"prompt_tokens": 101, "completion_tokens": 11}
usage_b = {"prompt_tokens": 202, "completion_tokens": 22}
```

Assert each emitted `turn_end.context_usage` matches its own turn and each saved
session metadata matches its own key. Add a self-tool test with two bound
runtimes whose iteration/usage values differ and assert each task reads its own
values. Retain Task 4’s `/status` regression proving session A is not affected
by a bound turn for session B.

- [ ] **Step 2: Observe the shared-read failure**

```powershell
uv run pytest tests/agent/test_turn_concurrency.py tests/agent/test_loop_progress.py tests/agent/tools/test_self_tool.py tests/session/test_webui_turns.py -k "usage or iteration or turn_end" -v
```

Expected: the new cross-session assertions fail because progress, self-tool,
and turn-end still use loop-global fields.

- [ ] **Step 3: Update iteration and usage on the bound runtime**

Replace `_run_agent_loop()`’s iteration lambda with:

```python
def _record_iteration(iteration: int) -> None:
    runtime = current_turn_runtime()
    if runtime is not None:
        runtime.iteration = iteration
```

After each cumulative usage update in `AgentRunner.run()`, including the
finalization-retry path, copy into the bound runtime:

```python
runtime = current_turn_runtime()
if runtime is not None:
    runtime.usage = dict(usage)
    if raw_usage:
        runtime.last_call_usage = dict(raw_usage)
```

Use `retry_usage` rather than `raw_usage` in the retry branch. At final return,
copy `AgentRunResult.usage` and `.last_call_usage` once more so every exit path
is covered.

- [ ] **Step 4: Replace loop storage with calculated compatibility properties**

Delete constructor assignments for all four removed fields. On `AgentLoop`:

```python
@property
def _current_iteration(self) -> int:
    runtime = current_turn_runtime()
    return runtime.iteration if runtime is not None else 0


@property
def _last_usage(self) -> dict[str, int]:
    runtime = current_turn_runtime()
    return dict(runtime.usage) if runtime is not None else {}
```

Do not add setters. Do not add a `_last_call_usage` compatibility property.

- [ ] **Step 5: Bind self-tool compatibility reads to the current turn**

Keep `_current_iteration` and `_last_usage` in the self-tool description. Add `_last_usage` to `READ_ONLY`. The `RuntimeState` protocol retains those properties, backed by this task’s calculated properties. Update tests to bind a `TurnRuntime` before checking either key:

```python
runtime = TurnRuntime(
    turn_id="test",
    session_key="ws:a",
    iteration=3,
    usage={"prompt_tokens": 42},
)
token = bind_turn_runtime(runtime)
try:
    assert await tool.execute(action="check", key="_current_iteration") == "3"
    assert "42" in await tool.execute(action="check", key="_last_usage")
finally:
    reset_turn_runtime(token)
```

Use the tool’s actual serialized response assertions rather than forcing these
exact strings if its current format differs. Replace old tests that assign
`loop._last_usage` with bound-runtime setup.

- [ ] **Step 6: Send WebUI turn-end from the completed turn**

Call:

```python
await self._webui_turns.handle_turn_end(
    msg,
    session_key=session_key,
    latency_ms=turn_runtime.latency_ms,
    context_usage=turn_runtime.last_call_usage,
)
```

Remove the session-keyed `_pending_turn_latency_ms` map, the `_state_save()`
assignment, and every cleanup pop. Latency is written to
`TurnContext.turn_latency_ms`, copied to `TurnRuntime`, then consumed in the
same coordinator scope.

- [ ] **Step 7: Run Batch 1 regression**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest tests/tools/test_exec_platform.py tests/tools/test_exec_tool.py tests/config/test_api_security.py tests/cli/test_commands.py tests/agent/test_onboard_logic.py tests/agent/test_turn_runtime.py tests/agent/test_turn_concurrency.py $runnerTests tests/agent/test_loop_runner_integration.py tests/agent/tools/test_self_tool.py tests/session -q
uv run ruff check miniunicorn tests
uv run ruff format --check miniunicorn/agent miniunicorn/config/schema.py miniunicorn/cli/commands.py tests/agent tests/config/test_api_security.py
git diff --check
```

Expected: selected suites pass; no Ruff or whitespace failures.

- [ ] **Step 8: Scan forbidden shared mutations**

```powershell
rg -n 'self\._(last_usage|last_call_usage|current_iteration|pending_turn_latency_ms)\s*=' miniunicorn
rg -n '_last_call_usage|_pending_turn_latency_ms' miniunicorn tests
```

Expected: first command has no matches. The second has no production matches; test references may remain only when explicitly asserting removal.

- [ ] **Step 9: Commit the session-facing migration**

```powershell
git add miniunicorn/agent/_state_machine.py miniunicorn/agent/loop.py miniunicorn/agent/runner.py miniunicorn/agent/tools/runtime_state.py miniunicorn/agent/tools/self.py tests/agent/test_loop_progress.py tests/agent/tools/test_self_tool.py tests/session/test_webui_turns.py tests/agent/test_turn_concurrency.py
git commit -m "fix(agent): isolate usage across concurrent sessions"
```

### Task 7: Close Batch 1 with explicit concurrency documentation

**Files:**
- Create: `docs/concurrency.md`
- Modify: `docs/configuration.md`

**Interfaces:**
- Produces an operator/developer contract for process, session, and turn state.
- No runtime behavior change.

- [ ] **Step 1: Document concurrency semantics**

Include this exact contract:

```markdown
- Calls with the same effective session key are serialized, regardless of whether
  they enter through the message bus or `process_direct()`.
- Calls with different effective session keys may run concurrently up to
  `max_concurrent_requests`.
- Waiting for a same-session lock does not consume a global concurrency slot.
- Per-turn iteration, usage, hooks, latency, and traces are task-local and are
  reset even when a turn is cancelled or raises.
- Provider rate limits remain global and may further constrain throughput.
```

- [ ] **Step 2: Re-run the cross-session proof**

```powershell
uv run pytest tests/agent/test_turn_concurrency.py -v
git diff --check
```

Expected: all concurrency tests pass.

- [ ] **Step 3: Commit the Batch 1 contract**

```powershell
git add docs/concurrency.md docs/configuration.md
git commit -m "docs(agent): define turn concurrency ownership"
```

**Batch 1 stop/go checkpoint:** Do not begin Batch 2 unless Tasks 1–7 are committed, the Batch 1 regression passes, and `git status --short` contains only the pre-existing user changes recorded in Task 0.

---

# Batch 2 — Typed Event Contract, Telemetry, Supervision, and Fast CI

## Batch 2 Acceptance Contract

- Python is the source of truth for WebSocket server event schemas.
- TypeScript event types are generated and checked for drift in CI.
- Every emitted server event validates before serialization.
- Every completed or failed turn produces one structured telemetry record; telemetry failure never fails the turn.
- Core fire-and-forget tasks have named ownership and observable exceptions.
- A Linux/Python 3.13 core gate fails fast before the full OS/Python matrix starts.

### Task 8: Define the backend event protocol as Pydantic models

**Files:**
- Create: `miniunicorn/bus/agent_events.py`
- Create: `tests/bus/test_agent_events.py`
- Modify: `miniunicorn/bus/events.py`
- Modify: `miniunicorn/utils/progress_events.py`

**Interfaces:**
- `PROTOCOL_VERSION: Literal[1] = 1`.
- `AgentEvent` is a discriminated union on the existing `event` field.
- `serialize_agent_event(event: AgentEvent) -> dict[str, Any]` is the only new-event serialization path.
- Add `OUTBOUND_META_AGENT_EVENT = "_agent_event"` for typed internal envelopes.
- Preserve `OUTBOUND_META_AGENT_UI` and every legacy `_progress`/`_turn_end` metadata flag for one compatibility release.

- [ ] **Step 1: Write event validation and serialization tests**

Create `tests/bus/test_agent_events.py` covering:

```python
def test_message_event_serializes_existing_wire_shape():
    event = MessageEvent(chat_id="chat-1", text="hello", kind="progress")
    assert serialize_agent_event(event) == {
        "protocol_version": 1,
        "event": "message",
        "chat_id": "chat-1",
        "text": "hello",
        "kind": "progress",
    }


def test_turn_end_rejects_negative_latency():
    with pytest.raises(ValidationError):
        TurnEndEvent(chat_id="chat-1", latency_ms=-1)


def test_discriminated_union_rejects_unknown_event():
    with pytest.raises(ValidationError):
        AGENT_EVENT_ADAPTER.validate_python(
            {"protocol_version": 1, "event": "unknown"}
        )


def test_tool_progress_event_preserves_version_one_payload():
    payload = ToolProgressEvent(
        phase="start",
        call_id="call-1",
        name="exec",
        arguments={"command": "pwd"},
    )
    assert payload.version == 1
    assert payload.files == []
```

- [ ] **Step 2: Observe the missing-protocol failure**

```powershell
uv run pytest tests/bus/test_agent_events.py -v
```

Expected: import failure because `miniunicorn.bus.agent_events` does not exist.

- [ ] **Step 3: Implement the complete protocol model set**

Create `miniunicorn/bus/agent_events.py`. Use a strict shared base:

```python
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION: Literal[1] = 1


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal[1] = PROTOCOL_VERSION


class ContextUsagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class GoalStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    ui_summary: str | None = None
    objective: str | None = None


class FileEditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int | None = None
    call_id: str
    tool: str
    path: str
    absolute_path: str | None = None
    phase: str | None = None
    added: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    approximate: bool | None = None
    status: Literal["editing", "done", "error"]
    operation: str | None = None
    binary: bool | None = None
    error: str | None = None
    pending: bool | None = None


class SandboxStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restrict_to_workspace: bool
    workspace_root: str
    level: str
    enforced: bool
    provider: str
    provider_label: str
    summary: str


class WorkspaceScopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_path: str
    project_name: str | None = None
    access_mode: Literal["restricted", "full"]
    restrict_to_workspace: bool | None = None
    sandbox_status: SandboxStatusPayload | None = None


class ToolProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    phase: Literal["start", "end", "error"]
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    files: list[Any] = Field(default_factory=list)
    embeds: list[Any] = Field(default_factory=list)


class ReadyEvent(EventBase):
    event: Literal["ready"] = "ready"
    chat_id: str
    client_id: str


class AttachedEvent(EventBase):
    event: Literal["attached"] = "attached"
    chat_id: str
    request_id: str | None = None


class MessageEvent(EventBase):
    event: Literal["message"] = "message"
    chat_id: str
    text: str
    reply_to: str | None = None
    media: list[str] | None = None
    media_urls: list[dict[str, str]] | None = None
    tool_events: list[ToolProgressEvent] | None = None
    kind: Literal["tool_hint", "progress", "reasoning"] | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    agent_ui: dict[str, Any] | None = None


class FileEditEvent(EventBase):
    event: Literal["file_edit"] = "file_edit"
    chat_id: str
    edits: list[FileEditPayload]


class DeltaEvent(EventBase):
    event: Literal["delta"] = "delta"
    chat_id: str
    text: str
    stream_id: str | None = None


class StreamEndEvent(EventBase):
    event: Literal["stream_end"] = "stream_end"
    chat_id: str
    stream_id: str | None = None
    text: str | None = None


class ReasoningDeltaEvent(EventBase):
    event: Literal["reasoning_delta"] = "reasoning_delta"
    chat_id: str
    text: str
    stream_id: str | None = None


class ReasoningEndEvent(EventBase):
    event: Literal["reasoning_end"] = "reasoning_end"
    chat_id: str
    stream_id: str | None = None


class RuntimeModelUpdatedEvent(EventBase):
    event: Literal["runtime_model_updated"] = "runtime_model_updated"
    model_name: str
    model_preset: str | None = None


class TurnEndEvent(EventBase):
    event: Literal["turn_end"] = "turn_end"
    chat_id: str
    latency_ms: int | None = Field(default=None, ge=0)
    goal_state: GoalStatePayload | None = None
    context_usage: ContextUsagePayload | None = None


class GoalStatusEvent(EventBase):
    event: Literal["goal_status"] = "goal_status"
    chat_id: str
    status: Literal["running", "idle"]
    started_at: float | None = None


class GoalStateEvent(EventBase):
    event: Literal["goal_state"] = "goal_state"
    chat_id: str
    goal_state: GoalStatePayload


class SessionUpdatedEvent(EventBase):
    event: Literal["session_updated"] = "session_updated"
    chat_id: str
    scope: str | None = None
    workspace_scope: WorkspaceScopePayload | None = None


class SubagentActivityEvent(EventBase):
    event: Literal["subagent_activity"] = "subagent_activity"
    chat_id: str
    label: str | None = None
    task_id: str | None = None
    content: str


class ErrorEvent(EventBase):
    event: Literal["error"] = "error"
    chat_id: str | None = None
    detail: str | None = None
    reason: str | None = None


AgentEvent = Annotated[
    ReadyEvent
    | AttachedEvent
    | MessageEvent
    | FileEditEvent
    | DeltaEvent
    | StreamEndEvent
    | ReasoningDeltaEvent
    | ReasoningEndEvent
    | RuntimeModelUpdatedEvent
    | TurnEndEvent
    | GoalStatusEvent
    | GoalStateEvent
    | SessionUpdatedEvent
    | SubagentActivityEvent
    | ErrorEvent,
    Field(discriminator="event"),
]

AGENT_EVENT_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


def serialize_agent_event(event: AgentEvent) -> dict[str, Any]:
    return AGENT_EVENT_ADAPTER.dump_python(event, mode="json", exclude_none=True)
```

If the current backend emits a field absent from this list, add that exact field to the owning model and add a fixture assertion; do not loosen `extra="forbid"`.

- [ ] **Step 4: Type tool-progress builders**

Change the builders in `miniunicorn/utils/progress_events.py` to construct `ToolProgressEvent` and then call `.model_dump(mode="json")`. Keep their public return type `dict[str, Any]` for compatibility:

```python
def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    return ToolProgressEvent(
        phase="start",
        call_id=str(getattr(tool_call, "id", "") or ""),
        name=getattr(tool_call, "name", ""),
        arguments=getattr(tool_call, "arguments", {}) or {},
    ).model_dump(mode="json")
```

- [ ] **Step 5: Add the typed envelope constant**

In `miniunicorn/bus/events.py`:

```python
OUTBOUND_META_AGENT_EVENT = "_agent_event"
```

Document that its value is a serialized `AgentEvent`, and that channels not supporting structured events may ignore it.

- [ ] **Step 6: Verify and commit**

```powershell
uv run pytest tests/bus/test_agent_events.py tests/utils/test_progress_events.py -q
uv run ruff check miniunicorn/bus/agent_events.py miniunicorn/bus/events.py miniunicorn/utils/progress_events.py tests/bus/test_agent_events.py
git diff --check
git add miniunicorn/bus/agent_events.py miniunicorn/bus/events.py miniunicorn/utils/progress_events.py tests/bus/test_agent_events.py
git commit -m "feat(protocol): define typed agent events"
```

### Task 9: Validate every WebSocket server event before sending

**Files:**
- Modify: `miniunicorn/channels/websocket/channel.py`
- Modify: `tests/channels/test_websocket_channel.py`
- Modify: `tests/channels/test_websocket_integration.py`
- Modify: `miniunicorn/session/webui_turns.py`

**Interfaces:**
- Add `_send_agent_event(event: AgentEvent, connections: Iterable[Any] | None = None, label: str = "")`.
- The helper serializes through `serialize_agent_event()`, appends replayable events to the transcript where current code does, then sends JSON.
- Existing public `send_*` methods remain callable with their current arguments.

- [ ] **Step 1: Add wire-compatibility tests**

For representative `message`, `delta`, `turn_end`, `goal_state`, `subagent_activity`, and `runtime_model_updated` events:

```python
wire = json.loads(connection.send.await_args.args[0])
assert wire["protocol_version"] == 1
assert wire["event"] == "turn_end"
assert wire["chat_id"] == "chat-1"
assert wire["context_usage"]["prompt_tokens"] == 12
```

Also assert that invalid negative latency raises Pydantic validation before `connection.send`.

- [ ] **Step 2: Observe the protocol-version failure**

```powershell
uv run pytest tests/channels/test_websocket_channel.py tests/channels/test_websocket_integration.py -k "protocol or turn_end or subagent" -v
```

Expected: new assertions fail because current dictionaries have no `protocol_version` and bypass validation.

- [ ] **Step 3: Add one event serialization helper**

Implement:

```python
async def _send_agent_event(
    self,
    event: AgentEvent,
    *,
    connections: list[Any] | None = None,
    label: str = "",
    persist: bool = False,
) -> None:
    payload = serialize_agent_event(event)
    chat_id = payload.get("chat_id")
    if persist and isinstance(chat_id, str):
        self._try_append_webui_transcript(chat_id, payload)
    raw = json.dumps(payload, ensure_ascii=False)
    targets = connections
    if targets is None and isinstance(chat_id, str):
        targets = list(self._subs.get(chat_id, ()))
    for connection in targets or []:
        await self._safe_send_to(connection, raw, label=label)
```

- [ ] **Step 4: Convert server emitters without changing their public signatures**

Replace hand-built dictionaries in:

- `_connection_loop()` for the initial `ready` frame;
- `_send_event()` for `attached`, `session_updated`, and `error`;
- `send()` for `message`, `file_edit`, and `subagent_activity`;
- `send_reasoning_delta()` and `send_reasoning_end()`;
- `send_delta()` for `delta` and `stream_end`;
- `send_turn_end()`;
- `send_goal_state()` and `send_goal_status()`;
- `send_session_updated()`;
- `send_runtime_model_updated()`.

Keep media signing and full-stream buffering before model construction. Construct the exact relevant Pydantic type after those transformations.

- [ ] **Step 5: Prefer typed internal envelopes, retain fallback flags**

At the beginning of `send()`:

```python
typed_payload = msg.metadata.get(OUTBOUND_META_AGENT_EVENT)
if isinstance(typed_payload, dict):
    event = AGENT_EVENT_ADAPTER.validate_python(typed_payload)
    await self._send_agent_event(event, persist=True)
    return
```

Change `WebuiTurnCoordinator` to attach a serialized `TurnEndEvent` under `OUTBOUND_META_AGENT_EVENT` while continuing to attach `_turn_end`, `latency_ms`, `goal_state`, and `context_usage`. Typed-aware WebSocket code uses the new field; other code and older tests still see the flags.

- [ ] **Step 6: Run the full WebSocket suite**

```powershell
uv run pytest tests/channels/test_websocket_channel.py tests/channels/test_websocket_integration.py tests/channels/test_websocket_envelope_media.py tests/agent/test_loop_direct_websocket_status.py tests/session/test_webui_turns.py -q
uv run ruff check miniunicorn/channels/websocket/channel.py miniunicorn/session/webui_turns.py
git diff --check
```

Expected: selected suites pass; existing event fields remain unchanged apart from `protocol_version`.

- [ ] **Step 7: Commit the validated emitter path**

```powershell
git add miniunicorn/channels/websocket/channel.py miniunicorn/session/webui_turns.py tests/channels/test_websocket_channel.py tests/channels/test_websocket_integration.py
git commit -m "refactor(websocket): validate outbound agent events"
```

### Task 10: Generate TypeScript event types and enforce schema drift checks

**Files:**
- Create: `scripts/export_agent_event_schema.py`
- Create: `webui/scripts/generate-agent-events.mjs`
- Create: `webui/src/generated/agent-events.schema.json`
- Create: `webui/src/generated/agent-events.ts`
- Create: `webui/src/tests/agent-events-contract.test.ts`
- Modify: `webui/src/lib/types.ts`
- Modify: `webui/package.json`
- Modify: `webui/bun.lock`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Python emits the committed JSON Schema.
- Node generates a committed TypeScript declaration from that schema.
- Both generators support `--check` and exit nonzero without writing on drift.
- Frontend code imports `InboundEvent` from generated output; no hand-maintained duplicate union remains.

- [ ] **Step 1: Write a frontend contract test against generated types**

Create `webui/src/tests/agent-events-contract.test.ts`:

```typescript
import { describe, expect, it } from "vitest";

import type { InboundEvent } from "@/lib/types";

function eventName(event: InboundEvent): string {
  return event.event;
}

describe("generated agent event contract", () => {
  it("accepts a versioned turn_end event", () => {
    const event: InboundEvent = {
      protocol_version: 1,
      event: "turn_end",
      chat_id: "chat-1",
      context_usage: {
        prompt_tokens: 12,
        completion_tokens: 3,
        total_tokens: 15,
        cached_tokens: 0,
      },
    };
    expect(eventName(event)).toBe("turn_end");
  });

  it("includes subagent activity in the discriminated union", () => {
    const event: InboundEvent = {
      protocol_version: 1,
      event: "subagent_activity",
      chat_id: "chat-1",
      content: "working",
    };
    expect(eventName(event)).toBe("subagent_activity");
  });
});
```

- [ ] **Step 2: Export a deterministic Python schema**

Create `scripts/export_agent_event_schema.py`:

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from miniunicorn.bus.agent_events import AGENT_EVENT_ADAPTER

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPOSITORY_ROOT / "webui" / "src" / "generated" / "agent-events.schema.json"


def render_schema() -> str:
    schema = AGENT_EVENT_ADAPTER.json_schema()
    schema["title"] = "InboundEvent"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_schema()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != rendered:
            raise SystemExit("agent event JSON schema is stale")
        return
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Add the TypeScript generator**

Install exactly one dev dependency:

```powershell
Set-Location webui
bun add --dev json-schema-to-typescript
Set-Location ..
```

Create `webui/scripts/generate-agent-events.mjs`:

```javascript
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import process from "node:process";

import { compileFromFile } from "json-schema-to-typescript";

const webuiRoot = fileURLToPath(new URL("../", import.meta.url));
const schemaPath = path.join(
  webuiRoot,
  "src",
  "generated",
  "agent-events.schema.json",
);
const outputPath = path.join(
  webuiRoot,
  "src",
  "generated",
  "agent-events.ts",
);
const generated = await compileFromFile(schemaPath, {
  bannerComment: "/* Generated from Python Pydantic models. Do not edit. */",
});
const normalized = `${generated.trimEnd()}\n`;
const checkOnly = process.argv.includes("--check");

if (checkOnly) {
  let current = "";
  try {
    current = await readFile(outputPath, "utf8");
  } catch {
    process.stderr.write("generated agent event types are missing\n");
    process.exitCode = 1;
  }
  if (current && current !== normalized) {
    process.stderr.write("generated agent event types are stale\n");
    process.exitCode = 1;
  }
} else {
  await mkdir(path.dirname(outputPath), { recursive: true });
  await writeFile(outputPath, normalized, "utf8");
}
```

- [ ] **Step 4: Add deterministic package scripts**

In `webui/package.json`:

```json
"generate:protocol": "python ../scripts/export_agent_event_schema.py && node scripts/generate-agent-events.mjs",
"check:protocol": "node scripts/generate-agent-events.mjs --check"
```

The frontend-only `check:protocol` intentionally checks TypeScript against the committed JSON schema without requiring a Python environment.

- [ ] **Step 5: Generate and replace the manual union**

```powershell
uv run python scripts/export_agent_event_schema.py
Set-Location webui
bun run generate:protocol
Set-Location ..
```

In `webui/src/lib/types.ts`, delete only the hand-written `InboundEvent` union and replace it with:

```typescript
export type { InboundEvent } from "@/generated/agent-events";
```

If `json-schema-to-typescript` chooses a different root export despite the schema title, change the generator to force the root name; do not add a second manual event union.

- [ ] **Step 6: Add CI drift gates**

In the Python `lint` job after dependency installation:

```yaml
- name: Check agent event JSON schema
  run: uv run python scripts/export_agent_event_schema.py --check
```

In the frontend job after `bun install`:

```yaml
- name: Check generated event types
  run: bun run check:protocol
```

- [ ] **Step 7: Verify generated artifacts and frontend consumers**

```powershell
uv run python scripts/export_agent_event_schema.py --check
Set-Location webui
bun run check:protocol
bun run lint
bun x tsc --noEmit
bun run test -- agent-events-contract
Set-Location ..
git diff --check
```

Expected: both drift checks exit zero, TypeScript compiles, and the contract tests pass.

- [ ] **Step 8: Commit protocol generation**

```powershell
git add scripts/export_agent_event_schema.py webui/scripts/generate-agent-events.mjs webui/src/generated/agent-events.schema.json webui/src/generated/agent-events.ts webui/src/tests/agent-events-contract.test.ts webui/src/lib/types.ts webui/package.json webui/bun.lock .github/workflows/ci.yml
git commit -m "build(protocol): generate frontend event types"
```

Do not stage the pre-existing `webui/package-lock.json`.

### Task 11: Emit one structured telemetry record per turn

**Files:**
- Create: `miniunicorn/agent/telemetry.py`
- Create: `tests/agent/test_telemetry.py`
- Modify: `miniunicorn/agent/turn_runtime.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/_state_machine.py`

**Interfaces:**
- `TelemetrySink.emit_turn(record: TurnTelemetry) -> Awaitable[None]`.
- Default `LogTelemetrySink` writes one structured Loguru event named `turn_completed`.
- Provider/tool call metrics are appended to the currently bound `TurnRuntime`.
- Sink exceptions are logged and suppressed.
- Do not add an OpenTelemetry/network dependency in this batch.

- [ ] **Step 1: Write telemetry model and failure-isolation tests**

Create tests for:

- success: one sink call with turn/session IDs, stop reason, usage, latency;
- provider exception: one sink call with error stop reason;
- concurrent sessions: distinct LLM/tool metrics;
- durations: every metric is nonnegative;
- sink exception: outbound turn still completes.

Use an in-memory sink:

```python
class CapturingTelemetrySink:
    def __init__(self):
        self.records: list[TurnTelemetry] = []

    async def emit_turn(self, record: TurnTelemetry) -> None:
        self.records.append(record)
```

- [ ] **Step 2: Observe missing telemetry**

```powershell
uv run pytest tests/agent/test_telemetry.py -v
```

Expected: import failure because `telemetry.py` does not exist.

- [ ] **Step 3: Implement telemetry types and sink**

Create `miniunicorn/agent/telemetry.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from loguru import logger


@dataclass(slots=True)
class LlmCallMetric:
    iteration: int
    duration_ms: float
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ToolCallMetric:
    name: str
    duration_ms: float
    status: str
    error: str | None = None


@dataclass(slots=True)
class TurnTelemetry:
    turn_id: str
    session_key: str
    queue_wait_ms: int
    duration_ms: int | None
    state_durations_ms: dict[str, float]
    llm_calls: list[LlmCallMetric]
    tool_calls: list[ToolCallMetric]
    usage: dict[str, int]
    last_call_usage: dict[str, int]
    stop_reason: str


class TelemetrySink(Protocol):
    async def emit_turn(self, record: TurnTelemetry) -> None: ...


class LogTelemetrySink:
    async def emit_turn(self, record: TurnTelemetry) -> None:
        logger.bind(**asdict(record)).info("turn_completed")
```

Change `TurnRuntime.llm_calls` and `.tool_calls` from raw dictionaries to these typed metric lists, using `TYPE_CHECKING` or direct imports without creating a cycle.

- [ ] **Step 4: Time provider calls**

Wrap the actual provider await in `_request_model()` with `time.monotonic()`. In `finally`, append one `LlmCallMetric` to the current runtime when bound. Populate usage/finish reason on success and a sanitized exception class/message on failure. Do not log prompts, API keys, tool arguments, or response content.

- [ ] **Step 5: Time tool calls**

Wrap each actual `tool.execute()` in `_run_tool()` with the same monotonic pattern. Record tool name, `ok`/`error`/`cancelled`, duration, and a bounded error summary. Do not record tool arguments or full tool output.

- [ ] **Step 6: Aggregate state durations and emit once**

At the end of state-machine execution, aggregate:

```python
runtime.state_durations_ms = {
    state.name.lower(): sum(
        entry.duration_ms for entry in ctx.trace if entry.state is state
    )
    for state in TurnState
}
```

Add a constructor-injected `telemetry_sink: TelemetrySink | None = None` to `AgentLoop`, defaulting to `LogTelemetrySink`. After final response/error handling but before leaving the coordinator scope, build and emit one `TurnTelemetry`.

Use:

```python
try:
    await self.telemetry_sink.emit_turn(build_turn_telemetry(turn_runtime))
except Exception:
    logger.exception("Telemetry sink failed for turn {}", turn_runtime.turn_id)
```

Cancellation must still produce a record before re-raising, using `stop_reason="cancelled"`.

- [ ] **Step 7: Verify telemetry isolation**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest tests/agent/test_telemetry.py tests/agent/test_turn_concurrency.py $runnerTests -q
uv run ruff check miniunicorn/agent/telemetry.py miniunicorn/agent/turn_runtime.py miniunicorn/agent/runner.py miniunicorn/agent/loop.py tests/agent/test_telemetry.py
git diff --check
```

Expected: all selected tests pass; concurrent records are correctly attributed.

- [ ] **Step 8: Commit telemetry**

```powershell
git add miniunicorn/agent/telemetry.py miniunicorn/agent/turn_runtime.py miniunicorn/agent/runner.py miniunicorn/agent/loop.py miniunicorn/agent/_state_machine.py tests/agent/test_telemetry.py
git commit -m "feat(agent): emit structured turn telemetry"
```

### Task 12: Supervise core background tasks and surface their exceptions

**Files:**
- Create: `miniunicorn/utils/task_supervisor.py`
- Create: `tests/utils/test_task_supervisor.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/_mcp_lifecycle.py`
- Create: `tests/agent/test_runner_reflection.py`
- Modify: `tests/agent/test_mcp_connection.py`

**Interfaces:**
- `TaskSupervisor.create(coro, *, name) -> asyncio.Task`.
- `TaskSupervisor.close(*, cancel: bool, timeout_s: float | None)`.
- Done callbacks consume and log exceptions once.
- AgentLoop owns one supervisor for archives/background jobs; AgentRunner owns one for reflections.

- [ ] **Step 1: Write lifecycle tests**

Test:

```python
@pytest.mark.asyncio
async def test_supervisor_logs_background_exception(caplog):
    supervisor = TaskSupervisor()

    async def fail():
        raise RuntimeError("background boom")

    supervisor.create(fail(), name="failing-test")
    await supervisor.close(cancel=False, timeout_s=1)
    assert "failing-test" in caplog.text
    assert "background boom" in caplog.text


@pytest.mark.asyncio
async def test_close_can_cancel_and_drain_tasks():
    supervisor = TaskSupervisor()
    cancelled = asyncio.Event()

    async def wait_forever():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor.create(wait_forever(), name="wait-forever")
    await asyncio.sleep(0)
    await supervisor.close(cancel=True, timeout_s=1)
    assert cancelled.is_set()
    assert supervisor.pending_count == 0
```

- [ ] **Step 2: Implement the supervisor**

The complete public behavior:

```python
class TaskSupervisor:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def create(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Background task '{}' failed", task.get_name())

    async def close(self, *, cancel: bool, timeout_s: float | None = None) -> None:
        tasks = tuple(self._tasks)
        if cancel:
            for task in tasks:
                task.cancel()
        if not tasks:
            return
        waiter = asyncio.gather(*tasks, return_exceptions=True)
        if timeout_s is None:
            await waiter
        else:
            try:
                await asyncio.wait_for(waiter, timeout=timeout_s)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
```

Import `Coroutine` from `collections.abc` and `Any` from `typing`.

- [ ] **Step 3: Migrate AgentLoop-owned tasks**

Replace `_background_tasks` with `_background_supervisor`. `_schedule_background(coro)` delegates with a meaningful name supplied by its caller or defaults to `"agent-background"`. In `close_mcp()`:

```python
await self._background_supervisor.close(cancel=False, timeout_s=30)
await self.runner.aclose()
```

- [ ] **Step 4: Migrate reflection tasks**

Replace `_reflection_tasks` with `_reflection_supervisor`, call:

```python
self._reflection_supervisor.create(
    reflection.reflect(
        trigger="periodic",
        iteration=iteration,
        context_summary=f"Periodic reflection at iteration {iteration}",
        messages=messages,
        session_key=spec.session_key,
    ),
    name=f"reflection:{spec.session_key or 'default'}:{iteration}",
)
```

Add:

```python
async def aclose(self) -> None:
    await self._reflection_supervisor.close(cancel=False, timeout_s=10)
```

Do not migrate channel-specific keepalive/server tasks in this task; they already have channel shutdown ownership and will be addressed only if Batch 3 extraction exposes a leak.

- [ ] **Step 5: Verify shutdown and commit**

```powershell
uv run pytest tests/utils/test_task_supervisor.py tests/agent/test_runner_reflection.py tests/agent/test_mcp_connection.py -q
uv run ruff check miniunicorn/utils/task_supervisor.py miniunicorn/agent/loop.py miniunicorn/agent/runner.py miniunicorn/agent/_mcp_lifecycle.py
git diff --check
git add miniunicorn/utils/task_supervisor.py miniunicorn/agent/loop.py miniunicorn/agent/runner.py miniunicorn/agent/_mcp_lifecycle.py tests/utils/test_task_supervisor.py tests/agent/test_runner_reflection.py tests/agent/test_mcp_connection.py
git commit -m "refactor(runtime): supervise agent background tasks"
```

### Task 13: Add a fast core CI gate and remove frontend test warnings

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `tests/test_core_marker_policy.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `webui/src/tests/setup.ts`
- Modify: `webui/src/tests/message-bubble.test.tsx`
- Modify: `webui/src/tests/agent-activity-cluster.test.tsx`

**Interfaces:**
- Add pytest markers: `core`, `integration`, `channel`, `platform`, `slow`.
- Core classification is deterministic from repository-relative paths.
- Full test matrix remains; it gains `needs: core`.
- Frontend tests must exit without the known missing-doctype and unwrapped-update warnings.

- [ ] **Step 1: Write marker-policy tests**

Create `tests/test_core_marker_policy.py` against a pure helper in `tests/conftest.py`:

```python
import pytest

from conftest import is_core_test_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/agent/test_runner_core.py", True),
        ("tests/agent/test_turn_concurrency.py", True),
        ("tests/session/test_goal_state.py", True),
        ("tests/providers/test_openai_responses.py", True),
        ("tests/config/test_api_security.py", True),
        ("tests/channels/test_feishu_streaming.py", False),
        ("tests/test_document_parsing.py", False),
    ],
)
def test_core_path_policy(path, expected):
    assert is_core_test_path(path) is expected
```

- [ ] **Step 2: Configure marker declarations**

Under `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
markers = [
    "core: fast correctness gate for orchestration, sessions, providers, config, and security",
    "integration: multi-component integration test",
    "channel: external channel adapter test",
    "platform: operating-system-specific test",
    "slow: test intentionally excluded from the fast gate",
]
```

Use `git add -p pyproject.toml` later because this file had pre-existing user changes.

- [ ] **Step 3: Implement deterministic core collection**

In `tests/conftest.py`:

```python
import pytest


CORE_PREFIXES = (
    "tests/agent/test_runner_",
    "tests/agent/test_loop_runner_integration.py",
    "tests/agent/test_loop_progress.py",
    "tests/agent/test_loop_save_turn.py",
    "tests/agent/test_turn_",
    "tests/session/",
    "tests/providers/",
    "tests/config/",
    "tests/security/",
)


def is_core_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized.startswith(prefix) for prefix in CORE_PREFIXES)


def pytest_collection_modifyitems(config, items):
    for item in items:
        repo_relative = item.path.relative_to(config.rootpath).as_posix()
        if is_core_test_path(repo_relative) and "slow" not in item.keywords:
            item.add_marker(pytest.mark.core)
```

- [ ] **Step 4: Add the CI core job**

Before the full `test` matrix:

```yaml
core:
  name: Core gate
  runs-on: ubuntu-latest
  timeout-minutes: 5
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with:
        python-version: "3.13"
    - uses: astral-sh/setup-uv@v4
    - name: Install dependencies
      run: uv sync --all-extras
    - name: Run core tests
      run: uv run pytest -m core -q
```

Add `needs: core` to the existing matrix `test` job. Do not remove any OS/Python combination.

- [ ] **Step 5: Add a real HTML doctype in test setup**

At module initialization in `webui/src/tests/setup.ts`:

```typescript
if (!document.doctype) {
  const doctype = document.implementation.createDocumentType("html", "", "");
  document.insertBefore(doctype, document.documentElement);
}
```

- [ ] **Step 6: Remove React act warnings at their source**

Run `bun run test 2>&1` and list every warning-producing test. For asynchronous lazy imports, resource completion, timers, or state transitions, wrap the trigger and awaited resolution:

```typescript
await act(async () => {
  resolveDeferredModule();
  await Promise.resolve();
});
```

Use fake timers only where the component already depends on timers, and restore real timers in `afterEach`. Do not suppress `console.error` or filter warning text.

- [ ] **Step 7: Verify the fast gate and clean frontend output**

```powershell
uv run pytest tests/test_core_marker_policy.py -q
uv run pytest -m core -q
Set-Location webui
$vitestOutput = bun run test 2>&1
$vitestOutput
if ($vitestOutput -match 'not wrapped in act|quirks mode') { throw 'frontend warnings remain' }
bun run lint
bun run build
Set-Location ..
```

Expected: marker tests pass, core suite completes within five minutes, all frontend tests/build pass, and the warning check does not throw.

- [ ] **Step 8: Commit the fast gate**

```powershell
git diff -- pyproject.toml
git add -p pyproject.toml
git add tests/conftest.py tests/test_core_marker_policy.py .github/workflows/ci.yml webui/src/tests/setup.ts
git add webui/src/tests
git commit -m "ci: add fast core gate and clean frontend tests"
```

Before committing, inspect `git diff --cached --name-only` and unstage any frontend test that was not changed specifically to eliminate a warning.

### Task 14: Close Batch 2 with protocol and telemetry documentation

**Files:**
- Create: `docs/agent-event-protocol.md`
- Create: `docs/telemetry.md`
- Modify: `docs/concurrency.md`

**Interfaces:**
- Documents event-version policy, compatibility flags, generation commands, telemetry field meanings, and data-redaction rules.

- [ ] **Step 1: Document the protocol policy**

Include:

- Python Pydantic models are authoritative.
- `protocol_version` starts at 1.
- Additive optional fields do not increment the version.
- Renames/removals or semantic changes require a new version and a migration period.
- Legacy `OutboundMessage.metadata` flags remain supported for one release and must not be removed in Batch 3.
- Regeneration commands are `uv run python scripts/export_agent_event_schema.py` and `bun run generate:protocol`.

- [ ] **Step 2: Document telemetry privacy**

State that telemetry includes IDs, timing, usage counts, stop reasons, provider finish reasons, tool names, and bounded error summaries. It excludes prompts, response text, tool arguments/results, media, credentials, and environment variables.

- [ ] **Step 3: Run the complete Batch 2 gate**

```powershell
uv run python scripts/export_agent_event_schema.py --check
uv run pytest -m core -q
uv run pytest tests/bus/test_agent_events.py tests/channels/test_websocket_channel.py tests/channels/test_websocket_integration.py tests/agent/test_telemetry.py tests/utils/test_task_supervisor.py -q
uv run ruff check miniunicorn tests scripts/export_agent_event_schema.py
Set-Location webui
bun run check:protocol
bun run lint
bun run test
bun run build
Set-Location ..
git diff --check
```

Expected: all commands pass; generated files are current; no known frontend warnings remain.

- [ ] **Step 4: Commit Batch 2 documentation**

```powershell
git add docs/agent-event-protocol.md docs/telemetry.md docs/concurrency.md
git commit -m "docs(protocol): describe events and turn telemetry"
```

**Batch 2 stop/go checkpoint:** Do not begin extraction refactors until the typed schema drift checks, `pytest -m core`, frontend lint/test/build, and telemetry isolation tests all pass from a clean index.

---

# Batch 3 — Behavior-Preserving Decomposition

## Batch 3 Acceptance Contract

- Characterization tests lock the runner’s observable outputs before code moves.
- `AgentRunner` remains the import-compatible façade.
- Provider switching after runner construction still affects the next request.
- The WebUI stream hook owns effects/subscriptions while pure reducers own message transitions.
- `AgentActivityCluster` becomes composition over independently tested parser and view modules.
- WebSocket, Feishu, and Weixin public channel classes and wire behavior remain compatible.
- No legacy event metadata flags are removed in this batch.

### Task 15: Freeze `AgentRunner` behavior and file-size boundaries

- [x] Implemented by remediation Task 9, commit f7294799543ae72e5c42ffa513dda57260897e4b, verified by `pytest tests/agent/test_runner_characterization.py tests/agent/test_runner_boundaries.py -q`.

**Files:**
- Create: `tests/agent/test_runner_characterization.py`
- Create: `tests/agent/test_runner_structure.py`
- Read: `tests/agent/test_runner_*.py`
- No production changes in this task.

**Interfaces:**
- Characterization assertions use `AgentRunResult`, messages, hooks, provider requests, tool calls, stop reasons, usage, and injection ordering.
- Structural test will enforce the end-state, but must be marked `xfail(strict=True)` until Task 18 completes.

- [ ] **Step 1: Build a scripted provider fixture**

Use the repository’s closest existing fake response/provider helpers. The fixture must accept an ordered list of `LLMResponse` objects, record request kwargs, and fail if the runner makes an unexpected extra call:

```python
class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.generation = SimpleNamespace(max_tokens=4096)
        self.supports_progress_deltas = False

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("AgentRunner made an unexpected provider call")
        return self.responses.pop(0)

    async def chat_stream_with_retry(
        self,
        *,
        on_content_delta,
        on_thinking_delta=None,
        on_tool_call_delta=None,
        **kwargs,
    ):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("AgentRunner made an unexpected provider call")
        response = self.responses.pop(0)
        if on_thinking_delta is not None and response.reasoning_content:
            await on_thinking_delta(response.reasoning_content)
        if response.content:
            await on_content_delta(response.content)
        return response
```

Do not add special scripted-provider handling in production.

- [ ] **Step 2: Add the characterization matrix**

Cover these cases with one focused test each:

| Case | Required assertions |
|---|---|
| direct final response | `completed`, one assistant append, cumulative/last usage |
| tool round trip | assistant tool-call message, tool message, final response, `tools_used` order |
| recoverable tool error | normalized tool error passed back to the model |
| fatal tool error | `tool_error`, final error append, hook order |
| two blank replies | finalization retry occurs at current threshold |
| length recovery | partial assistant plus continuation prompt, maximum three recoveries |
| max iterations | `max_iterations` and configured/default message |
| injection after tools | injected user message precedes next provider request |
| injection after final | stream end receives `resuming=True` before continuation |
| budget stop | `budget_exceeded`, usage retained, no extra tool call |
| planner replan | successful steps retained and failed step reason supplied to replan |
| reflection | failure reflection awaited; periodic reflection supervised |

For hook order, assert a concrete sequence such as:

```python
assert hook.events == [
    "before_iteration:0",
    "before_execute_tools:0",
    "after_iteration:0",
    "before_iteration:1",
    "after_iteration:1",
]
```

- [ ] **Step 3: Add a temporary structural xfail**

`tests/agent/test_runner_structure.py`:

```python
from pathlib import Path

import pytest


@pytest.mark.xfail(strict=True, reason="completed by Batch 3 Task 18")
def test_runner_facade_stays_small():
    path = Path("miniunicorn/agent/runner.py")
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 450
```

Task 18 must remove the `xfail` marker; leaving an XPASS under `strict=True` will fail.

- [ ] **Step 4: Run and stabilize characterization tests**

```powershell
uv run pytest tests/agent/test_runner_characterization.py tests/agent/test_runner_structure.py -q
```

Expected: characterization tests pass against current code; the structural test is one expected xfail.

- [ ] **Step 5: Commit only the behavioral net**

```powershell
git add tests/agent/test_runner_characterization.py tests/agent/test_runner_structure.py
git commit -m "test(runner): characterize orchestration behavior"
```

### Task 16: Extract runner contracts and provider requests

- [x] Implemented by remediation Task 10, commit 496e5b353d45e0f570d88d14dc00deee393fc0df, verified by `pytest tests/agent/test_runner_model.py -q`.

**Files:**
- Create: `miniunicorn/agent/runner_types.py`
- Create: `miniunicorn/agent/runner_model.py`
- Create: `tests/agent/test_runner_model.py`
- Modify: `miniunicorn/agent/runner.py`

**Interfaces:**
- `AgentRunSpec` and `AgentRunResult` move to `runner_types.py` and remain re-exported from `miniunicorn.agent.runner`.
- `ModelRequester` receives a provider getter, not a provider snapshot.
- Existing monkeypatch points `runner._request_model` and `runner._request_finalization_retry` remain as thin delegates.

- [ ] **Step 1: Add provider-switching and kwargs tests**

Test that:

```python
runner = AgentRunner(provider_a)
await runner.run(one_response_spec)
runner.provider = provider_b
await runner.run(second_response_spec)
assert provider_a.call_count == 1
assert provider_b.call_count == 1
```

Also assert temperature, max tokens, reasoning effort, retry mode, timeout, retry callback, tool definitions, and streaming callback are forwarded exactly as today.

- [ ] **Step 2: Move public dataclasses without changing fields**

Move `AgentRunSpec` and `AgentRunResult` byte-for-byte to `runner_types.py`. In `runner.py`:

```python
from miniunicorn.agent.runner_types import AgentRunResult, AgentRunSpec

__all__ = ["AgentRunner", "AgentRunResult", "AgentRunSpec"]
```

Do not rename fields or change defaults. Run import tests immediately:

```powershell
uv run python -c "from miniunicorn.agent.runner import AgentRunResult, AgentRunSpec; print(AgentRunResult.__name__, AgentRunSpec.__name__)"
```

Expected: `AgentRunResult AgentRunSpec`.

- [ ] **Step 3: Implement `ModelRequester`**

Use this ownership pattern:

```python
class ModelRequester:
    def __init__(self, provider_getter: Callable[[], LLMProvider]) -> None:
        self._provider_getter = provider_getter

    @property
    def provider(self) -> LLMProvider:
        return self._provider_getter()

    def build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs
```

Move the exact current request/stream/retry/timeout behavior from `_request_model()` and `_request_finalization_retry()` into methods `request()` and `request_finalization()`. Preserve Task 11 telemetry timing inside `ModelRequester.request()`.

- [ ] **Step 4: Add façade delegates**

Construct with:

```python
self._model_requester = ModelRequester(lambda: self.provider)
```

Keep:

```python
async def _request_model(self, spec, messages, hook, context):
    return await self._model_requester.request(spec, messages, hook, context)


async def _request_finalization_retry(self, spec, messages):
    return await self._model_requester.request_finalization(spec, messages)
```

This preserves tests/extensions that monkeypatch the façade methods. Remove the old method bodies only after all focused tests pass.

- [ ] **Step 5: Verify and commit**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest tests/agent/test_runner_model.py tests/agent/test_runner_characterization.py $runnerTests -q
uv run ruff check miniunicorn/agent/runner.py miniunicorn/agent/runner_types.py miniunicorn/agent/runner_model.py tests/agent/test_runner_model.py
git diff --check
git add miniunicorn/agent/runner.py miniunicorn/agent/runner_types.py miniunicorn/agent/runner_model.py tests/agent/test_runner_model.py
git commit -m "refactor(runner): extract provider request boundary"
```

### Task 17: Extract tool execution and result normalization

- [x] Implemented by remediation Task 11, commit b7a6369e03767a8d1fea24cc0b1c87f2162bdb6a, verified by `pytest tests/agent/test_runner_tools.py -q`.

**Files:**
- Create: `miniunicorn/agent/runner_tools.py`
- Create: `tests/agent/test_runner_tools.py`
- Modify: `miniunicorn/agent/runner.py`

**Interfaces:**
- `ToolBatchResult` replaces the internal three-tuple while façade `_execute_tools()` preserves the existing tuple return for monkeypatch compatibility.
- `ToolExecutor` owns tool execution, concurrency partitioning, safety classification, result normalization, output budgets, and Task 11 metrics.
- File edit tracking/progress behavior remains byte-for-byte compatible.

- [ ] **Step 1: Add focused tool executor tests**

Cover:

- sequential order when `concurrent_tools=False`;
- parallel overlap when `concurrent_tools=True`;
- writes remain partitioned according to current safety ordering;
- SSRF/workspace violations reach existing retry thresholds;
- `fail_on_tool_error` returns a fatal exception;
- large output is truncated with the same marker;
- file edit start/end/error events retain exact dictionaries;
- telemetry records `ok`, `error`, and `cancelled`.

- [ ] **Step 2: Add the typed batch result**

In `runner_types.py`:

```python
@dataclass(slots=True)
class ToolBatchResult:
    results: list[Any]
    events: list[dict[str, str]]
    fatal_error: Exception | None = None
```

- [ ] **Step 3: Move tool-owned methods**

Move these methods from `AgentRunner` to `ToolExecutor`, preserving their bodies and constants:

- `_execute_tools` → `execute`;
- `_run_tool`;
- `_is_ssrf_violation`;
- `_is_workspace_violation`;
- `_classify_violation`;
- `_ssrf_soft_payload`;
- `_event_detail`;
- `_normalize_tool_result`;
- `_apply_tool_result_budget`;
- `_partition_tool_batches`.

Pass dependencies in the constructor instead of retaining the whole runner:

```python
class ToolExecutor:
    def __init__(self, *, checkpoint_emitter: CheckpointEmitter) -> None:
        self._checkpoint_emitter = checkpoint_emitter
```

Where a moved method needs `spec`, accept it explicitly. Do not give `ToolExecutor` direct access to `AgentRunner.__dict__`.

- [ ] **Step 4: Preserve façade monkeypatch points**

```python
async def _execute_tools(
    self,
    spec,
    tool_calls,
    external_lookup_counts,
    workspace_violation_counts,
):
    batch = await self._tool_executor.execute(
        spec,
        tool_calls,
        external_lookup_counts,
        workspace_violation_counts,
    )
    return batch.results, batch.events, batch.fatal_error
```

Keep delegation wrappers for `_normalize_tool_result()` and any static helper referenced directly by existing tests; wrappers must contain one return statement only.

- [ ] **Step 5: Verify and commit**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest tests/agent/test_runner_tools.py tests/agent/test_runner_characterization.py $runnerTests tests/utils/test_progress_events.py -q
uv run ruff check miniunicorn/agent/runner.py miniunicorn/agent/runner_tools.py miniunicorn/agent/runner_types.py tests/agent/test_runner_tools.py
git diff --check
git add miniunicorn/agent/runner.py miniunicorn/agent/runner_tools.py miniunicorn/agent/runner_types.py tests/agent/test_runner_tools.py
git commit -m "refactor(runner): extract tool execution boundary"
```

### Task 18: Split the ReAct loop into explicit control phases

- [x] Implemented by remediation Task 12, commit a0318667eaab3329f84ae8a910c2e483d5052c09, verified by `pytest tests/agent/test_runner_control.py tests/agent/test_runner_boundaries.py -q`.

**Files:**
- Create: `miniunicorn/agent/runner_control.py`
- Create: `tests/agent/test_runner_control.py`
- Modify: `miniunicorn/agent/runner_types.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `tests/agent/test_runner_structure.py`

**Interfaces:**
- `RunController.run(spec) -> AgentRunResult`.
- Explicit phases: setup, prepare iteration, consume model response, execute tools, consume final response, finish exhausted.
- `AgentRunner.run()` is a one-line delegate.
- `AgentRunner.provider` remains mutable; collaborators observe the current provider.

- [ ] **Step 1: Define control state and decisions**

Add to `runner_types.py`:

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

Add no provider, registry, callback, or session object to this state; those remain dependencies/spec.

- [ ] **Step 2: Create controller phase tests**

In `tests/agent/test_runner_control.py`, use fake owner methods to assert:

- `_prepare_iteration()` governs context before plan guidance;
- `_handle_tool_response()` checkpoints before and after tools;
- `_handle_final_response()` drains injection before stream end;
- `_finish_exhausted()` drains remaining injections but cannot continue;
- every phase calls `hook.after_iteration()` exactly once on its exit path.

- [ ] **Step 3: Move and split `run()`**

`RunController` receives an `AgentRunner` façade reference and the reflection supervisor. Its public method:

```python
async def run(self, spec: AgentRunSpec) -> AgentRunResult:
    state = await self._initialize(spec)
    hook = spec.hook or AgentHook()
    for iteration in range(spec.max_iterations):
        action = await self._run_iteration(spec, state, hook, iteration)
        if action is IterationAction.BREAK:
            break
    else:
        await self._finish_exhausted(spec, state)
    return self._build_result(state)
```

Split `_run_iteration()` further so no method exceeds 200 physical lines:

- `_prepare_iteration(...) -> tuple[list[dict[str, Any]], AgentHookContext]`;
- `_record_model_response(...)`;
- `_handle_budget_result(...) -> IterationAction | None`;
- `_handle_tool_response(...) -> IterationAction`;
- `_handle_blank_or_length_response(...) -> IterationAction | None`;
- `_handle_final_response(...) -> IterationAction`;
- `_finish_exhausted(...)`;
- `_build_result(...) -> AgentRunResult`.

Move injection/planner/reflection helpers to the controller when they govern phase transitions. Keep pure message/usage helpers as module-level functions. Copy logic before deleting it; compare each moved block with `git diff --word-diff=porcelain`.

- [ ] **Step 4: Reduce `AgentRunner` to a façade**

The façade owns:

- current provider;
- default context governor;
- `ModelRequester`;
- `ToolExecutor`;
- `RunController`;
- reflection `TaskSupervisor`;
- compatibility delegates/re-exports.

Its public method:

```python
async def run(self, spec: AgentRunSpec) -> AgentRunResult:
    return await self._controller.run(spec)
```

- [ ] **Step 5: Enforce structural limits**

Remove `xfail` from `test_runner_structure.py` and add AST checks:

```python
def test_runner_control_methods_are_bounded():
    tree = ast.parse(Path("miniunicorn/agent/runner_control.py").read_text(encoding="utf-8"))
    oversized = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
        and node.end_lineno - node.lineno + 1 > 200
    }
    assert oversized == {}
```

Keep the façade limit at 450 lines. If compatibility delegates make it slightly larger, extract pure helpers rather than relaxing the limit.

- [ ] **Step 6: Run all runner and core regressions**

```powershell
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
uv run pytest tests/agent/test_runner_structure.py tests/agent/test_runner_control.py tests/agent/test_runner_characterization.py $runnerTests tests/agent/test_loop_runner_integration.py tests/agent/test_telemetry.py -q
uv run pytest -m core -q
uv run ruff check miniunicorn/agent/runner.py miniunicorn/agent/runner_types.py miniunicorn/agent/runner_model.py miniunicorn/agent/runner_tools.py miniunicorn/agent/runner_control.py
git diff --check
```

Expected: no xfail/xpass, all runner/core tests pass, and structure bounds hold.

- [ ] **Step 7: Commit the control split**

```powershell
git add miniunicorn/agent/runner.py miniunicorn/agent/runner_types.py miniunicorn/agent/runner_control.py tests/agent/test_runner_control.py tests/agent/test_runner_structure.py
git commit -m "refactor(runner): split ReAct control phases"
```

### Task 19: Extract pure WebUI stream state transitions

- [x] Implemented by remediation Task 13, commit e5bb6f19cd823116e2c0a20df626ceaf4a366dc3, verified by `npm test` in `webui/`.

**Files:**
- Create: `webui/src/hooks/stream-state.ts`
- Create: `webui/src/hooks/stream-reducer.ts`
- Create: `webui/src/tests/stream-reducer.test.ts`
- Modify: `webui/src/hooks/useMiniunicornStream.ts`

**Interfaces:**
- Pure modules do not import React, WebSocket, timers, fetch, or browser storage.
- Hook retains connection lifecycle, refs, callbacks, timers, HTTP refresh effects, and React state setters.
- Reducer consumes generated `InboundEvent`.

- [ ] **Step 1: Characterize current message transitions**

Create table-driven reducer tests for:

- reasoning delta opens/extends the correct placeholder;
- reasoning end closes only its matching stream;
- delta and stream end assemble one assistant message;
- complete message absorbs a streaming placeholder;
- tool progress merges by call ID;
- file edits deduplicate by call/tool/path and supersede older phase;
- turn end stamps latency and prunes reasoning-only placeholders;
- events for another chat ID do not mutate current state.

Use fixed ID and time factories so snapshots are deterministic.

- [ ] **Step 2: Move pure types and helpers**

`stream-state.ts` owns:

```typescript
export interface StreamBuffer {
  text: string;
  messageIndex: number | null;
}

export interface StreamState {
  messages: UIMessage[];
  activeStreamId: string | null;
  buffers: Record<string, StreamBuffer>;
}

export interface ReduceContext {
  chatId: string;
  now: () => number;
  createId: () => string;
}
```

Move the existing pure helpers from lines 45–402 of `useMiniunicornStream.ts` into this file, exporting only helpers used in tests/reducer. Preserve their bodies before formatting.

- [ ] **Step 3: Implement the pure reducer**

Use:

```typescript
export interface StreamReduction {
  state: StreamState;
  effects: StreamEffect[];
}

export type StreamEffect =
  | { type: "refresh_session"; scope?: string }
  | { type: "sync_goal"; goalState: GoalStateWsPayload }
  | { type: "runtime_model_updated"; modelName: string; modelPreset?: string | null };

export function reduceStreamEvent(
  state: StreamState,
  event: InboundEvent,
  context: ReduceContext,
): StreamReduction
```

Return the identical state object when the event is irrelevant, and immutable changed objects when handled. Effects describe work; they never perform it.

- [ ] **Step 4: Make the hook execute reducer effects**

The WebSocket callback should:

1. parse/validate enough to treat input as `InboundEvent`;
2. call `reduceStreamEvent`;
3. commit returned messages/state;
4. dispatch returned effects through existing callbacks/fetch functions.

Keep `sendMessage`, reconnect/backoff, attach/new-chat, media upload, and workspace-scope code in the hook.

- [ ] **Step 5: Verify and commit**

```powershell
Set-Location webui
bun run test -- stream-reducer
bun run test
bun run lint
bun x tsc --noEmit
Set-Location ..
git diff --check
git add webui/src/hooks/stream-state.ts webui/src/hooks/stream-reducer.ts webui/src/hooks/useMiniunicornStream.ts webui/src/tests/stream-reducer.test.ts
git commit -m "refactor(webui): extract stream event reducer"
```

Expected: all frontend tests pass and `useMiniunicornStream.ts` is below 650 lines. If not, extract another pure helper; do not move connection effects into the reducer.

### Task 20: Split activity parsing from activity rendering

- [x] Implemented by remediation Task 14, commit 6b71eb579121f6fd4ff9d41d7f62041a75bfeb87, verified by `npm test` in `webui/`.

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
- Parser modules are pure and contain no JSX or translation hook.
- View modules receive already-normalized summaries.
- Existing exports `isReasoningOnlyAssistant`, `isAgentActivityMember`, and `AgentActivityCluster` remain at the original path.

- [ ] **Step 1: Add parser characterization**

Copy representative existing UI fixtures into pure tests and assert:

- shell command redaction and path compaction;
- private-host URL suppression;
- CLI status precedence `error > done > running`;
- MCP tool-name parsing and argument preview;
- file-edit deduplication and add/delete counts;
- malformed tool arguments never throw.

- [ ] **Step 2: Move shared types**

Move `ActivityCounts`, `FileEditSummary`, `CliRunSummary`, `McpRunSummary`, and their status unions into `activity/types.ts`. Export them with the same field names.

- [ ] **Step 3: Move pure parsers mechanically**

Use these boundaries:

- `trace-format.ts`: `traceLines`, `describeTraceLine`, shell/URL parsing, redaction, duration formatting;
- `cli-runs.ts`: CLI trace/event parsing, merge, collection, labels/argument formatting;
- `mcp-runs.ts`: MCP trace/event parsing, merge, collection, labels/preview;
- `file-edits.ts`: collection, latest-event selection, summary, error formatting.

Move code before changing names. Only after tests pass may imports be sorted.

- [ ] **Step 4: Move view components**

Move the JSX blocks into the listed component files. Translation strings remain unchanged. Pass `t`, `active`, and normalized arrays as explicit props rather than importing parent state.

- [ ] **Step 5: Reduce the original component to composition**

`AgentActivityCluster.tsx` keeps:

- public membership helpers;
- activity grouping/scroll behavior;
- top-level expansion state;
- composition of extracted groups.

It must not contain tool argument parsing, URL security parsing, AES/path logic, or rolling-digit implementation.

- [ ] **Step 6: Verify size and rendering**

```powershell
Set-Location webui
bun run test -- activity-parsers agent-activity-cluster
bun run test
bun run lint
bun run build
Set-Location ..
(Get-Content webui/src/components/thread/AgentActivityCluster.tsx).Count
```

Expected: all commands pass and the final line count is at most 500.

- [ ] **Step 7: Commit the activity split**

```powershell
git add webui/src/components/thread/AgentActivityCluster.tsx webui/src/components/thread/activity webui/src/tests/activity-parsers.test.ts webui/src/tests/agent-activity-cluster.test.tsx
git commit -m "refactor(webui): split agent activity modules"
```

### Task 21: Extract WebSocket outbound emission from channel lifecycle

- [x] Implemented by remediation Task 15, commit d3fd81e2a5e545083f1da42a86499a15ce80d94b, verified by `pytest tests/channels -q`.

**Files:**
- Create: `miniunicorn/channels/websocket/outbound.py`
- Create: `tests/channels/test_websocket_outbound.py`
- Modify: `miniunicorn/channels/websocket/channel.py`
- Modify: `tests/channels/test_websocket_channel.py`

**Interfaces:**
- `WebSocketOutboundEmitter` owns all typed agent event emission, stream buffers, media rewrite/signing callbacks, and transcript callback.
- `WebSocketChannel` retains HTTP routing, authentication, rate limits, subscriptions, connection lifecycle, inbound envelope dispatch, and public send method compatibility.

- [ ] **Step 1: Move current outbound tests to a direct emitter contract**

Cover subscriber snapshots, disconnected cleanup, message/media, progress kinds, typed envelope preference, reasoning, delta buffer rewrite, turn end, goal/session/model broadcasts, and transcript persistence.

- [ ] **Step 2: Define explicit dependencies**

```python
@dataclass(slots=True)
class WebSocketOutboundDeps:
    subscribers_for: Callable[[str], list[Any]]
    all_connections: Callable[[], list[Any]]
    safe_send: Callable[..., Awaitable[None]]
    append_transcript: Callable[[str, dict[str, Any]], None]
    rewrite_markdown_images: Callable[[str], str]
    sign_media_path: Callable[[Path], dict[str, str] | None]
    logger: Any
```

The emitter stores only these dependencies plus stream buffers/timestamps.

- [ ] **Step 3: Move methods**

Move `_send_agent_event`, `send`, `send_reasoning_delta`, `send_reasoning_end`, `send_delta`, `send_turn_end`, `send_goal_state`, `send_goal_status`, `send_session_updated`, `send_runtime_model_updated`, and stale stream-buffer cleanup into `outbound.py`.

- [ ] **Step 4: Keep public delegates**

Each existing method on `WebSocketChannel` delegates with identical arguments:

```python
async def send_turn_end(self, chat_id, latency_ms=None, *, goal_state=None, context_usage=None):
    await self._outbound.send_turn_end(
        chat_id,
        latency_ms,
        goal_state=goal_state,
        context_usage=context_usage,
    )
```

Do not change call sites in other modules in this task.

- [ ] **Step 5: Verify and commit**

```powershell
uv run pytest tests/channels/test_websocket_outbound.py tests/channels/test_websocket_channel.py tests/channels/test_websocket_integration.py tests/channels/test_websocket_envelope_media.py -q
uv run ruff check miniunicorn/channels/websocket/outbound.py miniunicorn/channels/websocket/channel.py tests/channels/test_websocket_outbound.py
(Get-Content miniunicorn/channels/websocket/channel.py).Count
git diff --check
git add miniunicorn/channels/websocket/outbound.py miniunicorn/channels/websocket/channel.py tests/channels/test_websocket_outbound.py tests/channels/test_websocket_channel.py
git commit -m "refactor(websocket): extract outbound emitter"
```

Expected: all tests pass and `channel.py` is below 1450 lines.

### Task 22: Extract Feishu rendering and media services

- [x] Implemented by remediation Tasks 16 and 17, commits d3756912d36373c847fdb228e75ffb6001d05af2 and 12dfebd621d377c46ce178e12256ed28cab5a910, verified by `pytest tests/channels -q`.

**Files:**
- Create: `miniunicorn/channels/feishu/rendering.py`
- Create: `miniunicorn/channels/feishu/media.py`
- Create: `tests/channels/test_feishu_rendering_service.py`
- Create: `tests/channels/test_feishu_media_service.py`
- Modify: `miniunicorn/channels/feishu/channel.py`
- Modify: existing `tests/channels/test_feishu_*.py`

**Interfaces:**
- Rendering functions are pure.
- `FeishuMediaService` receives SDK/client and media-directory dependencies.
- Existing `FeishuChannel` helper methods remain as delegates for compatibility.

- [ ] **Step 1: Lock existing rendering/media behavior**

Run and retain all current Feishu tests before moving:

```powershell
uv run pytest tests/channels/test_feishu_markdown_rendering.py tests/channels/test_feishu_post_content.py tests/channels/test_feishu_table_split.py tests/channels/test_feishu_media_filename_security.py -q
```

Expected: pass.

- [ ] **Step 2: Move pure rendering methods**

Move:

- `_strip_md_formatting`;
- `_parse_md_table`;
- `_build_card_elements`;
- `_split_elements_by_table_limit`;
- `_split_headings`;
- `_detect_msg_format`;
- `_markdown_to_post`;
- `_interactive_content_to_text`;
- `_fallback_text_chunks`;
- `_format_tool_hint_lines`;
- `_format_tool_hint_delta`.

Convert class/static methods to module functions with the same parameter and return types. Keep constants colocated with the functions that use them.

- [ ] **Step 3: Build a media service**

Move:

- `_upload_image_sync`;
- `_upload_file_sync`;
- `_download_image_sync`;
- `_download_file_sync`;
- `_safe_media_filename`;
- `_download_and_save_media`.

Use constructor-injected callables:

```python
class FeishuMediaService:
    def __init__(
        self,
        *,
        client_getter: Callable[[], Any],
        media_dir_getter: Callable[[], Path],
    ) -> None:
        self._client_getter = client_getter
        self._media_dir_getter = media_dir_getter
```

Preserve lazy SDK imports and safe filename behavior. Do not import `lark_oapi` at module import time.

- [ ] **Step 4: Keep compatibility delegates on the channel**

Delegate each old helper name to the module function/service so current tests and extensions do not break. Rendering delegates should be at most three lines.

- [ ] **Step 5: Run the full Feishu suite**

```powershell
$feishuTests = (Get-ChildItem tests/channels -Filter 'test_feishu_*.py').FullName
uv run pytest $feishuTests -q
uv run ruff check miniunicorn/channels/feishu tests/channels/test_feishu_rendering_service.py tests/channels/test_feishu_media_service.py
(Get-Content miniunicorn/channels/feishu/channel.py).Count
git diff --check
```

Expected: all tests pass and `channel.py` is below 1500 lines.

- [ ] **Step 6: Commit the Feishu split**

```powershell
git add miniunicorn/channels/feishu/channel.py miniunicorn/channels/feishu/rendering.py miniunicorn/channels/feishu/media.py tests/channels/test_feishu_rendering_service.py tests/channels/test_feishu_media_service.py
git add tests/channels/test_feishu_*.py
git commit -m "refactor(feishu): extract rendering and media services"
```

Inspect the cached diff so unchanged Feishu test files are not accidentally staged.

### Task 23: Extract Weixin crypto, API client, and media service

- [x] Implemented by remediation Task 18, commit 236635b1b35f1186a961fe93f4981a826c93af15, verified by `pytest tests/channels/test_weixin_crypto.py tests/channels/test_weixin_api_client.py tests/channels/test_weixin_media.py tests/channels/test_weixin_channel.py -q`.

**Files:**
- Create: `miniunicorn/channels/weixin/crypto.py`
- Create: `miniunicorn/channels/weixin/api_client.py`
- Create: `miniunicorn/channels/weixin/media.py`
- Create: `tests/channels/test_weixin_crypto.py`
- Create: `tests/channels/test_weixin_media.py`
- Modify: `miniunicorn/channels/weixin/channel.py`
- Modify: `tests/channels/test_weixin_channel.py`

**Interfaces:**
- Crypto functions are pure byte transforms.
- `WeixinApiClient` owns authenticated GET/POST/base URL/retry mechanics.
- `WeixinMediaService` owns download/decrypt and upload/encrypt; it calls the API client but not the message bus.
- `WeixinChannel` retains QR/login state, polling, inbound normalization, typing, and high-level send flow.

- [ ] **Step 1: Add crypto vector tests**

Move existing vectors or add fixed data/key vectors for:

- `_parse_aes_key`;
- AES-128-ECB encrypt/decrypt round trip;
- PKCS#7 unpadding;
- invalid key length;
- invalid padding fallback behavior.

Tests must use fixed bytes, not random values.

- [ ] **Step 2: Move pure crypto helpers**

Move `_parse_aes_key`, `_encrypt_aes_ecb`, `_decrypt_aes_ecb`, and `_pkcs7_unpad_safe` into `crypto.py`. Re-export them from `channel.py` during this release:

```python
from miniunicorn.channels.weixin.crypto import (
    decrypt_aes_ecb as _decrypt_aes_ecb,
    encrypt_aes_ecb as _encrypt_aes_ecb,
    parse_aes_key as _parse_aes_key,
    pkcs7_unpad_safe as _pkcs7_unpad_safe,
)
```

- [ ] **Step 3: Extract the API client**

Move `_make_headers`, `_api_get`, `_api_get_with_base`, and `_api_post` into:

```python
class WeixinApiClient:
    def __init__(
        self,
        *,
        config: WeixinConfig,
        http_client_getter: Callable[[], httpx.AsyncClient],
        token_getter: Callable[[], str],
    ) -> None:
        self._config = config
        self._http_client_getter = http_client_getter
        self._token_getter = token_getter
```

Preserve timeouts, headers, error bodies, route tag, retry classification, and session-expired behavior.

- [ ] **Step 4: Extract media download/upload**

Move `_has_downloadable_media_locator`, `_is_retryable_media_download_error`, `_download_media_item`, `_send_media_file`, `_ext_for_type`, and media-type/extension constants to `media.py`.

Define:

```python
class WeixinMediaService:
    def __init__(
        self,
        *,
        api: WeixinApiClient,
        http_client_getter: Callable[[], httpx.AsyncClient],
        cdn_base_url: str,
        media_dir_getter: Callable[[], Path],
    ) -> None:
        self._api = api
        self._http_client_getter = http_client_getter
        self._cdn_base_url = cdn_base_url
        self._media_dir_getter = media_dir_getter
```

Return the same local path/`None` values for downloads and perform the same final send-message API call for uploads.

- [ ] **Step 5: Keep channel delegates and test fallback order**

The old `_download_media_item()` and `_send_media_file()` remain delegates. Test:

- `full_url` first, `encrypt_query_param` fallback only on retryable failure;
- non-image media without AES key is rejected;
- image without a key retains current plain-byte behavior;
- upload prefers `upload_full_url`, otherwise builds CDN URL from `upload_param`;
- missing `x-encrypted-param` raises the current error;
- extension/media type mapping is unchanged.

- [ ] **Step 6: Run Weixin and channel regressions**

```powershell
uv run pytest tests/channels/test_weixin_crypto.py tests/channels/test_weixin_media.py tests/channels/test_weixin_channel.py -q
uv run ruff check miniunicorn/channels/weixin tests/channels/test_weixin_crypto.py tests/channels/test_weixin_media.py
(Get-Content miniunicorn/channels/weixin/channel.py).Count
git diff --check
```

Expected: all tests pass and `channel.py` is below 1150 lines.

- [ ] **Step 7: Commit the Weixin split**

```powershell
git add miniunicorn/channels/weixin/channel.py miniunicorn/channels/weixin/crypto.py miniunicorn/channels/weixin/api_client.py miniunicorn/channels/weixin/media.py tests/channels/test_weixin_channel.py tests/channels/test_weixin_crypto.py tests/channels/test_weixin_media.py
git commit -m "refactor(weixin): extract API crypto and media services"
```

### Task 24: Close Batch 3 with whole-system regression and architecture map

- [x] Superseded by remediation Task 18 Step 5 because the whole-system regression was folded into the Stage B checkpoint at the end of the channel extraction, commit 236635b1b35f1186a961fe93f4981a826c93af15, verified by `pytest tests/agent tests/channels tests/architecture -q`.

**Files:**
- Create: `docs/architecture.md`
- Modify: `docs/agent-event-protocol.md` only if module paths need updating

**Interfaces:**
- Documents façade/control/dependency boundaries and channel service ownership.

- [ ] **Step 1: Update architecture ownership**

Document:

- `AgentRunner` façade → `RunController`, `ModelRequester`, `ToolExecutor`;
- hook/reducer effect boundary in WebUI;
- WebSocket channel lifecycle vs outbound emitter;
- Feishu rendering/media separation;
- Weixin channel/API/crypto/media separation.

- [ ] **Step 2: Verify no compatibility import broke**

```powershell
uv run python -c "from miniunicorn.agent.runner import AgentRunner, AgentRunSpec, AgentRunResult; from miniunicorn.channels.websocket.channel import WebSocketChannel; from miniunicorn.channels.feishu.channel import FeishuChannel; from miniunicorn.channels.weixin.channel import WeixinChannel; print('imports ok')"
```

Expected: `imports ok`.

- [ ] **Step 3: Run Batch 3 backend regression**

```powershell
uv run pytest -m core -q
$runnerTests = (Get-ChildItem tests/agent -Filter 'test_runner_*.py').FullName
$websocketTests = (Get-ChildItem tests/channels -Filter 'test_websocket_*.py').FullName
$feishuTests = (Get-ChildItem tests/channels -Filter 'test_feishu_*.py').FullName
$weixinTests = (Get-ChildItem tests/channels -Filter 'test_weixin_*.py').FullName
uv run pytest $runnerTests $websocketTests $feishuTests $weixinTests -q
uv run ruff check miniunicorn tests
uv run ruff format --check miniunicorn/agent miniunicorn/channels
```

Expected: all selected suites and checks pass.

- [ ] **Step 4: Run Batch 3 frontend regression**

```powershell
Set-Location webui
bun run check:protocol
bun run lint
bun run test
bun run build
Set-Location ..
git diff --check
```

Expected: all commands pass with no known `act`/quirks warnings.

- [ ] **Step 5: Record final size bounds**

```powershell
@(
  'miniunicorn/agent/runner.py',
  'miniunicorn/agent/runner_control.py',
  'webui/src/hooks/useMiniunicornStream.ts',
  'webui/src/components/thread/AgentActivityCluster.tsx',
  'miniunicorn/channels/websocket/channel.py',
  'miniunicorn/channels/feishu/channel.py',
  'miniunicorn/channels/weixin/channel.py'
) | ForEach-Object { "$_ $((Get-Content $_).Count)" }
```

Expected maximums: 450, no control method over 200, 650, 500, 1450, 1500, and 1150 respectively.

- [ ] **Step 6: Commit Batch 3 documentation**

```powershell
git add docs/architecture.md docs/agent-event-protocol.md
git commit -m "docs(architecture): map decomposed runtime boundaries"
```

Stage the protocol document only if changed.

**Batch 3 stop/go checkpoint:** Do not change dependency declarations until all characterization, core, channel, frontend, import-compatibility, and size-bound checks pass.

---

# Batch 4 — Optional Document Dependencies and Packaging Reliability

## Batch 4 Acceptance Contract

- Base wheel metadata no longer requires `pypdf`, `python-docx`, `openpyxl`, or `python-pptx`.
- `miniunicorn-ai[documents]` installs all four extraction backends.
- Existing `miniunicorn-ai[pdf]` users retain PDF support.
- `miniunicorn-ai[dev]`, `uv sync --all-extras`, and Docker retain the dependencies needed by their test/runtime profiles.
- Missing optional parsers produce actionable user-visible errors rather than silent attachment loss.
- Source imports survive unreadable or malformed `pyproject.toml`.
- Wheel, sdist, minimal install, frontend assets, imports, and full tests are verified before completion.

### Task 25: Define the document extra and actionable missing-dependency behavior

- [x] Implemented by remediation Task 19, commit 74ba6d073cf3de9626b126ad89f53bfaa17e90df, verified by `pytest tests/test_document_parsing.py tests/test_api_attachment.py tests/test_context_documents.py -q`.

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `miniunicorn/utils/document.py`
- Modify: `tests/test_document_parsing.py`
- Modify: `tests/agent/test_document_extraction_toggle.py`
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `docs/quick-start.md`

**Interfaces:**
- New extra: `documents`.
- Compatibility extra: `pdf` continues to include `pypdf` in addition to its current `pymupdf`.
- Error hint: `Install document support with pip install "miniunicorn-ai[documents]"`.

- [ ] **Step 1: Write missing-backend tests before changing metadata**

In `tests/test_document_parsing.py`, patch import resolution for each backend and assert the exact package plus extra hint:

```python
@pytest.mark.parametrize(
    ("suffix", "blocked_import", "package_name"),
    [
        (".pdf", "pypdf", "pypdf"),
        (".docx", "docx", "python-docx"),
        (".xlsx", "openpyxl", "openpyxl"),
        (".pptx", "pptx", "python-pptx"),
    ],
)
def test_missing_document_backend_has_install_hint(
    tmp_path,
    monkeypatch,
    suffix,
    blocked_import,
    package_name,
):
    path = tmp_path / f"sample{suffix}"
    path.write_bytes(b"not-a-real-document")
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == blocked_import or name.startswith(f"{blocked_import}."):
            raise ImportError(f"blocked {blocked_import}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = extract_text(path)

    assert result is not None
    assert package_name in result
    assert 'miniunicorn-ai[documents]' in result
```

Import `builtins` in the test module.

- [ ] **Step 2: Prove high-level extraction surfaces the error**

Add:

```python
def test_extract_documents_keeps_actionable_parser_error(tmp_path, monkeypatch):
    path = tmp_path / "report.docx"
    path.write_bytes(b"not-a-real-document")
    monkeypatch.setattr(
        document_module,
        "extract_text",
        lambda _path: (
            '[error: python-docx not installed. Install document support with '
            'pip install "miniunicorn-ai[documents]"]'
        ),
    )

    content, images = extract_documents("summarize", [str(path)])

    assert images == []
    assert "[File: report.docx]" in content
    assert 'miniunicorn-ai[documents]' in content
```

- [ ] **Step 3: Observe the current silent/error-text failures**

```powershell
uv run pytest tests/test_document_parsing.py tests/agent/test_document_extraction_toggle.py -k "missing_document_backend or actionable_parser_error" -v
```

Expected: assertions fail because low-level errors have no install hint and `extract_documents()` drops error strings.

- [ ] **Step 4: Centralize the install hint**

In `miniunicorn/utils/document.py`:

```python
DOCUMENTS_EXTRA_HINT = (
    'Install document support with pip install "miniunicorn-ai[documents]"'
)


def _missing_document_dependency(package_name: str) -> str:
    return f"[error: {package_name} not installed. {DOCUMENTS_EXTRA_HINT}]"
```

Use `_missing_document_dependency()` in all four lazy-import branches.

- [ ] **Step 5: Preserve extraction errors in the turn context**

Change the high-level append condition:

```python
extracted = extract_text(p)
if extracted:
    doc_texts.append(f"[File: {p.name}]\n{extracted}")
```

This intentionally surfaces missing/corrupt-parser errors. It must not append unsupported types because `extract_text()` still returns `None` for them.

- [ ] **Step 6: Move dependencies into explicit extras**

In `pyproject.toml`, remove these four entries from `[project].dependencies`:

```toml
"pypdf>=5.0.0,<6.0.0",
"python-docx>=1.1.0,<2.0.0",
"openpyxl>=3.1.0,<4.0.0",
"python-pptx>=1.0.0,<2.0.0",
```

Add:

```toml
documents = [
    "pypdf>=5.0.0,<6.0.0",
    "python-docx>=1.1.0,<2.0.0",
    "openpyxl>=3.1.0,<4.0.0",
    "python-pptx>=1.0.0,<2.0.0",
]
pdf = [
    "pymupdf>=1.25.0",
    "pypdf>=5.0.0,<6.0.0",
]
```

Add the four document dependencies to `dev` so `pip install -e ".[dev]"` retains the current complete test environment. Keep `pymupdf` in `dev`.

- [ ] **Step 7: Regenerate the uv lockfile**

```powershell
uv lock
uv lock --check
```

Expected: lock succeeds and the local project package metadata shows the new `documents` extra.

- [ ] **Step 8: Update install documentation**

Use these profiles consistently:

```bash
pip install miniunicorn-ai
pip install "miniunicorn-ai[documents]"          # PDF/DOCX/XLSX/PPTX extraction
pip install -e ".[api,vector,pdf,documents,dev]" # complete source-development profile
```

Explain that `[pdf]` remains for PDF-specific compatibility/features while `[documents]` is the complete attachment extraction set.

- [ ] **Step 9: Verify metadata hunks and behavior**

```powershell
uv run pytest tests/test_document_parsing.py tests/test_context_documents.py tests/agent/test_document_extraction_toggle.py -q
uv run ruff check miniunicorn/utils/document.py tests/test_document_parsing.py tests/agent/test_document_extraction_toggle.py
uv lock --check
git diff -- pyproject.toml
git diff --check
```

Expected: tests/checks pass; the `pyproject.toml` diff includes both the pre-existing user hunk and this task’s dependency hunks.

- [ ] **Step 10: Stage only this task’s metadata changes and commit**

```powershell
git add -p pyproject.toml
git add uv.lock miniunicorn/utils/document.py tests/test_document_parsing.py tests/agent/test_document_extraction_toggle.py README.md README.en.md docs/quick-start.md
git diff --cached --check
git commit -m "packaging: move document parsers to optional extra"
```

Before accepting each `git add -p` hunk, reject unrelated author/metadata changes recorded in Task 0.

### Task 26: Keep Docker document-capable and verify a truly minimal wheel install

- [x] Superseded by remediation Tasks 20 and 22 because the artifact, minimal-install, and Docker verification were split across the packaging verification and checkpoint tasks, commits 6132d92500cd7bc6e36bef0503b149594865844c and 2cc93164f41909a2632767cd800e24e97f1cfd8a, verified by `pytest tests/packaging -q` and the packaging checkpoint commands.

**Files:**
- Modify: `Dockerfile`
- Create: `tests/packaging/test_dependency_metadata.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Docker installs `.[documents]` in both dependency and final source layers.
- Minimal-install CI uses a fresh virtual environment and asserts document modules are absent.
- Wheel import and CLI version work without document extras.

- [ ] **Step 1: Add project metadata tests**

Create `tests/packaging/test_dependency_metadata.py`:

```python
import tomllib
from pathlib import Path


def project_metadata():
    root = Path(__file__).resolve().parents[2]
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_document_parsers_are_not_base_dependencies():
    dependencies = "\n".join(project_metadata()["dependencies"])
    for package in ("pypdf", "python-docx", "openpyxl", "python-pptx"):
        assert package not in dependencies


def test_documents_extra_contains_every_extractor_backend():
    extras = project_metadata()["optional-dependencies"]
    documents = "\n".join(extras["documents"])
    for package in ("pypdf", "python-docx", "openpyxl", "python-pptx"):
        assert package in documents
    assert "pypdf" in "\n".join(extras["pdf"])
```

- [ ] **Step 2: Make Docker select the complete document profile**

Change both Docker install commands:

```dockerfile
uv pip install --system --no-cache ".[documents]"
```

The first metadata-only layer and final source layer must use the same extra so cached dependencies are complete.

- [ ] **Step 3: Add a packaging CI job**

Add:

```yaml
packaging:
  name: Wheel and minimal install
  runs-on: ubuntu-latest
  timeout-minutes: 15
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with:
        python-version: "3.13"
    - uses: astral-sh/setup-uv@v4
    - uses: oven-sh/setup-bun@v2
      with:
        bun-version: latest
    - name: Build frontend assets
      working-directory: webui
      run: |
        bun install --frozen-lockfile
        bun run build
    - name: Build wheel and sdist
      run: uv build
      env:
        MINIUNICORN_SKIP_WEBUI_BUILD: "1"
    - name: Install base wheel in an isolated environment
      run: |
        uv venv .venv-minimal
        uv pip install --python .venv-minimal/bin/python dist/*.whl
    - name: Verify base imports without document extras
      run: |
        .venv-minimal/bin/python -c "import miniunicorn; print(miniunicorn.__version__)"
        .venv-minimal/bin/python -c "from miniunicorn.agent.runner import AgentRunner; print(AgentRunner.__name__)"
        .venv-minimal/bin/python -c "import importlib.util; names=('pypdf','docx','openpyxl','pptx'); found=[n for n in names if importlib.util.find_spec(n)]; assert not found, found"
    - name: Verify artifacts
      run: uv run --with twine twine check dist/*
```

The job runs on Linux, so `.venv-minimal/bin/python` is intentional.

- [ ] **Step 4: Add Docker build smoke verification**

After the packaging job or as a separate step:

```yaml
- name: Build document-capable Docker image
  run: docker build -t miniunicorn-packaging-test .
- name: Verify document imports in Docker image
  run: >
    docker run --rm --entrypoint python miniunicorn-packaging-test
    -c "import pypdf, docx, openpyxl, pptx; print('documents ok')"
```

If CI duration exceeds the existing budget, retain this step on `push` to `main` and run wheel/minimal checks on every pull request. Do not drop the wheel/minimal checks.

- [ ] **Step 5: Verify locally**

```powershell
uv run pytest tests/packaging/test_dependency_metadata.py -q
uv build
uv run --with twine twine check dist/*
docker build -t miniunicorn-packaging-test .
docker run --rm --entrypoint python miniunicorn-packaging-test -c "import pypdf, docx, openpyxl, pptx; print('documents ok')"
```

Expected: metadata tests pass; wheel/sdist pass Twine; Docker prints `documents ok`.

- [ ] **Step 6: Commit packaging verification**

```powershell
git add Dockerfile tests/packaging/test_dependency_metadata.py .github/workflows/ci.yml
git commit -m "ci(packaging): verify minimal and document installs"
```

Do not commit `dist/` or `.venv-minimal/`.

### Task 27: Make version resolution robust to malformed source metadata

- [x] Implemented by remediation Task 21, commit 062006a798ec52829cf5a22bc73c14aae4c40d40, verified by `pytest tests/test_package_version.py -q`.

**Files:**
- Modify: `miniunicorn/__init__.py`
- Modify: `tests/test_package_version.py`

**Interfaces:**
- `_read_pyproject_version() -> str | None` returns `None` for missing, unreadable, undecodable, or malformed TOML.
- `_resolve_version()` then falls back to installed metadata and finally `0.0.0+unknown`.
- No logging dependency is introduced during package bootstrap.

- [ ] **Step 1: Add malformed/unreadable metadata tests**

```python
def test_read_pyproject_version_returns_none_for_malformed_toml(monkeypatch):
    import miniunicorn

    miniunicorn._read_pyproject_version.cache_clear()
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "[project\nbroken")
    try:
        assert miniunicorn._read_pyproject_version() is None
    finally:
        miniunicorn._read_pyproject_version.cache_clear()


def test_read_pyproject_version_returns_none_for_read_error(monkeypatch):
    import miniunicorn

    miniunicorn._read_pyproject_version.cache_clear()

    def fail_read(self, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", fail_read)
    try:
        assert miniunicorn._read_pyproject_version() is None
    finally:
        miniunicorn._read_pyproject_version.cache_clear()
```

- [ ] **Step 2: Observe import-helper failures**

```powershell
uv run pytest tests/test_package_version.py -k "malformed or read_error" -v
```

Expected: both tests fail because exceptions escape.

- [ ] **Step 3: Guard source metadata parsing**

Use:

```python
try:
    raw = pyproject.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
except (OSError, UnicodeError, tomllib.TOMLDecodeError):
    return None
version = data.get("project", {}).get("version")
return version if isinstance(version, str) and version else None
```

Keep the existing `exists()` short circuit. Do not catch `BaseException`.

- [ ] **Step 4: Verify all version paths**

```powershell
uv run pytest tests/test_package_version.py -q
uv run ruff check miniunicorn/__init__.py tests/test_package_version.py
git diff --check
```

Expected: source, installed-metadata fallback, unknown fallback, malformed TOML, and read-error tests pass.

- [ ] **Step 5: Commit version resilience**

```powershell
git add miniunicorn/__init__.py tests/test_package_version.py
git commit -m "fix(packaging): tolerate malformed source metadata"
```

### Task 28: Final four-batch verification, release notes, and handoff

- [x] Superseded by remediation Task 27 because the final verification, release notes, and handoff were folded into the remediation final matrix and evidence report, verified by the gate matrix in `docs/superpowers/evidence/2026-08-03-remediation-final.md` and release notes in `docs/four-batch-hardening-release-notes.md`.

**Files:**
- Create: `docs/four-batch-hardening-release-notes.md`
- Modify: this plan only to check completed boxes or record deviations.
- No new runtime behavior in this task.

**Interfaces:**
- Produces an evidence-backed release summary and a clean branch ready for review.

- [ ] **Step 1: Run generated-contract checks**

```powershell
uv lock --check
uv run python scripts/export_agent_event_schema.py --check
Set-Location webui
bun run check:protocol
Set-Location ..
```

Expected: all checks exit zero.

- [ ] **Step 2: Run static and frontend gates**

```powershell
uv run ruff check miniunicorn tests scripts
uv run ruff format --check miniunicorn tests scripts
Set-Location webui
bun run lint
bun x tsc --noEmit
bun run test
bun run build
Set-Location ..
```

Expected: Ruff passes; frontend lint/type/test/build pass without the known warning strings.

- [ ] **Step 3: Run backend core and full suites**

```powershell
uv run pytest -m core -q
uv run pytest tests/ -q
```

Expected: both commands pass. Allow the full suite up to the CI job’s 25-minute timeout; do not claim completion from the core subset alone.

- [ ] **Step 4: Build and inspect distribution artifacts**

```powershell
uv build
uv run --with twine twine check dist/*
docker build -t miniunicorn-final-verification .
docker run --rm --entrypoint python miniunicorn-final-verification -c "import pypdf, docx, openpyxl, pptx; print('documents ok')"
```

Expected: wheel/sdist validate and Docker prints `documents ok`.

- [ ] **Step 5: Re-run critical cross-session and security proofs**

```powershell
uv run pytest tests/agent/test_turn_concurrency.py tests/agent/test_telemetry.py tests/config/test_api_security.py tests/tools/test_exec_platform.py -v
```

Expected: same-session serialization, different-session overlap, per-turn attribution, fail-closed sandbox, and authenticated public bind cases all pass.

- [ ] **Step 6: Scan for prohibited leftovers**

```powershell
rg -n 'self\._(last_usage|last_call_usage|current_iteration|pending_turn_latency_ms)\s*=' miniunicorn
rg -n 'TODO|FIXME|NotImplementedError|pass\s+#' miniunicorn/agent/runner_*.py miniunicorn/agent/turn_*.py miniunicorn/bus/agent_events.py miniunicorn/channels/websocket/outbound.py miniunicorn/channels/feishu miniunicorn/channels/weixin webui/src/hooks/stream-*.ts webui/src/components/thread/activity
rg -n 'pypdf|python-docx|openpyxl|python-pptx' pyproject.toml
git diff --check
```

Expected:

- no forbidden shared-turn assignment;
- no implementation placeholders introduced by this plan;
- document dependencies appear only in `documents`, compatibility `pdf` where applicable, and `dev`, not base dependencies;
- no whitespace errors.

- [ ] **Step 7: Write release notes with measured evidence**

Create `docs/four-batch-hardening-release-notes.md` with:

- security changes and explicit compatibility flags;
- concurrency ownership and proof that cross-session concurrency remains;
- protocol version/generation commands and legacy-flag migration status;
- telemetry fields/privacy;
- runner/WebUI/channel module boundaries and final measured line counts;
- base vs `documents`/`pdf`/`dev`/Docker install profiles;
- exact test counts and durations from the final run;
- any documented deviations from this plan.

Do not state that legacy flags were removed; they intentionally remain during the compatibility window.

- [ ] **Step 8: Commit verification documentation**

```powershell
git add docs/four-batch-hardening-release-notes.md
git commit -m "docs: record four-batch hardening verification"
```

This historical plan is intentionally tracked on `codex/full-remediation` so another computer receives the mapping ledger. Stage its Task 15–28 checkbox updates only as directed by the authoritative remediation plan.

- [ ] **Step 9: Audit branch scope**

```powershell
git status --short
git log --oneline --decorate --max-count=35
git diff --stat main...HEAD
git diff --name-status main...HEAD
```

Expected: only planned files are in the branch; pre-existing user changes remain unstaged/uncommitted unless the user explicitly authorized including them.

- [ ] **Step 10: Use the branch-finishing skill**

Invoke `superpowers:finishing-a-development-branch`. Present merge/PR/keep-branch options, but do not push or open a PR without the user’s instruction.

## Final Definition of Done

- Tasks 0–28 (29 tasks total) are checked or have a written, evidence-backed deviation.
- Every task-level commit exists and each batch checkpoint passed before the next batch began.
- Security defaults are fail-closed.
- Public unauthenticated API binds require an explicit unsafe override.
- Same-session serialization and cross-session concurrency are both proven.
- No mutable turn state is shared across sessions.
- Backend and frontend share one generated versioned event contract.
- Turn telemetry is structured, privacy-bounded, and failure-isolated.
- Runner, stream, activity, and channel decompositions satisfy size and compatibility gates.
- Base installation excludes document backends; document/Docker/development profiles retain them.
- Full backend, frontend, package, and Docker verification passed from the final source state.

## Executor Handoff Prompt

Paste the following into the implementing agent:

```text
Work from the checked-out MiniUnicorn repository root on branch
codex/full-remediation. Read
docs/superpowers/specs/2026-08-03-embedding-three-worker-completion-remediation-design.md
and docs/superpowers/plans/2026-08-03-embedding-three-worker-completion-remediation.md
completely. Treat this 2026-07-29 file only as the historical checklist and
commit-evidence ledger used by remediation Task 27; do not execute it as a
separate plan. Preserve all pre-existing uncommitted user changes, use TDD,
commit each remediation task separately, and do not claim completion until
all gates and real embedding/three-Worker evidence in the authoritative plan
pass. Do not push or open a PR without explicit user authorization.
```
