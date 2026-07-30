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
    from miniunicorn.runtime.contracts import ExecutionJournal, ResourceLedger


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
    ) -> None:
        self._registry = tool_registry
        self._journal = execution_journal
        self._resources = resource_ledger

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

        # Step 2: Prepare the logical tool call durably.
        tool_record = self._prepare_call(request)

        # Step 3: Check approval policy.
        if tool_record.state == "WAITING_APPROVAL":
            return ToolExecutionResult(
                state="WAITING_APPROVAL",
                error=SafeError(
                    error_code="TOOL_APPROVAL_REQUIRED",
                    error_summary=f"Tool {request.tool_name} requires approval",
                ),
            )

        # Step 4: Check for a durable result to reuse (idempotency).
        if tool_record.state == "SUCCEEDED" and tool_record.result_blob_id:
            return ToolExecutionResult(
                state="SUCCEEDED",
                result_blob_id=tool_record.result_blob_id,
                result_hash=tool_record.result_hash,
                effect_receipt_ref=tool_record.effect_receipt_ref,
            )

        if tool_record.state in ("REJECTED", "FAILED"):
            return ToolExecutionResult(
                state=tool_record.state,
                error=tool_record.error or SafeError(
                    error_code="TOOL_POLICY_DENIED",
                    error_summary=f"Tool {request.tool_name} was {tool_record.state}",
                ),
            )

        # Step 5: Acquire resource lease if needed.
        resource_token = self._acquire_resource(request)

        # Step 6: Begin attempt.
        attempt_no = tool_record.attempt_count + 1
        attempt = self._begin_attempt(request, attempt_no, resource_token)

        # Step 7: Execute the tool.
        try:
            result_value = await self._invoke_tool(request)
        except Exception as exc:
            # Mark the attempt as failed.
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
            self._release_resource(request, resource_token)
            return ToolExecutionResult(
                state="FAILED",
                error=SafeError(
                    error_code="TOOL_RETRYABLE",
                    error_summary=str(exc)[:500],
                ),
            )

        # Step 8: Finish attempt with result.
        result = self._build_result(request, result_value)
        self._finish_attempt(request, attempt.tool_attempt_id, result)
        self._release_resource(request, resource_token)

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

    # ------------------------------------------------------------------
    # Recovery: check existing durable tool call state
    # ------------------------------------------------------------------

    def _check_existing_call(
        self, request: ToolExecutionRequest
    ) -> ToolExecutionResult | None:
        """Check if a durable tool_calls row already exists for this call.

        Returns a :class:`ToolExecutionResult` if the call is already
        terminal (reuse/deny), or ``None`` if the call is new or needs
        execution.
        """
        # The journal's prepare_tool_call is idempotent — it will return
        # the existing row. We don't need a separate read here because
        # prepare_tool_call handles the INSERT OR IGNORE + verification.
        return None

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
        """Invoke the tool through the registry."""
        tool = self._registry.get(request.tool_name)
        if tool is None:
            raise RuntimeError(f"Tool not found: {request.tool_name}")
        return await tool.execute(**request.normalized_arguments)

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


__all__ = [
    "ToolGateway",
    "compute_idempotency_key",
    "compute_arguments_hash",
    "build_tool_execution_request",
]
