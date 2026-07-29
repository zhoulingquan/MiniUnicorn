"""Agent-owned ports for the durable runtime (design §10.1, §11.1).

Agent Core defines the ports it needs from execution infrastructure. The
durable runtime implements these ports. This keeps the dependency direction
correct: Agent Core never imports ``miniunicorn.runtime``, SQLite,
multiprocessing, or Channel infrastructure.

This module imports only standard-library types and existing Agent DTOs
(``AgentEvent`` from :mod:`miniunicorn.bus.agent_events`). It must remain
free of any runtime implementation import so Agent unit tests can run
without SQLite or multiprocessing (design §6.17, §6.23, acceptance #23).

The DTOs below are shared between Agent Core (which consumes the ports) and
the runtime (which implements the ports). Defining them here keeps Agent
Core the owner of the contract; the runtime imports from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# Existing Agent DTO. Importing this does not pull in SQLite or
# multiprocessing; it is a pure Pydantic discriminated union.
from miniunicorn.bus.agent_events import AgentEvent

# ---------------------------------------------------------------------------
# Enums and value types (kept as Literal unions so the file stays dependency
# light and the values are inline with the design).
# ---------------------------------------------------------------------------

TaskKind = Literal["USER_TURN", "MEMORY_CONSOLIDATION", "MEMORY_INDEX", "REFLECTION", "DREAM", "MAINTENANCE"]

TaskState = Literal[
    "QUEUED",
    "LEASED",
    "RUNNING",
    "RETRY_WAIT",
    "WAITING_USER",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]

CheckpointPhase = Literal[
    "ACCEPTED",
    "INBOUND_SESSION_COMMITTED",
    "RESTORED",
    "COMPACTED",
    "COMMAND_DONE",
    "CONTEXT_BUILT",
    "MODEL_DECISION_COMMITTED",
    "TOOLS_COMMITTED",
    "SESSION_COMMIT_PREPARED",
    "SESSION_COMMITTED",
    "REPLY_ENQUEUED",
    "TERMINAL",
]

ControlKind = Literal[
    "CANCEL",
    "APPROVE_TOOL",
    "REJECT_TOOL",
    "RESOLVE_EFFECT",
    "STEER",
    "CONTINUE",
    "STOP_AFTER_CHECKPOINT",
]

ControlState = Literal["PENDING", "APPLIED", "REJECTED", "EXPIRED"]

ToolLogicalState = Literal[
    "PREPARED",
    "WAITING_APPROVAL",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "OUTCOME_UNKNOWN",
    "REJECTED",
]

ModelAttemptState = Literal["STARTED", "COMPLETED", "FAILED"]

CommitKind = Literal["INBOUND", "FINAL"]

EffectClass = Literal["READ", "LOCAL_WRITE", "EXTERNAL_WRITE"]
RiskClass = Literal["LOW", "MEDIUM", "HIGH"]
IdempotencyMode = Literal["REPLAY_SAFE", "NATIVE_KEY", "RUNTIME_RESULT", "NONE"]
ApprovalPolicy = Literal["NEVER", "POLICY", "ALWAYS"]
RecoveryPolicy = Literal["REPLAY", "QUERY_THEN_RETRY", "REUSE_RESULT", "MANUAL"]
ConcurrencyScope = Literal["NONE", "SESSION", "WORKSPACE", "GLOBAL"]

ToolOutcome = Literal["ALLOW", "WAIT_APPROVAL", "DENY", "REUSE", "MANUAL_RECOVERY"]


# ---------------------------------------------------------------------------
# Safe error
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SafeError:
    """Safe, redacted error metadata suitable for SQLite and logs.

    ``error_code`` is one of the safe codes from design §26.
    ``error_summary`` is a bounded, redacted string. Raw prompts, responses,
    tool arguments, results, credentials, and paths outside the workspace
    are never placed here (design §6.20).
    """

    error_code: str
    error_summary: str


# ---------------------------------------------------------------------------
# Task and attempt identities
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TaskIdentity:
    """Immutable identity of a claimed durable task."""

    task_id: str
    turn_id: str | None
    session_key: str
    session_sequence: int
    run_segment: int
    lease_epoch: int


@dataclass(slots=True, frozen=True)
class AttemptIdentity:
    """Identity of one Provider network attempt, durable before response use."""

    task_id: str
    model_attempt_id: str
    logical_call_id: str
    attempt_no: int


@dataclass(slots=True, frozen=True)
class CheckpointIdentity:
    """Identity of a saved durable checkpoint."""

    checkpoint_id: str
    ordinal: int


# ---------------------------------------------------------------------------
# Restore point and model/tool facts
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CompletedModelDecision:
    """Reference to a durable-completed model decision (response + usage)."""

    model_attempt_id: str
    logical_call_id: str
    attempt_no: int
    response_blob_id: str
    response_hash: str
    input_tokens: int
    output_tokens: int
    finish_reason: str | None


@dataclass(slots=True, frozen=True)
class CompletedToolDecision:
    """Reference to a durable-completed logical tool call."""

    tool_call_id: str
    tool_name: str
    arguments_hash: str
    state: ToolLogicalState
    result_blob_id: str | None
    result_hash: str | None
    error: SafeError | None


@dataclass(slots=True, frozen=True)
class RestorePoint:
    """Latest durable recovery boundary for a claimed task.

    A ``None`` value means the task has no prior durable execution state
    and must start from the beginning of its current ``run_segment``.
    """

    checkpoint_id: str | None
    phase: CheckpointPhase
    run_segment: int
    session_base_revision: int
    completed_models: tuple[CompletedModelDecision, ...]
    completed_tools: tuple[CompletedToolDecision, ...]
    control_cursor: int
    payload_blob_id: str
    payload_hash: str


@dataclass(slots=True, frozen=True)
class ModelAttemptStarted:
    """Durable facts recorded before a Provider network call."""

    logical_call_id: str
    attempt_no: int
    provider_name: str
    model_name: str
    request_hash: str
    started_at_ms: int


@dataclass(slots=True, frozen=True)
class ModelAttemptResult:
    """Terminal facts for one completed Provider attempt."""

    response_blob_id: str
    response_hash: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None


@dataclass(slots=True, frozen=True)
class ModelDecision:
    """Returned to Runner only after the completed attempt is durable."""

    attempt: AttemptIdentity
    response_blob_id: str
    response_hash: str
    input_tokens: int
    output_tokens: int
    finish_reason: str | None


# ---------------------------------------------------------------------------
# Checkpoint and progress
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TurnCheckpoint:
    """A durable Agent execution checkpoint at a defined safe boundary."""

    phase: CheckpointPhase
    run_segment: int
    inner_loop_iteration: int
    session_base_revision: int
    payload_blob_id: str
    payload_hash: str


@dataclass(slots=True, frozen=True)
class DurableProgress:
    """Bounded, redacted durable progress written by Worker at safe boundaries.

    Never contains raw prompts, responses, tool arguments, tool results, or
    streaming Token deltas (design §6.20, §23.1).
    """

    phase: CheckpointPhase
    progress_summary: str
    progress_at_ms: int
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EffectiveToolPolicy:
    """Invocation-specific effective metadata for one logical tool call."""

    effect_class: EffectClass
    risk_class: RiskClass
    idempotency_mode: IdempotencyMode
    approval_policy: ApprovalPolicy
    recovery_policy: RecoveryPolicy
    concurrency_scope: ConcurrencyScope
    progress_required: bool = False
    timeout_s: int | None = None


@dataclass(slots=True, frozen=True)
class ToolExecutionRequest:
    """A single Agent-originated tool call to be executed through the gateway.

    ``normalized_arguments`` is the JSON-serializable arguments dict after
    ToolRegistry preparation. The gateway stores a hash of it durably and
    uses that for idempotency, approval binding, and recovery.
    """

    task_id: str
    tool_call_id: str
    tool_name: str
    normalized_arguments: dict[str, Any]
    arguments_hash: str
    policy: EffectiveToolPolicy
    idempotency_key: str


@dataclass(slots=True, frozen=True)
class ToolExecutionResult:
    """Terminal result of one logical tool call.

    ``state`` is the logical state to advance to. ``result_blob_id`` and
    ``result_hash`` are present on ``SUCCEEDED``; ``error`` is present on
    ``FAILED`` and ``OUTCOME_UNKNOWN``. ``effect_receipt_ref`` is an opaque
    reference to a Channel/provider receipt when available.
    """

    state: ToolLogicalState
    result_blob_id: str | None = None
    result_hash: str | None = None
    effect_receipt_ref: str | None = None
    error: SafeError | None = None


# ---------------------------------------------------------------------------
# Session commit
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SessionMutation:
    """Normalized, idempotent session transcript mutation.

    ``messages`` are the assistant/tool messages to append (in order). For
    ``INBOUND`` commits, this is the sanitized triggering user message
    exactly once. For ``FINAL`` commits, this is the messages produced
    after the inbound commit (never the triggering user message again).
    ``metadata_updates`` is a flat dict merged into session metadata.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata_updates: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SessionCommitRequest:
    """One idempotent session commit requested by the Worker."""

    task_id: str
    session_key: str
    commit_id: str
    commit_kind: CommitKind
    base_revision: int
    target_revision: int
    mutation: SessionMutation
    content_hash: str


@dataclass(slots=True, frozen=True)
class SessionCommitResult:
    """Outcome of an idempotent ``commit_turn`` operation."""

    state: Literal["COMMITTED", "ALREADY_COMMITTED", "REVISION_CONFLICT", "IO_FAILURE"]
    revision: int
    error: SafeError | None = None


# ---------------------------------------------------------------------------
# Control inbox
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ControlRequest:
    """One pending durable control request relevant to the current task."""

    control_id: str
    task_id: str
    kind: ControlKind
    dedup_key: str
    payload_blob_id: str | None
    requested_by: str
    requested_at_ms: int


@dataclass(slots=True, frozen=True)
class ControlBatch:
    """A snapshot of pending controls read at a safe boundary."""

    controls: tuple[ControlRequest, ...]
    next_cursor: int


@dataclass(slots=True, frozen=True)
class ControlOutcome:
    """Acknowledgement outcome for one control."""

    state: ControlState
    error: SafeError | None = None


# ---------------------------------------------------------------------------
# Agent-owned ports (Protocols)
# ---------------------------------------------------------------------------


@runtime_checkable
class TurnJournalPort(Protocol):
    """Durable turn journal consumed by Agent Core (design §11.1).

    Every method returns or accepts only references and hashes. Raw
    prompts, responses, tool arguments, and tool results live in protected
    runtime blobs referenced by these methods.
    """

    async def load_restore_point(self, task: TaskIdentity) -> RestorePoint | None:
        """Load the latest durable restore point for ``task``."""
        ...

    async def record_model_started(self, call: ModelAttemptStarted) -> AttemptIdentity:
        """Durable-record that a Provider attempt started, before the call.

        Runner may consume the Provider response only after the matching
        ``record_model_completed`` succeeds (design §6.13).
        """
        ...

    async def record_model_completed(
        self, attempt: AttemptIdentity, result: ModelAttemptResult
    ) -> ModelDecision:
        """Durable-record the completed attempt and return the decision.

        The returned :class:`ModelDecision` is the only value Runner may
        consume as the model response.
        """
        ...

    async def record_model_failed(self, attempt: AttemptIdentity, error: SafeError) -> None:
        """Durable-record a failed Provider attempt."""
        ...

    async def save_checkpoint(self, checkpoint: TurnCheckpoint) -> CheckpointIdentity:
        """Persist a checkpoint at a safe boundary."""
        ...

    async def record_progress(self, progress: DurableProgress) -> None:
        """Persist bounded, redacted progress at a safe boundary."""
        ...


@runtime_checkable
class ToolExecutionPort(Protocol):
    """Agent Core's only path to invoke a tool (design §11.1, §20).

    Every Agent tool request is routed through this port so the runtime
    can apply risk classification, approval policy, idempotency, journaling,
    resource locking, and recovery decisions.
    """

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        ...


@runtime_checkable
class SessionCommitPort(Protocol):
    """Agent Core's only path to mutate the session transcript (design §11.1).

    The runtime implements this with the prepare/apply/confirm coordinator
    described in design §17.7. Agent Core receives only the structured
    outcome.
    """

    async def commit_turn(self, request: SessionCommitRequest) -> SessionCommitResult:
        ...


@runtime_checkable
class ControlInboxPort(Protocol):
    """Reads and acknowledges pending controls at safe boundaries (design §11.1).

    Workers poll controls before/after model and tool calls, before session
    commit, and during known long-running tool progress callbacks
    (design §23.3).
    """

    async def pending_controls(self, cursor: int) -> ControlBatch:
        ...

    async def acknowledge(self, control_id: str, outcome: ControlOutcome) -> None:
        ...


@runtime_checkable
class ProgressPort(Protocol):
    """Emits a typed, transient :class:`AgentEvent` to realtime consumers.

    Lossy by design (design §8.8). Token deltas may be coalesced or
    dropped; final complete messages are delivered only from Outbox.
    """

    async def emit(self, event: AgentEvent) -> None:
        ...


__all__ = [
    # Ports
    "TurnJournalPort",
    "ToolExecutionPort",
    "SessionCommitPort",
    "ControlInboxPort",
    "ProgressPort",
    # Enums / Literals
    "TaskKind",
    "TaskState",
    "CheckpointPhase",
    "ControlKind",
    "ControlState",
    "ToolLogicalState",
    "ModelAttemptState",
    "CommitKind",
    "EffectClass",
    "RiskClass",
    "IdempotencyMode",
    "ApprovalPolicy",
    "RecoveryPolicy",
    "ConcurrencyScope",
    "ToolOutcome",
    # DTOs
    "SafeError",
    "TaskIdentity",
    "AttemptIdentity",
    "CheckpointIdentity",
    "CompletedModelDecision",
    "CompletedToolDecision",
    "RestorePoint",
    "ModelAttemptStarted",
    "ModelAttemptResult",
    "ModelDecision",
    "TurnCheckpoint",
    "DurableProgress",
    "EffectiveToolPolicy",
    "ToolExecutionRequest",
    "ToolExecutionResult",
    "SessionMutation",
    "SessionCommitRequest",
    "SessionCommitResult",
    "ControlRequest",
    "ControlBatch",
    "ControlOutcome",
]
