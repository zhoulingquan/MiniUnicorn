"""Tool Gateway — durable ToolExecutionPort implementation (design §8.5, §20).

The Tool Gateway is the single path through which the Agent Core executes
tools when the durable runtime is enabled. It implements
:class:`~miniunicorn.agent.ports.ToolExecutionPort` and orchestrates:

- logical tool call preparation (durable ``tool_calls`` row);
- approval policy (``WAITING_APPROVAL`` → ``WAITING_USER`` task state);
- resource lease acquisition for exclusive/concurrent tools;
- attempt journaling (``tool_attempts`` rows);
- recovery decisions (reuse, retry, mark unknown);
- result durability (response only after the store commits).

Design §20.4 policy outcomes: ``ALLOW``, ``WAIT_APPROVAL``, ``DENY``,
``REUSE``, ``MANUAL_RECOVERY``.

The gateway does NOT duplicate the Tool Registry — it uses the registry
for schema validation and actual invocation, adding the durable safety
layer around it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from loguru import logger

from miniunicorn.agent.ports import (
    SafeError,
    ToolExecutionPort,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from miniunicorn.runtime.models import (
    PreparedToolWrite,
    ToolAttemptWrite,
    ToolResultWrite,
)

if TYPE_CHECKING:
    from miniunicorn.agent.tools.registry import ToolRegistry
    from miniunicorn.runtime.contracts import ExecutionJournal, ResourceLedger, WorkerLedger


# ---------------------------------------------------------------------------
# Tool policy classification exceptions (Task 6 Step 4)
# ---------------------------------------------------------------------------


class ToolValidationError(Exception):
    """Tool arguments failed schema validation (definite pre-invocation)."""


class ToolNotFoundError(Exception):
    """The requested tool name is not registered (definite pre-invocation)."""


class ToolPreflightError(Exception):
    """A tool preflight check refused the call (definite pre-invocation)."""


def effect_may_have_happened(exc: BaseException, policy: Any) -> bool:
    """Decide whether an exception may have left an external effect (Task 6 Step 4).

    Pre-invocation failures (validation, not-found, preflight refusal) are
    definite — no effect happened. READ-only tools cannot have side
    effects. Anything else is ambiguous and must surface as
    ``OUTCOME_UNKNOWN`` rather than an ordinary retryable failure.
    """
    return policy.effect_class != "READ" and not isinstance(
        exc, (ToolValidationError, ToolNotFoundError, ToolPreflightError)
    )


class ToolGateway:
    """Durable :class:`ToolExecutionPort` implementation (design §8.5, §20).

    Constructed by the LightweightHost (or Supervised Host) with the
    Runtime Store's ``ExecutionJournal`` and ``ResourceLedger`` views,
    plus the existing :class:`ToolRegistry` for actual tool invocation.

    The gateway is stateless between calls — all durable state lives in
    the Runtime Store.
    """

    def __init__(
        self,
        tool_registry: "ToolRegistry",
        execution_journal: "ExecutionJournal",
        resource_ledger: "ResourceLedger | None" = None,
        worker_ledger: "WorkerLedger | None" = None,
    ) -> None:
        self._registry = tool_registry
        self._journal = execution_journal
        self._resources = resource_ledger
        # WorkerLedger is required to enter WAITING_USER when a tool's
        # outcome is ambiguous (Task 6 Step 4). Falls back to the journal
        # when it also implements WorkerLedger (single-façade store).
        self._worker_ledger = worker_ledger
        # Test-only fault hook: if set, called after the tool returns its
        # value but before finish_tool_attempt commits. Simulates a crash
        # between external effect and result commit (Task 6 Step 9).
        self._post_invoke_hook: Any = None

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """Execute one logical tool call through the durable path.

        Returns a :class:`ToolExecutionResult` carrying the terminal
        logical state, result references, or a safe error. The Runner
        receives this only after the Runtime Store commits.
        """
        # Step 1: Check for an existing durable tool call (recovery).
        existing = self._check_existing_call(request)
        if existing is not None:
            return existing

        # Step 2: Prepare the logical tool call durably (idempotent).
        tool_record = self._prepare_call(request)

        # Step 3: Check approval policy after prepare (re-checked state).
        if tool_record.state == "WAITING_APPROVAL":
            return ToolExecutionResult(
                state="WAITING_APPROVAL",
                error=SafeError(
                    error_code="TOOL_APPROVAL_REQUIRED",
                    error_summary=f"Tool {request.tool_name} requires approval",
                ),
            )

        # Step 4: Acquire resource lease if needed.
        resource_token = self._acquire_resource(request)

        # Step 5: Begin attempt.
        attempt_no = tool_record.attempt_count + 1
        attempt = self._begin_attempt(request, attempt_no, resource_token)

        # Step 6: Execute the tool with try/finally for resource release.
        # Resource is released only after the terminal write commits
        # (Task 6 Step 5). On ambiguous effectful failure, the attempt is
        # marked OUTCOME_UNKNOWN and the task enters WAITING_USER (Step 4).
        try:
            result_value = await self._invoke_tool(request)
            # Test-only crash injection (Task 6 Step 9). The hook fires
            # after the tool has produced its effect but before the
            # result is durably committed.
            if self._post_invoke_hook is not None:
                self._post_invoke_hook(request, attempt.tool_attempt_id)
            # Step 7: Finish attempt with result (terminal commit first).
            result = self._build_result(request, result_value)
            self._finish_attempt(request, attempt.tool_attempt_id, result)
            return ToolExecutionResult(
                state=result.state,
                result_blob_id=result.result_blob_id,
                result_hash=result.result_hash,
                effect_receipt_ref=result.effect_receipt_ref,
                error=result.error,
                # Ephemeral in-memory echo for the Agent Core (design §20). The
                # durable fact is the blob reference committed above.
                content=result_value,
            )
        except Exception as exc:
            ambiguous = effect_may_have_happened(exc, request.policy)
            if ambiguous:
                # Ambiguous effectful failure: mark OUTCOME_UNKNOWN and
                # surface WAITING_USER so the user decides whether the
                # effect happened (Task 6 Step 4).
                self._mark_outcome_unknown_and_wait(
                    request, attempt.tool_attempt_id, exc
                )
                # Task 14 Step 7: expose recovery metrics.
                from miniunicorn.runtime.observability import get_runtime_metrics

                get_runtime_metrics().inc("tool_outcome_unknown_total")
                return ToolExecutionResult(
                    state="OUTCOME_UNKNOWN",
                    error=SafeError(
                        error_code="TOOL_OUTCOME_UNKNOWN",
                        error_summary=str(exc)[:500],
                    ),
                )
            # Definite pre-invocation failure: record as FAILED.
            self._finish_attempt(
                request,
                attempt.tool_attempt_id,
                ToolResultWrite(
                    state="FAILED",
                    error=SafeError(
                        error_code="TOOL_RETRYABLE",
                        error_summary=str(exc)[:500],
                    ),
                ),
            )
            return ToolExecutionResult(
                state="FAILED",
                error=SafeError(
                    error_code="TOOL_RETRYABLE",
                    error_summary=str(exc)[:500],
                ),
            )
        finally:
            # Resource release is deferred until after the terminal write
            # commits so a cancelled Worker leaves the durable attempt
            # recoverable instead of writing a false FAILED (Task 6 Step 5).
            self._release_resource(request, resource_token)

    # ------------------------------------------------------------------
    # Recovery: check existing durable tool call state (Task 6 Step 3)
    # ------------------------------------------------------------------

    def _check_existing_call(
        self, request: ToolExecutionRequest
    ) -> ToolExecutionResult | None:
        """Inspect the durable ``tool_calls`` row and decide recovery action.

        Returns a :class:`ToolExecutionResult` for terminal / unrecoverable
        states, or ``None`` when the call is new or safe to retry.
        """
        record = self._journal.read_tool_call(request.task_id, request.tool_call_id)
        if record is None:
            return None  # New call — proceed to prepare.

        state = record.state

        if state == "SUCCEEDED" and record.result_blob_id:
            content = self._journal.read_tool_result_content(record.result_blob_id)
            return ToolExecutionResult(
                state="SUCCEEDED",
                result_blob_id=record.result_blob_id,
                result_hash=record.result_hash,
                effect_receipt_ref=record.effect_receipt_ref,
                content=content,
            )

        if state == "WAITING_APPROVAL":
            return ToolExecutionResult(
                state="WAITING_APPROVAL",
                error=SafeError(
                    error_code="TOOL_APPROVAL_REQUIRED",
                    error_summary=f"Tool {request.tool_name} requires approval",
                ),
            )

        if state == "REJECTED":
            return ToolExecutionResult(
                state="REJECTED",
                error=record.error
                or SafeError(
                    error_code="TOOL_REJECTED",
                    error_summary=f"Tool {request.tool_name} was rejected",
                ),
            )

        if state == "OUTCOME_UNKNOWN":
            # Already ambiguous from a prior run — surface WAITING_USER
            # without re-invoking. The tool_calls row is already terminal.
            self._enter_waiting_user(request, request.tool_call_id, "TOOL_OUTCOME_UNKNOWN")
            # Task 14 Step 7: expose recovery metrics.
            from miniunicorn.runtime.observability import get_runtime_metrics

            get_runtime_metrics().inc("tool_outcome_unknown_total")
            return ToolExecutionResult(
                state="OUTCOME_UNKNOWN",
                error=record.error
                or SafeError(
                    error_code="TOOL_OUTCOME_UNKNOWN",
                    error_summary="Tool outcome is unknown; manual recovery required",
                ),
            )

        if state == "FAILED":
            # Retry-safe policies proceed to a new attempt.
            if self._is_retry_safe(request.policy):
                return None
            return ToolExecutionResult(
                state="FAILED",
                error=record.error
                or SafeError(
                    error_code="TOOL_PRIOR_FAILURE",
                    error_summary=f"Tool {request.tool_name} previously failed",
                ),
            )

        if state == "RUNNING":
            # Native idempotency (and other safe modes) may retry with
            # the same idempotency key. Non-idempotent / manual policies
            # cannot retry safely — mark OUTCOME_UNKNOWN and surface
            # WAITING_USER (Task 6 Step 1 matrix).
            if self._is_retry_safe(request.policy) and request.policy.idempotency_mode != "NONE":
                return None  # Retry with the same idempotency key.
            self._mark_running_unknown_and_wait(request, record)
            return ToolExecutionResult(
                state="OUTCOME_UNKNOWN",
                error=SafeError(
                    error_code="TOOL_OUTCOME_UNKNOWN",
                    error_summary="Tool was RUNNING; manual recovery required",
                ),
            )

        # PREPARED: proceed to invoke.
        return None

    # ------------------------------------------------------------------
    # Recovery helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_retry_safe(policy: Any) -> bool:
        """A policy is retry-safe when idempotency or recovery policy allows replay."""
        return policy.idempotency_mode in ("REPLAY_SAFE", "NATIVE_KEY", "RUNTIME_RESULT") or (
            policy.recovery_policy in ("REPLAY", "REUSE_RESULT", "QUERY_THEN_RETRY")
        )

    def _mark_outcome_unknown_and_wait(
        self, request: ToolExecutionRequest, attempt_id: str, exc: BaseException
    ) -> None:
        """Mark the current attempt OUTCOME_UNKNOWN and enter WAITING_USER.

        Used when an effectful tool raises mid-invocation: the attempt is
        terminal (we cannot retry safely) and the user must decide whether
        the effect happened (Task 6 Step 4).
        """
        claim = self._get_claim()
        if claim is None:
            return  # Nothing durable to write without a claim.
        try:
            self._journal.mark_tool_unknown(
                claim,
                attempt_id,
                SafeError(
                    error_code="TOOL_OUTCOME_UNKNOWN",
                    error_summary=str(exc)[:500],
                ),
                _now_ms(),
            )
        except Exception:
            logger.exception("mark_tool_unknown failed for attempt {}", attempt_id)
        self._enter_waiting_user(request, request.tool_call_id, "TOOL_OUTCOME_UNKNOWN")

    def _mark_running_unknown_and_wait(
        self, request: ToolExecutionRequest, record: Any
    ) -> None:
        """Mark a previously-RUNNING call OUTCOME_UNKNOWN and enter WAITING_USER.

        Used when a recovered RUNNING call cannot be retried safely. The
        previous attempt is left as STARTED (lease/containment will reap
        it) and the logical tool_calls row is marked OUTCOME_UNKNOWN via
        ``mark_tool_unknown`` on the most recent attempt.
        """
        claim = self._get_claim()
        if claim is None:
            self._enter_waiting_user(request, record.tool_call_id, "TOOL_OUTCOME_UNKNOWN")
            return
        # Find the open attempt (if any) to mark OUTCOME_UNKNOWN. If the
        # row was left in RUNNING without an attempt, just enter WAITING_USER.
        attempt_id = self._find_open_attempt(claim.task_id, record.tool_call_id)
        if attempt_id is not None:
            try:
                self._journal.mark_tool_unknown(
                    claim,
                    attempt_id,
                    SafeError(
                        error_code="TOOL_OUTCOME_UNKNOWN",
                        error_summary="Recovered RUNNING call; manual recovery required",
                    ),
                    _now_ms(),
                )
            except Exception:
                logger.exception("mark_tool_unknown failed for running call {}", record.tool_call_id)
        self._enter_waiting_user(request, record.tool_call_id, "TOOL_OUTCOME_UNKNOWN")

    def _find_open_attempt(self, task_id: str, tool_call_id: str) -> str | None:
        """Return the most recent STARTED attempt id for a tool call, if any."""
        try:
            row = self._journal._conn.execute(  # type: ignore[attr-defined]
                "SELECT tool_attempt_id FROM tool_attempts "
                "WHERE task_id=? AND tool_call_id=? AND state='STARTED' "
                "ORDER BY attempt_no DESC LIMIT 1",
                (task_id, tool_call_id),
            ).fetchone()
            return row["tool_attempt_id"] if row else None
        except Exception:
            return None

    def _enter_waiting_user(
        self, request: ToolExecutionRequest, ref: str, reason: str
    ) -> None:
        """Transition the task to ``WAITING_USER`` with a recovery prompt.

        Best-effort: if no WorkerLedger is bound or the transition fails
        (e.g. stale lease), the tool_calls row remains in its terminal
        state and the caller still receives the OUTCOME_UNKNOWN result.
        """
        ledger = self._worker_ledger
        if ledger is None:
            # Fall back to the journal if it implements WorkerLedger
            # (single-façade store).
            ledger = self._journal if hasattr(self._journal, "enter_waiting_user") else None
        if ledger is None:
            return
        claim = self._get_claim()
        if claim is None:
            return
        prompt = (
            f"Tool '{request.tool_name}' requires manual recovery: {reason}. "
            "Confirm whether the effect happened before retrying."
        ).encode("utf-8")
        prompt_hash = hashlib.sha256(prompt).hexdigest()
        try:
            from miniunicorn.runtime.models import BlobWrite, WaitDecision

            blob = self._journal.write_blob(  # type: ignore[attr-defined]
                BlobWrite(
                    scope_key=f"task/{request.task_id}",
                    blob_kind="RECOVERY_PROMPT",
                    content_hash=prompt_hash,
                    encoding="RAW_BYTES",
                    inline_content=prompt,
                    size_bytes=len(prompt),
                )
            )
            wait = WaitDecision(
                waiting_reason=reason,
                waiting_ref=ref,
                wait_until_ms=None,
                prompt_blob_id=blob.blob_id,
                prompt_hash=prompt_hash,
                prompt_dedup_key=f"recover:{ref}",
                control_token=uuid.uuid4().hex,
            )
            ledger.enter_waiting_user(claim, wait)
        except Exception:
            logger.exception("enter_waiting_user failed for tool call {}", ref)

    # ------------------------------------------------------------------
    # Subagent derivation (Task 6 Step 6)
    # ------------------------------------------------------------------

    def derive(self, lineage: str) -> ToolExecutionPort:
        """Return a derived port scoped to ``lineage`` (Task 6 Step 6).

        Subagents call this on the root task's port to obtain a gateway
        that namespaces ``tool_call_id`` by lineage. The derived port
        shares the durable store and registry; only the call ID is
        prefixed so root and subagent attempts cannot collide.
        """
        return _DerivedToolGateway(self, lineage)

    # ------------------------------------------------------------------
    # Preparation
    # ------------------------------------------------------------------

    def _prepare_call(self, request: ToolExecutionRequest) -> Any:
        """Persist the logical tool call and return the store record.

        Stores normalized arguments as a blob and inserts the
        ``tool_calls`` row via :meth:`ExecutionJournal.prepare_tool_call`.
        """
        from miniunicorn.runtime.models import BlobWrite

        # The execution journal is the SqliteRuntimeStore, which also
        # implements BlobStore. We store arguments inline.
        store = self._journal  # type: ignore[assignment]
        args_bytes = json.dumps(
            request.normalized_arguments, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        args_hash = hashlib.sha256(args_bytes).hexdigest()

        # Use the store's write_blob if available; otherwise use the hash.
        try:
            blob = store.write_blob(  # type: ignore[attr-defined]
                BlobWrite(
                    scope_key=f"task/{request.task_id}",
                    blob_kind="TOOL_ARGUMENTS",
                    content_hash=args_hash,
                    encoding="RAW_BYTES",
                    inline_content=args_bytes,
                    size_bytes=len(args_bytes),
                )
            )
            args_blob_id = blob.blob_id
        except AttributeError:
            # Store doesn't expose write_blob on this view; use a synthetic id.
            args_blob_id = f"inline:{args_hash[:16]}"

        # Get the claim from the session committer context.
        claim = self._get_claim()
        if claim is None:
            raise RuntimeError(
                "ToolGateway.execute requires an active task claim"
            )

        write = PreparedToolWrite(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            arguments_blob_id=args_blob_id,
            arguments_hash=request.arguments_hash,
            effect_class=request.policy.effect_class,
            risk_class=request.policy.risk_class,
            idempotency_mode=request.policy.idempotency_mode,
            idempotency_key=request.idempotency_key,
            approval_policy=request.policy.approval_policy,
            recovery_policy=request.policy.recovery_policy,
            concurrency_scope=request.policy.concurrency_scope,
            created_at_ms=_now_ms(),
        )
        return self._journal.prepare_tool_call(claim, write)

    # ------------------------------------------------------------------
    # Resource lease
    # ------------------------------------------------------------------

    def _acquire_resource(
        self, request: ToolExecutionRequest
    ) -> str | None:
        """Acquire a resource lease for an exclusive tool, if needed.

        Returns a lease token, or ``None`` when no resource lease is
        required.
        """
        if self._resources is None:
            return None
        if request.policy.concurrency_scope == "NONE":
            return None

        from miniunicorn.runtime.models import ResourceLeaseRequest

        resource_key = self._resource_key(request)
        request_obj = ResourceLeaseRequest(
            resource_key=resource_key,
            holder_kind="TASK",
            holder_id=request.task_id,
            units=1,
            lease_ms=300_000,  # 5-minute default lease
            now_ms=_now_ms(),
        )
        lease = self._resources.acquire_resource(request_obj)
        if lease is None:
            raise RuntimeError(
                f"Resource capacity exceeded for {resource_key}"
            )
        return lease.lease_token

    def _release_resource(
        self, request: ToolExecutionRequest, token: str | None
    ) -> None:
        """Release the resource lease after tool completion."""
        if self._resources is None or token is None:
            return
        from miniunicorn.runtime.models import ResourceLease

        resource_key = self._resource_key(request)
        lease = ResourceLease(
            resource_key=resource_key,
            holder_kind="TASK",
            holder_id=request.task_id,
            units=1,
            lease_token=token,
            lease_until_ms=0,
        )
        self._resources.release_resource(lease)

    def _resource_key(self, request: ToolExecutionRequest) -> str:
        """Build a resource key from the concurrency scope."""
        scope = request.policy.concurrency_scope
        if scope == "GLOBAL":
            return f"tool:{request.tool_name}"
        if scope == "WORKSPACE":
            return f"tool:{request.tool_name}:workspace"
        if scope == "SESSION":
            return f"tool:{request.tool_name}:session:{request.task_id}"
        return f"tool:{request.tool_name}"

    # ------------------------------------------------------------------
    # Attempt journaling
    # ------------------------------------------------------------------

    def _begin_attempt(
        self,
        request: ToolExecutionRequest,
        attempt_no: int,
        resource_token: str | None,
    ) -> Any:
        """Begin a durable tool attempt."""
        claim = self._get_claim()
        write = ToolAttemptWrite(
            tool_call_id=request.tool_call_id,
            attempt_no=attempt_no,
            resource_token=resource_token,
            started_at_ms=_now_ms(),
        )
        return self._journal.begin_tool_attempt(claim, write)

    def _finish_attempt(
        self,
        request: ToolExecutionRequest,
        attempt_id: str,
        result: ToolResultWrite,
    ) -> None:
        """Finish a durable tool attempt with the terminal result."""
        claim = self._get_claim()
        self._journal.finish_tool_attempt(claim, attempt_id, result)

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    async def _invoke_tool(self, request: ToolExecutionRequest) -> Any:
        """Invoke the tool through the registry.

        Binds the current ``tool_call_id`` to the task context so tools
        that need it (e.g. MessageTool for its Outbox dedup key) can
        retrieve it without a hard runtime dependency (design §20.6, WP5).
        """
        from miniunicorn.runtime.session_committer import (
            clear_active_tool_call_id,
            set_active_tool_call_id,
        )

        tool = self._registry.get(request.tool_name)
        if tool is None:
            # Definite pre-invocation failure (Task 6 Step 4): a missing
            # tool cannot have produced an effect.
            raise ToolNotFoundError(f"Tool not found: {request.tool_name}")
        set_active_tool_call_id(request.task_id, request.tool_call_id)
        try:
            return await tool.execute(**request.normalized_arguments)
        finally:
            clear_active_tool_call_id(request.task_id)

    def _build_result(
        self,
        request: ToolExecutionRequest,
        result_value: Any,
    ) -> ToolResultWrite:
        """Build a :class:`ToolResultWrite` from the raw tool output."""
        from miniunicorn.runtime.models import BlobWrite

        # Serialize the result.
        if isinstance(result_value, str):
            result_str = result_value
        else:
            result_str = json.dumps(result_value, default=str, ensure_ascii=False)
        result_bytes = result_str.encode("utf-8")
        result_hash = hashlib.sha256(result_bytes).hexdigest()

        # Store the result as a blob.
        store = self._journal  # type: ignore[assignment]
        try:
            blob = store.write_blob(  # type: ignore[attr-defined]
                BlobWrite(
                    scope_key=f"task/{request.task_id}",
                    blob_kind="TOOL_RESULT",
                    content_hash=result_hash,
                    encoding="RAW_BYTES",
                    inline_content=result_bytes,
                    size_bytes=len(result_bytes),
                )
            )
            result_blob_id = blob.blob_id
        except AttributeError:
            result_blob_id = f"inline:{result_hash[:16]}"

        return ToolResultWrite(
            state="SUCCEEDED",
            result_blob_id=result_blob_id,
            result_hash=result_hash,
            finished_at_ms=_now_ms(),
        )

    # ------------------------------------------------------------------
    # Claim context
    # ------------------------------------------------------------------

    def _get_claim(self) -> Any:
        """Retrieve the active TaskClaim from the Worker's claim context."""
        from miniunicorn.agent.turn_runtime import current_turn_runtime
        from miniunicorn.runtime.session_committer import _get_claim_for_task

        runtime = current_turn_runtime()
        if runtime is None or runtime.task_id is None:
            return None
        return _get_claim_for_task(runtime.task_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Current UTC Unix milliseconds (design §12.2)."""
    import time

    return int(time.time() * 1000)


def compute_idempotency_key(
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments_hash: str,
) -> str:
    """Compute the stable idempotency key for a tool call (design §20.3).

    Default: ``sha256(task_id + tool_call_id + tool_name + arguments_hash)``.
    """
    raw = f"{task_id}\n{tool_call_id}\n{tool_name}\n{arguments_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_arguments_hash(normalized_arguments: dict[str, Any]) -> str:
    """Compute a canonical SHA-256 hash of normalized tool arguments."""
    raw = json.dumps(normalized_arguments, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_tool_execution_request(
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    policy: Any | None = None,
) -> ToolExecutionRequest:
    """Build a :class:`ToolExecutionRequest` from raw tool call arguments.

    Convenience helper for the Runner. When ``policy`` is ``None``, the
    policy is derived from the tool's declarative metadata via the
    :class:`ToolRegistry`.
    """
    from miniunicorn.agent.ports import EffectiveToolPolicy

    args_hash = compute_arguments_hash(arguments)
    idempotency_key = compute_idempotency_key(
        task_id, tool_call_id, tool_name, args_hash
    )

    if policy is None:
        # Default conservative policy; the Runner should look up the
        # tool's metadata from the registry to get the real policy.
        policy = EffectiveToolPolicy(
            effect_class="EXTERNAL_WRITE",
            risk_class="HIGH",
            idempotency_mode="NONE",
            approval_policy="POLICY",
            recovery_policy="MANUAL",
            concurrency_scope="NONE",
        )

    return ToolExecutionRequest(
        task_id=task_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        normalized_arguments=arguments,
        arguments_hash=args_hash,
        policy=policy,
        idempotency_key=idempotency_key,
    )


class _DerivedToolGateway:
    """ToolExecutionPort derived from a :class:`ToolGateway` for subagents.

    Shares the parent gateway's registry, journal, and resource ledger.
    Only the ``tool_call_id`` is namespaced by lineage so root and
    subagent attempts cannot collide (Task 6 Step 6). Derived call IDs
    are stable per ``(root_task_id, lineage, provider_tool_call_id)``.
    """

    def __init__(self, parent: "ToolGateway", lineage: str) -> None:
        self._parent = parent
        self._lineage = lineage

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        from dataclasses import replace

        derived_id = _derived_tool_call_id(
            request.task_id, self._lineage, request.tool_call_id
        )
        derived_request = replace(request, tool_call_id=derived_id)
        return await self._parent.execute(derived_request)

    def derive(self, lineage: str) -> "_DerivedToolGateway":
        return _DerivedToolGateway(self._parent, f"{self._lineage}:{lineage}")


def _derived_tool_call_id(
    root_task_id: str, lineage: str, tool_call_id: str
) -> str:
    """Stable derived call id: ``sha256(root_task_id:lineage:tool_call_id)``.

    Uses a colon-separated digest so collisions between root and subagent
    call IDs are cryptographically impossible (Task 6 Step 6).
    """
    raw = f"{root_task_id}:{lineage}:{tool_call_id}".encode("utf-8")
    return "sub:" + hashlib.sha256(raw).hexdigest()[:32]


__all__ = [
    "ToolGateway",
    "ToolValidationError",
    "ToolNotFoundError",
    "ToolPreflightError",
    "effect_may_have_happened",
    "compute_idempotency_key",
    "compute_arguments_hash",
    "build_tool_execution_request",
]
