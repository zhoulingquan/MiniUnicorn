"""Thin durable runtime kernel (design §10.2).

This package adds task coordination, recovery facts, fencing, and reliable
delivery around the existing Agent Core. It does not replace Agent Core.

Public surface:

- :class:`RuntimeConfig` — parsed and validated runtime configuration.
- :class:`SqliteRuntimeStore` — the only production Runtime Store façade.
- Narrow view Protocols in :mod:`miniunicorn.runtime.contracts`.

Importing this package does not import SQLite; SQLite is imported lazily
by :mod:`miniunicorn.runtime.sqlite` so Agent unit tests can run without
the runtime implementation (design §6.17).
"""

from __future__ import annotations

from miniunicorn.runtime.config import RuntimeConfig, RuntimeMode, parse_runtime_config
from miniunicorn.runtime.contracts import (
    ClaimRequest,
    ClaimedTask,
    ClaimResult,
    CompletionResult,
    ControlResult,
    DeliveryLedger,
    ExecutionJournal,
    InboundTaskEnvelope,
    InternalTaskEnvelope,
    MaintenanceLedger,
    OutboxClaim,
    ReclaimResult,
    ResourceLedger,
    ResourceLease,
    ResourceLeaseRequest,
    RetryDecision,
    RuntimeStore,
    SessionCommitLedger,
    SubmitResult,
    TaskClaim,
    TaskControlRequest,
    TaskHandle,
    TaskIngressStore,
    TaskRecord,
    TaskSnapshot,
    WaitResult,
    WorkerLedger,
)
from miniunicorn.runtime.session_committer import (
    SessionCommitter,
    clear_active_claim,
    set_active_claim,
)
from miniunicorn.runtime.scheduler import Scheduler, ClaimOutcome
from miniunicorn.runtime.task_service import TaskService
from miniunicorn.runtime.worker import (
    AgentTaskWorker,
    WorkerTaskPayload,
    WorkerExecutionResult,
    ExecutionCallback,
)
from miniunicorn.runtime.hosts import LightweightHost, SupervisedHost, RealtimeEventBridge
from miniunicorn.runtime.agent_adapter import AgentExecutionCallback
from miniunicorn.runtime.tool_gateway import ToolGateway
from miniunicorn.runtime.containment import (
    ContainmentScope,
    NullContainmentScope,
    ProcessContainmentScope,
    SupervisorContainment,
    bind_containment_scope,
    current_containment_scope,
    posix_set_child_death_signal,
    posix_start_new_session,
    reset_containment_scope,
)
from miniunicorn.runtime.durable_journal import (
    DurableTurnJournalAdapter,
    JournalProviderObserver,
)
from miniunicorn.runtime.outbox import OutboxSender
from miniunicorn.runtime.ipc import (
    IPC_PROTOCOL_VERSION,
    IpcEnvelope,
    ProcessIpcChannel,
    agent_event,
    child_ready,
    child_stopped,
    fan_out_wake_hints,
    shutdown_signal,
    wake_hint,
    worker_ready,
)
from miniunicorn.runtime.supervisor import (
    ChildEntrypoint,
    RestartPolicy,
    Supervisor,
)
from miniunicorn.runtime.models import (
    BLOB_ENCODING,
    BLOB_KIND,
    DELIVERY_RECOVERY,
    EVENT_TYPES,
    OUTBOX_DEDUP_KINDS,
    TASK_KINDS,
    CONTROL_KINDS,
    TASK_STATES,
    TERMINAL_TASK_STATES,
    TRANSITIONS,
    BlobRecord,
    BlobWrite,
    CheckpointWrite,
    CompletionWrite,
    ControlOutcomeWrite,
    DeliveryReceipt,
    DeliveryResolution,
    DurableEventRecord,
    InternalCompletionWrite,
    MediaRef,
    ModelAttemptWrite,
    ModelResultWrite,
    OutboxRecord,
    PreparedToolWrite,
    RequestScope,
    ResourceLeaseRecord,
    RetentionBatch,
    RetentionPolicy,
    RetentionResult,
    RetryKind,
    SessionCommitRecord,
    SessionCommitWrite,
    TaskControlRecord,
    TaskEventRecord,
    TaskFailure,
    ToolAttemptRecord,
    ToolAttemptWrite,
    ToolCallRecord,
    ToolResultWrite,
    WaitDecision,
    is_allowed_transition,
    is_terminal_state,
)

__all__ = [
    "RuntimeConfig",
    "RuntimeMode",
    "parse_runtime_config",
    # Session Committer
    "SessionCommitter",
    "clear_active_claim",
    "set_active_claim",
    # Task Service & Scheduler
    "TaskService",
    "Scheduler",
    "ClaimOutcome",
    # Worker
    "AgentTaskWorker",
    "WorkerTaskPayload",
    "WorkerExecutionResult",
    "ExecutionCallback",
    # Hosts
    "LightweightHost",
    "SupervisedHost",
    "RealtimeEventBridge",
    # Agent adapter
    "AgentExecutionCallback",
    # Tool Gateway & durable journal (WP4)
    "ToolGateway",
    "DurableTurnJournalAdapter",
    "JournalProviderObserver",
    # Child-process containment (WP4 task 9, WP6 task 6)
    "ContainmentScope",
    "NullContainmentScope",
    "ProcessContainmentScope",
    "SupervisorContainment",
    "bind_containment_scope",
    "current_containment_scope",
    "reset_containment_scope",
    "posix_set_child_death_signal",
    "posix_start_new_session",
    # Durable Outbox Sender (WP5)
    "OutboxSender",
    # IPC + Supervisor (WP6)
    "IPC_PROTOCOL_VERSION",
    "IpcEnvelope",
    "ProcessIpcChannel",
    "agent_event",
    "child_ready",
    "child_stopped",
    "fan_out_wake_hints",
    "shutdown_signal",
    "wake_hint",
    "worker_ready",
    "ChildEntrypoint",
    "RestartPolicy",
    "Supervisor",
    # Contracts
    "ClaimRequest",
    "ClaimedTask",
    "ClaimResult",
    "CompletionResult",
    "ControlResult",
    "DeliveryLedger",
    "ExecutionJournal",
    "InboundTaskEnvelope",
    "InternalTaskEnvelope",
    "MaintenanceLedger",
    "OutboxClaim",
    "ReclaimResult",
    "ResourceLedger",
    "ResourceLease",
    "ResourceLeaseRequest",
    "RetryDecision",
    "RuntimeStore",
    "SessionCommitLedger",
    "SubmitResult",
    "TaskClaim",
    "TaskControlRequest",
    "TaskHandle",
    "TaskIngressStore",
    "TaskRecord",
    "TaskSnapshot",
    "WaitResult",
    "WorkerLedger",
    # Models
    "BLOB_ENCODING",
    "BLOB_KIND",
    "DELIVERY_RECOVERY",
    "EVENT_TYPES",
    "OUTBOX_DEDUP_KINDS",
    "TASK_KINDS",
    "CONTROL_KINDS",
    "TASK_STATES",
    "TERMINAL_TASK_STATES",
    "TRANSITIONS",
    "BlobRecord",
    "BlobWrite",
    "CheckpointWrite",
    "CompletionWrite",
    "ControlOutcomeWrite",
    "DeliveryReceipt",
    "DeliveryResolution",
    "DurableEventRecord",
    "InternalCompletionWrite",
    "MediaRef",
    "ModelAttemptWrite",
    "ModelResultWrite",
    "OutboxRecord",
    "PreparedToolWrite",
    "RequestScope",
    "ResourceLeaseRecord",
    "RetentionBatch",
    "RetentionPolicy",
    "RetentionResult",
    "RetryKind",
    "SessionCommitRecord",
    "SessionCommitWrite",
    "TaskControlRecord",
    "TaskEventRecord",
    "TaskFailure",
    "ToolAttemptRecord",
    "ToolAttemptWrite",
    "ToolCallRecord",
    "ToolResultWrite",
    "WaitDecision",
    "is_allowed_transition",
    "is_terminal_state",
]
