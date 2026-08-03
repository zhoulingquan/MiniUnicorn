"""Runtime Store protocols and host-neutral service contracts (design §10.2, §11).

These Protocols are the narrow views that consumers depend on. One
:class:`~miniunicorn.runtime.sqlite.store.SqliteRuntimeStore` implements all
of them; the views exist for interface segregation and tests (design §7.3).

Contracts must not import the Agent loop, SQLite, multiprocessing, or any
implementation module. They depend only on the DTOs from
:mod:`miniunicorn.agent.ports` and :mod:`miniunicorn.runtime.models`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from miniunicorn.agent.ports import (
    CompletedModelDecision,
    CompletedToolDecision,
    RestorePoint,
    SafeError,
    ToolExecutionResult,
)
from miniunicorn.runtime.models import (
    BlobRecord,
    BlobWrite,
    CheckpointWrite,
    CompletionWrite,
    ControlOutcomeWrite,
    DeliveryReceipt,
    DeliveryResolution,
    DurableEventRecord,
    DurableReply,
    InboundTaskEnvelope,
    InternalCompletionWrite,
    InternalTaskEnvelope,
    ModelAttemptWrite,
    ModelResultWrite,
    OutboxClaim,
    OutboxRecord,
    PreparedToolWrite,
    RequestScope,
    ResourceLease,
    ResourceLeaseRecord,
    ResourceLeaseRequest,
    RetentionBatch,
    RetentionPolicy,
    RetentionResult,
    RetryDecision,
    SessionCommitRecord,
    SessionCommitWrite,
    TaskClaim,
    TaskControlRecord,
    TaskControlRequest,
    TaskFailure,
    TaskRecord,
    TaskSnapshot,
    ToolAttemptRecord,
    ToolAttemptWrite,
    ToolCallRecord,
    ToolResultWrite,
    WaitDecision,
    WaitResult,
)

# ---------------------------------------------------------------------------
# Fencing primitives
# ---------------------------------------------------------------------------


class StaleLeaseError(RuntimeError):
    """Raised by mutating Worker methods when the lease token/epoch is stale.

    Rejection is a normal fencing outcome, not an internal exception to
    retry blindly (design §11.2 closing note, §6.10, §6.11).
    """

    def __init__(self, task_id: str, lease_epoch: int, message: str = "stale lease") -> None:
        super().__init__(f"{message}: task={task_id} epoch={lease_epoch}")
        self.task_id = task_id
        self.lease_epoch = lease_epoch


class LeaseLostError(RuntimeError):
    """Raised when the heartbeat fails to renew the lease (Task 2 Step 7).

    A rejected renewal must cancel the active Agent execution before it
    can reach session or completion writes (design §6.11).
    """

    def __init__(self, task_id: str, message: str = "lease lost (heartbeat rejected)") -> None:
        super().__init__(f"{message}: task={task_id}")
        self.task_id = task_id


class SessionCommitMismatchError(RuntimeError):
    """Raised when a prepared session commit is retried with different fields (Task 3 Step 7).

    The ``(task_id, commit_kind)`` pair is the idempotency key. If a
    retry provides different ``session_key``, ``base_revision``,
    ``target_revision``, or ``content_hash``, the store must reject
    rather than silently return the existing row (design §17.7).
    """

    def __init__(self, commit_id: str, field_names: list[str]) -> None:
        super().__init__(
            f"session commit mismatch: commit_id={commit_id} fields={field_names}"
        )
        self.commit_id = commit_id
        self.field_names = field_names


class RecoveryIdentityMismatch(RuntimeError):  # noqa: N818 - plan-mandated name
    """Raised when a Provider-attempt begin is replayed with a different identity (Task 4).

    The ``(task_id, logical_call_id, attempt_no)`` triple is the
    idempotency key for Provider-attempt begin. A replay with a
    different ``(provider_name, model_name, request_hash)`` identity
    must be rejected rather than silently returning the existing row
    (design §19.4 Provider-attempt idempotency).
    """

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


# ---------------------------------------------------------------------------
# Result DTOs used directly by Protocol signatures
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SubmitResult:
    """Result of ``TaskIngressStore.submit_task``.

    ``status`` is ``ACCEPTED`` for a new task, ``DUPLICATE`` for a
    successful dedup hit (the original ``task_id`` is returned).
    """

    status: Literal["ACCEPTED", "DUPLICATE"]
    task_id: str
    session_sequence: int


@dataclass(slots=True, frozen=True)
class ControlResult:
    """Result of ``TaskIngressStore.append_control``."""

    status: Literal["APPENDED", "DUPLICATE", "TASK_NOT_FOUND", "TASK_TERMINAL"]
    control_id: str | None


@dataclass(slots=True, frozen=True)
class ClaimRequest:
    """Request payload for ``WorkerLedger.claim_next``."""

    worker_id: str
    now_ms: int
    lease_ms: int
    max_root_attempts: int = 3


@dataclass(slots=True, frozen=True)
class ClaimedTask:
    """Result of a successful claim."""

    record: TaskRecord
    claim: TaskClaim


@dataclass(slots=True, frozen=True)
class ClaimResult:
    """Either a claimed task or ``None`` when nothing is eligible."""

    claimed: ClaimedTask | None


@dataclass(slots=True, frozen=True)
class ReclaimResult:
    """Outcome of a lease reclaim scan (design §24.2)."""

    reclaimed_count: int
    failed_count: int
    waiting_user_count: int


@dataclass(slots=True, frozen=True)
class CompletionResult:
    """Outcome of a completion transaction."""

    status: Literal["COMPLETED", "STALE_LEASE", "TASK_ATTEMPTS_EXHAUSTED"]
    task_id: str
    outbox_id: int | None


@dataclass(slots=True, frozen=True)
class TaskHandle:
    """Initial durable identity + status returned by ``submit``."""

    task_id: str
    snapshot: TaskSnapshot


# ---------------------------------------------------------------------------
# Task ingress view (Task Service)
# ---------------------------------------------------------------------------


@runtime_checkable
class TaskIngressStore(Protocol):
    """Ingress view of Runtime Store (design §11.2, §8.1).

    The Task Service is the only normal ingress for durable work. It
    normalizes envelopes, validates scope, deduplicates, allocates
    ``session_sequence``, inserts tasks, and appends controls.
    """

    def submit_task(self, envelope: InboundTaskEnvelope) -> SubmitResult:
        ...

    def submit_internal(self, envelope: InternalTaskEnvelope) -> SubmitResult:
        ...

    def append_control(self, control: TaskControlRequest) -> ControlResult:
        ...

    def read_task(self, task_id: str) -> TaskRecord | None:
        ...

    def read_task_snapshot(self, scope: RequestScope, task_id: str) -> TaskSnapshot | None:
        ...

    def read_final_reply(self, scope: RequestScope, task_id: str) -> DurableReply | None:
        ...


# ---------------------------------------------------------------------------
# Worker ledger view (Scheduler + Worker)
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerLedger(Protocol):
    """Worker-side view of Runtime Store (design §11.2, §8.2, §8.3).

    Every mutating method validates the lease token/epoch before applying.
    A mismatch raises :class:`StaleLeaseError` (or returns a stale status
    in some methods for ergonomic polling). No method holds a transaction
    across an external call (design §6.12).
    """

    def claim_next(self, request: ClaimRequest) -> ClaimResult:
        ...

    def mark_running(self, claim: TaskClaim, now_ms: int) -> TaskRecord:
        ...

    def renew_lease(
        self,
        claim: TaskClaim,
        lease_until_ms: int,
        *,
        now_ms: int | None = None,
    ) -> bool:
        ...

    def heartbeat(self, claim: TaskClaim, now_ms: int) -> bool:
        ...

    def checkpoint(self, claim: TaskClaim, value: CheckpointWrite) -> str:
        ...

    def record_progress(self, claim: TaskClaim, value: dict, now_ms: int) -> None:
        ...

    def enter_retry_wait(self, claim: TaskClaim, retry: RetryDecision) -> None:
        ...

    def enter_waiting_user(self, claim: TaskClaim, wait: WaitDecision) -> WaitResult:
        ...

    def fail_task(self, claim: TaskClaim, failure: TaskFailure) -> None:
        ...

    def cancel_task(self, claim: TaskClaim, reason: SafeError) -> None:
        ...

    def complete_with_outbox(
        self, claim: TaskClaim, completion: CompletionWrite
    ) -> CompletionResult:
        ...

    def complete_internal(
        self, claim: TaskClaim, completion: InternalCompletionWrite
    ) -> CompletionResult:
        ...

    def promote_due_retries(self, now_ms: int, limit: int) -> int:
        ...

    def reclaim_expired(self, now_ms: int, limit: int) -> ReclaimResult:
        ...


# ---------------------------------------------------------------------------
# Execution journal view (Worker Adapter / TurnJournalPort impl)
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutionJournal(Protocol):
    """Execution facts view of Runtime Store (design §11.2, §17.4–§17.6).

    The Worker Adapter implements :class:`TurnJournalPort` against this
    view. Every method returns or accepts only references and hashes.
    """

    def load_restore_point(self, task_id: str) -> RestorePoint | None:
        ...

    def list_completed_models(self, task_id: str) -> tuple[CompletedModelDecision, ...]:
        ...

    def list_completed_tools(self, task_id: str) -> tuple[CompletedToolDecision, ...]:
        ...

    def begin_model_attempt(self, claim: TaskClaim, value: ModelAttemptWrite) -> str:
        ...

    def finish_model_attempt(
        self, claim: TaskClaim, attempt_id: str, value: ModelResultWrite
    ) -> None:
        ...

    def fail_model_attempt(
        self, claim: TaskClaim, attempt_id: str, error: SafeError, finished_at_ms: int
    ) -> None:
        ...

    def prepare_tool_call(
        self, claim: TaskClaim, value: PreparedToolWrite
    ) -> ToolCallRecord:
        ...

    def begin_tool_attempt(
        self, claim: TaskClaim, value: ToolAttemptWrite
    ) -> ToolAttemptRecord:
        ...

    def finish_tool_attempt(
        self, claim: TaskClaim, attempt_id: str, value: ToolResultWrite
    ) -> ToolExecutionResult:
        ...

    def mark_tool_unknown(
        self, claim: TaskClaim, attempt_id: str, error: SafeError, finished_at_ms: int
    ) -> None:
        ...

    def read_tool_call(
        self, task_id: str, tool_call_id: str
    ) -> ToolCallRecord | None:
        ...

    def read_tool_result_content(self, result_blob_id: str) -> Any:
        ...

    def list_pending_controls(
        self, claim: TaskClaim, after_control_seq: int
    ) -> list[TaskControlRecord]:
        ...

    def acknowledge_control(self, claim: TaskClaim, outcome: ControlOutcomeWrite) -> None:
        ...


# ---------------------------------------------------------------------------
# Session commit ledger view
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionCommitLedger(Protocol):
    """Session commit bookkeeping view (design §11.2, §17.7).

    The :class:`~miniunicorn.runtime.session_committer.SessionCommitter`
    uses this view to coordinate idempotent commits between the Runtime
    Store and the Session Manager. It does not store transcript content.
    """

    def prepare_session_commit(
        self, claim: TaskClaim, value: SessionCommitWrite
    ) -> SessionCommitRecord:
        ...

    def confirm_session_commit(
        self, claim: TaskClaim, commit_id: str, revision: int, committed_at_ms: int
    ) -> SessionCommitRecord:
        ...

    def mark_session_conflict(
        self, claim: TaskClaim, commit_id: str, error: SafeError
    ) -> SessionCommitRecord:
        ...

    def read_session_commit(
        self, task_id: str, commit_kind: str
    ) -> SessionCommitRecord | None:
        ...


# ---------------------------------------------------------------------------
# Delivery ledger view (Outbox Sender)
# ---------------------------------------------------------------------------


@runtime_checkable
class DeliveryLedger(Protocol):
    """Outbox delivery view (design §11.2, §17.9, §17.13)."""

    def claim_next_delivery(
        self, sender_id: str, now_ms: int, lease_ms: int
    ) -> OutboxClaim | None:
        ...

    def renew_delivery_lease(self, claim: OutboxClaim, until_ms: int) -> bool:
        ...

    def mark_delivered(self, claim: OutboxClaim, receipt: DeliveryReceipt) -> None:
        ...

    def retry_delivery(self, claim: OutboxClaim, retry: RetryDecision) -> None:
        ...

    def fail_delivery(self, claim: OutboxClaim, error: SafeError) -> None:
        ...

    def resolve_unknown_delivery(
        self,
        outbox_id: int,
        resolution: DeliveryResolution,
        *,
        receipt: DeliveryReceipt | None = None,
        resolved_by: str,
    ) -> None:
        ...

    def claim_expired_deliveries(
        self,
        sender_id: str,
        now_ms: int,
        lease_ms: int,
        limit: int = 10,
    ) -> tuple[OutboxClaim, ...]:
        """Claim expired SENDING rows for recovery (Task 8, design §17.13).

        Selects bounded rows in state SENDING whose lease_until_ms has
        expired. Each row is fenced by its current lease_token and
        lease_epoch so only the recovery caller may write the result.
        """
        ...

    def mark_delivery_outcome_unknown(
        self, claim: OutboxClaim, error: SafeError
    ) -> None:
        """Transition a claimed SENDING row to OUTCOME_UNKNOWN (Task 8).

        Fenced by the recovery claim's lease token and epoch.
        """
        ...

    def read_outbox_record(self, outbox_id: int) -> OutboxRecord | None:
        ...


# ---------------------------------------------------------------------------
# Resource ledger view (Tool Gateway / capacity)
# ---------------------------------------------------------------------------


@runtime_checkable
class ResourceLedger(Protocol):
    """Generic cross-process resource lease view (design §11.2, §16.14).

    One table supports globally exclusive tools, workspace-scoped tools,
    Provider quotas, global subagent capacity, and bounded maintenance
    capacity.
    """

    def acquire_resource(self, request: ResourceLeaseRequest) -> ResourceLease | None:
        ...

    def renew_resource(self, lease: ResourceLease, until_ms: int) -> bool:
        ...

    def release_resource(self, lease: ResourceLease) -> bool:
        ...

    def read_resource_lease(
        self, resource_key: str, holder_kind: str, holder_id: str
    ) -> ResourceLeaseRecord | None:
        ...


# ---------------------------------------------------------------------------
# Maintenance ledger view
# ---------------------------------------------------------------------------


@runtime_checkable
class MaintenanceLedger(Protocol):
    """Retention and blob GC view (design §11.2, §33.4, §16.16)."""

    def list_retention_batch(
        self, policy: RetentionPolicy, now_ms: int
    ) -> RetentionBatch:
        ...

    def delete_retention_batch(self, batch: RetentionBatch) -> RetentionResult:
        ...

    def list_unreferenced_blobs(self, limit: int) -> list[str]:
        ...

    def delete_unreferenced_blobs(self, blob_ids: list[str]) -> int:
        ...


# ---------------------------------------------------------------------------
# Durable events read view (telemetry / status)
# ---------------------------------------------------------------------------


@runtime_checkable
class DurableEventLog(Protocol):
    """Read-only access to durable task_events (design §16.6).

    Used by status APIs and the realtime event bridge to reconstruct
    state. The Runtime Store is the only writer.
    """

    def list_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[DurableEventRecord]:
        ...


# ---------------------------------------------------------------------------
# Blob store view
# ---------------------------------------------------------------------------


@runtime_checkable
class BlobStore(Protocol):
    """Protected runtime blob view (design §16.15, §12.3).

    Raw user content, model responses, tool arguments, tool results, and
    full checkpoints are stored as protected runtime blobs or existing
    artifact references.
    """

    def write_blob(self, write: BlobWrite) -> BlobRecord:
        ...

    def read_blob(self, blob_id: str) -> BlobRecord | None:
        ...

    def read_blob_content(self, blob_id: str) -> bytes | None:
        ...


# ---------------------------------------------------------------------------
# Combined façade
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeStore(
    TaskIngressStore,
    WorkerLedger,
    ExecutionJournal,
    SessionCommitLedger,
    DeliveryLedger,
    ResourceLedger,
    MaintenanceLedger,
    DurableEventLog,
    BlobStore,
    Protocol,
):
    """The combined Runtime Store façade (design §7.3, §8.4).

    One SQLite implementation owns every transactional fact. The narrow
    views exist for interface segregation and tests; they share one
    connection factory, migration owner, transaction policy, and database
    file.
    """


__all__ = [
    "StaleLeaseError",
    "LeaseLostError",
    "SessionCommitMismatchError",
    "SubmitResult",
    "ControlResult",
    "ClaimRequest",
    "ClaimedTask",
    "ClaimResult",
    "ReclaimResult",
    "CompletionResult",
    "TaskHandle",
    "TaskIngressStore",
    "WorkerLedger",
    "ExecutionJournal",
    "SessionCommitLedger",
    "DeliveryLedger",
    "ResourceLedger",
    "MaintenanceLedger",
    "DurableEventLog",
    "BlobStore",
    "RuntimeStore",
]
