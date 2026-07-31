"""WP4 — Provider journaling and Tool Gateway tests (design §19, §20, §30 WP4).

Covers:

- Provider attempt observer records every retry/fallback attempt in order;
- Tool Gateway journals a logical tool call and returns durable references
  plus the in-memory content echo;
- Tool Gateway approval policy surfaces ``WAITING_APPROVAL`` without
  invoking the tool;
- Resource ledger serializes concurrent exclusive tools;
- stale Worker (wrong lease epoch) cannot commit a tool result.

These tests use the real :class:`SqliteRuntimeStore` for the
ExecutionJournal / ResourceLedger views and a stub ToolRegistry so the
durable safety layer is exercised without the full Agent Core.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.agent.ports import (
    EffectiveToolPolicy,
    NullProviderAttemptObserver,
    ProviderAttemptCompleted,
    ProviderAttemptFailed,
    ProviderAttemptStarted,
    SafeError,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from miniunicorn.runtime.contracts import ClaimRequest
from miniunicorn.runtime.durable_journal import JournalProviderObserver
from miniunicorn.runtime.models import (
    ModelAttemptWrite,
    ResourceLeaseRequest,
)
from miniunicorn.runtime.session_committer import set_active_claim, clear_active_claim
from miniunicorn.runtime.tool_gateway import (
    ToolGateway,
    build_tool_execution_request,
    compute_arguments_hash,
    compute_idempotency_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingObserver:
    """Records every started/completed/failed call for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def started(self, value: ProviderAttemptStarted) -> str:
        aid = f"att-{len(self.events)}"
        self.events.append(("started", value))
        return aid

    async def completed(self, attempt_id: str, value: ProviderAttemptCompleted) -> None:
        self.events.append(("completed", attempt_id, value))

    async def failed(self, attempt_id: str, value: ProviderAttemptFailed) -> None:
        self.events.append(("failed", attempt_id, value))


class _StubTool:
    """Minimal tool satisfying the ToolGateway invocation contract."""

    def __init__(self, name: str, result: Any = "ok", *, read_only: bool = True) -> None:
        self._name = name
        self._result = result
        self.read_only = read_only
        self.invocations = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        self.invocations += 1
        return self._result

    # Effective tool policy (design §20.1)
    @property
    def effect_class(self) -> str:
        return "READ" if self.read_only else "EXTERNAL_WRITE"

    @property
    def risk_class(self) -> str:
        return "LOW" if self.read_only else "HIGH"

    @property
    def idempotency_mode(self) -> str:
        return "REPLAY_SAFE" if self.read_only else "NONE"

    @property
    def approval_policy(self) -> str:
        return "NEVER" if self.read_only else "POLICY"

    @property
    def recovery_policy(self) -> str:
        return "REPLAY" if self.read_only else "MANUAL"

    @property
    def concurrency_scope(self) -> str:
        return "NONE"

    @property
    def progress_required(self) -> bool:
        return False

    @property
    def timeout_s(self) -> int | None:
        return None


class _StubRegistry:
    """Minimal registry satisfying the ToolGateway lookup contract."""

    def __init__(self, tools: dict[str, _StubTool] | None = None) -> None:
        self._tools = tools or {}

    def get(self, name: str) -> _StubTool | None:
        return self._tools.get(name)


def _claim_running_task(store: Any, sample_scope: Any, make_inbound_envelope: Any) -> Any:
    """Submit, claim, mark-running a task and return the active claim."""
    # Use real-time now_ms so the lease deadline is in the future for
    # downstream mutations that call _validate_lease with real-time
    # _now_ms() (Task 2 Step 5 deadline fencing).
    now_ms = int(time.time() * 1000)
    env = make_inbound_envelope(sample_scope, session_key="wp4-session")
    submit = store.submit_task(env)
    assert submit.status == "ACCEPTED"
    result = store.claim_next(ClaimRequest(worker_id="wp4-worker", now_ms=now_ms, lease_ms=60_000))
    assert result.claimed is not None
    claim = result.claimed.claim
    store.mark_running(claim, now_ms=now_ms + 1)
    return claim


# ---------------------------------------------------------------------------
# Provider attempt observer
# ---------------------------------------------------------------------------


class TestProviderAttemptObserver:
    """Design §19: every retry and fallback attempt is journaled."""

    @pytest.mark.asyncio
    async def test_null_observer_is_noop(self) -> None:
        observer = NullProviderAttemptObserver()
        aid = await observer.started(
            ProviderAttemptStarted(
                provider_name="p", model_name="m", request_hash="h", started_at_ms=1
            )
        )
        await observer.completed(aid, ProviderAttemptCompleted(response_blob_id="b", response_hash="r"))
        await observer.failed(aid, ProviderAttemptFailed(error_code="E", error_summary="s"))
        # No exception means success; the null observer must never raise.

    @pytest.mark.asyncio
    async def test_journal_observer_records_attempts_in_order(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.ports import TaskIdentity
        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime

        runtime = TurnRuntime(turn_id="t1", session_key="wp4-session", task_id=claim.task_id)
        token = bind_turn_runtime(runtime)
        try:
            from miniunicorn.runtime.durable_journal import DurableTurnJournalAdapter

            journal = DurableTurnJournalAdapter(worker_ledger=store, execution_journal=store)
            observer = JournalProviderObserver(journal)
            observer.begin_logical_call()

            # Simulate two network attempts: first fails, second succeeds.
            aid1 = await observer.started(
                ProviderAttemptStarted(
                    provider_name="openai", model_name="gpt", request_hash="rh", started_at_ms=100
                )
            )
            await observer.failed(
                aid1, ProviderAttemptFailed(error_code="TIMEOUT", error_summary="timed out")
            )
            aid2 = await observer.started(
                ProviderAttemptStarted(
                    provider_name="openai", model_name="gpt", request_hash="rh", started_at_ms=200
                )
            )
            await observer.completed(
                aid2,
                ProviderAttemptCompleted(
                    response_blob_id="blob:1",
                    response_hash="rh2",
                    input_tokens=10,
                    output_tokens=20,
                    finish_reason="stop",
                    content="model response text",
                ),
            )

            # Both attempts must be durable in the model_attempts table.
            from miniunicorn.runtime.sqlite.store import SqliteRuntimeStore

            assert isinstance(store, SqliteRuntimeStore)
            rows = store._conn.execute(
                "SELECT attempt_no, state FROM model_attempts WHERE task_id = ? ORDER BY attempt_no",
                (claim.task_id,),
            ).fetchall()
            states = {row[0]: row[1] for row in rows}
            assert states.get(1) == "FAILED"
            assert states.get(2) == "COMPLETED"
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)


# ---------------------------------------------------------------------------
# Tool Gateway
# ---------------------------------------------------------------------------


class TestToolGateway:
    """Design §20: durable tool execution through ToolExecutionPort."""

    @pytest.mark.asyncio
    async def test_success_returns_content_and_durable_refs(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime

        runtime = TurnRuntime(turn_id="t1", session_key="wp4-session", task_id=claim.task_id)
        token = bind_turn_runtime(runtime)
        try:
            tool = _StubTool("read_file", result="file contents", read_only=True)
            registry = _StubRegistry({"read_file": tool})
            gateway = ToolGateway(registry, store, store)

            request = build_tool_execution_request(
                task_id=claim.task_id,
                tool_call_id="call-1",
                tool_name="read_file",
                arguments={"path": "/x"},
            )
            result = await gateway.execute(request)

            assert result.state == "SUCCEEDED"
            assert result.content == "file contents"
            assert result.result_blob_id is not None
            assert result.result_hash is not None
            assert tool.invocations == 1

            # The logical tool call must be durable.
            rows = store._conn.execute(
                "SELECT state, attempt_count FROM tool_calls WHERE tool_call_id = ?",
                ("call-1",),
            ).fetchall()
            assert [tuple(r) for r in rows] == [("SUCCEEDED", 1)]
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)

    @pytest.mark.asyncio
    async def test_failed_attempt_is_journaled(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """A READ tool that fails mid-invocation is a definite FAILED (Task 6 Step 4).

        READ tools cannot leave external effects, so any exception is a
        definite pre-invocation-or-runtime failure that may be safely
        recorded as FAILED and retried. Effectful tools that fail
        mid-invocation are covered separately as OUTCOME_UNKNOWN.
        """
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime

        runtime = TurnRuntime(turn_id="t1", session_key="wp4-session", task_id=claim.task_id)
        token = bind_turn_runtime(runtime)
        try:
            class _BoomTool(_StubTool):
                async def execute(self, **kwargs: Any) -> Any:
                    self.invocations += 1
                    raise RuntimeError("boom")

            tool = _BoomTool("read_file", read_only=True)
            registry = _StubRegistry({"read_file": tool})
            gateway = ToolGateway(registry, store, store)

            # Pass an explicit READ policy so effect_may_have_happened
            # returns False (READ tools cannot leave external effects).
            read_policy = EffectiveToolPolicy(
                effect_class="READ",
                risk_class="LOW",
                idempotency_mode="REPLAY_SAFE",
                approval_policy="NEVER",
                recovery_policy="REPLAY",
                concurrency_scope="NONE",
            )
            request = build_tool_execution_request(
                task_id=claim.task_id,
                tool_call_id="call-2",
                tool_name="read_file",
                arguments={"path": "/y"},
                policy=read_policy,
            )
            result = await gateway.execute(request)

            assert result.state == "FAILED"
            assert result.error is not None
            assert result.error.error_code == "TOOL_RETRYABLE"
            assert tool.invocations == 1

            rows = store._conn.execute(
                "SELECT state FROM tool_calls WHERE tool_call_id = ?",
                ("call-2",),
            ).fetchall()
            assert [tuple(r) for r in rows] == [("FAILED",)]
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)

    @pytest.mark.asyncio
    async def test_effectful_failure_becomes_outcome_unknown(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """An effectful tool that fails mid-invocation is OUTCOME_UNKNOWN (Task 6 Step 4).

        ``effect_may_have_happened`` returns True for non-READ tools raising
        anything other than the pre-invocation exceptions. The attempt must
        be marked OUTCOME_UNKNOWN (not FAILED) and the task enters
        WAITING_USER for manual recovery.
        """
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime

        runtime = TurnRuntime(turn_id="t1", session_key="wp4-session", task_id=claim.task_id)
        token = bind_turn_runtime(runtime)
        try:
            class _BoomEffectTool(_StubTool):
                async def execute(self, **kwargs: Any) -> Any:
                    self.invocations += 1
                    raise RuntimeError("effect maybe happened")

            tool = _BoomEffectTool("write_file", read_only=False)
            registry = _StubRegistry({"write_file": tool})
            gateway = ToolGateway(registry, store, store)

            request = build_tool_execution_request(
                task_id=claim.task_id,
                tool_call_id="call-effect",
                tool_name="write_file",
                arguments={"path": "/y", "content": "z"},
            )
            result = await gateway.execute(request)

            assert result.state == "OUTCOME_UNKNOWN"
            assert result.error is not None
            assert result.error.error_code == "TOOL_OUTCOME_UNKNOWN"
            assert tool.invocations == 1

            rows = store._conn.execute(
                "SELECT state FROM tool_calls WHERE tool_call_id = ?",
                ("call-effect",),
            ).fetchall()
            assert [tuple(r) for r in rows] == [("OUTCOME_UNKNOWN",)]

            # Task must be in WAITING_USER for manual recovery.
            row = store._conn.execute(
                "SELECT state FROM tasks WHERE task_id=?",
                (claim.task_id,),
            ).fetchone()
            assert row["state"] == "WAITING_USER"
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)

    def test_idempotency_key_is_stable(self) -> None:
        args = {"path": "/a", "n": 1}
        h = compute_arguments_hash(args)
        k1 = compute_idempotency_key("t1", "c1", "read_file", h)
        k2 = compute_idempotency_key("t1", "c1", "read_file", h)
        assert k1 == k2
        # Different tool name -> different key.
        k3 = compute_idempotency_key("t1", "c1", "write_file", h)
        assert k1 != k3


# ---------------------------------------------------------------------------
# Recovery decision matrix (Task 6 Step 1)
# ---------------------------------------------------------------------------


def _seed_tool_call_state(
    store: Any,
    claim: Any,
    *,
    tool_call_id: str,
    state: str,
    policy: EffectiveToolPolicy,
    args: dict[str, Any] | None = None,
) -> None:
    """Seed a durable tool_calls row in ``state`` for recovery-matrix tests.

    Drives the store through the same methods the gateway uses so the
    resulting row matches a real prior attempt the gateway must observe.
    """
    from miniunicorn.runtime.models import (
        BlobWrite,
        PreparedToolWrite,
        ToolAttemptWrite,
        ToolResultWrite,
    )

    args = args or {"path": "/x"}
    args_bytes = __import__("json").dumps(args, sort_keys=True).encode("utf-8")
    args_hash = __import__("hashlib").sha256(args_bytes).hexdigest()
    blob = store.write_blob(
        BlobWrite(
            scope_key=f"task/{claim.task_id}",
            blob_kind="TOOL_ARGUMENTS",
            content_hash=args_hash,
            encoding="RAW_BYTES",
            inline_content=args_bytes,
            size_bytes=len(args_bytes),
        )
    )

    write = PreparedToolWrite(
        tool_call_id=tool_call_id,
        tool_name="read_file",
        arguments_blob_id=blob.blob_id,
        arguments_hash=args_hash,
        effect_class=policy.effect_class,
        risk_class=policy.risk_class,
        idempotency_mode=policy.idempotency_mode,
        idempotency_key=f"key:{tool_call_id}",
        approval_policy=policy.approval_policy,
        recovery_policy=policy.recovery_policy,
        concurrency_scope=policy.concurrency_scope,
        created_at_ms=1_000_000,
    )
    record = store.prepare_tool_call(claim, write)

    if state == "WAITING_APPROVAL":
        # The default approval_policy=NEVER produces PREPARED on insert.
        # Force the row into WAITING_APPROVAL so the gateway observes it
        # on lookup (Task 6 Step 1 matrix).
        store._conn.execute(
            "UPDATE tool_calls SET state='WAITING_APPROVAL', updated_at_ms=? "
            "WHERE task_id=? AND tool_call_id=?",
            (1_000_002, claim.task_id, tool_call_id),
        )
        return

    attempt = store.begin_tool_attempt(
        claim,
        ToolAttemptWrite(
            tool_call_id=tool_call_id,
            attempt_no=record.attempt_count + 1,
            resource_token=None,
            started_at_ms=1_000_001,
        ),
    )

    if state == "RUNNING":
        return  # leave the attempt open

    if state == "OUTCOME_UNKNOWN":
        store.mark_tool_unknown(
            claim,
            attempt.tool_attempt_id,
            SafeError(error_code="TOOL_OUTCOME_UNKNOWN", error_summary="ambiguous"),
            1_000_002,
        )
        return

    if state == "REJECTED":
        # ``tool_attempts.state`` does not allow REJECTED (only
        # STARTED/SUCCEEDED/FAILED/OUTCOME_UNKNOWN). Mark the logical
        # tool_calls row REJECTED directly and leave the attempt FAILED
        # so the gateway observes the rejection on lookup.
        store.finish_tool_attempt(
            claim,
            attempt.tool_attempt_id,
            ToolResultWrite(
                state="FAILED",
                error=SafeError(error_code="TOOL_REJECTED", error_summary="seeded"),
                finished_at_ms=1_000_002,
            ),
        )
        store._conn.execute(
            "UPDATE tool_calls SET state='REJECTED', "
            "error_code='TOOL_REJECTED', error_summary='seeded', "
            "updated_at_ms=? WHERE task_id=? AND tool_call_id=?",
            (1_000_002, claim.task_id, tool_call_id),
        )
        return

    if state in ("SUCCEEDED", "FAILED"):
        result_blob_id = ""
        result_hash = ""
        error: SafeError | None = None
        if state == "SUCCEEDED":
            result_bytes = b'"file contents"'
            result_hash = __import__("hashlib").sha256(result_bytes).hexdigest()
            result_blob = store.write_blob(
                BlobWrite(
                    scope_key=f"task/{claim.task_id}",
                    blob_kind="TOOL_RESULT",
                    content_hash=result_hash,
                    encoding="RAW_BYTES",
                    inline_content=result_bytes,
                    size_bytes=len(result_bytes),
                )
            )
            result_blob_id = result_blob.blob_id
        else:
            error = SafeError(
                error_code="TOOL_PRIOR_FAILURE",
                error_summary="seeded",
            )
        store.finish_tool_attempt(
            claim,
            attempt.tool_attempt_id,
            ToolResultWrite(
                state=state,
                result_blob_id=result_blob_id or None,
                result_hash=result_hash or None,
                error=error,
                finished_at_ms=1_000_002,
            ),
        )


class TestToolGatewayRecoveryMatrix:
    """Design §20.4: existing durable state decides reuse / retry / unknown.

    Each row matches the table in Task 6 Step 1. Non-invocation rows must
    not call ``tool.execute``; the gateway returns the prior terminal
    state (or surfaces ``WAITING_USER`` for ambiguous states).
    """

    @pytest.mark.parametrize(
        "existing_state,policy_kwargs,expected_state,expected_invocations",
        [
            ("SUCCEEDED", {}, "SUCCEEDED", 0),
            ("FAILED", {"idempotency_mode": "REPLAY_SAFE", "recovery_policy": "REPLAY"}, "SUCCEEDED", 1),
            ("RUNNING", {"idempotency_mode": "NATIVE_KEY", "recovery_policy": "REUSE_RESULT"}, "SUCCEEDED", 1),
            ("RUNNING", {"idempotency_mode": "NONE", "recovery_policy": "MANUAL"}, "OUTCOME_UNKNOWN", 0),
            ("OUTCOME_UNKNOWN", {}, "OUTCOME_UNKNOWN", 0),
            ("WAITING_APPROVAL", {}, "WAITING_APPROVAL", 0),
            ("REJECTED", {}, "REJECTED", 0),
        ],
        ids=[
            "succeeded-reuse",
            "failed-retry-safe",
            "running-native-idempotent",
            "running-manual-unknown",
            "outcome-unknown-waiting",
            "waiting-approval",
            "rejected-return",
        ],
    )
    @pytest.mark.asyncio
    async def test_recovery_matrix(
        self,
        store: Any,
        sample_scope: Any,
        make_inbound_envelope: Any,
        existing_state: str,
        policy_kwargs: dict[str, Any],
        expected_state: str,
        expected_invocations: int,
    ) -> None:
        from unittest.mock import AsyncMock

        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import (
            TurnRuntime,
            bind_turn_runtime,
            reset_turn_runtime,
        )

        runtime = TurnRuntime(turn_id="t1", session_key="wp4-session", task_id=claim.task_id)
        token = bind_turn_runtime(runtime)
        try:
            base_policy: dict[str, Any] = dict(
                effect_class="READ",
                risk_class="LOW",
                idempotency_mode="REPLAY_SAFE",
                approval_policy="NEVER",
                recovery_policy="REPLAY",
                concurrency_scope="NONE",
            )
            base_policy.update(policy_kwargs)
            policy = EffectiveToolPolicy(**base_policy)

            # Seed the durable row in the existing state.
            _seed_tool_call_state(
                store,
                claim,
                tool_call_id="call-matrix",
                state=existing_state,
                policy=policy,
            )

            tool = _StubTool("read_file", result="replayed", read_only=True)
            tool.execute = AsyncMock(return_value="replayed")  # type: ignore[assignment]
            registry = _StubRegistry({"read_file": tool})
            gateway = ToolGateway(registry, store, store)

            request = build_tool_execution_request(
                task_id=claim.task_id,
                tool_call_id="call-matrix",
                tool_name="read_file",
                arguments={"path": "/x"},
                policy=policy,
            )
            result = await gateway.execute(request)

            assert result.state == expected_state, (
                f"state={result.state!r} expected={expected_state!r} "
                f"existing={existing_state!r}"
            )
            assert tool.execute.await_count == expected_invocations, (
                f"await_count={tool.execute.await_count} expected={expected_invocations} "
                f"existing={existing_state!r}"
            )

            # WAITING_USER rows: the task must be marked WAITING_USER when
            # the gateway surfaces OUTCOME_UNKNOWN for an unrecoverable state.
            if existing_state in ("OUTCOME_UNKNOWN",) or (
                existing_state == "RUNNING" and expected_state == "OUTCOME_UNKNOWN"
            ):
                row = store._conn.execute(
                    "SELECT state, waiting_reason FROM tasks WHERE task_id=?",
                    (claim.task_id,),
                ).fetchone()
                assert row["state"] == "WAITING_USER", (
                    f"task should be WAITING_USER for {existing_state!r}, got {row['state']!r}"
                )
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)


# ---------------------------------------------------------------------------
# Subagent effect recovery (Task 6 Step 9)
# ---------------------------------------------------------------------------


class TestSubagentEffectRecovery:
    """Task 6 Step 9: subagent tool calls that crash after the external
    effect but before result commit must surface OUTCOME_UNKNOWN with
    exactly one invocation (no automatic replay).

    The subagent's derived port routes through the root task's
    ``ToolGateway`` so the same durable safety layer applies. The
    post-invoke hook simulates a Worker crash between the external
    effect and the durable result commit.
    """

    @pytest.mark.asyncio
    async def test_subagent_crash_after_effect_is_outcome_unknown(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import (
            TurnRuntime,
            bind_turn_runtime,
            reset_turn_runtime,
        )

        runtime = TurnRuntime(
            turn_id="t1",
            session_key="wp4-session",
            task_id=claim.task_id,
        )
        token = bind_turn_runtime(runtime)
        try:
            # Effectful tool that succeeds — the external effect happens
            # during tool.execute(), before the crash hook fires.
            tool = _StubTool("write_file", result="written", read_only=False)
            registry = _StubRegistry({"write_file": tool})
            gateway = ToolGateway(registry, store, store)

            # Inject a crash after the tool returns its value but before
            # finish_tool_attempt commits (Task 6 Step 9). The hook fires
            # inside the try block, so the except path classifies the
            # crash via effect_may_have_happened.
            crash_calls: list[str] = []

            def _crash_hook(request: Any, attempt_id: str) -> None:
                crash_calls.append(attempt_id)
                raise RuntimeError("simulated crash after effect")

            gateway._post_invoke_hook = _crash_hook

            # Bind the gateway as the root task's tool_execution_port so
            # SubagentManager._resolve_tool_execution_port would derive
            # from it. Here we call derive() directly to test the
            # derived port path in isolation.
            runtime.tool_execution_port = gateway

            # Derive a subagent port (Task 6 Step 6). The lineage
            # namespaces call IDs so root and subagent attempts cannot
            # collide.
            subagent_port = gateway.derive("sub:sub1")

            # Build an effectful request through the derived port. The
            # policy is non-idempotent (NONE) and effectful
            # (EXTERNAL_WRITE) so the crash is ambiguous.
            effect_policy = EffectiveToolPolicy(
                effect_class="EXTERNAL_WRITE",
                risk_class="HIGH",
                idempotency_mode="NONE",
                approval_policy="NEVER",
                recovery_policy="MANUAL",
                concurrency_scope="NONE",
            )
            request = build_tool_execution_request(
                task_id=claim.task_id,
                tool_call_id="provider-call-1",
                tool_name="write_file",
                arguments={"path": "/y", "content": "z"},
                policy=effect_policy,
            )
            result = await subagent_port.execute(request)

            # Exactly one external effect — the tool ran once, the crash
            # happened after, and the gateway must not auto-replay.
            assert tool.invocations == 1, (
                f"expected exactly 1 invocation (one external effect), "
                f"got {tool.invocations}"
            )
            assert len(crash_calls) == 1, (
                f"expected exactly 1 crash injection, got {len(crash_calls)}"
            )

            # Result is OUTCOME_UNKNOWN (not FAILED, not SUCCEEDED).
            assert result.state == "OUTCOME_UNKNOWN"
            assert result.error is not None
            assert result.error.error_code == "TOOL_OUTCOME_UNKNOWN"

            # The durable tool_calls row for the derived (namespaced)
            # call ID must be OUTCOME_UNKNOWN.
            rows = store._conn.execute(
                "SELECT state FROM tool_calls WHERE task_id=? AND state='OUTCOME_UNKNOWN'",
                (claim.task_id,),
            ).fetchall()
            assert rows, (
                "expected at least one OUTCOME_UNKNOWN tool_calls row for the "
                "derived subagent call"
            )

            # Task must be in WAITING_USER for manual recovery.
            row = store._conn.execute(
                "SELECT state FROM tasks WHERE task_id=?",
                (claim.task_id,),
            ).fetchone()
            assert row["state"] == "WAITING_USER", (
                f"expected WAITING_USER for manual recovery, got {row['state']!r}"
            )
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)

    @pytest.mark.asyncio
    async def test_subagent_reuse_after_crash_does_not_reinvoke(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        """Resuming after a crash must not re-invoke the tool (Task 6 Step 9).

        When the root task resumes and the subagent port is called again
        with the same call ID, the gateway must observe the existing
        OUTCOME_UNKNOWN row and surface it without re-invoking — the
        effect may or may not have happened, and automatic replay is
        forbidden for non-idempotent tools.
        """
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import (
            TurnRuntime,
            bind_turn_runtime,
            reset_turn_runtime,
        )

        runtime = TurnRuntime(
            turn_id="t1",
            session_key="wp4-session",
            task_id=claim.task_id,
        )
        token = bind_turn_runtime(runtime)
        try:
            tool = _StubTool("write_file", result="written", read_only=False)
            registry = _StubRegistry({"write_file": tool})
            gateway = ToolGateway(registry, store, store)

            # First call: crash after effect.
            def _crash_hook(request: Any, attempt_id: str) -> None:
                raise RuntimeError("simulated crash after effect")

            gateway._post_invoke_hook = _crash_hook
            runtime.tool_execution_port = gateway

            subagent_port = gateway.derive("sub:sub2")
            effect_policy = EffectiveToolPolicy(
                effect_class="EXTERNAL_WRITE",
                risk_class="HIGH",
                idempotency_mode="NONE",
                approval_policy="NEVER",
                recovery_policy="MANUAL",
                concurrency_scope="NONE",
            )
            request = build_tool_execution_request(
                task_id=claim.task_id,
                tool_call_id="provider-call-2",
                tool_name="write_file",
                arguments={"path": "/z", "content": "w"},
                policy=effect_policy,
            )
            first = await subagent_port.execute(request)
            assert first.state == "OUTCOME_UNKNOWN"
            assert tool.invocations == 1

            # Second call (resume): the gateway must observe the existing
            # OUTCOME_UNKNOWN row and return without re-invoking.
            gateway._post_invoke_hook = None  # Remove the crash hook.
            second = await subagent_port.execute(request)
            assert second.state == "OUTCOME_UNKNOWN"
            assert tool.invocations == 1, (
                "resume must not re-invoke the tool; the existing "
                "OUTCOME_UNKNOWN row must be reused"
            )
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)


# ---------------------------------------------------------------------------
# Resource ledger serialization
# ---------------------------------------------------------------------------


class TestResourceLedger:
    """Design §16.14, §20: exclusive tools serialize via resource leases."""

    def test_exclusive_lease_blocks_second_holder(self, store: Any) -> None:
        now = 1_000_000
        req1 = ResourceLeaseRequest(
            resource_key="tool:shell:workspace",
            holder_kind="TASK",
            holder_id="task-A",
            units=1,
            lease_ms=300_000,
            now_ms=now,
        )
        lease1 = store.acquire_resource(req1)
        assert lease1 is not None

        # Same resource, different task, overlapping window -> denied.
        req2 = ResourceLeaseRequest(
            resource_key="tool:shell:workspace",
            holder_kind="TASK",
            holder_id="task-B",
            units=1,
            lease_ms=300_000,
            now_ms=now + 1_000,
        )
        lease2 = store.acquire_resource(req2)
        assert lease2 is None

    def test_releasing_lease_allows_next_holder(self, store: Any) -> None:
        now = 1_000_000
        req1 = ResourceLeaseRequest(
            resource_key="tool:shell",
            holder_kind="TASK",
            holder_id="task-A",
            units=1,
            lease_ms=300_000,
            now_ms=now,
        )
        lease1 = store.acquire_resource(req1)
        assert lease1 is not None
        assert store.release_resource(lease1) is True

        req2 = ResourceLeaseRequest(
            resource_key="tool:shell",
            holder_kind="TASK",
            holder_id="task-B",
            units=1,
            lease_ms=300_000,
            now_ms=now + 1_000,
        )
        lease2 = store.acquire_resource(req2)
        assert lease2 is not None

    def test_expired_lease_is_reclaimed(self, store: Any) -> None:
        now = 1_000_000
        # Acquire with a short lease.
        req1 = ResourceLeaseRequest(
            resource_key="tool:shell",
            holder_kind="TASK",
            holder_id="task-A",
            units=1,
            lease_ms=1_000,
            now_ms=now,
        )
        lease1 = store.acquire_resource(req1)
        assert lease1 is not None

        # After expiry, a new holder may acquire.
        req2 = ResourceLeaseRequest(
            resource_key="tool:shell",
            holder_kind="TASK",
            holder_id="task-B",
            units=1,
            lease_ms=1_000,
            now_ms=now + 5_000,
        )
        lease2 = store.acquire_resource(req2)
        assert lease2 is not None


# ---------------------------------------------------------------------------
# Stale lease fencing
# ---------------------------------------------------------------------------


class TestStaleLeaseFencing:
    """Design §6.10, §6.11: a stale Worker cannot commit tool results."""

    @pytest.mark.asyncio
    async def test_stale_claim_cannot_prepare_tool_call(
        self, store: Any, sample_scope: Any, make_inbound_envelope: Any
    ) -> None:
        claim = _claim_running_task(store, sample_scope, make_inbound_envelope)
        set_active_claim(claim.task_id, claim)

        from miniunicorn.agent.turn_runtime import TurnRuntime, bind_turn_runtime, reset_turn_runtime
        from miniunicorn.runtime.contracts import StaleLeaseError
        from miniunicorn.runtime.models import PreparedToolWrite

        runtime = TurnRuntime(turn_id="t1", session_key="wp4-session", task_id=claim.task_id)
        token = bind_turn_runtime(runtime)
        try:
            # Forge a stale claim with a wrong epoch.
            from miniunicorn.runtime.models import TaskClaim

            stale_claim = TaskClaim(
                task_id=claim.task_id,
                lease_token=claim.lease_token,
                lease_epoch=claim.lease_epoch + 999,
                leased_by="stale-worker",
                lease_until_ms=claim.lease_until_ms,
            )
            write = PreparedToolWrite(
                tool_call_id="call-stale",
                tool_name="read_file",
                arguments_blob_id="blob:x",
                arguments_hash="h",
                effect_class="READ",
                risk_class="LOW",
                idempotency_mode="REPLAY_SAFE",
                idempotency_key="k",
                approval_policy="NEVER",
                recovery_policy="REPLAY",
                concurrency_scope="NONE",
                created_at_ms=1,
            )
            with pytest.raises(StaleLeaseError):
                store.prepare_tool_call(stale_claim, write)
        finally:
            reset_turn_runtime(token)
            clear_active_claim(claim.task_id)


# ---------------------------------------------------------------------------
# Child-process containment (WP4 task 9, design §20.7)
# ---------------------------------------------------------------------------


class TestContainmentScope:
    """Child-process containment scope tests (design §20.7)."""

    def test_null_scope_is_noop(self) -> None:
        """NullContainmentScope register/close are no-ops."""
        from miniunicorn.runtime.containment import NullContainmentScope

        scope = NullContainmentScope()
        scope.register(12345)
        scope.close()
        scope.close()  # Idempotent.

    def test_process_scope_tracks_pids(self) -> None:
        """ProcessContainmentScope tracks registered PIDs."""
        from miniunicorn.runtime.containment import ProcessContainmentScope

        scope = ProcessContainmentScope(task_id="test-task")
        scope.register(100)
        scope.register(200)
        scope.register(100)  # Duplicate is ignored.
        assert scope._pids == {100, 200}

    def test_process_scope_close_is_idempotent(self) -> None:
        """Closing a scope twice is safe."""
        from miniunicorn.runtime.containment import ProcessContainmentScope

        scope = ProcessContainmentScope(task_id="test-task")
        scope.close()
        scope.close()  # Must not raise.

    def test_register_after_close_is_ignored(self) -> None:
        """Registering a PID after close is a no-op (no raise)."""
        from miniunicorn.runtime.containment import ProcessContainmentScope

        scope = ProcessContainmentScope(task_id="test-task")
        scope.close()
        scope.register(999)  # Must not raise.
        assert scope._pids == set()

    def test_context_var_binding(self) -> None:
        """current_containment_scope returns the bound scope."""
        from miniunicorn.runtime.containment import (
            NullContainmentScope,
            bind_containment_scope,
            current_containment_scope,
            reset_containment_scope,
        )

        assert current_containment_scope() is None
        scope = NullContainmentScope()
        token = bind_containment_scope(scope)
        try:
            assert current_containment_scope() is scope
        finally:
            reset_containment_scope(token)
        assert current_containment_scope() is None

    def test_child_process_tree_dies_on_close(self) -> None:
        """Spawned child process is terminated when the scope closes.

        This is the WP4 exit test: "child process tree dies with Worker".
        Uses a long-sleeping child process so we can verify it is alive
        before close and gone after close.
        """
        import sys

        import asyncio

        from miniunicorn.runtime.containment import (
            ProcessContainmentScope,
            bind_containment_scope,
            current_containment_scope,
            reset_containment_scope,
        )

        async def _run() -> None:
            scope = ProcessContainmentScope(task_id="test-kill")
            token = bind_containment_scope(scope)
            try:
                # Spawn a long-sleeping child process.
                if sys.platform == "win32":
                    proc = await asyncio.create_subprocess_exec(
                        "ping", "-n", "60", "127.0.0.1",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        "sleep", "60",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )

                # Register and verify the scope tracks it.
                scope.register(proc.pid)
                assert current_containment_scope() is scope

                # Verify the child is alive.
                assert proc.returncode is None

                # Close the scope — should terminate the child.
                scope.close()

                # Give the OS a moment to reap the process.
                await asyncio.sleep(0.5)

                # The child should no longer be running.
                assert proc.returncode is not None
            finally:
                reset_containment_scope(token)

        asyncio.run(_run())
