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

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, runtime_checkable

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

    ``content`` is the ephemeral in-memory echo of the tool output that the
    Agent Core may feed back into the LLM context on the success path. It is
    NOT a durable fact — the durable fact is the ``result_blob_id`` /
    ``result_hash`` pair. On recovery, callers must read the result back from
    the Runtime Store blob rather than rely on ``content`` (design §20).
    """

    state: ToolLogicalState
    result_blob_id: str | None = None
    result_hash: str | None = None
    effect_receipt_ref: str | None = None
    error: SafeError | None = None
    content: Any = None


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


# ---------------------------------------------------------------------------
# Provider attempt observer (design §19)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ProviderAttemptStarted:
    """Per-network-attempt start facts reported by a Provider (design §19)."""

    provider_name: str
    model_name: str
    request_hash: str
    started_at_ms: int


@dataclass(slots=True, frozen=True)
class ProviderAttemptCompleted:
    """Per-network-attempt completion facts reported by a Provider (design §19).

    ``content`` carries the raw response text transiently so the observer
    can write it into a protected runtime blob; it is NOT stored in task
    events or logs (design §19: "raw response content stay in protected
    blobs").
    """

    response_blob_id: str
    response_hash: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None
    latency_ms: int = 0
    content: str | None = None


@dataclass(slots=True, frozen=True)
class ProviderAttemptFailed:
    """Per-network-attempt failure facts reported by a Provider (design §19)."""

    error_code: str
    error_summary: str
    latency_ms: int = 0


@runtime_checkable
class ProviderAttemptObserver(Protocol):
    """Observer the Provider calls for every network attempt (design §19).

    The Runtime supplies an observer backed by
    :class:`TurnJournalPort`; tests and legacy mode may supply a no-op
    observer. A completed response is returned to the Runner only after
    ``completed()`` succeeds (design §19).
    """

    async def started(self, value: ProviderAttemptStarted) -> str:
        """Record that a network attempt started; return the attempt id."""
        ...

    async def completed(self, attempt_id: str, value: ProviderAttemptCompleted) -> None:
        """Record that a network attempt completed durably."""
        ...

    async def failed(self, attempt_id: str, value: ProviderAttemptFailed) -> None:
        """Record that a network attempt failed."""
        ...


class NullProviderAttemptObserver:
    """No-op observer used by legacy mode and tests (design §19)."""

    async def started(self, value: ProviderAttemptStarted) -> str:
        return ""

    async def completed(self, attempt_id: str, value: ProviderAttemptCompleted) -> None:
        return None

    async def failed(self, attempt_id: str, value: ProviderAttemptFailed) -> None:
        return None


# ---------------------------------------------------------------------------
# Provider attempt observer binding (ContextVar, design §19)
# ---------------------------------------------------------------------------

_provider_attempt_observer: ContextVar[ProviderAttemptObserver | None] = ContextVar(
    "miniunicorn_provider_attempt_observer", default=None
)


@contextmanager
def bind_provider_attempt_observer(observer: ProviderAttemptObserver | None):
    """Bind a Provider attempt observer for the duration of a turn."""
    token = _provider_attempt_observer.set(observer)
    try:
        yield
    finally:
        _provider_attempt_observer.reset(token)


def current_provider_attempt_observer() -> ProviderAttemptObserver | None:
    """Return the observer bound to the current async context, or None."""
    return _provider_attempt_observer.get()


# ---------------------------------------------------------------------------
# Outbound message port (design §10.1 — Agent-owned)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class OutboundRequest:
    """A user-visible outbound message to be delivered through the Outbox."""

    content: str
    channel: str
    target_key: str
    media: tuple[str, ...] = ()
    same_target: bool = False


@dataclass(slots=True, frozen=True)
class OutboundReceipt:
    """Receipt proving the message was durably enqueued in the Outbox."""

    outbox_id: int
    dedup_key: str


@runtime_checkable
class OutboundPort(Protocol):
    """Agent Core's only path to enqueue a user-visible message."""

    async def enqueue(self, request: OutboundRequest) -> OutboundReceipt:
        ...


class DirectOutboundPort:
    """Legacy/test fallback that publishes directly to the MessageBus.

    Used when no durable Outbox is bound (legacy/test mode). Mirrors the
    former ``send_callback`` path: constructs an :class:`OutboundMessage`
    from the request and calls ``bus.publish_outbound``. The receipt
    carries a synthetic outbox_id of 0 so callers can distinguish it from
    real Outbox receipts (which are always positive integers).
    """

    def __init__(self, bus: Any) -> None:
        self._bus = bus

    async def enqueue(self, request: OutboundRequest) -> OutboundReceipt:
        from miniunicorn.bus.events import OutboundMessage

        msg = OutboundMessage(
            channel=request.channel,
            chat_id=request.target_key,
            content=request.content,
            media=list(request.media),
            metadata={},
            buttons=[],
        )
        await self._bus.publish_outbound(msg)
        return OutboundReceipt(outbox_id=0, dedup_key="")


# ---------------------------------------------------------------------------
# Containment port (design §20.7 — Agent-owned)
# ---------------------------------------------------------------------------


@runtime_checkable
class ContainmentPort(Protocol):
    """Registers child PIDs for task-scoped process-tree containment."""

    def register(self, pid: int, *, pgid: int | None = None) -> None:
        ...


# ---------------------------------------------------------------------------
# Vector memory port (design §22.2 — Agent-owned)
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorMemoryPort(Protocol):
    """Agent Core's interface to the derived vector memory index."""

    def index(self, entry: Any) -> None:
        ...

    def search(self, query: Any, *, top_k: int = 5) -> list[Any]:
        ...

    def count(self) -> int:
        ...

    def close(self) -> None:
        ...


#: Agent-owned factory callable type. Production wiring injects
#: :func:`miniunicorn.runtime.sqlite.vector_memory_store.create_vector_store`;
#: unit tests inject a callable returning a ``NoOpVectorStore`` or fake.
#:
#: The factory is typed as ``Callable[..., Any]`` because the Agent
#: memory store consumes the returned object via duck typing (it uses
#: ``index``, ``search``, ``count``, ``enabled``, plus maintenance-only
#: methods like ``decay_importance`` and ``rebuild`` that the Agent
#: never calls directly). Tightening this to a strict Protocol would
#: force the Agent package to mirror every method the SQLite
#: implementation exposes.
VectorMemoryFactory = Callable[..., Any]


# ---------------------------------------------------------------------------
# Tool execution request builder (Agent-owned, design §20.3)
# ---------------------------------------------------------------------------


def build_tool_execution_request(
    *,
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    policy: EffectiveToolPolicy,
) -> ToolExecutionRequest:
    """Build a :class:`ToolExecutionRequest` from raw tool call arguments.

    Computes the canonical arguments hash and idempotency key so the
    Runner does not depend on ``miniunicorn.runtime``.
    """
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


__all__ = [
    # Ports
    "TurnJournalPort",
    "ToolExecutionPort",
    "SessionCommitPort",
    "ControlInboxPort",
    "ProgressPort",
    "ProviderAttemptObserver",
    "OutboundPort",
    "ContainmentPort",
    "VectorMemoryPort",
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
    "ProviderAttemptStarted",
    "ProviderAttemptCompleted",
    "ProviderAttemptFailed",
    "NullProviderAttemptObserver",
    # Outbound / containment / vector
    "OutboundRequest",
    "OutboundReceipt",
    "VectorMemoryFactory",
    # Observer binding
    "bind_provider_attempt_observer",
    "current_provider_attempt_observer",
    # Builder
    "build_tool_execution_request",
]
