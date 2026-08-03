"""Runtime enums, immutable DTOs, and state-machine helpers (design §10.2).

This module is dependency-light: it imports only standard-library types and
the Agent-owned DTOs from :mod:`miniunicorn.agent.ports`. SQLite, Channels,
multiprocessing, and the Agent loop are never imported here, so contracts and
DTOs remain importable by every runtime layer (and by tests) without pulling
in heavy infrastructure.

The state-machine transition table and the ``is_allowed_transition`` helper
are the single source of truth for task state transitions (design §14.2).
Every mutating Runtime Store method must validate the transition against
this table before applying it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from miniunicorn.agent.ports import (
    CommitKind,
    ControlKind,
    ControlState,
    ModelAttemptState,
    SafeError,
    TaskKind,
    TaskState,
    ToolLogicalState,
)

# ---------------------------------------------------------------------------
# Enumerations (frozen tuples for iteration + membership checks)
# ---------------------------------------------------------------------------

TASK_KINDS: tuple[TaskKind, ...] = (
    "USER_TURN",
    "MEMORY_CONSOLIDATION",
    "MEMORY_INDEX",
    "REFLECTION",
    "DREAM",
    "MAINTENANCE",
)

TASK_STATES: tuple[TaskState, ...] = (
    "QUEUED",
    "LEASED",
    "RUNNING",
    "RETRY_WAIT",
    "WAITING_USER",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)

TERMINAL_TASK_STATES: tuple[TaskState, ...] = (
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)

CONTROL_KINDS: tuple[ControlKind, ...] = (
    "CANCEL",
    "APPROVE_TOOL",
    "REJECT_TOOL",
    "RESOLVE_EFFECT",
    "STEER",
    "CONTINUE",
    "STOP_AFTER_CHECKPOINT",
)

CONTROL_STATES: tuple[ControlState, ...] = (
    "PENDING",
    "APPLIED",
    "REJECTED",
    "EXPIRED",
)

TOOL_LOGICAL_STATES: tuple[ToolLogicalState, ...] = (
    "PREPARED",
    "WAITING_APPROVAL",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "OUTCOME_UNKNOWN",
    "REJECTED",
)

MODEL_ATTEMPT_STATES: tuple[ModelAttemptState, ...] = (
    "STARTED",
    "COMPLETED",
    "FAILED",
)

COMMIT_KINDS: tuple[CommitKind, ...] = ("INBOUND", "FINAL")

# event_type values for task_events (design §16.6)
EVENT_TYPES: tuple[str, ...] = (
    "TASK_ACCEPTED",
    "TASK_LEASED",
    "TASK_RUNNING",
    "CHECKPOINT_SAVED",
    "MODEL_STARTED",
    "MODEL_COMPLETED",
    "MODEL_FAILED",
    "TOOL_PREPARED",
    "TOOL_STARTED",
    "TOOL_COMPLETED",
    "TOOL_FAILED",
    "TOOL_OUTCOME_UNKNOWN",
    "CONTROL_RECEIVED",
    "CONTROL_APPLIED",
    "SESSION_COMMIT_PREPARED",
    "SESSION_COMMITTED",
    "OUTBOX_ENQUEUED",
    "TASK_RETRY_WAIT",
    "TASK_WAITING_USER",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_CANCELLED",
    "LEASE_RECLAIMED",
)

# blob_kind values for runtime_blobs (design §16.15)
BLOB_KIND: tuple[str, ...] = (
    "TASK_PAYLOAD",
    "MODEL_REQUEST",
    "MODEL_RESPONSE",
    "TOOL_ARGUMENTS",
    "TOOL_RESULT",
    "CHECKPOINT",
    "CONTROL_PAYLOAD",
    "SESSION_MUTATION",
    "OUTBOX_PAYLOAD",
    "MUTATION_PAYLOAD",
)

BLOB_ENCODING: tuple[str, ...] = (
    "RAW_JSON",
    "RAW_BYTES",
    "BASE64",
    "MSGPACK",
)

# outbox message_kind values (design §16.13, §17.8, §20.6)
OUTBOX_MESSAGE_KINDS: tuple[str, ...] = (
    "FINAL_REPLY",
    "TERMINAL_FAILURE_REPLY",
    "MESSAGE_TOOL",
    "WAITING_PROMPT",
)

# Stable dedup-key suffixes for Outbox (design §16.13).
OUTBOX_DEDUP_KINDS: tuple[str, ...] = (
    "final-reply",
    "terminal-reply",
    "message",
    "waiting-prompt",
)

# Channel delivery recovery capability (design §23.5, §17.13)
DELIVERY_RECOVERY: tuple[str, ...] = (
    "NATIVE_IDEMPOTENCY",
    "QUERYABLE_RECEIPT",
    "NONE",
)

# Outbox states (design §16.13)
OUTBOX_STATES: tuple[str, ...] = (
    "PENDING",
    "SENDING",
    "RETRY_WAIT",
    "OUTCOME_UNKNOWN",
    "DELIVERED",
    "FAILED",
)

# session_commit states (design §16.12)
SESSION_COMMIT_STATES: tuple[str, ...] = (
    "PREPARED",
    "COMMITTED",
    "CONFLICT",
)

# resource_lease holder_kind values (design §16.14)
RESOURCE_HOLDER_KINDS: tuple[str, ...] = (
    "TASK",
    "WORKER",
    "MAINTENANCE",
)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Allowed transitions (design §14.2). Source state -> set of target states.
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    # QUEUED -> FAILED is permitted at recovery claim time when the root
    # attempt budget is exhausted (design §15.3 step 7, Task 2 Step 6).
    "QUEUED": frozenset({"LEASED", "FAILED", "CANCELLED"}),
    # LEASED -> FAILED is permitted by the reaper when the root attempt
    # budget is exhausted during lease reclaim (design §24.2 step 5).
    "LEASED": frozenset({"RUNNING", "QUEUED", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"COMPLETED", "RETRY_WAIT", "WAITING_USER", "FAILED", "CANCELLED"}),
    "RETRY_WAIT": frozenset({"QUEUED", "CANCELLED", "FAILED"}),
    "WAITING_USER": frozenset({"QUEUED", "CANCELLED", "FAILED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


def is_allowed_transition(source: TaskState, target: TaskState) -> bool:
    """Return True when ``source -> target`` is permitted by design §14.2."""
    if source == target:
        # No-op transitions are allowed only for idempotent re-applications
        # of the same state (e.g. re-claiming the same lease epoch). The
        # caller must validate the lease token/epoch separately.
        return True
    return target in TRANSITIONS.get(source, frozenset())


def is_terminal_state(state: TaskState) -> bool:
    """Return True when ``state`` may not transition to anything else."""
    return state in TERMINAL_TASK_STATES


# ---------------------------------------------------------------------------
# Request scope and envelopes
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RequestScope:
    """Tenant/principal/Agent/workspace scope carried by every external request.

    Task, control, status, artifact, and Outbox APIs require this scope
    (design §33.3). An opaque ``task_id`` alone is not authorization.
    """

    tenant_id: str
    principal_id: str
    agent_id: str
    workspace_id: str


@dataclass(slots=True, frozen=True)
class MediaRef:
    """A content-addressed media reference inside an inbound task envelope."""

    artifact_ref: str
    content_hash: str
    media_kind: str
    size_bytes: int


@dataclass(slots=True, frozen=True)
class InboundTaskEnvelope:
    """Durable ingress envelope for a Channel/CLI/SDK user task (design §13.1).

    All fields are required unless noted. The Task Service normalizes this
    into a task row plus a protected payload blob.
    """

    protocol_version: int
    task_kind: TaskKind
    priority: int
    scope: RequestScope
    session_key: str
    channel: str | None
    channel_account: str | None
    channel_message_id: str | None
    dedup_key: str | None
    normalized_payload_ref: str
    payload_hash: str
    media_refs: tuple[MediaRef, ...] = ()
    reply_to: str | None = None
    trace_id: str | None = None
    received_at_ms: int = 0
    turn_id: str | None = None
    available_at_ms: int | None = None
    # WP3: inline payload content. When present, the Task Service stores
    # the content directly in the protected blob (inline_content) instead
    # of just recording the external_ref. This lets the Worker decode the
    # payload without a separate artifact store (design §13.1, §16.15).
    payload_content: bytes | None = None
    # Task 7 Step 2: immutable delivery target for the final reply. The
    # Worker copies this verbatim into the FINAL_REPLY Outbox row so the
    # Agent's final text cannot alter routing (design §17.8).
    target_key: str = ""


@dataclass(slots=True, frozen=True)
class InternalTaskEnvelope:
    """Durable ingress envelope for a maintenance/internal task.

    Internal tasks use a system-scoped session key and a deterministic
    dedup key that identifies one logical occurrence (design §13.1).
    """

    protocol_version: int
    task_kind: TaskKind
    priority: int
    scope: RequestScope
    session_key: str
    dedup_key: str
    normalized_payload_ref: str
    payload_hash: str
    trace_id: str | None = None
    received_at_ms: int = 0
    available_at_ms: int | None = None
    payload_content: bytes | None = None


@dataclass(slots=True, frozen=True)
class TaskControlRequest:
    """A control request appended to a task (design §13.3, §17.12)."""

    task_id: str
    kind: ControlKind
    dedup_key: str
    payload_blob_id: str | None
    requested_by: str
    requested_at_ms: int
    control_id: str | None = None  # generated by store if absent


# ---------------------------------------------------------------------------
# Task record and snapshots
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskRecord:
    """A durable task row (design §16.4).

    Mutable in the store layer (the store updates ``state``, ``lease_*``
    and progress fields in place) but immutable from the consumer's
    perspective: every mutation returns a fresh copy.
    """

    task_id: str
    turn_id: str | None
    protocol_version: int
    tenant_id: str
    principal_id: str
    agent_id: str
    workspace_id: str
    session_key: str
    session_sequence: int
    channel: str | None
    channel_account: str | None
    channel_message_id: str | None
    dedup_key: str | None
    task_kind: TaskKind
    priority: int
    payload_blob_id: str
    payload_hash: str
    state: TaskState
    checkpoint_phase: str
    # Task 7 Step 2: immutable delivery target copied from the inbound
    # envelope. Empty string for legacy rows migrated from schema v1.
    target_key: str = ""
    run_segment: int = 0
    root_attempt_count: int = 0
    max_root_attempts: int = 3
    recovery_pending: int = 0
    leased_by: str | None = None
    lease_token: str | None = None
    lease_epoch: int = 0
    lease_until_ms: int | None = None
    last_heartbeat_at_ms: int | None = None
    last_progress_at_ms: int | None = None
    available_at_ms: int = 0
    state_version: int = 0
    control_cursor: int = 0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    error_code: str | None = None
    error_summary: str | None = None
    waiting_reason: str | None = None
    waiting_ref: str | None = None
    wait_until_ms: int | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    completed_at_ms: int | None = None


@dataclass(slots=True, frozen=True)
class TaskSnapshot:
    """Public read-only snapshot of a task (design §11.3)."""

    task_id: str
    state: TaskState
    checkpoint_phase: str
    run_segment: int
    root_attempt_count: int
    max_root_attempts: int
    recovery_pending: int
    session_sequence: int = 0
    waiting_reason: str | None = None
    waiting_ref: str | None = None
    error: SafeError | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    completed_at_ms: int | None = None


@dataclass(slots=True, frozen=True)
class TaskHandle:
    """Initial durable identity + status returned by ``TaskService.submit``.

    Contains the opaque task id and its initial snapshot. Not a reference
    to an ``asyncio.Task`` (design §11.3).
    """

    task_id: str
    snapshot: TaskSnapshot


@dataclass(slots=True, frozen=True)
class TaskClaim:
    """Lease identity returned by ``claim_next`` and required by every
    subsequent Worker mutation.

    Fencing: any mutation whose ``task_id``, ``lease_token``, and
    ``lease_epoch`` do not exactly match the current row is rejected
    (design §6.10, §6.11, §15.3).
    """

    task_id: str
    lease_token: str
    lease_epoch: int
    leased_by: str
    lease_until_ms: int


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
    """Either a claimed task or ``None`` when no eligible task exists."""

    claimed: ClaimedTask | None


@dataclass(slots=True, frozen=True)
class SubmitResult:
    """Result of ``TaskIngressStore.submit_task``.

    ``status`` is ``ACCEPTED`` for a new task, ``DUPLICATE`` for a
    successful dedup hit (the original task id is returned).
    """

    status: Literal["ACCEPTED", "DUPLICATE"]
    task_id: str
    session_sequence: int


@dataclass(slots=True, frozen=True)
class ControlResult:
    """Result of ``append_control`` (design §17.12)."""

    status: Literal["APPENDED", "DUPLICATE", "TASK_NOT_FOUND", "TASK_TERMINAL"]
    control_id: str | None


# ---------------------------------------------------------------------------
# Checkpoint, progress, and execution facts
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CheckpointWrite:
    """A checkpoint to be persisted (design §17.4)."""

    phase: str
    run_segment: int
    ordinal: int
    payload_blob_id: str
    payload_hash: str
    lease_epoch: int
    created_at_ms: int
    checkpoint_id: str | None = None  # generated by store if absent
    format_version: int = 1


@dataclass(slots=True, frozen=True)
class ModelAttemptWrite:
    """A model attempt start record (design §17.5)."""

    logical_call_id: str
    attempt_no: int
    provider_name: str
    model_name: str
    request_hash: str
    started_at_ms: int
    model_attempt_id: str | None = None  # generated by store if absent


@dataclass(slots=True, frozen=True)
class ModelResultWrite:
    """A completed model attempt result (design §17.5)."""

    response_blob_id: str
    response_hash: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None
    finished_at_ms: int = 0


@dataclass(slots=True, frozen=True)
class PreparedToolWrite:
    """A prepared logical tool call (design §17.6, §20)."""

    tool_call_id: str
    tool_name: str
    arguments_blob_id: str
    arguments_hash: str
    effect_class: str
    risk_class: str
    idempotency_mode: str
    idempotency_key: str
    approval_policy: str
    recovery_policy: str
    concurrency_scope: str
    created_at_ms: int


@dataclass(slots=True, frozen=True)
class ToolAttemptWrite:
    """A tool invocation attempt start (design §17.6)."""

    tool_call_id: str
    attempt_no: int
    resource_token: str | None
    started_at_ms: int
    tool_attempt_id: str | None = None  # generated by store if absent


@dataclass(slots=True, frozen=True)
class ToolResultWrite:
    """A completed tool attempt result (design §17.6)."""

    state: ToolLogicalState
    result_blob_id: str | None = None
    result_hash: str | None = None
    effect_receipt_ref: str | None = None
    error: SafeError | None = None
    finished_at_ms: int = 0


@dataclass(slots=True, frozen=True)
class ToolCallRecord:
    """A logical tool call row read back from the store."""

    task_id: str
    tool_call_id: str
    tool_name: str
    arguments_blob_id: str
    arguments_hash: str
    state: ToolLogicalState
    attempt_count: int
    result_blob_id: str | None
    result_hash: str | None
    effect_receipt_ref: str | None
    error: SafeError | None


@dataclass(slots=True, frozen=True)
class ToolAttemptRecord:
    """A tool attempt row read back from the store."""

    tool_attempt_id: str
    task_id: str
    tool_call_id: str
    attempt_no: int
    state: str
    resource_token: str | None
    effect_receipt_ref: str | None
    error: SafeError | None
    started_at_ms: int
    finished_at_ms: int | None


# ---------------------------------------------------------------------------
# Session commits
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SessionCommitWrite:
    """A session commit preparation request (design §17.7)."""

    session_key: str
    commit_kind: CommitKind
    base_revision: int
    target_revision: int
    content_hash: str
    payload_blob_id: str
    created_at_ms: int
    session_commit_id: str | None = None  # generated by store if absent


@dataclass(slots=True, frozen=True)
class SessionCommitRecord:
    """A session_commit row read back from the store."""

    session_commit_id: str
    task_id: str
    commit_kind: CommitKind
    session_key: str
    base_revision: int
    target_revision: int
    content_hash: str
    state: str
    error_code: str | None
    created_at_ms: int
    committed_at_ms: int | None


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TaskControlRecord:
    """A task_controls row read back from the store."""

    control_seq: int
    control_id: str
    task_id: str
    kind: ControlKind
    dedup_key: str
    payload_blob_id: str | None
    requested_by: str
    state: ControlState
    outcome_code: str | None
    requested_at_ms: int
    applied_at_ms: int | None


@dataclass(slots=True, frozen=True)
class ControlOutcomeWrite:
    """Acknowledgement outcome for one control."""

    control_id: str
    state: ControlState
    outcome_code: str | None = None
    applied_at_ms: int = 0


# ---------------------------------------------------------------------------
# Completion, retry, wait, failure
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CompletionWrite:
    """Final completion for a user task (design §17.8)."""

    final_reply_blob_id: str | None
    final_reply_hash: str | None
    final_reply_dedup_key: str | None
    suppress_final: bool = False
    completed_at_ms: int = 0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    # Task 7 Step 3: durable delivery routing for the final reply. Copied
    # verbatim from the immutable claimed TaskRecord so the Agent's final
    # text cannot choose or alter them. Empty strings are allowed only
    # for explicitly local/suppressed completions (design §17.8).
    channel: str = ""
    channel_account: str = ""
    target_key: str = ""


@dataclass(slots=True, frozen=True)
class InternalCompletionWrite:
    """Final completion for an internal task (design §17.10)."""

    result_ref: str | None
    completed_at_ms: int = 0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0


@dataclass(slots=True, frozen=True)
class CompletionResult:
    """Outcome of a completion transaction."""

    status: Literal["COMPLETED", "STALE_LEASE", "TASK_ATTEMPTS_EXHAUSTED"]
    task_id: str
    outbox_id: int | None


RetryKind = Literal["TRANSIENT", "STALLED", "LEASE_RECLAIMED", "POLICY"]


@dataclass(slots=True, frozen=True)
class RetryDecision:
    """A bounded retry decision (design §17.6, §24.2)."""

    kind: RetryKind
    available_at_ms: int
    error: SafeError
    increment_root_attempt: bool = False


@dataclass(slots=True, frozen=True)
class WaitDecision:
    """A decision to enter ``WAITING_USER`` (design §17.11)."""

    waiting_reason: str
    waiting_ref: str
    wait_until_ms: int | None
    prompt_blob_id: str
    prompt_hash: str
    prompt_dedup_key: str
    control_token: str


@dataclass(slots=True, frozen=True)
class WaitResult:
    """Outcome of an enter_waiting_user transaction."""

    status: Literal["WAITING", "STALE_LEASE"]
    task_id: str
    outbox_id: int | None


@dataclass(slots=True, frozen=True)
class TaskFailure:
    """A terminal failure description (design §17.10, §26)."""

    error: SafeError
    failed_at_ms: int
    failure_reply_blob_id: str | None = None
    failure_reply_hash: str | None = None
    failure_reply_dedup_key: str | None = None


@dataclass(slots=True, frozen=True)
class ReclaimResult:
    """Outcome of a lease reclaim scan (design §24.2)."""

    reclaimed_count: int
    failed_count: int
    waiting_user_count: int


# ---------------------------------------------------------------------------
# Delivery (Outbox)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class OutboxClaim:
    """A delivery lease returned by ``claim_next_delivery``."""

    outbox_id: int
    task_id: str
    channel: str
    channel_account: str
    target_key: str
    message_kind: str
    payload_blob_id: str
    payload_hash: str
    dedup_key: str
    lease_token: str
    lease_epoch: int
    lease_until_ms: int
    attempt_count: int


@dataclass(slots=True, frozen=True)
class OutboxRecord:
    """An outbox row read back from the store."""

    outbox_id: int
    task_id: str
    channel: str
    channel_account: str
    target_key: str
    message_kind: str
    payload_blob_id: str
    payload_hash: str
    dedup_key: str
    state: str
    attempt_count: int
    max_attempts: int
    available_at_ms: int
    lease_token: str | None
    lease_epoch: int
    lease_until_ms: int | None
    provider_receipt_ref: str | None
    error: SafeError | None
    created_at_ms: int
    delivered_at_ms: int | None


@dataclass(slots=True, frozen=True)
class DeliveryReceipt:
    """Channel send result (design §23.5).

    Task 8: ``OUTCOME_UNKNOWN`` is returned when the send may or may not
    have reached the Channel and the recovery policy cannot reconcile it
    (design §17.13). The Outbox row transitions to ``OUTCOME_UNKNOWN``
    and awaits explicit resolution.
    """

    status: Literal[
        "DELIVERED",
        "RETRYABLE_FAILURE",
        "PERMANENT_FAILURE",
        "OUTCOME_UNKNOWN",
    ]
    provider_message_id: str | None = None
    safe_error_code: str | None = None
    retry_after_ms: int | None = None
    receipt_ref: str | None = None


DeliveryResolution = Literal["MARK_DELIVERED", "RETRY"]


# ---------------------------------------------------------------------------
# Durable reply (design Task 4 — application façade result)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DurableReply:
    """Final reply content read from the Runtime Store (design Task 4).

    Returned by ``TaskIngressStore.read_final_reply`` and surfaced through
    ``RuntimeApplication.read_reply``. The ``outbox_id`` is ``None`` when no
    final-reply Outbox row exists (e.g. empty or suppressed replies).
    """

    content: str
    outbox_id: int | None
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Resource leases
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ResourceLeaseRequest:
    """A resource lease acquisition request (design §16.14)."""

    resource_key: str
    holder_kind: str
    holder_id: str
    units: int
    lease_ms: int
    now_ms: int
    lease_token: str | None = None  # generated by store if absent


@dataclass(slots=True, frozen=True)
class ResourceLease:
    """An acquired resource lease."""

    resource_key: str
    holder_kind: str
    holder_id: str
    units: int
    lease_token: str
    lease_until_ms: int


@dataclass(slots=True, frozen=True)
class ResourceLeaseRecord:
    """A resource_leases row read back from the store."""

    resource_key: str
    holder_kind: str
    holder_id: str
    units: int
    lease_token: str
    lease_until_ms: int
    created_at_ms: int
    updated_at_ms: int


# ---------------------------------------------------------------------------
# Blobs, events, retention
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BlobRecord:
    """A runtime_blobs row read back from the store (design §16.15)."""

    blob_id: str
    scope_key: str
    blob_kind: str
    content_hash: str
    encoding: str
    compression: str | None
    encryption_key_id: str | None
    inline_content: bytes | None
    external_ref: str | None
    size_bytes: int
    created_at_ms: int


@dataclass(slots=True, frozen=True)
class DurableEventRecord:
    """A task_events row read back from the store (design §16.6)."""

    event_seq: int
    event_id: str
    task_id: str
    event_type: str
    phase: str | None
    safe_payload_json: str | None
    payload_blob_id: str | None
    lease_epoch: int | None
    created_at_ms: int


@dataclass(slots=True, frozen=True)
class TaskEventRecord:
    """Alias kept for callers that name the type ``TaskEventRecord``."""

    event: DurableEventRecord


@dataclass(slots=True, frozen=True)
class RetentionPolicy:
    """A retention selection policy (design §33.4)."""

    successful_task_age_days: int = 7
    failed_task_age_days: int = 30
    batch_size: int = 100


@dataclass(slots=True, frozen=True)
class RetentionBatch:
    """A bounded batch of rows selected for retention deletion."""

    task_ids: tuple[str, ...]
    outbox_ids: tuple[int, ...]
    blob_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class RetentionResult:
    """Outcome of a retention deletion batch."""

    deleted_tasks: int
    deleted_outbox: int
    deleted_blobs: int
    skipped: int


# ---------------------------------------------------------------------------
# Payload blob write helper (used by both ingress and tool gateway)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BlobWrite:
    """A blob write request.

    Exactly one of ``inline_content`` or ``external_ref`` must be present
    (design §16.15). ``scope_key`` must include tenant/principal scope so
    deduplication never crosses protection boundaries.
    """

    scope_key: str
    blob_kind: str
    content_hash: str
    encoding: str
    inline_content: bytes | None = None
    external_ref: str | None = None
    compression: str | None = None
    encryption_key_id: str | None = None
    size_bytes: int = 0
    blob_id: str | None = None  # generated by store if absent
    created_at_ms: int = 0


__all__ = [
    # Enumerations
    "TASK_KINDS",
    "TASK_STATES",
    "TERMINAL_TASK_STATES",
    "CONTROL_KINDS",
    "CONTROL_STATES",
    "TOOL_LOGICAL_STATES",
    "MODEL_ATTEMPT_STATES",
    "COMMIT_KINDS",
    "EVENT_TYPES",
    "BLOB_KIND",
    "BLOB_ENCODING",
    "OUTBOX_MESSAGE_KINDS",
    "OUTBOX_DEDUP_KINDS",
    "DELIVERY_RECOVERY",
    "OUTBOX_STATES",
    "SESSION_COMMIT_STATES",
    "RESOURCE_HOLDER_KINDS",
    # State machine
    "TRANSITIONS",
    "is_allowed_transition",
    "is_terminal_state",
    # Request scope and envelopes
    "RequestScope",
    "MediaRef",
    "InboundTaskEnvelope",
    "InternalTaskEnvelope",
    "TaskControlRequest",
    # Task records
    "TaskRecord",
    "TaskSnapshot",
    "TaskHandle",
    "TaskClaim",
    "ClaimRequest",
    "ClaimedTask",
    "ClaimResult",
    "SubmitResult",
    "ControlResult",
    # Execution facts
    "CheckpointWrite",
    "ModelAttemptWrite",
    "ModelResultWrite",
    "PreparedToolWrite",
    "ToolAttemptWrite",
    "ToolResultWrite",
    "ToolCallRecord",
    "ToolAttemptRecord",
    # Session commits
    "SessionCommitWrite",
    "SessionCommitRecord",
    # Controls
    "TaskControlRecord",
    "ControlOutcomeWrite",
    # Completion, retry, wait, failure
    "CompletionWrite",
    "InternalCompletionWrite",
    "CompletionResult",
    "RetryKind",
    "RetryDecision",
    "WaitDecision",
    "WaitResult",
    "TaskFailure",
    "ReclaimResult",
    # Delivery
    "OutboxClaim",
    "OutboxRecord",
    "DeliveryReceipt",
    "DeliveryResolution",
    "DurableReply",
    # Resources
    "ResourceLeaseRequest",
    "ResourceLease",
    "ResourceLeaseRecord",
    # Blobs and events
    "BlobRecord",
    "DurableEventRecord",
    "TaskEventRecord",
    "RetentionBatch",
    "RetentionPolicy",
    "RetentionResult",
    "BlobWrite",
    # Re-exported for convenience
    "SafeError",
]
