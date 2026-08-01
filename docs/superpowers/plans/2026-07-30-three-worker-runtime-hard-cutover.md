# Three-Worker Runtime Hard-Cutover Implementation Plan

> **Status (2026-07-31):** Tasks 1-10 were implemented on this branch. Tasks
> 11 and 12 are **superseded / completed** by the follow-up remediation plan
> `docs/superpowers/plans/2026-07-31-three-worker-production-readiness-remediation.md`,
> which corrected the disconnected production paths and recovery semantics
> found by the 2026-07-31 review. Do not re-implement them here.
>
> - Old Task 11 (SQLite façade split) → completed by remediation Task 12,
>   commit `1a96b7b4` (`refactor: split sqlite runtime store by responsibility`).
> - Old Task 12 (parity / crash recovery / load / docs) → completed by
>   remediation Tasks 13-16: commits `53aa9a26` (duplicate-path cleanup),
>   `def6dd2e` (golden-flow + fault gates), `d0d795db` (load + soak gates),
>   and the documentation commit from remediation Task 16.
>
> The checkboxes below are left unchecked intentionally; this document is the
> historical plan, not a live task tracker. The remediation plan is the
> authority for production-readiness status.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the durable Runtime the only MiniUnicorn execution path, start the long-running Gateway as one Control Plane plus three Worker processes, and delete the legacy in-process task authority before merge.

**Architecture:** Keep the existing Agent Core and place it behind Agent-owned ports. Lightweight and supervised launchers assemble the same Task Service, Scheduler, Worker, Tool Gateway, Session Committer, Outbox, and SQLite ledger; supervised mode changes only process boundaries and uses three single-concurrency Worker children. Perform a staged hard cutover inside the branch, but ship no `runtime.enabled=false` path.

**Tech Stack:** Python 3.11–3.13, asyncio, Typer, Pydantic 2, aiohttp, multiprocessing with `spawn`, SQLite WAL, pytest/pytest-asyncio, Vitest/Vite.

## Global Constraints

- Implement against `docs/superpowers/specs/2026-07-30-three-worker-runtime-hard-cutover-design.md`.
- Begin from commit `7d2919e4` or a descendant containing only intentional changes.
- Use an isolated Git worktree when execution begins; do not mix implementation with the user's existing untracked files.
- Long-running `gateway` defaults to `supervised`; one-shot `agent` and `serve` default to `lightweight`.
- Supervised mode defaults to exactly three Workers and `worker_concurrency=1`.
- Lightweight mode uses the same durable kernel and defaults to one Worker coroutine.
- The final configuration contains no `runtime.enabled` field and no legacy execution switch.
- Agent Core imports no `miniunicorn.runtime`, `sqlite3`, `multiprocessing`, Supervisor, or Channel implementation.
- Runtime Store is the only task/lease/checkpoint/tool/outbox authority; SessionManager remains the transcript authority.
- Message Bus and IPC carry wake hints and lossy realtime events only.
- No SQLite transaction spans a Provider, tool, Channel, filesystem, or user-code call.
- Final replies and Message Tool output use Outbox only.
- Required background work is submitted as durable tasks; `asyncio.create_task()` is allowed only for reconstructable lifecycle helpers.
- Every task follows red-green-refactor, ends with focused verification, and creates one focused commit.
- Do not weaken an earlier architecture test to make a later task pass.

## Target File Structure

New files:

```text
miniunicorn/config/runtime.py
miniunicorn/runtime/application.py
miniunicorn/runtime/bootstrap.py
miniunicorn/runtime/ingress.py
miniunicorn/runtime/message_delivery.py
miniunicorn/runtime/process_entrypoints.py
miniunicorn/runtime/realtime.py
miniunicorn/runtime/sqlite/task_store.py
miniunicorn/runtime/sqlite/execution_store.py
miniunicorn/runtime/sqlite/session_store.py
miniunicorn/runtime/sqlite/outbox_store.py
miniunicorn/runtime/sqlite/resource_store.py
miniunicorn/runtime/sqlite/maintenance_store.py
miniunicorn/runtime/sqlite/vector_memory_store.py
tests/runtime/test_application.py
tests/runtime/test_bootstrap.py
tests/runtime/test_process_entrypoints.py
tests/runtime/test_production_cutover.py
tests/runtime/test_three_worker_acceptance.py
tests/runtime/test_runtime_load.py
```

Responsibility map:

- `config/runtime.py`: root-owned Runtime configuration, path and mode precedence.
- `runtime/application.py`: small application-facing submit/wait/result/lifecycle façade.
- `runtime/ingress.py`: deterministic conversion from normalized inbound requests to durable envelopes.
- `runtime/bootstrap.py`: production construction for lightweight and supervised modes.
- `runtime/message_delivery.py`: Agent-owned outbound-port implementation backed by Outbox, plus local CLI/API delivery receipts.
- `runtime/process_entrypoints.py`: top-level picklable Control Plane and Worker child functions.
- `runtime/realtime.py`: bounded Worker-event relay and Control Plane subscriptions.
- `runtime/sqlite/*_store.py`: cohesive SQL operation groups behind one `SqliteRuntimeStore` façade.
- `runtime/sqlite/vector_memory_store.py`: injected derived-memory index; never a task authority.

---

### Task 1: Reproduce the locked development test environment

**Files:**
- Modify: `pyproject.toml:70-78`
- Modify: `uv.lock`
- Create: `tests/test_dependency_contract.py`
- Test: `tests/cli/test_heartbeat.py`
- Test: `tests/test_api_attachment.py`

**Interfaces:**
- Consumes: Python requirement `>=3.11` and the existing `dev` optional dependency group.
- Produces: a development environment that can collect all backend tests on Windows and includes aiohttp API tests.

- [ ] **Step 1: Add a dependency-presence characterization test**

Add to `tests/test_dependency_contract.py`:

```python
from importlib.util import find_spec
import sys


def test_api_test_dependency_is_installed() -> None:
    assert find_spec("aiohttp") is not None


def test_windows_zoneinfo_dependency_is_installed() -> None:
    if sys.platform == "win32":
        assert find_spec("tzdata") is not None
```

- [ ] **Step 2: Run the dependency test and full collection**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_dependency_contract.py -q
.venv\Scripts\python.exe -m pytest --collect-only -q
```

Expected before synchronization: the dependency test or collection fails for missing `aiohttp` and/or `tzdata`. Record the exact missing packages in the commit message body.

- [ ] **Step 3: Declare Windows timezone data and synchronize the environment**

Add this exact entry to `[project.optional-dependencies].dev`:

```toml
"tzdata>=2025.2; sys_platform == 'win32'",
```

Then run:

```powershell
uv lock
uv sync --extra dev
```

Do not hand-edit generated package records in `uv.lock`.

- [ ] **Step 4: Verify clean collection**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_dependency_contract.py -q
.venv\Scripts\python.exe -m pytest --collect-only -q
```

Expected: dependency tests pass and collection exits zero with no import or ZoneInfo errors.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml uv.lock tests/test_dependency_contract.py
git commit -m "test: make runtime verification environment reproducible"
```

---

### Task 2: Integrate Runtime configuration into the root Config model

**Files:**
- Create: `miniunicorn/config/runtime.py`
- Modify: `miniunicorn/config/schema.py:547-562`
- Modify: `miniunicorn/config/__init__.py`
- Modify: `miniunicorn/runtime/config.py`
- Modify: `miniunicorn/cli/commands.py:360-490`
- Test: `tests/runtime/test_config.py`
- Create: `tests/runtime/test_cutover_config.py`

**Interfaces:**
- Consumes: existing Runtime validation fields from `miniunicorn/runtime/config.py`.
- Produces:
  - `RuntimeMode = Literal["lightweight", "supervised"]`
  - `RuntimeConfig` with no `enabled` field
  - `resolve_runtime_mode(configured, cli_value, environment, launcher_default) -> RuntimeMode`
  - `resolve_runtime_paths(config, data_root) -> RuntimeConfig`
  - CLI option `--runtime-mode lightweight|supervised`

- [ ] **Step 1: Write failing root-configuration tests**

Create `tests/runtime/test_cutover_config.py`:

```python
from miniunicorn.config.runtime import resolve_runtime_mode, RuntimeConfig
from miniunicorn.config.schema import Config


def test_root_config_owns_runtime_settings() -> None:
    cfg = Config.model_validate({"runtime": {"workerCount": 3}})
    assert cfg.runtime.worker_count == 3
    assert not hasattr(cfg.runtime, "enabled")


def test_runtime_mode_precedence() -> None:
    assert resolve_runtime_mode(
        configured="lightweight",
        cli_value="supervised",
        environment="lightweight",
        launcher_default="lightweight",
    ) == "supervised"
    assert resolve_runtime_mode(
        configured="supervised",
        cli_value=None,
        environment="lightweight",
        launcher_default="lightweight",
    ) == "lightweight"
    assert resolve_runtime_mode(
        configured="supervised",
        cli_value=None,
        environment=None,
        launcher_default="lightweight",
    ) == "supervised"
    assert resolve_runtime_mode(
        configured=None,
        cli_value=None,
        environment=None,
        launcher_default="supervised",
    ) == "supervised"


def test_supervised_defaults_to_three_single_concurrency_workers() -> None:
    cfg = RuntimeConfig()
    assert cfg.worker_count == 3
    assert cfg.worker_concurrency == 1
```

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_cutover_config.py -q
```

Expected: import failure for `miniunicorn.config.runtime` or missing `Config.runtime`.

- [ ] **Step 3: Move the neutral configuration model**

Create `miniunicorn/config/runtime.py` by moving the existing RuntimeConfig fields and validators, removing `enabled`, and making `mode` optional until launcher resolution:

```python
from __future__ import annotations

from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

RuntimeMode = Literal["lightweight", "supervised"]


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    mode: RuntimeMode | None = None
    database_path: str = "runtime/runtime.sqlite"
    backup_path: str = "runtime/backups"
    worker_count: int = Field(default=3, ge=2)
    lightweight_execution_slots: int = Field(default=1, ge=1, le=3)
    worker_concurrency: int = Field(default=1, ge=1, le=1)
    heartbeat_interval_s: int = Field(default=15, ge=1)
    lease_timeout_s: int = Field(default=180, ge=1)
    lease_scan_interval_s: int = Field(default=15, ge=1)
    progress_timeout_s: int = Field(default=600, ge=1)
    task_max_attempts: int = Field(default=3, ge=1)
    queue_poll_min_ms: int = Field(default=250, ge=10)
    queue_poll_max_ms: int = Field(default=2000, ge=10)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=1)
    realtime_event_queue_capacity: int = Field(default=1000, ge=1)
    shutdown_grace_s: int = Field(default=60, ge=1)
    approval_timeout_m: int = Field(default=30, ge=1)
    waiting_alert_m: int = Field(default=60, ge=1)
    outbox_lease_timeout_s: int = Field(default=120, ge=1)
    outbox_max_attempts: int = Field(default=8, ge=1)
    channel_send_timeout_s: int = Field(default=60, ge=1)
    successful_retention_d: int = Field(default=7, ge=1)
    failure_retention_d: int = Field(default=30, ge=1)
    backup_interval_h: int = Field(default=6, ge=1)
    backup_retention_d: int = Field(default=7, ge=1)
    inline_blob_max_bytes: int = Field(default=1_048_576, ge=1)
    minimum_free_disk_mb: int = Field(default=1024, ge=1)
    max_turn_wall_time_m: int = Field(default=120, ge=1)
    stable_max_tool_iterations: int = Field(default=50, ge=1)
    global_max_subagents: int = Field(default=4, ge=1)
    worker_max_rss_mb: int | None = Field(default=None, ge=1)
    worker_max_uptime_h: int | None = Field(default=None, ge=1)
    worker_max_tasks_before_recycle: int | None = Field(default=None, ge=1)
    database_path_resolved: Path | None = None
    backup_path_resolved: Path | None = None

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "RuntimeConfig":
        if self.heartbeat_interval_s * 3 >= self.lease_timeout_s:
            raise ValueError("heartbeat interval must be less than one third of lease timeout")
        if self.progress_timeout_s <= self.heartbeat_interval_s:
            raise ValueError("progress timeout must exceed heartbeat interval")
        if self.queue_poll_max_ms >= self.lease_scan_interval_s * 1000:
            raise ValueError("maximum queue poll must be below lease scan interval")
        if self.outbox_lease_timeout_s <= self.channel_send_timeout_s:
            raise ValueError("Outbox lease timeout must exceed Channel send timeout")
        if self.sqlite_busy_timeout_ms >= self.lease_timeout_s * 1000:
            raise ValueError("SQLite busy timeout must be shorter than task lease timeout")
        if self.backup_path == self.database_path:
            raise ValueError("backup path must differ from database path")
        return self


def resolve_runtime_mode(
    *,
    configured: RuntimeMode | None,
    cli_value: RuntimeMode | None,
    environment: RuntimeMode | None,
    launcher_default: RuntimeMode,
) -> RuntimeMode:
    return cli_value or environment or configured or launcher_default


def resolve_runtime_paths(config: RuntimeConfig, data_root: Path) -> RuntimeConfig:
    update = {
        "database_path_resolved": (
            Path(config.database_path)
            if Path(config.database_path).is_absolute()
            else (data_root / config.database_path).resolve()
        ),
        "backup_path_resolved": (
            Path(config.backup_path)
            if Path(config.backup_path).is_absolute()
            else (data_root / config.backup_path).resolve()
        ),
    }
    return config.model_copy(update=update)
```

- [ ] **Step 4: Attach RuntimeConfig to Config and re-export it**

Add to `Config`:

```python
runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
```

Import from `miniunicorn.config.runtime`, and make
`miniunicorn/runtime/config.py` a compatibility re-export containing only:

```python
from miniunicorn.config.runtime import (
    RuntimeConfig,
    RuntimeMode,
    resolve_runtime_mode,
    resolve_runtime_paths,
)

__all__ = [
    "RuntimeConfig",
    "RuntimeMode",
    "resolve_runtime_mode",
    "resolve_runtime_paths",
]
```

- [ ] **Step 5: Add mode options to launchers**

Add this option to `serve`, `gateway`, `desktop_gateway`, and `agent`:

```python
runtime_mode: str | None = typer.Option(
    None,
    "--runtime-mode",
    help="Runtime mode: lightweight or supervised",
)
```

Validate it through `RuntimeMode` and resolve with:

```python
mode = resolve_runtime_mode(
    configured=cfg.runtime.mode,
    cli_value=runtime_mode,
    environment=os.getenv("MINIUNICORN_RUNTIME_MODE"),
    launcher_default="supervised",  # gateway/desktop-gateway
)
```

Use `"lightweight"` as the launcher default for `agent` and `serve`.

- [ ] **Step 6: Verify configuration and existing config tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_config.py tests\runtime\test_cutover_config.py tests\test_config.py -q
```

Expected: all selected tests pass; no test references `runtime.enabled`.

- [ ] **Step 7: Commit**

```powershell
git add miniunicorn/config/runtime.py miniunicorn/config/schema.py miniunicorn/config/__init__.py miniunicorn/runtime/config.py miniunicorn/cli/commands.py tests/runtime/test_config.py tests/runtime/test_cutover_config.py
git commit -m "feat: integrate durable runtime configuration"
```

---

### Task 3: Restore Agent-owned dependency direction

**Files:**
- Modify: `miniunicorn/agent/ports.py`
- Modify: `miniunicorn/agent/turn_runtime.py`
- Modify: `miniunicorn/agent/loop.py`
- Modify: `miniunicorn/agent/loop_builder.py`
- Modify: `miniunicorn/agent/vector_memory.py`
- Modify: `miniunicorn/agent/runner.py:145-170,400-425,1350-1535`
- Modify: `miniunicorn/agent/agent_run_adapter.py`
- Modify: `miniunicorn/agent/tools/message.py:150-270`
- Modify: `miniunicorn/agent/tools/shell.py:510-570`
- Modify: `miniunicorn/providers/base.py`
- Modify: `miniunicorn/providers/fallback_provider.py`
- Modify: `miniunicorn/runtime/agent_adapter.py`
- Modify: `miniunicorn/runtime/tool_gateway.py`
- Modify: `miniunicorn/runtime/session_committer.py`
- Create: `miniunicorn/runtime/message_delivery.py`
- Create: `miniunicorn/runtime/sqlite/vector_memory_store.py`
- Test: `tests/architecture/test_runtime_dependencies.py`
- Create: `tests/agent/test_runtime_ports_only.py`
- Modify: `tests/agent/test_upgrade_integration.py`
- Modify: `tests/agent/test_vector_memory_fingerprint.py`
- Modify: `tests/runtime/test_wp7_maintenance.py`

**Interfaces:**
- Consumes: existing `TurnJournalPort`, `ToolExecutionPort`, and Provider observer DTOs.
- Produces:
  - `build_tool_execution_request(...) -> ToolExecutionRequest` in Agent-owned code
  - `OutboundRequest`, `OutboundReceipt`, `OutboundPort`
  - `ContainmentPort`
  - `VectorMemoryPort` and `VectorMemoryFactory`
  - typed `TurnRuntime` fields for observer, progress, outbound, and containment
  - task-local Provider observer binding with no shared mutable Provider state
  - no Agent import of `miniunicorn.runtime`

- [ ] **Step 1: Add failing dependency and port tests**

Create `tests/agent/test_runtime_ports_only.py`:

```python
import ast
from pathlib import Path

from miniunicorn.agent.ports import build_tool_execution_request, EffectiveToolPolicy


def test_tool_request_builder_is_agent_owned() -> None:
    request = build_tool_execution_request(
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="read_file",
        arguments={"path": "README.md"},
        policy=EffectiveToolPolicy(
            effect_class="READ",
            risk_class="LOW",
            idempotency_mode="REPLAY_SAFE",
            approval_policy="NEVER",
            recovery_policy="REPLAY",
            concurrency_scope="NONE",
        ),
    )
    assert request.task_id == "task-1"
    assert len(request.arguments_hash) == 64
    assert len(request.idempotency_key) == 64


def test_agent_source_has_no_runtime_import() -> None:
    root = Path("miniunicorn/agent")
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("miniunicorn.runtime"):
                violations.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("miniunicorn.runtime"):
                        violations.append(f"{path}:{node.lineno}")
    assert violations == []
```

- [ ] **Step 2: Verify the tests fail with current reverse imports**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\agent\test_runtime_ports_only.py tests\architecture\test_runtime_dependencies.py -q
```

Expected: failures identify `runner.py`, `tools/message.py`, and `tools/shell.py`.

- [ ] **Step 3: Add Agent-owned outbound and containment contracts**

Add to `miniunicorn/agent/ports.py`:

```python
import hashlib
import json


@dataclass(slots=True, frozen=True)
class OutboundRequest:
    content: str
    channel: str
    target_key: str
    media: tuple[str, ...] = ()
    same_target: bool = False


@dataclass(slots=True, frozen=True)
class OutboundReceipt:
    outbox_id: int
    dedup_key: str


class OutboundPort(Protocol):
    async def enqueue(self, request: OutboundRequest) -> OutboundReceipt: ...


class ContainmentPort(Protocol):
    def register(self, pid: int, *, pgid: int | None = None) -> None: ...


def build_tool_execution_request(
    *,
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    policy: EffectiveToolPolicy,
) -> ToolExecutionRequest:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    arguments_hash = hashlib.sha256(encoded).hexdigest()
    idempotency_key = hashlib.sha256(
        f"{task_id}:{tool_call_id}:{arguments_hash}".encode("utf-8")
    ).hexdigest()
    return ToolExecutionRequest(
        task_id=task_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        normalized_arguments=arguments,
        arguments_hash=arguments_hash,
        policy=policy,
        idempotency_key=idempotency_key,
    )
```

Export all new names through `__all__`.

- [ ] **Step 4: Type TurnRuntime ports and inject the Provider observer**

Add these fields to `TurnRuntime`:

```python
tool_execution_port: ToolExecutionPort | None = None
turn_journal: TurnJournalPort | None = None
provider_attempt_observer: ProviderAttemptObserver | None = None
progress_port: ProgressPort | None = None
outbound_port: OutboundPort | None = None
containment_port: ContainmentPort | None = None
```

Add `provider_attempt_observer` to `AgentRunSpec`. Pass it from
`AgentRunAdapter` using `_durable_runtime_port("provider_attempt_observer")`.
Add an Agent-owned `ContextVar` binding helper in `ports.py`:

```python
@contextmanager
def bind_provider_attempt_observer(observer: ProviderAttemptObserver | None):
    token = _provider_attempt_observer.set(observer)
    try:
        yield
    finally:
        _provider_attempt_observer.reset(token)


def current_provider_attempt_observer() -> ProviderAttemptObserver | None:
    return _provider_attempt_observer.get()
```

Runtime's `AgentExecutionCallback` constructs `JournalProviderObserver` and
binds it to `TurnRuntime`; `AgentRunAdapter` copies it into `AgentRunSpec`, and
`AgentRunner.run()` scopes the whole run with
`bind_provider_attempt_observer(spec.provider_attempt_observer)`.

Change `LLMProvider._call_with_attempt_journal()` to read
`current_provider_attempt_observer()` for every call. Remove the mutable
`LLMProvider.attempt_observer` class attribute and the FallbackProvider
getter/setter/propagation code. Add a concurrency test that runs two Provider
calls under different observers with an `asyncio.Event` barrier and proves
that each observer receives only its own attempt. Never assign an observer to
a shared Provider instance.

Extend `AgentExecutionCallback` with a
`progress_port_factory: Callable[[str], ProgressPort]`. For each task it binds
the returned port to `TurnRuntime` and emits typed `DeltaEvent`,
`StreamEndEvent`, reasoning, tool-progress, and turn-status events through
that port. Remove its current transient `host.bus.publish_outbound(...)`
streaming path. Final replies must still bypass `ProgressPort` and be written
only to Outbox.

- [ ] **Step 5: Move tool request construction out of Runtime**

Import `build_tool_execution_request` from `miniunicorn.agent.ports` in Runner.
Delete the duplicate builder from `runtime/tool_gateway.py` and update Runtime
tests to import the Agent-owned function.

- [ ] **Step 6: Replace Message Tool Runtime lookups with OutboundPort**

Make `_try_durable_enqueue` asynchronous and remove every fallback to direct
delivery. Its decisive branch becomes:

```python
runtime = require_turn_runtime()
if runtime.outbound_port is None:
    raise RuntimeError("OutboundPort is not bound for this task")
receipt = await runtime.outbound_port.enqueue(
    OutboundRequest(
        content=content,
        channel=channel,
        target_key=chat_id,
        media=tuple(media or ()),
        same_target=same_target,
    )
)
return (
    f"Message queued for delivery to {channel}:{chat_id} "
    f"(outbox_id={receipt.outbox_id})"
)
```

Implement `DurableMessageDelivery` in `runtime/message_delivery.py`; it owns
the active claim, writes the payload blob, computes the stable dedup key, and
calls `enqueue_message_tool_outbox`.

Also implement a local sender used only after an Outbox claim:

```python
class LocalResultSender:
    def __init__(self, channels: frozenset[str] = frozenset({"cli", "api"})) -> None:
        self._channels = channels

    async def send_with_receipt(
        self,
        channel_name: str,
        message: OutboundMessage,
    ) -> DeliveryReceipt:
        if channel_name not in self._channels:
            return DeliveryReceipt(
                status="PERMANENT_FAILURE",
                safe_error_code="CHANNEL_NOT_CONFIGURED",
            )
        return DeliveryReceipt(
            status="DELIVERED",
            receipt_ref=(message.metadata or {}).get("message_id"),
        )

    def get_channel_recovery(self, channel_name: str) -> str:
        return "NATIVE_IDEMPOTENCY" if channel_name in self._channels else "NONE"
```

This preserves Outbox as the delivery authority even for synchronous CLI/API
results; it is not a direct Agent return path.

- [ ] **Step 7: Replace Shell Runtime lookup with ContainmentPort**

Change Shell's registration to:

```python
runtime = current_turn_runtime()
containment = runtime.containment_port if runtime is not None else None
if containment is not None:
    containment.register(proc.pid, pgid=(proc.pid if not _IS_WINDOWS else None))
```

Runtime Worker binds the concrete containment scope to the TurnRuntime and
clears it in `finally`.

- [ ] **Step 8: Move the SQLite vector index behind an Agent-owned port**

Define `VectorMemoryPort` and `VectorMemoryFactory` in Agent-owned code. Move
the concrete `VectorMemoryStore`, SQLite connection logic, schema, and
`create_vector_store()` from `agent/vector_memory.py` to
`runtime/sqlite/vector_memory_store.py`; leave only DTOs, protocols, and
`NoOpVectorStore` in the Agent package.

Add this constructor dependency to AgentLoop/AgentLoopBuilder:

```python
vector_memory_factory: VectorMemoryFactory | None = None
```

When `vector_recall=True`, AgentLoop creates the local embedding Provider, then
requires the injected factory and calls it with the derived database path,
dimension, and model ID. Production lightweight and Worker bootstrap inject
the Runtime SQLite factory. Unit tests inject either `NoOpVectorStore` or a
fake; real SQLite vector tests import the concrete Store from its new Runtime
path.

Delete `_KNOWN_VIOLATIONS["vector_memory.py"]` from the architecture test.
Add an assertion that no Python file under `miniunicorn/agent/` imports
`sqlite3`. This is process-local derived memory, not a new service or a second
task authority.

- [ ] **Step 9: Verify dependency purity**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\agent\test_runtime_ports_only.py tests\architecture\test_runtime_dependencies.py tests\agent\test_upgrade_integration.py tests\agent\test_vector_memory_fingerprint.py tests\runtime\test_wp4_provider_tool_gateway.py tests\runtime\test_wp5_outbox.py tests\runtime\test_wp7_maintenance.py -q
```

Expected: no Agent→Runtime/SQLite/multiprocessing violation and no dependency
test xfail.

- [ ] **Step 10: Commit**

```powershell
git add miniunicorn/agent miniunicorn/providers/base.py miniunicorn/providers/fallback_provider.py miniunicorn/runtime/agent_adapter.py miniunicorn/runtime/tool_gateway.py miniunicorn/runtime/session_committer.py miniunicorn/runtime/message_delivery.py miniunicorn/runtime/sqlite/vector_memory_store.py tests/agent/test_runtime_ports_only.py tests/agent/test_upgrade_integration.py tests/agent/test_vector_memory_fingerprint.py tests/architecture/test_runtime_dependencies.py tests/runtime/test_wp4_provider_tool_gateway.py tests/runtime/test_wp5_outbox.py tests/runtime/test_wp7_maintenance.py
git commit -m "refactor: restore agent-owned runtime ports"
```

---

### Task 4: Add one durable ingress and result façade

**Files:**
- Create: `miniunicorn/runtime/application.py`
- Create: `miniunicorn/runtime/ingress.py`
- Create: `miniunicorn/runtime/realtime.py`
- Modify: `miniunicorn/runtime/models.py`
- Modify: `miniunicorn/runtime/contracts.py`
- Modify: `miniunicorn/runtime/task_service.py`
- Modify: `miniunicorn/runtime/sqlite/store.py`
- Modify: `miniunicorn/runtime/agent_adapter.py`
- Create: `tests/runtime/test_application.py`

**Interfaces:**
- Consumes: `TaskService`, `RequestScope`, `InboundTaskEnvelope`, and protected blobs.
- Produces:
  - `RuntimeInboundRequest`
  - `DurableReply` in `runtime.models`
  - `RuntimeTurnResult`
  - `RuntimeApplication.submit()`, `wait()`, `read_reply()`, `submit_and_wait()`
  - `TaskIngressStore.read_final_reply(scope, task_id)`
  - `RealtimeSubscriptionHub`
  - `local_request_scope(config, principal_id) -> RequestScope`
  - `build_internal_envelope(...) -> InternalTaskEnvelope`

- [ ] **Step 1: Write failing façade tests**

Create `tests/runtime/test_application.py` with a fake store:

```python
import pytest

from miniunicorn.runtime.application import RuntimeApplication, RuntimeInboundRequest
from miniunicorn.runtime.models import RequestScope


@pytest.mark.asyncio
async def test_submit_and_wait_returns_durable_reply(runtime_application) -> None:
    result = await runtime_application.submit_and_wait(
        RuntimeInboundRequest(
            content="hello",
            media=(),
            metadata={},
            session_key="api:test",
            channel="api",
            channel_account="user",
            channel_message_id="msg-1",
            scope=RequestScope(
                tenant_id="local",
                principal_id="user",
                agent_id="default",
                workspace_id="default",
            ),
        ),
        timeout_s=5,
    )
    assert result.snapshot.state == "COMPLETED"
    assert result.reply.content == "world"
```

- [ ] **Step 2: Verify the façade test fails**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_application.py -q
```

Expected: import failure for `runtime.application`.

- [ ] **Step 3: Define request and result DTOs**

Define `DurableReply` in `runtime/models.py`, beside the other Store DTOs, so
`runtime/contracts.py` can reference it without importing the application
layer:

```python
@dataclass(slots=True, frozen=True)
class DurableReply:
    content: str
    outbox_id: int | None
    metadata: dict[str, Any]
```

In `runtime/application.py` import `DurableReply` and define:

```python
@dataclass(slots=True, frozen=True)
class RuntimeInboundRequest:
    content: str
    media: tuple[str, ...]
    metadata: dict[str, Any]
    session_key: str
    channel: str
    channel_account: str
    channel_message_id: str | None
    scope: RequestScope


@dataclass(slots=True, frozen=True)
class RuntimeTurnResult:
    snapshot: TaskSnapshot
    reply: DurableReply
```

Define `RealtimeSubscriptionHub` in `runtime/realtime.py` now, using the
bounded task-scoped subscription contract shown in Task 7. Task 7 only adds
the multiprocess-envelope adapter to this already-tested hub.

Add to the Runtime Store ingress/result view:

```python
def read_final_reply(
    self,
    scope: RequestScope,
    task_id: str,
) -> DurableReply | None: ...
```

- [ ] **Step 4: Move deterministic envelope construction to ingress.py**

Implement:

```python
def build_inbound_envelope(request: RuntimeInboundRequest, *, now_ms: int) -> InboundTaskEnvelope:
    payload = {
        "content": request.content,
        "media": list(request.media),
        "metadata": request.metadata,
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    dedup_key = (
        f"{request.channel}:{request.session_key}:{request.channel_message_id}"
        if request.channel_message_id
        else None
    )
    return InboundTaskEnvelope(
        protocol_version=1,
        task_kind="USER_TURN",
        priority=100,
        scope=request.scope,
        session_key=request.session_key,
        channel=request.channel,
        channel_account=request.channel_account,
        channel_message_id=request.channel_message_id,
        dedup_key=dedup_key,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        media_refs=(),
        received_at_ms=now_ms,
        turn_id=None,
        payload_content=payload_bytes,
    )
```

Add these two helpers to the same module:

```python
def local_request_scope(config: Config, principal_id: str = "local-user") -> RequestScope:
    workspace = str(config.workspace_path.resolve())
    workspace_id = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:16]
    return RequestScope(
        tenant_id="local",
        principal_id=principal_id,
        agent_id="default",
        workspace_id=workspace_id,
    )


def build_internal_envelope(
    *,
    kind: TaskKind,
    scope: RequestScope,
    session_key: str,
    dedup_key: str,
    payload: dict[str, Any],
    priority: int,
    now_ms: int,
) -> InternalTaskEnvelope:
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    return InternalTaskEnvelope(
        protocol_version=1,
        task_kind=kind,
        priority=priority,
        scope=scope,
        session_key=session_key,
        dedup_key=dedup_key,
        normalized_payload_ref=f"inline:{payload_hash[:16]}",
        payload_hash=payload_hash,
        received_at_ms=now_ms,
        payload_content=payload_bytes,
    )
```

Extend `InternalTaskEnvelope` with:

```python
payload_content: bytes | None = None
```

Update `SqliteRuntimeStore.submit_internal()` so the `BlobWrite` uses
`inline_content=envelope.payload_content` and `size_bytes` when content is
present; otherwise it retains the existing `external_ref` behavior. This gives
required background tasks a durable payload instead of an unresolvable inline
reference.

Delete `submit_durable` and `dispatch_durable` envelope-building duplication
from `runtime/agent_adapter.py`.

- [ ] **Step 5: Implement RuntimeApplication**

Use this public surface:

```python
class RuntimeApplication:
    def __init__(
        self,
        task_service: TaskService,
        result_store: TaskIngressStore,
        realtime: RealtimeSubscriptionHub,
    ) -> None:
        self.task_service = task_service
        self._result_store = result_store
        self._realtime = realtime
        self._accepting = True

    async def submit(self, request: RuntimeInboundRequest) -> TaskHandle:
        if not self._accepting:
            raise RuntimeError("runtime ingress is draining")
        envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
        return await self.task_service.submit(envelope)

    def start_accepting(self) -> None:
        self._accepting = True

    def stop_accepting(self) -> None:
        self._accepting = False

    async def wait(
        self,
        scope: RequestScope,
        task_id: str,
        timeout_s: float | None,
    ) -> TaskSnapshot:
        return await self.task_service.wait_terminal(scope, task_id, timeout_s)

    def read_reply(self, scope: RequestScope, task_id: str) -> DurableReply:
        reply = self._result_store.read_final_reply(scope, task_id)
        if reply is None:
            return DurableReply(content="", outbox_id=None, metadata={})
        return reply

    def subscribe(self, task_id: str):
        return self._realtime.subscribe(task_id)

    async def submit_and_wait(
        self,
        request: RuntimeInboundRequest,
        timeout_s: float | None = None,
    ) -> RuntimeTurnResult:
        handle = await self.submit(request)
        snapshot = await self.wait(request.scope, handle.task_id, timeout_s)
        return RuntimeTurnResult(snapshot, self.read_reply(request.scope, handle.task_id))
```

Test that `stop_accepting()` rejects a new submit without writing a task, while
`wait()` and `read_reply()` continue to work for already accepted task IDs.

- [ ] **Step 6: Implement scope-checked SQLite result lookup**

Join `tasks` to the task's final-reply Outbox row and `runtime_blobs`. Verify
tenant, principal, agent, and workspace scope before returning content. Decode
only `RAW_BYTES`; return safe metadata containing `task_id`, `state`, and
`outbox_id`. Do not expose arbitrary blob reads through the application façade.

- [ ] **Step 7: Verify façade and ingress race tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_application.py tests\runtime\test_ingress.py tests\runtime\test_wp5_outbox.py -q
```

Expected: all selected tests pass, including duplicate inbound submission.

- [ ] **Step 8: Commit**

```powershell
git add miniunicorn/runtime/application.py miniunicorn/runtime/ingress.py miniunicorn/runtime/realtime.py miniunicorn/runtime/models.py miniunicorn/runtime/contracts.py miniunicorn/runtime/task_service.py miniunicorn/runtime/sqlite/store.py miniunicorn/runtime/agent_adapter.py tests/runtime/test_application.py
git commit -m "feat: add durable runtime application facade"
```

---

### Task 5: Build production lightweight composition

**Files:**
- Create: `miniunicorn/runtime/bootstrap.py`
- Modify: `miniunicorn/runtime/hosts/lightweight.py`
- Modify: `miniunicorn/runtime/worker.py`
- Modify: `miniunicorn/runtime/outbox.py`
- Modify: `miniunicorn/runtime/observability.py`
- Modify: `miniunicorn/runtime/realtime.py`
- Create: `tests/runtime/test_bootstrap.py`
- Modify: `tests/runtime/test_lightweight_host.py`

**Interfaces:**
- Consumes: resolved `RuntimeConfig`, root `Config`, Store migrations, Agent factory.
- Produces:
  - `RuntimeResources`
  - `build_lightweight_runtime(config, *, provider_override=None, channel_sender=None, gateway=False, surface=None) -> RuntimeResources`
  - lifecycle-managed `RuntimeApplication`

- [ ] **Step 1: Write failing composition tests**

Create `tests/runtime/test_bootstrap.py`:

```python
import pytest

from miniunicorn.runtime.bootstrap import build_lightweight_runtime


@pytest.mark.asyncio
async def test_lightweight_bootstrap_runs_a_real_turn(runtime_root_config, fake_provider) -> None:
    resources = build_lightweight_runtime(
        runtime_root_config,
        provider_override=fake_provider,
    )
    await resources.start()
    try:
        assert resources.application is not None
        assert resources.host.worker_count == 1
        assert resources.outbox_sender.is_running
    finally:
        await resources.stop()
```

- [ ] **Step 2: Verify the test fails**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_bootstrap.py -q
```

Expected: import failure for `runtime.bootstrap`.

- [ ] **Step 3: Define lifecycle ownership**

Create:

```python
@dataclass(slots=True)
class RuntimeResources:
    application: RuntimeApplication
    host: LightweightHost
    outbox_sender: OutboxSender
    store: SqliteRuntimeStore
    connection: Closeable
    agent: Any
    shutdown_grace_s: float
    shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    _stopped: bool = False

    async def start(self) -> None:
        self.application.start_accepting()
        await self.outbox_sender.start()
        await self.host.start()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.application.stop_accepting()
        await self.host.stop(grace_s=self.shutdown_grace_s)
        await self.outbox_sender.drain(timeout_s=self.shutdown_grace_s)
        await self.outbox_sender.stop()
        await self.agent.close_mcp()
        self.connection.close()
        self.closed.set()

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()

    async def wait_for_shutdown(self) -> None:
        await self.shutdown_requested.wait()
```

Define `Closeable` as a local Protocol containing only
`def close(self) -> None`; `runtime/bootstrap.py` must not import `sqlite3`.
Make shutdown idempotent and continue closing later resources if an earlier
close fails; re-raise the first captured exception after all cleanup attempts.
Change `LightweightHost.stop(grace_s=...)` to stop new claims, await in-flight
Worker tasks up to the deadline, and cancel only after the deadline. Add
`OutboxSender.drain(timeout_s)` to wait until no immediately deliverable
claimed/pending rows remain; it must not wait forever on future retries.

- [ ] **Step 4: Implement build_lightweight_runtime**

Construction order:

```python
resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
connection = open_runtime_connection(resolved)
run_migrations(connection)
store = SqliteRuntimeStore(connection)
sessions = SessionManager(config.workspace_path)
bus = MessageBus()
agent = AgentLoop.from_config(
    config,
    bus,
    session_manager=sessions,
    provider=provider_override,
    vector_memory_factory=create_vector_store,
)
session_committer = SessionCommitter(store, sessions)
tool_gateway = ToolGateway(agent.tools, store, store)
journal = DurableTurnJournalAdapter(store, store)
realtime = RealtimeSubscriptionHub(resolved.realtime_event_queue_capacity)
callback = AgentExecutionCallback(
    agent.core_dispatcher,
    agent.turn_coordinator,
    tool_execution_port=tool_gateway,
    turn_journal=journal,
    progress_port_factory=lambda task_id: LocalProgressPort(task_id, realtime),
)
host = LightweightHost(
    store,
    session_committer,
    callback,
    worker_count=resolved.lightweight_execution_slots,
)
sender = channel_sender or LocalResultSender()
outbox = OutboxSender(store, sender)
application = RuntimeApplication(host.task_service, store, realtime)
```

The exact constructor order above matches the existing contracts:
`ToolGateway(tool_registry, execution_journal, resource_ledger)` and
`DurableTurnJournalAdapter(worker_ledger, execution_journal)`.
Here `create_vector_store` is imported from
`runtime.sqlite.vector_memory_store`, never from the Agent package.

Implement `LocalProgressPort` in `runtime/realtime.py`:

```python
class LocalProgressPort:
    def __init__(self, task_id: str, hub: RealtimeSubscriptionHub) -> None:
        self._task_id = task_id
        self._hub = hub

    async def emit(self, event: AgentEvent) -> None:
        self._hub.publish(self._task_id, serialize_agent_event(event))
```

This is a direct in-process transient fan-out, not another consumer of
`MessageBus`; therefore it cannot race ChannelManager for the same queue.

When `gateway=True`, construct ChannelManager in the same process and use it as
`channel_sender`; pass `surface` into the existing WebUI runtime-surface
arguments. When `gateway=False`, use `LocalResultSender`.

Add these exact read-only AgentLoop properties:

```python
@property
def core_dispatcher(self) -> TurnDispatcher:
    return self._turn_dispatcher


@property
def turn_coordinator(self) -> TurnCoordinator:
    return self._turn_coordinator
```

Use `provider_override` instead of the configured Provider when supplied by a
test. Bootstrap must use these accessors and must not reach private fields.

- [ ] **Step 5: Bind per-task outbound and containment ports**

Extend `AgentTaskWorker` construction with factories:

```python
message_delivery_factory: Callable[[TaskClaim], OutboundPort]
containment_factory: Callable[[str], ContainmentPort]
```

Bind them to `TurnRuntime` before Agent execution and clear them in the
Worker's existing `finally` block.

- [ ] **Step 6: Attach production observability automatically**

`RuntimeResources.start()` registers the Store and Host snapshot provider with
the existing health/status/metrics layer. Tests must no longer assign
`app["runtime_store"]` manually to prove production wiring.

- [ ] **Step 7: Verify lightweight composition**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_bootstrap.py tests\runtime\test_lightweight_host.py tests\runtime\test_session_committer.py -q
```

Expected: all selected tests pass with a real temporary SQLite database.

- [ ] **Step 8: Commit**

```powershell
git add miniunicorn/runtime/bootstrap.py miniunicorn/runtime/hosts/lightweight.py miniunicorn/runtime/worker.py miniunicorn/runtime/outbox.py miniunicorn/runtime/observability.py miniunicorn/runtime/realtime.py tests/runtime/test_bootstrap.py tests/runtime/test_lightweight_host.py
git commit -m "feat: assemble production lightweight runtime"
```

---

### Task 6: Cut one-shot CLI and OpenAI API to durable lightweight execution

**Files:**
- Modify: `miniunicorn/cli/commands.py:360-468,650-790`
- Modify: `miniunicorn/api/server.py:130-590`
- Modify: `tests/test_api_stream.py`
- Modify: `tests/test_openai_api.py`
- Create: `tests/runtime/test_production_cutover.py`

**Interfaces:**
- Consumes: `build_lightweight_runtime`, `RuntimeApplication`, `RuntimeInboundRequest`.
- Produces: CLI/API code that receives RuntimeApplication rather than AgentLoop.

- [ ] **Step 1: Write failing source-boundary tests**

Create `tests/runtime/test_production_cutover.py`:

```python
from pathlib import Path


def test_cli_does_not_call_process_direct() -> None:
    source = Path("miniunicorn/cli/commands.py").read_text(encoding="utf-8")
    assert ".process_direct(" not in source


def test_api_does_not_receive_agent_loop() -> None:
    source = Path("miniunicorn/api/server.py").read_text(encoding="utf-8")
    assert 'app["agent_loop"]' not in source
    assert "agent_loop.process_direct" not in source
```

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_production_cutover.py -q
```

Expected: both assertions fail.

- [ ] **Step 3: Cut CLI single-message execution**

Replace AgentLoop construction in `agent` with `build_lightweight_runtime`.
Inside `run_once()`:

```python
await resources.start()
try:
    result = await resources.application.submit_and_wait(
        RuntimeInboundRequest(
            content=message,
            media=(),
            metadata={"_wants_stream": True},
            session_key=session_id,
            channel="cli",
            channel_account="local-user",
            channel_message_id=None,
            scope=local_request_scope(config),
        ),
        timeout_s=config.api.timeout,
    )
    _print_agent_response(result.reply.content, render_markdown=markdown)
finally:
    await resources.stop()
```

Interactive CLI uses the same application per prompt and stops it once when
the prompt loop exits.

- [ ] **Step 4: Change create_app to accept RuntimeApplication**

Use:

```python
def create_app(
    runtime: RuntimeApplication,
    model_name: str = "MiniUnicorn",
    request_timeout: float = 120.0,
    api_key: str = "",
) -> web.Application:
    app = web.Application(
        client_max_size=20 * 1024 * 1024,
        middlewares=[_auth_middleware],
    )
    app["runtime"] = runtime
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["api_key"] = api_key
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/metrics", handle_metrics)
    return app
```

For non-streaming requests, construct `RuntimeInboundRequest`, call
`submit_and_wait`, and map:

- `COMPLETED` to HTTP 200;
- non-terminal timeout to HTTP 504;
- `FAILED` to HTTP 500 with the safe error code;
- `CANCELLED` to HTTP 409.

Do not retry an empty durable result by submitting the same user turn again.
Return the existing empty-response fallback without creating a second task.

- [ ] **Step 5: Preserve CLI streaming and SSE through task-scoped subscriptions**

Use `RuntimeApplication.subscribe(task_id)` from Task 4's realtime hub. The
lightweight Worker publishes directly to this local hub. The SSE loop consumes
only transient deltas and always reads the final durable reply after terminal
state.

For streaming callers, do not call `submit_and_wait()` first. Call `submit()`,
enter `async with application.subscribe(handle.task_id) as queue`, and run
`application.wait(...)` concurrently with queue consumption. Stop transient
consumption when the wait task reaches a terminal snapshot, then read the
durable final reply. A missed or dropped delta may affect animation only; it
must never affect the final text. Reuse this orchestration for the interactive
CLI renderer and OpenAI SSE.

- [ ] **Step 6: Own lifecycle in serve**

Build resources before `create_app`; register:

```python
async def on_startup(_app):
    await resources.start()


async def on_cleanup(_app):
    await resources.stop()
```

Remove direct MCP and AgentLoop cleanup.

- [ ] **Step 7: Verify CLI/API behavior**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_production_cutover.py tests\test_api_stream.py tests\test_openai_api.py tests\cli -q
```

Expected: selected tests pass; source contains no CLI/API `process_direct`.

- [ ] **Step 8: Commit**

```powershell
git add miniunicorn/cli/commands.py miniunicorn/api/server.py tests/runtime/test_production_cutover.py tests/test_api_stream.py tests/test_openai_api.py tests/cli
git commit -m "feat: route cli and api through durable runtime"
```

---

### Task 7: Implement real Worker and Control Plane child entrypoints

**Files:**
- Create: `miniunicorn/runtime/process_entrypoints.py`
- Modify: `miniunicorn/runtime/bootstrap.py`
- Modify: `miniunicorn/runtime/realtime.py`
- Modify: `miniunicorn/runtime/ipc.py`
- Modify: `miniunicorn/runtime/supervisor.py`
- Modify: `miniunicorn/runtime/hosts/supervised.py`
- Create: `tests/runtime/test_process_entrypoints.py`
- Modify: `tests/runtime/test_wp6_supervised.py`

**Interfaces:**
- Consumes: `ChildEntrypoint`, `ProcessIpcChannel`, root Config, lightweight component builders.
- Produces:
  - `control_plane_main(...) -> int`
  - `worker_main(...) -> int`
  - `build_control_plane_runtime(...)`
  - frozen `ChildBootstrapPayload`
  - `IpcProgressPort`
  - `RealtimeSubscriptionHub`
  - Supervisor relay from Workers to Control Plane
  - Control Plane wake request relay to Workers

- [ ] **Step 1: Write spawn/pickling and relay tests**

Create `tests/runtime/test_process_entrypoints.py`:

```python
import pickle

from miniunicorn.runtime.process_entrypoints import control_plane_main, worker_main


def test_child_entrypoints_are_top_level_picklable() -> None:
    pickle.dumps(control_plane_main)
    pickle.dumps(worker_main)


def test_supervisor_default_entrypoints_are_production_functions() -> None:
    assert control_plane_main.__module__ == "miniunicorn.runtime.process_entrypoints"
    assert worker_main.__module__ == "miniunicorn.runtime.process_entrypoints"
```

Add an integration test that sends `KIND_AGENT_EVENT` from a fake Worker pipe
and asserts the same envelope is received by the Control Plane pipe. Add a
backpressure test whose fake Control Plane send blocks: publishing more than
the configured relay capacity must return promptly, increment a dropped-event
counter, and never block a simulated Agent turn.

Add a startup-order test proving no Worker entrypoint opens the Runtime
database before the Control Plane reports ready after migrations.

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_process_entrypoints.py -q
```

Expected: import failure for `runtime.process_entrypoints`.

- [ ] **Step 3: Make Supervisor the only parent-side pipe reader**

Remove `RealtimeEventBridge._drain_all()` access to Supervisor private child
records, then delete the bridge's pipe-polling loop. No second object may call
`ProcessIpcChannel.parent_recv()`.

In `_handle_ipc`:

```python
if record.role == "worker" and env.kind in (KIND_AGENT_EVENT, KIND_TASK_PROGRESS):
    self._enqueue_control_relay(env)
    return
if record.role == "control" and env.kind == KIND_WAKE_HINT:
    self.fan_out_wake(reason=env.payload.get("reason", "submit"), task_id=env.task_id)
    return
```

`_enqueue_control_relay` uses `queue.put_nowait()` on a bounded, process-local
relay queue. One lifecycle-owned relay thread sends unchanged envelopes to the
live Control Plane child; direct `_handle_ipc` never blocks on a full pipe.
Queue full or missing Control Plane drops the realtime envelope and increments
`relay_dropped_events`. Closing the IPC handle unblocks the relay thread during
shutdown. Readiness, restart, and wake-hint handling must continue even while
the transient relay is saturated.

- [ ] **Step 4: Extend the bounded realtime hub for relayed process events**

Keep Task 4's subscription API unchanged. Add a method that accepts the
relayed IPC envelope and publishes its event payload:

```python
class RealtimeSubscriptionHub:
    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.dropped_events = 0

    @asynccontextmanager
    async def subscribe(self, task_id: str):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._capacity)
        self._queues.setdefault(task_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._queues.get(task_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._queues.pop(task_id, None)

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        for queue in tuple(self._queues.get(task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped_events += 1

    def publish_envelope(self, env: IpcEnvelope) -> None:
        if env.task_id is None:
            return
        event = (
            dict(env.payload["event"])
            if env.kind == KIND_AGENT_EVENT
            else {
                "kind": "task_progress",
                "phase": env.payload.get("phase"),
                "detail": env.payload.get("detail"),
            }
        )
        self.publish(env.task_id, event)
```

Final replies never enter this hub.

Implement one process-wide `RealtimeIpcEmitter` in each Worker with a bounded
`asyncio.Queue`. `IpcProgressPort` is constructed per claimed task and its
`emit()` only calls `put_nowait()` with
`agent_event(instance_id, task_id=task_id, event=serialize_agent_event(event))`.
One reconstructable emitter task performs the blocking
`ipc_channel.child_send()` via `asyncio.to_thread`. When the queue is full,
drop the transient event and increment `dropped_events`; never await free
space from the Agent turn. Stop it by closing IPC, cancelling the sender task,
and recording the final drop count. It never carries final content.

- [ ] **Step 5: Implement worker_main**

First define a top-level frozen `ChildBootstrapPayload` in `runtime/bootstrap.py`
with `config_json: str` and `surface: dict[str, JsonScalar]`. Validate the
surface recursively and reject callables or arbitrary objects before spawning.
Both entrypoints reconstruct `Config` with `Config.model_validate_json()`.

`worker_main`:

1. resolves Runtime paths;
2. opens its own connection and validates schema without migrating;
3. constructs SessionManager, Provider, Tool Registry, Agent adapter, Tool
   Gateway, Session Committer, Scheduler, and one AgentTaskWorker;
4. binds an IPC-backed `ProgressPort` backed by the bounded
   `RealtimeIpcEmitter`;
5. calls `ready_signal(role)` only after all process-local dependencies are
   ready;
6. runs the Worker until `KIND_SHUTDOWN`;
7. closes Agent, memory, and SQLite resources in `finally`.

Change `Supervisor.start()` to spawn the Control Plane, wait for its ready
signal with the configured deadline, then spawn Workers. A Control Plane
startup timeout terminates the partial child tree and raises; Workers must not
race first-run migrations.

The top-level function contains only:

```python
def worker_main(**kwargs: Any) -> int:
    return asyncio.run(_worker_async(**kwargs))
```

- [ ] **Step 6: Implement control_plane_main and child-local composition**

Add `build_control_plane_runtime()` to `runtime/bootstrap.py`. Unlike
`build_lightweight_runtime()`, it owns ingress, Outbox, Channels, Cron,
API/WebUI, and the realtime hub but creates no Agent and no Worker coroutine.
It accepts only a validated `Config`, the child IPC channel, and a JSON-safe
surface dictionary.

`control_plane_main`:

1. reconstructs `Config` from the serialized bootstrap payload;
2. opens its own Store connection and runs migrations before readiness;
3. calls `build_control_plane_runtime()` to construct MessageBus, TaskService,
   OutboxSender, ChannelManager, API/Web
   surface, Cron enqueue triggers, and RealtimeSubscriptionHub;
4. sends `KIND_WAKE_HINT` to the parent after every accepted submit;
5. consumes relayed Worker events from its child pipe and publishes them to
   the local hub/MessageBus;
6. reports ready only after Store, Outbox, and configured ingress bind;
7. drains and closes on `KIND_SHUTDOWN`.

Run aiohttp with `AppRunner`/`TCPSite` inside `_control_plane_async`; do not call
`web.run_app()` or nest another event loop. The IPC receive loop, Outbox,
Channels, Cron triggers, and HTTP/WebSocket surfaces are sibling lifecycle
tasks owned and cancelled/drained by the Control Plane resource object.

The top-level function contains only:

```python
def control_plane_main(**kwargs: Any) -> int:
    return asyncio.run(_control_plane_async(**kwargs))
```

- [ ] **Step 7: Verify spawn semantics and relay**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_process_entrypoints.py tests\runtime\test_wp6_supervised.py -q
```

Expected: top-level pickling, readiness, restart, relay, and no-inherited-object tests pass on Windows `spawn`.

- [ ] **Step 8: Commit**

```powershell
git add miniunicorn/runtime/process_entrypoints.py miniunicorn/runtime/bootstrap.py miniunicorn/runtime/realtime.py miniunicorn/runtime/ipc.py miniunicorn/runtime/supervisor.py miniunicorn/runtime/hosts/supervised.py tests/runtime/test_process_entrypoints.py tests/runtime/test_wp6_supervised.py
git commit -m "feat: add production control plane and worker entrypoints"
```

---

### Task 8: Assemble supervised mode with exactly three Workers

**Files:**
- Modify: `miniunicorn/runtime/bootstrap.py`
- Modify: `miniunicorn/runtime/supervisor.py:167-205`
- Modify: `miniunicorn/runtime/hosts/supervised.py:196-348`
- Modify: `miniunicorn/runtime/observability.py`
- Create: `tests/runtime/test_three_worker_acceptance.py`

**Interfaces:**
- Consumes: production child entrypoints and resolved RuntimeConfig.
- Produces:
  - `build_supervised_runtime(config) -> SupervisedRuntimeResources`
  - default `worker_count=3`
  - readiness requiring Control Plane plus configured minimum Worker capacity

- [ ] **Step 1: Write failing three-Worker acceptance tests**

Create `tests/runtime/test_three_worker_acceptance.py`:

```python
import pytest

from miniunicorn.runtime.bootstrap import build_supervised_runtime


@pytest.mark.asyncio
async def test_supervised_default_starts_three_workers(runtime_root_config) -> None:
    resources = build_supervised_runtime(runtime_root_config)
    await resources.start()
    try:
        snapshot = resources.host.snapshot()
        workers = [row for row in snapshot["children"] if row["role"] == "worker"]
        assert len(workers) == 3
        assert all(row["ready"] and row["alive"] for row in workers)
        assert resources.host.ready_workers() == 3
    finally:
        await resources.stop()
```

Configure a syntactically valid local/dummy Provider in the fixture;
readiness must not call an external model. Do not replace the production child
entrypoints with fake functions in this acceptance test.

- [ ] **Step 2: Verify the test fails with the current default of two**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_three_worker_acceptance.py -q
```

Expected: missing bootstrap function or Worker count equals two.

- [ ] **Step 3: Correct default counts**

Change `Supervisor` and `SupervisedHost` constructor defaults to:

```python
worker_count: int = 3
min_workers: int = 3
```

Production bootstrap passes `config.runtime.worker_count` explicitly and sets
`min_workers` to the same value. Tests that intentionally exercise degraded
readiness pass a smaller explicit `min_workers`.

- [ ] **Step 4: Add SupervisedRuntimeResources**

Define:

```python
@dataclass(slots=True)
class SupervisedRuntimeResources:
    host: SupervisedHost
    shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    _stopped: bool = False

    async def start(self) -> None:
        await self.host.start()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self.host.stop()
        self.closed.set()

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()

    async def wait_for_shutdown(self) -> None:
        await self.shutdown_requested.wait()
```

Build it using a frozen, top-level, picklable `ChildBootstrapPayload` containing
`config.model_dump_json()` and only JSON-safe surface fields, plus
`control_plane_main` and `worker_main`. Reconstruct `Config` inside each child;
never pass a live Config, Provider, Agent, Store, MessageBus, callable, or open
socket across `spawn`.

- [ ] **Step 5: Make readiness truthful**

Readiness is true only when:

- Runtime schema is valid;
- Control Plane is alive and ready;
- all three configured Workers are alive and ready for the default profile;
- Outbox and ingress startup succeeded.

`SupervisedHost.start()` returns successfully only after that predicate is
true. On timeout it shuts down the partial tree and raises a startup error
containing safe child-role/status details; it must not merely log and continue.

Expose `configured_workers`, `ready_workers`, and per-child restart information
through health/status without raw configuration secrets.

- [ ] **Step 6: Verify three-Worker lifecycle**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_three_worker_acceptance.py tests\runtime\test_wp6_supervised.py tests\runtime\test_wp8_cutover.py -q
```

Expected: all selected tests pass; no test manually supplies fake production
entrypoints to prove the default composition.

- [ ] **Step 7: Commit**

```powershell
git add miniunicorn/runtime/bootstrap.py miniunicorn/runtime/supervisor.py miniunicorn/runtime/hosts/supervised.py miniunicorn/runtime/observability.py tests/runtime/test_three_worker_acceptance.py tests/runtime/test_wp6_supervised.py tests/runtime/test_wp8_cutover.py
git commit -m "feat: make supervised runtime use three workers"
```

---

### Task 9: Cut Gateway, Desktop Gateway, Channels, and WebSocket to Control Plane

**Files:**
- Modify: `miniunicorn/cli/commands.py:469-650`
- Modify: `miniunicorn/cli/_gateway_runner.py:347-760`
- Modify: `miniunicorn/channels/manager.py`
- Modify: `miniunicorn/channels/websocket/channel.py`
- Modify: `miniunicorn/api/server.py`
- Test: `tests/cli/test_gateway.py`
- Test: `tests/channels/test_manager.py`
- Test: `tests/channels/test_websocket.py`
- Modify: `tests/runtime/test_production_cutover.py`

**Interfaces:**
- Consumes: `build_supervised_runtime`, Control Plane local RuntimeApplication.
- Produces: long-running launchers that start Supervisor only; ingress in the Control Plane submits through TaskService.

- [ ] **Step 1: Extend failing production-boundary tests**

Add:

```python
def test_gateway_does_not_construct_agent_loop() -> None:
    source = Path("miniunicorn/cli/_gateway_runner.py").read_text(encoding="utf-8")
    assert "AgentLoop.from_config" not in source
    assert "set_send_callback" not in source
    assert "await bus.publish_outbound(msg)" not in source


def test_runtime_composition_is_called_outside_runtime_package() -> None:
    source = Path("miniunicorn/cli/_gateway_runner.py").read_text(encoding="utf-8")
    assert "build_supervised_runtime" in source
```

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_production_cutover.py -q
```

Expected: Gateway boundary assertions fail.

- [ ] **Step 3: Reduce _run_gateway to launcher lifecycle**

Replace the existing Agent/Session/Cron/Channel construction with:

```python
def _run_gateway(config: Config, *, runtime_mode: RuntimeMode, **surface: Any) -> None:
    resources = (
        build_supervised_runtime(config, surface=surface)
        if runtime_mode == "supervised"
        else build_lightweight_runtime(
            config,
            gateway=True,
            surface=surface,
        )
    )

    async def run() -> None:
        await resources.start()
        try:
            await resources.wait_for_shutdown()
        finally:
            await resources.stop()

    asyncio.run(run())
```

The Control Plane bootstrap owns ChannelManager, Cron triggers, API/WebSocket
surface, Outbox, and model refresh integration. Console signals and the
Desktop Gateway shutdown callback call `resources.request_shutdown()`.
`open_browser_url` remains a reconstructable parent-launcher convenience:
after `resources.host.is_ready()` it opens the URL, but it is not included in
the child payload as a callable.

- [ ] **Step 4: Change Channel ingress callback**

ChannelManager receives:

```python
submit_inbound: Callable[[InboundMessage], Awaitable[TaskHandle]]
```

Inbound Channel messages call this callback. They do not publish correctness-
critical inbound work only to MessageBus. A wake hint may follow the durable
commit.

- [ ] **Step 5: Change WebSocket task control**

Cancel, steer, continue, and approval messages construct
`TaskControlRequest` and call `TaskService.control`. WebSocket code does not
mutate a live AgentLoop, pending queue, or active asyncio Task.

- [ ] **Step 6: Move model refresh to Worker reconstruction**

Configuration updates persist the new provider/model configuration and signal
Worker recycle or task-boundary refresh. Control Plane must not retain a mutable
Agent whose Provider is patched in place.

- [ ] **Step 7: Verify Gateway and Channel tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_production_cutover.py tests\cli\test_gateway.py tests\channels -q
```

Expected: launchers and ingress tests pass; source contains no Gateway
AgentLoop construction.

- [ ] **Step 8: Commit**

```powershell
git add miniunicorn/cli/commands.py miniunicorn/cli/_gateway_runner.py miniunicorn/channels/manager.py miniunicorn/channels/websocket/channel.py miniunicorn/api/server.py tests/runtime/test_production_cutover.py tests/cli/test_gateway.py tests/channels
git commit -m "feat: cut gateway ingress to supervised runtime"
```

---

### Task 10: Delete all legacy task, delivery, and maintenance authority

**Files:**
- Modify: `miniunicorn/agent/turn_dispatcher.py`
- Modify: `miniunicorn/agent/turn_persistence.py`
- Modify: `miniunicorn/agent/runner.py`
- Modify: `miniunicorn/agent/tools/message.py`
- Modify: `miniunicorn/agent/dream_trigger.py`
- Modify: `miniunicorn/cron/service.py`
- Modify: `miniunicorn/channels/manager.py`
- Modify: `miniunicorn/agent/loop.py`
- Delete if unreferenced: `miniunicorn/runtime/migration_reader.py`
- Modify: `tests/architecture/test_legacy_direct_paths_inventory.py`
- Modify: `tests/agent/test_agent_loop_structure.py`
- Modify: relevant Agent, Cron, Dream, and Channel tests

**Interfaces:**
- Consumes: production Runtime ingress, ToolExecutionPort, OutboundPort, durable maintenance submission.
- Produces: zero legacy inventory matches and no compatibility process_direct method.

- [ ] **Step 1: Convert inventory from xfail to hard assertions**

Remove every `pytest.mark.xfail` from
`test_legacy_direct_paths_inventory.py`. Change the two characterization totals:

```python
def test_inventory_count_of_process_direct_call_sites() -> None:
    source = _read(_DISPATCHER)
    assert source.count("process_direct") == 0


def test_inventory_count_of_publish_outbound_in_dispatcher() -> None:
    source = _read(_DISPATCHER)
    assert _count_pattern(source, "publish_outbound") == 0
```

Rewrite
`test_channel_manager_does_not_send_directly_without_receipt` as an AST
boundary test: calls to `channel.send(...)` are allowed only inside
`ChannelManager.send_with_receipt`; report every enclosing function name for
an offender. Do not assert that transport send disappears entirely—the
receipt adapter must invoke the actual Channel once.

- [ ] **Step 2: Verify all inventory tests fail before deletion**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\architecture\test_legacy_direct_paths_inventory.py -q
```

Expected: failures enumerate every remaining old path.

- [ ] **Step 3: Remove process-local turn ownership**

Delete from TurnDispatcher:

- `process_direct`;
- `pending_queues`;
- accepted-turn `asyncio.create_task(self._host._dispatch(...))`;
- final `publish_outbound`;
- legacy checkpoint restore/clear calls.

Keep only normalization and Agent Core execution helpers called from
`AgentExecutionCallback`. Rename the surviving interface `AgentCoreExecutor`
so it cannot accept new inbound work.

- [ ] **Step 4: Remove checkpoint writers**

Delete:

- `set_runtime_checkpoint`;
- `mark_pending_user_turn`;
- runtime calls that write `runtime_checkpoint`;
- runtime calls that write `pending_user_turn`.

Keep ordinary session transcript persistence and explicitly read-only session
format compatibility. Remove `migration_reader.py` if no production caller
remains after the hard cutover.

- [ ] **Step 5: Make ToolExecutionPort mandatory**

Remove Runner's direct branch:

```python
await tool.execute(...)
await spec.tools.execute(...)
```

At the AgentRunner boundary:

```python
if spec.tool_execution_port is None:
    raise RuntimeError("ToolExecutionPort is required")
```

Update every AgentRunSpec call site, including subagent and memory flows, to
receive the current task's port. Agent unit tests use a fake port that calls a
fake tool; production never installs a direct bypass adapter.

- [ ] **Step 6: Make OutboundPort mandatory for Message Tool**

Delete send callbacks and all fallback-to-bus/channel logic. OutboxSender is
the only caller of `ChannelManager.send_with_receipt` for durable user-visible
messages. Streaming-only Channel methods remain transient.

- [ ] **Step 7: Submit required background work**

Dream and Cron callbacks call:

```python
await task_service.submit_internal(
    build_internal_envelope(
        kind="DREAM",
        scope=scope,
        dedup_key=f"dream:{session_key}:{source_revision}",
        payload=payload,
    )
)
```

`build_internal_envelope` is the Task 4 helper with the exact signature:

```python
envelope = build_internal_envelope(
    kind="DREAM",
    scope=scope,
    session_key=session_key,
    dedup_key=f"dream:{session_key}:{source_revision}",
    payload=payload,
    priority=10,
    now_ms=int(time.time() * 1000),
)
```

Use the corresponding typed kinds and deterministic revision-based keys for
consolidation, indexing, retention, backup, blob GC, and WAL checkpoint.
Delete required-work `asyncio.create_task` ownership. Lifecycle pollers may
still create supervised tasks that can be reconstructed after restart.

- [ ] **Step 8: Remove compatibility surface**

Delete `AgentLoop.process_direct` and tests that require it. Update
`test_agent_loop_structure.py` to assert the remaining Agent Core façade only.
Remove `runtime.enabled` migration language from docstrings and tests.

- [ ] **Step 9: Verify architecture inventory and affected suites**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\architecture tests\agent tests\cron tests\channels tests\runtime -q
```

Expected:

- zero legacy-path xfails;
- no Agent→Runtime hard failure;
- no direct tool, final publish, Message Tool callback, or required-work
  create_task inventory match.

- [ ] **Step 10: Commit**

```powershell
git add miniunicorn/agent miniunicorn/cron miniunicorn/channels/manager.py miniunicorn/runtime tests/architecture tests/agent tests/cron tests/channels tests/runtime
git commit -m "refactor: remove legacy execution authority"
```

---

### Task 11: Split the SQLite implementation behind one façade

> **Superseded (2026-07-31):** Completed by remediation Task 12, commit
> `1a96b7b4` (`refactor: split sqlite runtime store by responsibility`).
> The `SqliteRuntimeStore` façade is now composed of responsibility mixins
> (`BlobStoreMixin`, `TaskStoreMixin`, `ExecutionStoreMixin`,
> `SessionStoreMixin`, `OutboxStoreMixin`, `ResourceStoreMixin`,
> `MaintenanceStoreMixin`) and is under 700 lines. See
> `tests/runtime/test_sqlite_store_modules.py` for the façade-identity and
> protocol-conformance gates.

**Files:**
- Create: `miniunicorn/runtime/sqlite/task_store.py`
- Create: `miniunicorn/runtime/sqlite/execution_store.py`
- Create: `miniunicorn/runtime/sqlite/session_store.py`
- Create: `miniunicorn/runtime/sqlite/outbox_store.py`
- Create: `miniunicorn/runtime/sqlite/resource_store.py`
- Create: `miniunicorn/runtime/sqlite/maintenance_store.py`
- Modify: `miniunicorn/runtime/sqlite/store.py`
- Modify: `miniunicorn/runtime/sqlite/__init__.py`
- Modify: `miniunicorn/runtime/observability.py`
- Modify: `tests/architecture/test_runtime_dependencies.py`
- Create: `tests/runtime/test_sqlite_store_modules.py`
- Modify: all Runtime real-SQL tests

**Interfaces:**
- Consumes: existing Runtime Store protocols and unchanged schema.
- Produces: one `SqliteRuntimeStore` façade delegating to cohesive internal components with no behavior/schema change.

- [ ] **Step 1: Write structural and protocol-conformance tests**

Create `tests/runtime/test_sqlite_store_modules.py`:

```python
from pathlib import Path

from miniunicorn.runtime.contracts import (
    DeliveryLedger,
    ExecutionJournal,
    MaintenanceLedger,
    ResourceLedger,
    SessionCommitLedger,
    TaskIngressStore,
    WorkerLedger,
)
from miniunicorn.runtime.sqlite.store import SqliteRuntimeStore


def test_store_facade_stays_focused() -> None:
    lines = Path("miniunicorn/runtime/sqlite/store.py").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) <= 700


def test_store_implements_every_narrow_view(sqlite_store: SqliteRuntimeStore) -> None:
    assert isinstance(sqlite_store, TaskIngressStore)
    assert isinstance(sqlite_store, WorkerLedger)
    assert isinstance(sqlite_store, ExecutionJournal)
    assert isinstance(sqlite_store, SessionCommitLedger)
    assert isinstance(sqlite_store, DeliveryLedger)
    assert isinstance(sqlite_store, ResourceLedger)
    assert isinstance(sqlite_store, MaintenanceLedger)
```

Also add `test_sqlite_imports_are_confined_to_runtime_sqlite()`. Parse every
production Python file with AST and assert that an `import sqlite3` or
`from sqlite3 ...` occurs only under `miniunicorn/runtime/sqlite/`. Remove
the type-only `sqlite3` import from `runtime/observability.py` by accepting a
narrow query/snapshot protocol instead of a concrete connection type.

- [ ] **Step 2: Verify the façade-size test fails**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_sqlite_store_modules.py -q
```

Expected: `store.py` exceeds 700 lines.

- [ ] **Step 3: Move cohesive method groups without changing SQL**

Move exact public method groups:

- `task_store.py`: `submit_task` through task events, claim/lease/state/control,
  retry promotion, reclaim, session slot release;
- `session_store.py`: `prepare_session_commit` through `read_session_commit`;
- `execution_store.py`: restore point, model attempts, tool calls and attempts;
- `resource_store.py`: acquire/renew/release/read resource lease;
- `outbox_store.py`: claim delivery through `enqueue_message_tool_outbox`, plus
  `read_final_reply`;
- `maintenance_store.py`: retention batches, blob GC, WAL checkpoint, backup;
- keep blob primitives in the façade or a private shared `BlobStore` used by
  execution/outbox/maintenance components.

Copy SQL and transaction boundaries byte-for-byte first. Do not refactor query
semantics during the move.

- [ ] **Step 4: Use explicit component composition**

`SqliteRuntimeStore.__init__` creates components with the same connection:

```python
self.tasks = SqliteTaskStore(conn)
self.execution = SqliteExecutionStore(conn, blobs=self.blobs)
self.sessions = SqliteSessionStore(conn, blobs=self.blobs)
self.outbox = SqliteOutboxStore(conn, blobs=self.blobs)
self.resources = SqliteResourceStore(conn)
self.maintenance = SqliteMaintenanceStore(conn, blobs=self.blobs)
```

Façade methods delegate one line each. Cross-table atomic completion stays in
the component that owns the complete use case; do not compose one transaction
from multiple public component calls.

- [ ] **Step 5: Run each real-SQL suite after its move**

Run after each method group:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_ingress.py tests\runtime\test_state_machine.py -q
.venv\Scripts\python.exe -m pytest tests\runtime\test_session_commit.py -q
.venv\Scripts\python.exe -m pytest tests\runtime\test_worker_ledger.py tests\runtime\test_wp4_provider_tool_gateway.py -q
.venv\Scripts\python.exe -m pytest tests\runtime\test_wp5_outbox.py -q
.venv\Scripts\python.exe -m pytest tests\runtime\test_wp7_maintenance.py -q
```

Expected: every command passes before moving the next group.

- [ ] **Step 6: Verify the complete Store contract**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\runtime\test_sqlite_store_modules.py tests\runtime -q
```

Expected: façade is at most 700 lines and all Runtime tests pass.

- [ ] **Step 7: Commit**

```powershell
git add miniunicorn/runtime/sqlite miniunicorn/runtime/observability.py tests/runtime tests/architecture/test_runtime_dependencies.py
git commit -m "refactor: split sqlite runtime store by responsibility"
```

---

### Task 12: Prove parity, crash recovery, load behavior, and clean acceptance

> **Superseded (2026-07-31):** Completed by remediation Tasks 13-16.
> Golden-flow parity and crash-boundary fault gates live in
> `tests/runtime/test_runtime_golden_flow.py` and
> `tests/runtime/test_runtime_fault_injection.py` (remediation Task 14,
> commit `def6dd2e`). The 1,000-task load gate and bounded soak harness
> live in `tests/runtime/test_runtime_load.py` and
> `scripts/runtime_soak.py` (remediation Task 15, commit `d0d795db`).
> Duplicate-path cleanup is commit `53aa9a26`. Operator documentation is
> updated by remediation Task 16.

**Files:**
- Create: `tests/runtime/test_runtime_load.py`
- Create: `scripts/runtime_soak.py`
- Modify: `tests/runtime/test_three_worker_acceptance.py`
- Modify: `tests/runtime/test_wp8_cutover.py`
- Modify: `miniunicorn/runtime/observability.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `docs/configuration.md`
- Modify: `docs/deployment.md`
- Modify: `docs/concurrency.md`
- Modify: `docs/cli-reference.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: final lightweight/supervised composition and observability.
- Produces: repeatable golden-flow, failure-injection, 1,000-task load, full-suite, documentation, and packaging gates.

- [ ] **Step 1: Add lightweight/supervised golden-flow comparison**

Start one local OpenAI-compatible stub HTTP server and parameterize the same
configured Provider/tool scenario over both modes. The stub returns a
deterministic tool call against a temporary workspace and then a deterministic
final answer; no fake Provider object crosses a process boundary. Normalize
volatile ids/timestamps and assert equality of:

```python
assert facts == {
    "task_state": "COMPLETED",
    "session_sequences": [1],
    "model_states": ["COMPLETED"],
    "tool_states": ["SUCCEEDED"],
    "session_commit_kinds": ["INBOUND", "FINAL"],
    "outbox_states": ["DELIVERED"],
}
```

- [ ] **Step 2: Add three-session concurrency and one-session serialization**

Use barrier endpoints on the local stub Provider server. The server records
request start/end by session and releases requests only after three distinct
sessions arrive; do not pass multiprocessing Events or test callables through
the production child payload.

```python
assert max_distinct_sessions_running >= 3
assert max_same_session_running == 1
assert completed_session_sequences["same-session"] == [1, 2, 3]
```

- [ ] **Step 3: Add crash-injection cases**

Kill the owning Worker PID after claim, after model-completion journal, after
tool effect, after session prepare, and after Outbox enqueue. Trigger each kill
by polling the corresponding durable row or an external tool-effect marker;
do not add timing sleeps or production-only test callbacks. For each case
assert the exact terminal/recovery state:

- stale lease writes rejected;
- replay-safe results reused;
- uncertain non-idempotent effect becomes `WAITING_USER`;
- inbound and final session commits appear once;
- final Outbox row appears once.

- [ ] **Step 4: Implement the 1,000-task load test**

Create `tests/runtime/test_runtime_load.py` marked `@pytest.mark.load`:

```python
TASKS = 1000
SESSIONS = 100
WORKERS = 3


@pytest.mark.load
def test_three_worker_mixed_load(load_harness) -> None:
    report = load_harness.run(tasks=TASKS, sessions=SESSIONS, workers=WORKERS)
    assert report.accepted_tasks == TASKS
    assert report.completed_tasks == TASKS
    assert report.missing_final_replies == 0
    assert report.duplicate_acceptance == 0
    assert report.session_order_violations == 0
    assert report.stale_lease_commits == 0
    assert report.orphan_child_processes == 0
```

The harness writes a JSON report with claim latency, SQLite busy count, maximum
queue age, Outbox age, WAL bytes, RSS, handle count, Worker restarts, and
dropped realtime events. Do not assert a machine-specific latency number;
assert correctness counters and record performance values for comparison.

- [ ] **Step 5: Add repeatable soak command**

Register pytest markers and document:

```powershell
.venv\Scripts\python.exe -m pytest -m load tests\runtime\test_runtime_load.py -q
.venv\Scripts\python.exe scripts\runtime_soak.py --hours 24 --workers 3 --report runtime-soak.json
```

`runtime_soak.py` uses the same harness, rotates bounded temporary sessions,
injects Worker/Channel failures, and exits nonzero on any correctness counter.

- [ ] **Step 6: Validate container configuration**

Ensure the supervised profile:

```yaml
command: ["gateway", "--runtime-mode", "supervised"]
deploy:
  resources:
    limits:
      cpus: "2"
      memory: 2G
      pids: 512
stop_grace_period: 75s
```

Ensure YAML uses one merge-key mapping per service rather than duplicate `<<`
keys. Run:

```powershell
docker compose config --quiet
```

Expected: exit zero on a machine with Docker. CI must run this command even if
the local executor lacks Docker.

- [ ] **Step 7: Update operator documentation**

Document only:

- durable lightweight mode;
- supervised mode with three Workers;
- CLI/env/config precedence;
- Runtime database and backup paths;
- readiness and metrics;
- graceful shutdown;
- Worker restart/recovery;
- load and soak commands.

Remove `runtime.enabled=false`, legacy checkpoint rollback, and direct
AgentLoop instructions.

- [ ] **Step 8: Run final backend gates**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\architecture -q
.venv\Scripts\python.exe -m pytest tests\runtime -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -m load tests\runtime\test_runtime_load.py -q
```

Expected:

- architecture: zero failures and zero legacy xfails;
- Runtime: zero failures;
- full backend: zero collection errors and zero failures;
- load: 1,000 accepted/completed with zero correctness violations.

- [ ] **Step 9: Run final WebUI and build gates**

Run:

```powershell
Set-Location webui
npm test -- --reporter=dot
npm run lint
npm run build
Set-Location ..
```

Expected: 295 or more tests pass, lint exits zero, and production build exits
zero. Record circular-chunk warnings separately; they do not block this
3 Worker cutover unless the build exits nonzero.

- [ ] **Step 10: Verify forbidden source patterns**

Run:

```powershell
rg -n "runtime\.enabled|process_direct|pending_queues|set_runtime_checkpoint|mark_pending_user_turn|await tool\.execute|spec\.tools\.execute|set_send_callback" miniunicorn
```

Expected: no production matches. Test fixtures and historical design documents
are outside this command's scope.

Run:

```powershell
rg -n "LightweightHost|SupervisedHost|build_lightweight_runtime|build_supervised_runtime" miniunicorn --glob "!**/runtime/**"
```

Expected: production launcher/composition call sites exist outside the Runtime
package.

- [ ] **Step 11: Commit final acceptance**

```powershell
git add tests/runtime/test_runtime_load.py tests/runtime/test_three_worker_acceptance.py tests/runtime/test_wp8_cutover.py miniunicorn/runtime/observability.py .env.example docker-compose.yml docs/configuration.md docs/deployment.md docs/concurrency.md docs/cli-reference.md scripts/runtime_soak.py pyproject.toml
git commit -m "test: prove three-worker runtime cutover"
```

## Final Review Checklist

- [ ] Root config contains Runtime settings and no `enabled` switch.
- [ ] `gateway` defaults to supervised and starts three ready Worker children.
- [ ] `agent` and `serve` use durable lightweight execution.
- [ ] API, Channel, WebSocket, Cron, Dream, and maintenance ingress use TaskService.
- [ ] Agent Core has no Runtime import.
- [ ] Runner has no direct tool execution.
- [ ] Message Tool and final replies use Outbox only.
- [ ] TurnDispatcher has no pending task authority or direct final publication.
- [ ] Legacy checkpoint writers are absent.
- [ ] Architecture inventory has no xfails.
- [ ] SQLite façade is focused and every narrow protocol still passes real-SQL tests.
- [ ] Lightweight and supervised durable facts match.
- [ ] Three different sessions run concurrently; one session never overlaps.
- [ ] Worker and Control Plane crash tests pass.
- [ ] 1,000-task load gate reports zero correctness violations.
- [ ] Backend, WebUI, lint, build, and container configuration gates pass.
