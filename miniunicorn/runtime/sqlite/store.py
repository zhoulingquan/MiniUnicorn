"""The only production Runtime Store façade (design §7.3, §8.4, §16).

One :class:`SqliteRuntimeStore` implements every narrow protocol view from
:mod:`miniunicorn.runtime.contracts`. The views exist for interface
segregation and tests; they share one connection factory, migration owner,
transaction policy, and database file (design §7.3).

WP1 scope (design §30 WP1): blob insert/read, task submit/dedup, control
append, session sequence allocation, claim/renew/fence, task events, retry
promotion and lease reclaim. Methods needed by later WPs (tool gateway,
session committer, outbox sender) are stubbed or deferred to their
respective WPs.

Safety invariants enforced here:

- every mutating Worker method validates ``task_id``, ``lease_token``, and
  ``lease_epoch`` before applying (design §6.10, §6.11);
- lease renewal does not increment ``state_version`` (design §6.11, §14.4);
- no SQLite transaction spans an external call (design §6.12);
- ``BEGIN IMMEDIATE`` is used for allocation, claim, reclaim, completion,
  and Outbox claim (design §16.1);
- state transitions are validated against :data:`TRANSITIONS` (design §14.2).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from typing import Any

from miniunicorn.agent.ports import (
    CompletedModelDecision,
    CompletedToolDecision,
    RestorePoint,
    SafeError,
    ToolExecutionResult,
)
from miniunicorn.runtime.contracts import (
    ClaimRequest,
    ClaimResult,
    ClaimedTask,
    CompletionResult,
    ControlResult,
    ReclaimResult,
    SessionCommitMismatchError,
    StaleLeaseError,
    SubmitResult,
    TaskClaim,
    TaskHandle,
)
from miniunicorn.runtime.models import (
    TERMINAL_TASK_STATES,
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
    is_allowed_transition,
    is_terminal_state,
)


# ---------------------------------------------------------------------------
# ID and time helpers (design §12.1, §12.2)
# ---------------------------------------------------------------------------


def _new_uuid() -> str:
    """Generate a UUID (design §12.1).

    Uses UUIDv4 for simplicity; UUIDv7 is recommended by the design but
    not required for correctness (design §12.1: "Correctness must not
    depend on UUID ordering").
    """
    return str(uuid.uuid4())


def _new_lease_token() -> str:
    """Cryptographically random 128-bit lease token encoded as hex (design §12.1)."""
    return secrets.token_hex(16)


def _now_ms() -> int:
    """Current UTC Unix milliseconds (design §12.2)."""
    import time

    return int(time.time() * 1000)


def _dedup_key_hash(parts: str) -> str:
    """SHA-256 hex of a dedup-key string (design §16.13)."""
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _content_hash(data: bytes | str) -> str:
    """SHA-256 hex of content (design §16.15)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _column_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    """Return ``row[name]`` if present, otherwise ``default``.

    The Runtime Store validates schema version before any query, so
    migration-added columns always exist on a migrated connection. The
    helper exists for ``_row_to_task_record`` so a row produced by an
    explicit ``SELECT`` that predates a migration column does not raise
    ``IndexError`` — it falls back to the column default instead.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_task_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        turn_id=row["turn_id"],
        protocol_version=row["protocol_version"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        agent_id=row["agent_id"],
        workspace_id=row["workspace_id"],
        session_key=row["session_key"],
        session_sequence=row["session_sequence"],
        channel=row["channel"],
        channel_account=row["channel_account"],
        channel_message_id=row["channel_message_id"],
        dedup_key=row["dedup_key"],
        target_key=_column_value(row, "target_key", ""),
        task_kind=row["task_kind"],
        priority=row["priority"],
        payload_blob_id=row["payload_blob_id"],
        payload_hash=row["payload_hash"],
        state=row["state"],
        checkpoint_phase=row["checkpoint_phase"],
        run_segment=row["run_segment"],
        root_attempt_count=row["root_attempt_count"],
        max_root_attempts=row["max_root_attempts"],
        recovery_pending=row["recovery_pending"],
        leased_by=row["leased_by"],
        lease_token=row["lease_token"],
        lease_epoch=row["lease_epoch"],
        lease_until_ms=row["lease_until_ms"],
        last_heartbeat_at_ms=row["last_heartbeat_at_ms"],
        last_progress_at_ms=row["last_progress_at_ms"],
        available_at_ms=row["available_at_ms"],
        state_version=row["state_version"],
        control_cursor=row["control_cursor"],
        cumulative_input_tokens=row["cumulative_input_tokens"],
        cumulative_output_tokens=row["cumulative_output_tokens"],
        error_code=row["error_code"],
        error_summary=row["error_summary"],
        waiting_reason=row["waiting_reason"],
        waiting_ref=row["waiting_ref"],
        wait_until_ms=row["wait_until_ms"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        completed_at_ms=row["completed_at_ms"],
    )


def _row_to_blob(row: sqlite3.Row) -> BlobRecord:
    return BlobRecord(
        blob_id=row["blob_id"],
        scope_key=row["scope_key"],
        blob_kind=row["blob_kind"],
        content_hash=row["content_hash"],
        encoding=row["encoding"],
        compression=row["compression"],
        encryption_key_id=row["encryption_key_id"],
        inline_content=row["inline_content"],
        external_ref=row["external_ref"],
        size_bytes=row["size_bytes"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_control(row: sqlite3.Row) -> TaskControlRecord:
    return TaskControlRecord(
        control_seq=row["control_seq"],
        control_id=row["control_id"],
        task_id=row["task_id"],
        kind=row["kind"],
        dedup_key=row["dedup_key"],
        payload_blob_id=row["payload_blob_id"],
        requested_by=row["requested_by"],
        state=row["state"],
        outcome_code=row["outcome_code"],
        requested_at_ms=row["requested_at_ms"],
        applied_at_ms=row["applied_at_ms"],
    )


def _row_to_event(row: sqlite3.Row) -> DurableEventRecord:
    return DurableEventRecord(
        event_seq=row["event_seq"],
        event_id=row["event_id"],
        task_id=row["task_id"],
        event_type=row["event_type"],
        phase=row["phase"],
        safe_payload_json=row["safe_payload_json"],
        payload_blob_id=row["payload_blob_id"],
        lease_epoch=row["lease_epoch"],
        created_at_ms=row["created_at_ms"],
    )


def _task_snapshot(record: TaskRecord) -> TaskSnapshot:
    error: SafeError | None = None
    if record.error_code:
        error = SafeError(
            error_code=record.error_code or "",
            error_summary=record.error_summary or "",
        )
    return TaskSnapshot(
        task_id=record.task_id,
        state=record.state,
        checkpoint_phase=record.checkpoint_phase,
        run_segment=record.run_segment,
        root_attempt_count=record.root_attempt_count,
        max_root_attempts=record.max_root_attempts,
        recovery_pending=record.recovery_pending,
        session_sequence=record.session_sequence,
        waiting_reason=record.waiting_reason,
        waiting_ref=record.waiting_ref,
        error=error,
        created_at_ms=record.created_at_ms,
        updated_at_ms=record.updated_at_ms,
        completed_at_ms=record.completed_at_ms,
    )


def _row_to_model_attempt(row: sqlite3.Row) -> CompletedModelDecision:
    """Map a ``model_attempts`` row (state=COMPLETED) to a decision."""
    return CompletedModelDecision(
        model_attempt_id=row["model_attempt_id"],
        logical_call_id=row["logical_call_id"],
        attempt_no=row["attempt_no"],
        response_blob_id=row["response_blob_id"],
        response_hash=row["response_hash"],
        request_hash=row["request_hash"] or "",
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        finish_reason=row["finish_reason"],
    )


def _row_to_completed_tool_decision(row: sqlite3.Row) -> CompletedToolDecision:
    """Map a ``tool_calls`` row to a completed tool decision."""
    error: SafeError | None = None
    if row["error_code"]:
        error = SafeError(
            error_code=row["error_code"],
            error_summary=row["error_summary"] or "",
        )
    return CompletedToolDecision(
        tool_call_id=row["tool_call_id"],
        tool_name=row["tool_name"],
        arguments_hash=row["arguments_hash"],
        state=row["state"],
        result_blob_id=row["result_blob_id"],
        result_hash=row["result_hash"],
        error=error,
    )


def _row_to_tool_call(row: sqlite3.Row) -> ToolCallRecord:
    """Map a ``tool_calls`` row to a :class:`ToolCallRecord`."""
    error: SafeError | None = None
    if row["error_code"]:
        error = SafeError(
            error_code=row["error_code"],
            error_summary=row["error_summary"] or "",
        )
    return ToolCallRecord(
        task_id=row["task_id"],
        tool_call_id=row["tool_call_id"],
        tool_name=row["tool_name"],
        arguments_blob_id=row["arguments_blob_id"],
        arguments_hash=row["arguments_hash"],
        state=row["state"],
        attempt_count=row["attempt_count"],
        result_blob_id=row["result_blob_id"],
        result_hash=row["result_hash"],
        effect_receipt_ref=row["effect_receipt_ref"],
        error=error,
    )


def _row_to_tool_attempt(row: sqlite3.Row) -> ToolAttemptRecord:
    """Map a ``tool_attempts`` row to a :class:`ToolAttemptRecord`."""
    error: SafeError | None = None
    if row["error_code"]:
        error = SafeError(
            error_code=row["error_code"],
            error_summary=row["error_summary"] or "",
        )
    return ToolAttemptRecord(
        tool_attempt_id=row["tool_attempt_id"],
        task_id=row["task_id"],
        tool_call_id=row["tool_call_id"],
        attempt_no=row["attempt_no"],
        state=row["state"],
        resource_token=row["resource_token"],
        effect_receipt_ref=row["effect_receipt_ref"],
        error=error,
        started_at_ms=row["started_at_ms"],
        finished_at_ms=row["finished_at_ms"],
    )


def _row_to_resource_lease(row: sqlite3.Row) -> ResourceLeaseRecord:
    """Map a ``resource_leases`` row to a :class:`ResourceLeaseRecord`."""
    return ResourceLeaseRecord(
        resource_key=row["resource_key"],
        holder_kind=row["holder_kind"],
        holder_id=row["holder_id"],
        units=row["units"],
        lease_token=row["lease_token"],
        lease_until_ms=row["lease_until_ms"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_outbox_claim(row: sqlite3.Row) -> OutboxClaim:
    """Map an ``outbox`` row to an :class:`OutboxClaim`."""
    return OutboxClaim(
        outbox_id=row["outbox_id"],
        task_id=row["task_id"],
        channel=row["channel"],
        channel_account=row["channel_account"],
        target_key=row["target_key"],
        message_kind=row["message_kind"],
        payload_blob_id=row["payload_blob_id"],
        payload_hash=row["payload_hash"],
        dedup_key=row["dedup_key"],
        lease_token=row["lease_token"] or "",
        lease_epoch=row["lease_epoch"],
        lease_until_ms=row["lease_until_ms"] or 0,
        attempt_count=row["attempt_count"],
    )


def _row_to_outbox_record(row: sqlite3.Row) -> OutboxRecord:
    """Map an ``outbox`` row to an :class:`OutboxRecord`."""
    error: SafeError | None = None
    err_code = row["error_code"]
    err_summary = row["error_summary"]
    if err_code or err_summary:
        error = SafeError(
            error_code=err_code or "DELIVERY_UNKNOWN",
            error_summary=err_summary or "",
        )
    return OutboxRecord(
        outbox_id=row["outbox_id"],
        task_id=row["task_id"],
        channel=row["channel"],
        channel_account=row["channel_account"],
        target_key=row["target_key"],
        message_kind=row["message_kind"],
        payload_blob_id=row["payload_blob_id"],
        payload_hash=row["payload_hash"],
        dedup_key=row["dedup_key"],
        state=row["state"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        available_at_ms=row["available_at_ms"],
        lease_token=row["lease_token"],
        lease_epoch=row["lease_epoch"],
        lease_until_ms=row["lease_until_ms"],
        provider_receipt_ref=row["provider_receipt_ref"],
        error=error,
        created_at_ms=row["created_at_ms"],
        delivered_at_ms=row["delivered_at_ms"],
    )


# ---------------------------------------------------------------------------
# SqliteRuntimeStore
# ---------------------------------------------------------------------------


class SqliteRuntimeStore:
    """The only production Runtime Store façade (design §7.3, §8.4).

    One SQLite implementation owns every transactional fact. The narrow
    protocol views (``TaskIngressStore``, ``WorkerLedger``, etc.) are
    satisfied by this class; callers may also use ``as_task_ingress()``,
    ``as_worker_ledger()`` etc. for interface-segregated access.

    The store is not thread-safe for concurrent use of the same
    connection (design §16.1). Each process creates its own connections.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying SQLite connection (for migration and tests)."""
        return self._conn

    # ------------------------------------------------------------------
    # Blob store (design §16.15, §12.3)
    # ------------------------------------------------------------------

    def write_blob(self, write: BlobWrite) -> BlobRecord:
        """Insert or reuse a protected runtime blob (design §16.15).

        Deduplication is by ``(scope_key, blob_kind, content_hash)``.
        ``blob_id`` is generated when absent; the caller may supply a
        deterministic id for known payloads.
        """
        if bool(write.inline_content) == bool(write.external_ref):
            raise ValueError(
                "BlobWrite requires exactly one of inline_content or external_ref"
            )
        blob_id = write.blob_id or _new_uuid()
        now_ms = write.created_at_ms or _now_ms()
        size = write.size_bytes or (
            len(write.inline_content) if write.inline_content else 0
        )

        self._conn.execute(
            "BEGIN IMMEDIATE"
        )
        try:
            # Check for an existing blob with the same dedup key.
            existing = self._conn.execute(
                "SELECT * FROM runtime_blobs "
                "WHERE scope_key=? AND blob_kind=? AND content_hash=?",
                (write.scope_key, write.blob_kind, write.content_hash),
            ).fetchone()
            if existing is not None:
                self._conn.execute("COMMIT")
                return _row_to_blob(existing)

            self._conn.execute(
                """
                INSERT INTO runtime_blobs (
                    blob_id, scope_key, blob_kind, content_hash, encoding,
                    compression, encryption_key_id, inline_content,
                    external_ref, size_bytes, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    blob_id,
                    write.scope_key,
                    write.blob_kind,
                    write.content_hash,
                    write.encoding,
                    write.compression,
                    write.encryption_key_id,
                    write.inline_content,
                    write.external_ref,
                    size,
                    now_ms,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM runtime_blobs WHERE blob_id=?", (blob_id,)
        ).fetchone()
        return _row_to_blob(row)

    def read_blob(self, blob_id: str) -> BlobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_blobs WHERE blob_id=?", (blob_id,)
        ).fetchone()
        return _row_to_blob(row) if row else None

    def read_blob_content(self, blob_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT inline_content, external_ref FROM runtime_blobs WHERE blob_id=?",
            (blob_id,),
        ).fetchone()
        if row is None:
            return None
        if row["inline_content"] is not None:
            return bytes(row["inline_content"])
        # External ref: caller must resolve via the artifact/media storage.
        return None

    # ------------------------------------------------------------------
    # Task ingress (design §11.2, §8.1, §17.1)
    # ------------------------------------------------------------------

    def submit_task(self, envelope: InboundTaskEnvelope) -> SubmitResult:
        """Submit a user task durably (design §17.1).

        One ``BEGIN IMMEDIATE`` transaction:
        1. insert or reuse payload blob;
        2. find duplicate inbound identity;
        3. allocate session sequence only when new;
        4. insert task;
        5. append ``TASK_ACCEPTED``;
        6. commit.
        """
        now_ms = envelope.received_at_ms or _now_ms()
        available_at = (
            envelope.available_at_ms
            if envelope.available_at_ms is not None
            else now_ms
        )

        # Write the payload blob first (outside the task transaction is fine
        # because blob write is idempotent and self-contained).
        scope_key = f"{envelope.scope.tenant_id}/{envelope.scope.principal_id}"
        # WP3: when the envelope carries inline payload_content, store it
        # directly in the blob so the Worker can decode it without an
        # external artifact store (design §13.1, §16.15).
        if envelope.payload_content is not None:
            blob = self.write_blob(
                BlobWrite(
                    scope_key=scope_key,
                    blob_kind="TASK_PAYLOAD",
                    content_hash=envelope.payload_hash,
                    encoding="RAW_BYTES",
                    inline_content=envelope.payload_content,
                    size_bytes=len(envelope.payload_content),
                    created_at_ms=now_ms,
                )
            )
        else:
            blob = self.write_blob(
                BlobWrite(
                    scope_key=scope_key,
                    blob_kind="TASK_PAYLOAD",
                    content_hash=envelope.payload_hash,
                    encoding="RAW_BYTES",
                    external_ref=envelope.normalized_payload_ref,
                    size_bytes=0,
                    created_at_ms=now_ms,
                )
            )

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Dedup by channel message id (design §13.1).
            if (
                envelope.channel
                and envelope.channel_message_id
                and envelope.channel_account
            ):
                dup = self._conn.execute(
                    "SELECT task_id, session_sequence FROM tasks "
                    "WHERE tenant_id=? AND channel=? AND channel_account=? "
                    "AND channel_message_id=?",
                    (
                        envelope.scope.tenant_id,
                        envelope.channel,
                        envelope.channel_account,
                        envelope.channel_message_id,
                    ),
                ).fetchone()
                if dup:
                    self._conn.execute("COMMIT")
                    return SubmitResult(
                        status="DUPLICATE",
                        task_id=dup["task_id"],
                        session_sequence=dup["session_sequence"],
                    )

            # Dedup by dedup_key (design §13.1).
            if envelope.dedup_key:
                dup = self._conn.execute(
                    "SELECT task_id, session_sequence FROM tasks "
                    "WHERE tenant_id=? AND agent_id=? AND workspace_id=? "
                    "AND task_kind=? AND dedup_key=?",
                    (
                        envelope.scope.tenant_id,
                        envelope.scope.agent_id,
                        envelope.scope.workspace_id,
                        envelope.task_kind,
                        envelope.dedup_key,
                    ),
                ).fetchone()
                if dup:
                    self._conn.execute("COMMIT")
                    return SubmitResult(
                        status="DUPLICATE",
                        task_id=dup["task_id"],
                        session_sequence=dup["session_sequence"],
                    )

            # Allocate session sequence (design §15.1).
            session = self._conn.execute(
                "SELECT next_sequence, state_version FROM session_slots "
                "WHERE session_key=?",
                (envelope.session_key,),
            ).fetchone()
            if session:
                seq = session["next_sequence"]
                self._conn.execute(
                    "UPDATE session_slots SET next_sequence=?, updated_at_ms=?, "
                    "state_version=state_version+1 WHERE session_key=?",
                    (seq + 1, now_ms, envelope.session_key),
                )
            else:
                seq = 0
                self._conn.execute(
                    "INSERT INTO session_slots "
                    "(session_key, next_sequence, active_task_id, state_version, updated_at_ms) "
                    "VALUES (?, ?, NULL, 0, ?)",
                    (envelope.session_key, seq + 1, now_ms),
                )

            task_id = _new_uuid()
            priority = envelope.priority if envelope.priority else 100
            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, turn_id, protocol_version, tenant_id, principal_id,
                    agent_id, workspace_id, session_key, session_sequence,
                    channel, channel_account, channel_message_id, dedup_key,
                    target_key,
                    task_kind, priority, payload_blob_id, payload_hash,
                    state, checkpoint_phase, run_segment, root_attempt_count,
                    max_root_attempts, recovery_pending, available_at_ms,
                    state_version, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 'ACCEPTED', 0, 0, ?, 0, ?, 0, ?, ?)
                """,
                (
                    task_id,
                    envelope.turn_id,
                    envelope.protocol_version,
                    envelope.scope.tenant_id,
                    envelope.scope.principal_id,
                    envelope.scope.agent_id,
                    envelope.scope.workspace_id,
                    envelope.session_key,
                    seq,
                    envelope.channel,
                    envelope.channel_account,
                    envelope.channel_message_id,
                    envelope.dedup_key,
                    envelope.target_key or "",
                    envelope.task_kind,
                    priority,
                    blob.blob_id,
                    envelope.payload_hash,
                    3,  # max_root_attempts
                    available_at,
                    now_ms,
                    now_ms,
                ),
            )

            self._append_event(
                task_id=task_id,
                event_type="TASK_ACCEPTED",
                phase="ACCEPTED",
                lease_epoch=None,
                safe_payload=None,
                now_ms=now_ms,
            )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return SubmitResult(
            status="ACCEPTED", task_id=task_id, session_sequence=seq
        )

    def submit_internal(self, envelope: InternalTaskEnvelope) -> SubmitResult:
        """Submit an internal/maintenance task durably (design §13.1).

        Internal tasks use a system-scoped session key and a deterministic
        dedup key that identifies one logical occurrence.
        """
        now_ms = envelope.received_at_ms or _now_ms()
        available_at = (
            envelope.available_at_ms
            if envelope.available_at_ms is not None
            else now_ms
        )
        scope_key = f"{envelope.scope.tenant_id}/{envelope.scope.principal_id}"

        if envelope.payload_content is not None:
            blob = self.write_blob(
                BlobWrite(
                    scope_key=scope_key,
                    blob_kind="TASK_PAYLOAD",
                    content_hash=envelope.payload_hash,
                    encoding="RAW_BYTES",
                    inline_content=envelope.payload_content,
                    size_bytes=len(envelope.payload_content),
                    created_at_ms=now_ms,
                )
            )
        else:
            blob = self.write_blob(
                BlobWrite(
                    scope_key=scope_key,
                    blob_kind="TASK_PAYLOAD",
                    content_hash=envelope.payload_hash,
                    encoding="RAW_BYTES",
                    external_ref=envelope.normalized_payload_ref,
                    size_bytes=0,
                    created_at_ms=now_ms,
                )
            )

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Internal dedup by (tenant, agent, workspace, task_kind, dedup_key).
            dup = self._conn.execute(
                "SELECT task_id, session_sequence FROM tasks "
                "WHERE tenant_id=? AND agent_id=? AND workspace_id=? "
                "AND task_kind=? AND dedup_key=?",
                (
                    envelope.scope.tenant_id,
                    envelope.scope.agent_id,
                    envelope.scope.workspace_id,
                    envelope.task_kind,
                    envelope.dedup_key,
                ),
            ).fetchone()
            if dup:
                self._conn.execute("COMMIT")
                return SubmitResult(
                    status="DUPLICATE",
                    task_id=dup["task_id"],
                    session_sequence=dup["session_sequence"],
                )

            session = self._conn.execute(
                "SELECT next_sequence FROM session_slots WHERE session_key=?",
                (envelope.session_key,),
            ).fetchone()
            if session:
                seq = session["next_sequence"]
                self._conn.execute(
                    "UPDATE session_slots SET next_sequence=?, updated_at_ms=? "
                    "WHERE session_key=?",
                    (seq + 1, now_ms, envelope.session_key),
                )
            else:
                seq = 0
                self._conn.execute(
                    "INSERT INTO session_slots "
                    "(session_key, next_sequence, active_task_id, state_version, updated_at_ms) "
                    "VALUES (?, ?, NULL, 0, ?)",
                    (envelope.session_key, seq + 1, now_ms),
                )

            task_id = _new_uuid()
            priority = envelope.priority if envelope.priority else 10
            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, turn_id, protocol_version, tenant_id, principal_id,
                    agent_id, workspace_id, session_key, session_sequence,
                    channel, channel_account, channel_message_id, dedup_key,
                    task_kind, priority, payload_blob_id, payload_hash,
                    state, checkpoint_phase, max_root_attempts, available_at_ms,
                    state_version, created_at_ms, updated_at_ms
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, 'QUEUED', 'ACCEPTED', ?, ?, 0, ?, ?)
                """,
                (
                    task_id,
                    envelope.protocol_version,
                    envelope.scope.tenant_id,
                    envelope.scope.principal_id,
                    envelope.scope.agent_id,
                    envelope.scope.workspace_id,
                    envelope.session_key,
                    seq,
                    envelope.dedup_key,
                    envelope.task_kind,
                    priority,
                    blob.blob_id,
                    envelope.payload_hash,
                    3,
                    available_at,
                    now_ms,
                    now_ms,
                ),
            )

            self._append_event(
                task_id=task_id,
                event_type="TASK_ACCEPTED",
                phase="ACCEPTED",
                lease_epoch=None,
                safe_payload=None,
                now_ms=now_ms,
            )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return SubmitResult(
            status="ACCEPTED", task_id=task_id, session_sequence=seq
        )

    def append_control(self, control: TaskControlRequest) -> ControlResult:
        """Append a control request to a task (design §17.12)."""
        now_ms = control.requested_at_ms or _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            task = self._conn.execute(
                "SELECT state FROM tasks WHERE task_id=?", (control.task_id,)
            ).fetchone()
            if task is None:
                self._conn.execute("COMMIT")
                return ControlResult(status="TASK_NOT_FOUND", control_id=None)
            if task["state"] in TERMINAL_TASK_STATES:
                self._conn.execute("COMMIT")
                return ControlResult(status="TASK_TERMINAL", control_id=None)

            # Dedup by (task_id, dedup_key) (design §17.12).
            dup = self._conn.execute(
                "SELECT control_id FROM task_controls "
                "WHERE task_id=? AND dedup_key=?",
                (control.task_id, control.dedup_key),
            ).fetchone()
            if dup:
                self._conn.execute("COMMIT")
                return ControlResult(status="DUPLICATE", control_id=dup["control_id"])

            control_id = control.control_id or _new_uuid()
            self._conn.execute(
                """
                INSERT INTO task_controls (
                    control_id, task_id, kind, dedup_key, payload_blob_id,
                    requested_by, state, requested_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    control_id,
                    control.task_id,
                    control.kind,
                    control.dedup_key,
                    control.payload_blob_id,
                    control.requested_by,
                    now_ms,
                ),
            )

            self._append_event(
                task_id=control.task_id,
                event_type="CONTROL_RECEIVED",
                phase=None,
                lease_epoch=None,
                safe_payload=json.dumps({"kind": control.kind}),
                now_ms=now_ms,
            )

            # CANCEL of a QUEUED task transitions directly to CANCELLED
            # (design §17.12 step 7). Record a CANCELLED error so the
            # snapshot reflects the reason (design §17.12).
            if control.kind == "CANCEL" and task["state"] == "QUEUED":
                self._transition_task(
                    control.task_id,
                    from_state="QUEUED",
                    to_state="CANCELLED",
                    now_ms=now_ms,
                    error=SafeError(
                        error_code="TASK_CANCELLED",
                        error_summary="cancelled by control request",
                    ),
                    clear_lease=True,
                )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return ControlResult(status="APPENDED", control_id=control_id)

    def read_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return _row_to_task_record(row) if row else None

    def read_task_snapshot(
        self, scope: RequestScope, task_id: str
    ) -> TaskSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=? AND tenant_id=? AND principal_id=?",
            (task_id, scope.tenant_id, scope.principal_id),
        ).fetchone()
        return _task_snapshot(_row_to_task_record(row)) if row else None

    # ------------------------------------------------------------------
    # Worker ledger (design §11.2, §8.2, §15)
    # ------------------------------------------------------------------

    def claim_next(self, request: ClaimRequest) -> ClaimResult:
        """Atomically claim the highest-priority eligible session head.

        Implements the claim algorithm from design §15.2–§15.3 in one
        ``BEGIN IMMEDIATE`` transaction. No Agent, Provider, filesystem,
        or IPC call occurs inside this transaction (design §15.3).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Promote a bounded number of due retry rows (design §15.3 step 1).
            self._promote_due_retries_impl(request.now_ms, limit=16)

            # Select one eligible task (design §15.2).
            # Maintenance gate (design §22.4): maintenance task kinds
            # (non-USER_TURN) are not claimable while any eligible
            # USER_TURN task is queued. USER_TURN tasks of any priority
            # are always user work and never gated.
            row = self._conn.execute(
                """
                SELECT t.* FROM tasks AS t
                WHERE t.state = 'QUEUED'
                  AND t.available_at_ms <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM tasks AS earlier
                      WHERE earlier.session_key = t.session_key
                        AND earlier.session_sequence < t.session_sequence
                        AND earlier.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM session_slots AS s
                      WHERE s.session_key = t.session_key
                        AND s.active_task_id IS NOT NULL
                        AND s.active_task_id <> t.task_id
                  )
                  AND (
                      t.task_kind = 'USER_TURN'
                      OR NOT EXISTS (
                          SELECT 1 FROM tasks AS ut
                          WHERE ut.state = 'QUEUED'
                            AND ut.available_at_ms <= ?
                            AND ut.task_kind = 'USER_TURN'
                            AND NOT EXISTS (
                                SELECT 1 FROM tasks AS earlier3
                                WHERE earlier3.session_key = ut.session_key
                                  AND earlier3.session_sequence < ut.session_sequence
                                  AND earlier3.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                            )
                            AND NOT EXISTS (
                                SELECT 1 FROM session_slots AS s3
                                WHERE s3.session_key = ut.session_key
                                  AND s3.active_task_id IS NOT NULL
                                  AND s3.active_task_id <> ut.task_id
                            )
                      )
                  )
                ORDER BY
                  CASE WHEN t.recovery_pending = 1 THEN 0 ELSE 1 END,
                  t.priority DESC,
                  t.available_at_ms ASC,
                  t.created_at_ms ASC
                LIMIT 1
                """,
                (request.now_ms, request.now_ms),
            ).fetchone()

            if row is None:
                self._conn.execute("COMMIT")
                return ClaimResult(claimed=None)

            record = _row_to_task_record(row)

            # Honor ClaimRequest.max_root_attempts when it is stricter than
            # the task's stored limit (design §15.3, §27). The Host sets
            # this when applying runtime policy; the task's stored value
            # is updated so subsequent reclaims respect the same cap.
            effective_max = record.max_root_attempts
            if request.max_root_attempts < effective_max:
                effective_max = request.max_root_attempts
                self._conn.execute(
                    "UPDATE tasks SET max_root_attempts=? WHERE task_id=?",
                    (effective_max, record.task_id),
                )
                record.max_root_attempts = effective_max

            # Root-attempt charging (design §15.3 step 7).
            new_root_count = record.root_attempt_count
            if record.recovery_pending == 1 or record.root_attempt_count == 0:
                new_root_count = record.root_attempt_count + 1
                if new_root_count > record.max_root_attempts:
                    # Fail terminally instead of claiming (design §15.3).
                    self._transition_task(
                        record.task_id,
                        from_state="QUEUED",
                        to_state="FAILED",
                        now_ms=request.now_ms,
                        error=SafeError(
                            error_code="TASK_ATTEMPTS_EXHAUSTED",
                            error_summary="root attempts exhausted",
                        ),
                        clear_lease=True,
                    )
                    self._append_event(
                        task_id=record.task_id,
                        event_type="TASK_FAILED",
                        phase=record.checkpoint_phase,
                        lease_epoch=record.lease_epoch,
                        safe_payload=json.dumps(
                            {"error_code": "TASK_ATTEMPTS_EXHAUSTED"}
                        ),
                        now_ms=request.now_ms,
                    )
                    self._conn.execute("COMMIT")
                    return ClaimResult(claimed=None)

            # Generate lease and transition to LEASED (design §15.3 steps 4-8).
            token = _new_lease_token()
            new_epoch = record.lease_epoch + 1
            lease_until = request.now_ms + request.lease_ms

            self._conn.execute(
                """
                UPDATE tasks SET
                    state='LEASED',
                    leased_by=?,
                    lease_token=?,
                    lease_epoch=?,
                    lease_until_ms=?,
                    last_heartbeat_at_ms=?,
                    recovery_pending=0,
                    root_attempt_count=?,
                    state_version=state_version+1,
                    updated_at_ms=?
                WHERE task_id=? AND state='QUEUED'
                """,
                (
                    request.worker_id,
                    token,
                    new_epoch,
                    lease_until,
                    request.now_ms,
                    new_root_count,
                    request.now_ms,
                    record.task_id,
                ),
            )

            # Set session_slots.active_task_id (design §15.3 step 3).
            self._conn.execute(
                "UPDATE session_slots SET active_task_id=?, updated_at_ms=? "
                "WHERE session_key=?",
                (record.task_id, request.now_ms, record.session_key),
            )

            self._append_event(
                task_id=record.task_id,
                event_type="TASK_LEASED",
                phase=record.checkpoint_phase,
                lease_epoch=new_epoch,
                safe_payload=json.dumps({"leased_by": request.worker_id}),
                now_ms=request.now_ms,
            )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        # Read back the updated record.
        updated = self.read_task(record.task_id)
        assert updated is not None
        claim = TaskClaim(
            task_id=record.task_id,
            lease_token=token,
            lease_epoch=new_epoch,
            leased_by=request.worker_id,
            lease_until_ms=lease_until,
        )
        return ClaimResult(claimed=ClaimedTask(record=updated, claim=claim))

    def mark_running(self, claim: TaskClaim, now_ms: int) -> TaskRecord:
        """Transition ``LEASED -> RUNNING`` (design §17.3)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)
            self._transition_task(
                claim.task_id,
                from_state="LEASED",
                to_state="RUNNING",
                now_ms=now_ms,
            )
            self._conn.execute(
                "UPDATE tasks SET last_progress_at_ms=? WHERE task_id=?",
                (now_ms, claim.task_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_RUNNING",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=None,
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        record = self.read_task(claim.task_id)
        assert record is not None
        return record

    def renew_lease(
        self,
        claim: TaskClaim,
        lease_until_ms: int,
        *,
        now_ms: int | None = None,
    ) -> bool:
        """Renew a valid lease without advancing ``state_version`` (design §6.11, §14.4).

        Lease renewal changes only ``lease_until_ms`` and
        ``last_heartbeat_at_ms``. It does not increment ``state_version``.
        Returns ``False`` when the lease is stale or expired so callers can
        cancel the active execution (Task 2 Step 5, §6.11).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current_now_ms = now_ms if now_ms is not None else _now_ms()
            try:
                self._validate_lease(claim, now_ms=current_now_ms, check_deadline=True)
            except StaleLeaseError:
                self._conn.execute("COMMIT")
                return False
            self._conn.execute(
                "UPDATE tasks SET lease_until_ms=?, last_heartbeat_at_ms=? "
                "WHERE task_id=? AND lease_token=? AND lease_epoch=?",
                (lease_until_ms, current_now_ms, claim.task_id, claim.lease_token, claim.lease_epoch),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return True

    def heartbeat(self, claim: TaskClaim, now_ms: int) -> bool:
        """Renew the lease and update heartbeat timestamp atomically.

        A successful heartbeat atomically sets both ``lease_until_ms`` and
        ``last_heartbeat_at_ms`` (Task 2 Step 4). Delegates to
        :meth:`renew_lease` so there is one lease-renewal transaction.
        The lease duration is preserved from the current state.
        """
        row = self._conn.execute(
            "SELECT lease_until_ms, last_heartbeat_at_ms FROM tasks "
            "WHERE task_id=? AND lease_token=? AND lease_epoch=?",
            (claim.task_id, claim.lease_token, claim.lease_epoch),
        ).fetchone()
        if row is None:
            return False
        prev_heartbeat = row["last_heartbeat_at_ms"]
        if prev_heartbeat is None:
            prev_heartbeat = row["lease_until_ms"]
        lease_duration = (
            row["lease_until_ms"] - prev_heartbeat
            if row["lease_until_ms"] is not None and prev_heartbeat is not None
            else 60_000
        )
        return self.renew_lease(claim, now_ms + lease_duration, now_ms=now_ms)

    def checkpoint(self, claim: TaskClaim, value: CheckpointWrite) -> str:
        """Save a durable checkpoint (design §17.4)."""
        checkpoint_id = value.checkpoint_id or _new_uuid()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=value.created_at_ms)
            self._conn.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, task_id, format_version, phase, run_segment,
                    ordinal, payload_blob_id, payload_hash, lease_epoch, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    claim.task_id,
                    value.format_version,
                    value.phase,
                    value.run_segment,
                    value.ordinal,
                    value.payload_blob_id,
                    value.payload_hash,
                    claim.lease_epoch,
                    value.created_at_ms,
                ),
            )
            self._conn.execute(
                "UPDATE tasks SET checkpoint_phase=?, last_progress_at_ms=?, "
                "state_version=state_version+1, updated_at_ms=? WHERE task_id=?",
                (value.phase, value.created_at_ms, value.created_at_ms, claim.task_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="CHECKPOINT_SAVED",
                phase=value.phase,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps({"ordinal": value.ordinal}),
                now_ms=value.created_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return checkpoint_id

    def record_progress(
        self, claim: TaskClaim, value: dict, now_ms: int
    ) -> None:
        """Record bounded, redacted durable progress (design §24.1)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)
            self._conn.execute(
                "UPDATE tasks SET last_progress_at_ms=?, updated_at_ms=? "
                "WHERE task_id=?",
                (now_ms, now_ms, claim.task_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def enter_retry_wait(self, claim: TaskClaim, retry: RetryDecision) -> None:
        """Transition ``RUNNING -> RETRY_WAIT`` (design §14.2)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(
                claim,
                now_ms=retry.available_at_ms,
                check_deadline=False,
            )
            self._transition_task(
                claim.task_id,
                from_state="RUNNING",
                to_state="RETRY_WAIT",
                now_ms=retry.available_at_ms,
                error=retry.error,
            )
            self._conn.execute(
                "UPDATE tasks SET available_at_ms=?, wait_until_ms=? WHERE task_id=?",
                (retry.available_at_ms, retry.available_at_ms, claim.task_id),
            )
            if retry.increment_root_attempt:
                self._conn.execute(
                    "UPDATE tasks SET root_attempt_count=root_attempt_count+1 "
                    "WHERE task_id=?",
                    (claim.task_id,),
                )
            # Release the session active slot (design §15.4).
            self._release_session_slot(claim.task_id, retry.available_at_ms)
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_RETRY_WAIT",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps({"error_code": retry.error.error_code}),
                now_ms=retry.available_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def enter_waiting_user(
        self, claim: TaskClaim, wait: WaitDecision
    ) -> WaitResult:
        """Transition ``RUNNING -> WAITING_USER`` (design §17.11)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(
                claim,
                now_ms=wait.wait_until_ms or _now_ms(),
                check_deadline=False,
            )
            self._transition_task(
                claim.task_id,
                from_state="RUNNING",
                to_state="WAITING_USER",
                now_ms=wait.wait_until_ms or _now_ms(),
            )
            self._conn.execute(
                "UPDATE tasks SET waiting_reason=?, waiting_ref=?, wait_until_ms=? "
                "WHERE task_id=?",
                (
                    wait.waiting_reason,
                    wait.waiting_ref,
                    wait.wait_until_ms,
                    claim.task_id,
                ),
            )
            # Release the session active slot but keep the task as the
            # earliest non-terminal sequence (design §15.4, §17.11).
            self._release_session_slot(claim.task_id, wait.wait_until_ms or _now_ms())
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_WAITING_USER",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps({"waiting_reason": wait.waiting_reason}),
                now_ms=wait.wait_until_ms or _now_ms(),
            )
            self._conn.execute("COMMIT")
        except StaleLeaseError:
            self._conn.execute("ROLLBACK")
            return WaitResult(status="STALE_LEASE", task_id=claim.task_id, outbox_id=None)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return WaitResult(
            status="WAITING", task_id=claim.task_id, outbox_id=None
        )

    def fail_task(self, claim: TaskClaim, failure: TaskFailure) -> None:
        """Transition to ``FAILED`` (design §17.10)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=failure.failed_at_ms)
            self._transition_task(
                claim.task_id,
                from_state=None,
                to_state="FAILED",
                now_ms=failure.failed_at_ms,
                error=failure.error,
                clear_lease=True,
            )
            self._release_session_slot(claim.task_id, failure.failed_at_ms)
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_FAILED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {"error_code": failure.error.error_code}
                ),
                now_ms=failure.failed_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def cancel_task(self, claim: TaskClaim, reason: SafeError) -> None:
        """Transition to ``CANCELLED`` (design §17.10)."""
        now_ms = _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms, check_deadline=False)
            self._transition_task(
                claim.task_id,
                from_state=None,
                to_state="CANCELLED",
                now_ms=now_ms,
                error=reason,
                clear_lease=True,
            )
            self._release_session_slot(claim.task_id, now_ms)
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_CANCELLED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps({"error_code": reason.error_code}),
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def complete_with_outbox(
        self, claim: TaskClaim, completion: CompletionWrite
    ) -> CompletionResult:
        """Complete a user task and atomically enqueue the final reply (design §17.8)."""
        now_ms = completion.completed_at_ms or _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)

            outbox_id: int | None = None
            if completion.final_reply_blob_id and not completion.suppress_final:
                # Task 7 Step 4: the final Outbox row must carry durable
                # delivery routing copied from the immutable claimed
                # TaskRecord. Empty channel/target is allowed only for
                # explicitly local/suppressed completions (design §17.8).
                if not completion.channel or not completion.target_key:
                    raise ValueError(
                        "final reply requires durable delivery routing "
                        "(channel and target_key must be non-empty)"
                    )
                dedup_key = completion.final_reply_dedup_key or _dedup_key_hash(
                    f"{claim.task_id}:final-reply"
                )
                cur = self._conn.execute(
                    """
                    INSERT INTO outbox (
                        task_id, channel, channel_account, target_key,
                        message_kind, payload_blob_id, payload_hash, dedup_key,
                        state, max_attempts, available_at_ms, lease_epoch,
                        created_at_ms
                    ) VALUES (?, ?, ?, ?, 'FINAL_REPLY', ?, ?, ?, 'PENDING', ?, ?, 0, ?)
                    """,
                    (
                        claim.task_id,
                        completion.channel,
                        completion.channel_account,
                        completion.target_key,
                        completion.final_reply_blob_id,
                        completion.final_reply_hash,
                        dedup_key,
                        8,
                        now_ms,
                        now_ms,
                    ),
                )
                outbox_id = int(cur.lastrowid)

            self._transition_task(
                claim.task_id,
                from_state=None,
                to_state="COMPLETED",
                now_ms=now_ms,
                clear_lease=True,
            )
            self._conn.execute(
                "UPDATE tasks SET cumulative_input_tokens=?, cumulative_output_tokens=? "
                "WHERE task_id=?",
                (
                    completion.cumulative_input_tokens,
                    completion.cumulative_output_tokens,
                    claim.task_id,
                ),
            )
            self._release_session_slot(
                claim.task_id, now_ms
            )

            if outbox_id is not None:
                self._append_event(
                    task_id=claim.task_id,
                    event_type="OUTBOX_ENQUEUED",
                    phase="REPLY_ENQUEUED",
                    lease_epoch=claim.lease_epoch,
                    safe_payload=json.dumps({"outbox_id": outbox_id}),
                    now_ms=now_ms,
                )
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_COMPLETED",
                phase="TERMINAL",
                lease_epoch=claim.lease_epoch,
                safe_payload=None,
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except StaleLeaseError:
            self._conn.execute("ROLLBACK")
            return CompletionResult(
                status="STALE_LEASE", task_id=claim.task_id, outbox_id=None
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return CompletionResult(
            status="COMPLETED", task_id=claim.task_id, outbox_id=outbox_id
        )

    def complete_internal(
        self, claim: TaskClaim, completion: InternalCompletionWrite
    ) -> CompletionResult:
        """Complete an internal task without a Channel/Outbox row (design §17.10)."""
        now_ms = completion.completed_at_ms or _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)
            self._transition_task(
                claim.task_id,
                from_state=None,
                to_state="COMPLETED",
                now_ms=now_ms,
                clear_lease=True,
            )
            self._release_session_slot(
                claim.task_id, now_ms
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="TASK_COMPLETED",
                phase="TERMINAL",
                lease_epoch=claim.lease_epoch,
                safe_payload=None,
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except StaleLeaseError:
            self._conn.execute("ROLLBACK")
            return CompletionResult(
                status="STALE_LEASE", task_id=claim.task_id, outbox_id=None
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return CompletionResult(
            status="COMPLETED", task_id=claim.task_id, outbox_id=None
        )

    def promote_due_retries(self, now_ms: int, limit: int) -> int:
        """Move elapsed ``RETRY_WAIT`` tasks back to ``QUEUED`` (design §8.2)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            count = self._promote_due_retries_impl(now_ms, limit)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return count

    def reclaim_expired(self, now_ms: int, limit: int) -> ReclaimResult:
        """Reclaim expired leases (design §24.2, Task 2 Step 6).

        Sets ``recovery_pending=1`` and returns the task to ``QUEUED``
        without incrementing ``root_attempt_count``. The next recovery
        claim is the only place that charges a new root attempt.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """
                SELECT task_id, session_key, lease_epoch, checkpoint_phase
                FROM tasks
                WHERE state IN ('LEASED', 'RUNNING')
                  AND lease_until_ms IS NOT NULL
                  AND lease_until_ms < ?
                LIMIT ?
                """,
                (now_ms, limit),
            ).fetchall()

            reclaimed = 0
            failed = 0
            waiting = 0
            for row in rows:
                # Return to QUEUED with recovery_pending=1 (design §24.2).
                # root_attempt_count is NOT incremented here; only the next
                # recovery claim charges a new root attempt (Task 2 Step 6).
                self._conn.execute(
                    """
                    UPDATE tasks SET
                        state='QUEUED',
                        recovery_pending=1,
                        leased_by=NULL,
                        lease_token=NULL,
                        lease_until_ms=NULL,
                        last_heartbeat_at_ms=NULL,
                        state_version=state_version+1,
                        updated_at_ms=?
                    WHERE task_id=?
                    """,
                    (now_ms, row["task_id"]),
                )
                self._release_session_slot(row["task_id"], now_ms)
                self._append_event(
                    task_id=row["task_id"],
                    event_type="LEASE_RECLAIMED",
                    phase=row["checkpoint_phase"],
                    lease_epoch=row["lease_epoch"],
                    safe_payload=None,
                    now_ms=now_ms,
                )
                reclaimed += 1

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return ReclaimResult(
            reclaimed_count=reclaimed,
            failed_count=failed,
            waiting_user_count=waiting,
        )

    # ------------------------------------------------------------------
    # Durable events (design §16.6)
    # ------------------------------------------------------------------

    def list_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[DurableEventRecord]:
        rows = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND event_seq>? "
            "ORDER BY event_seq ASC LIMIT ?",
            (task_id, after_seq, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def _append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        phase: str | None,
        lease_epoch: int | None,
        safe_payload: str | None,
        now_ms: int,
        payload_blob_id: str | None = None,
    ) -> None:
        """Append an immutable task event (design §16.6).

        Must be called inside an active ``BEGIN IMMEDIATE`` transaction.
        """
        event_id = _new_uuid()
        self._conn.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, event_type, phase, safe_payload_json,
                payload_blob_id, lease_epoch, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                event_type,
                phase,
                safe_payload,
                payload_blob_id,
                lease_epoch,
                now_ms,
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_lease(
        self,
        claim: TaskClaim,
        *,
        now_ms: int | None = None,
        check_deadline: bool = True,
    ) -> None:
        """Validate that ``claim`` matches the current task lease.

        Raises :class:`StaleLeaseError` when the token/epoch is stale, the
        task is no longer ``LEASED``/``RUNNING``, or (when
        ``check_deadline`` is true) the lease deadline has passed
        (design §6.10, §6.11, §11.2; Task 2 Step 5).

        Use one ``now_ms`` value per transaction so a transaction cannot
        observe two different deadlines. Pass ``check_deadline=False`` for
        voluntary state transitions (enter_retry_wait, enter_waiting_user,
        cancel_task) where the Worker is releasing the task — the
        token/epoch check is sufficient fencing for these.
        """
        if now_ms is None:
            now_ms = _now_ms()
        row = self._conn.execute(
            "SELECT state, lease_token, lease_epoch, lease_until_ms "
            "FROM tasks WHERE task_id=?",
            (claim.task_id,),
        ).fetchone()
        if row is None:
            raise StaleLeaseError(claim.task_id, claim.lease_epoch, "task not found")
        if check_deadline:
            deadline_ok = (
                row["lease_until_ms"] is not None
                and row["lease_until_ms"] >= now_ms
            )
        else:
            deadline_ok = True
        if (
            row["state"] not in ("LEASED", "RUNNING")
            or row["lease_token"] != claim.lease_token
            or row["lease_epoch"] != claim.lease_epoch
            or not deadline_ok
        ):
            raise StaleLeaseError(
                claim.task_id,
                claim.lease_epoch,
                f"lease mismatch: db_epoch={row['lease_epoch']}",
            )

    def _transition_task(
        self,
        task_id: str,
        *,
        from_state: str | None,
        to_state: str,
        now_ms: int,
        error: SafeError | None = None,
        clear_lease: bool = False,
    ) -> None:
        """Transition a task to ``to_state`` with validation (design §14.2).

        ``from_state=None`` means "any non-terminal state". The transition
        is validated against :data:`TRANSITIONS`. ``state_version`` is
        incremented (design §14.4). Must be called inside an active
        ``BEGIN IMMEDIATE`` transaction.
        """
        row = self._conn.execute(
            "SELECT state FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"task not found: {task_id}")
        current = row["state"]
        if from_state is not None and current != from_state:
            raise RuntimeError(
                f"task {task_id} state mismatch: expected {from_state}, got {current}"
            )
        if not is_allowed_transition(current, to_state):
            raise RuntimeError(
                f"forbidden transition: {current} -> {to_state} for task {task_id}"
            )

        if clear_lease:
            self._conn.execute(
                """
                UPDATE tasks SET
                    state=?,
                    leased_by=NULL,
                    lease_token=NULL,
                    lease_epoch=lease_epoch,
                    lease_until_ms=NULL,
                    last_heartbeat_at_ms=NULL,
                    error_code=?,
                    error_summary=?,
                    completed_at_ms=CASE WHEN ? IN ('COMPLETED','FAILED','CANCELLED') THEN ? ELSE completed_at_ms END,
                    state_version=state_version+1,
                    updated_at_ms=?
                WHERE task_id=?
                """,
                (
                    to_state,
                    error.error_code if error else None,
                    error.error_summary if error else None,
                    to_state,
                    now_ms,
                    now_ms,
                    task_id,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE tasks SET
                    state=?,
                    error_code=?,
                    error_summary=?,
                    state_version=state_version+1,
                    updated_at_ms=?
                WHERE task_id=?
                """,
                (
                    to_state,
                    error.error_code if error else None,
                    error.error_summary if error else None,
                    now_ms,
                    task_id,
                ),
            )

    def _release_session_slot(self, task_id: str, now_ms: int) -> None:
        """Clear ``session_slots.active_task_id`` if it points to ``task_id``."""
        self._conn.execute(
            "UPDATE session_slots SET active_task_id=NULL, updated_at_ms=? "
            "WHERE active_task_id=?",
            (now_ms, task_id),
        )

    def _promote_due_retries_impl(self, now_ms: int, limit: int) -> int:
        """Move elapsed ``RETRY_WAIT`` tasks back to ``QUEUED`` (design §8.2).

        Must be called inside an active ``BEGIN IMMEDIATE`` transaction.
        """
        rows = self._conn.execute(
            """
            SELECT task_id FROM tasks
            WHERE state='RETRY_WAIT' AND wait_until_ms <= ?
            LIMIT ?
            """,
            (now_ms, limit),
        ).fetchall()
        for row in rows:
            self._conn.execute(
                """
                UPDATE tasks SET state='QUEUED', available_at_ms=?, wait_until_ms=NULL,
                    state_version=state_version+1, updated_at_ms=?
                WHERE task_id=?
                """,
                (now_ms, now_ms, row["task_id"]),
            )
            self._append_event(
                task_id=row["task_id"],
                event_type="TASK_LEASED",
                phase=None,
                lease_epoch=None,
                safe_payload=json.dumps({"promoted_from": "RETRY_WAIT"}),
                now_ms=now_ms,
            )
        return len(rows)

    # ------------------------------------------------------------------
    # SessionCommitLedger (design §11.2, §17.7) — WP2
    # ------------------------------------------------------------------

    def _row_to_session_commit(self, row: sqlite3.Row) -> SessionCommitRecord:
        return SessionCommitRecord(
            session_commit_id=row["session_commit_id"],
            task_id=row["task_id"],
            commit_kind=row["commit_kind"],
            session_key=row["session_key"],
            base_revision=row["base_revision"],
            target_revision=row["target_revision"],
            content_hash=row["content_hash"],
            state=row["state"],
            error_code=row["error_code"],
            created_at_ms=row["created_at_ms"],
            committed_at_ms=row["committed_at_ms"],
        )

    def prepare_session_commit(
        self, claim: TaskClaim, value: SessionCommitWrite
    ) -> SessionCommitRecord:
        """Insert or verify a ``PREPARED`` session commit (design §17.7 step 3).

        Validates the task lease, then either:
        - inserts a new ``session_commits(PREPARED)`` row, or
        - returns the existing row if the same ``(task_id, commit_kind)`` is
          already prepared (idempotent retry).

        Appends ``SESSION_COMMIT_PREPARED``. Must be called inside an
        ``BEGIN IMMEDIATE`` transaction (opened by this method).
        """
        now_ms = value.created_at_ms or _now_ms()
        commit_id = value.session_commit_id or _new_uuid()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)

            # Check for an existing prepared/committed row (idempotent retry).
            existing = self._conn.execute(
                "SELECT * FROM session_commits WHERE task_id=? AND commit_kind=?",
                (claim.task_id, value.commit_kind),
            ).fetchone()
            if existing is not None:
                # A COMMITTED row means the work is already durably done.
                # The retry's request fields may legitimately differ
                # (e.g. base_revision advanced after INBOUND committed);
                # return the committed row so the caller sees
                # ALREADY_COMMITTED (design §17.7 step 5 crash recovery).
                if existing["state"] == "COMMITTED":
                    self._conn.execute("COMMIT")
                    return self._row_to_session_commit(existing)
                # For a PREPARED row, verify immutable fields match
                # (Task 3 Step 7). A retry with different fields is a
                # bug, not an idempotent retry of the same operation.
                mismatched: list[str] = []
                if existing["session_commit_id"] != commit_id:
                    mismatched.append("session_commit_id")
                if existing["session_key"] != value.session_key:
                    mismatched.append("session_key")
                if existing["base_revision"] != value.base_revision:
                    mismatched.append("base_revision")
                if existing["target_revision"] != value.target_revision:
                    mismatched.append("target_revision")
                if existing["content_hash"] != value.content_hash:
                    mismatched.append("content_hash")
                if mismatched:
                    # Let the except handler own ROLLBACK exactly once.
                    raise SessionCommitMismatchError(commit_id, mismatched)
                self._conn.execute("COMMIT")
                return self._row_to_session_commit(existing)

            self._conn.execute(
                """
                INSERT INTO session_commits (
                    session_commit_id, task_id, commit_kind, session_key,
                    base_revision, target_revision, content_hash, state,
                    error_code, created_at_ms, committed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PREPARED', NULL, ?, NULL)
                """,
                (
                    commit_id,
                    claim.task_id,
                    value.commit_kind,
                    value.session_key,
                    value.base_revision,
                    value.target_revision,
                    value.content_hash,
                    now_ms,
                ),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="SESSION_COMMIT_PREPARED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "session_commit_id": commit_id,
                        "commit_kind": value.commit_kind,
                        "session_key": value.session_key,
                        "base_revision": value.base_revision,
                        "target_revision": value.target_revision,
                    }
                ),
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE session_commit_id=?",
            (commit_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_session_commit(row)

    def confirm_session_commit(
        self, claim: TaskClaim, commit_id: str, revision: int, committed_at_ms: int
    ) -> SessionCommitRecord:
        """Mark a session commit ``COMMITTED`` (design §17.7 step 5).

        Revalidates the task lease, transitions the commit to ``COMMITTED``
        with the filesystem revision and timestamp, and appends
        ``SESSION_COMMITTED``. An already-confirmed commit is returned
        idempotently without re-validating the lease.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM session_commits WHERE session_commit_id=?",
                (commit_id,),
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise RuntimeError(f"session commit not found: {commit_id}")
            if row["state"] == "COMMITTED":
                # Idempotent: already confirmed — no lease revalidation.
                self._conn.execute("COMMIT")
                return self._row_to_session_commit(row)

            self._validate_lease(claim, now_ms=committed_at_ms)

            self._conn.execute(
                """
                UPDATE session_commits
                SET state='COMMITTED', committed_at_ms=?
                WHERE session_commit_id=? AND state='PREPARED'
                """,
                (committed_at_ms, commit_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="SESSION_COMMITTED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "session_commit_id": commit_id,
                        "revision": revision,
                    }
                ),
                now_ms=committed_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE session_commit_id=?",
            (commit_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_session_commit(row)

    def mark_session_conflict(
        self, claim: TaskClaim, commit_id: str, error: SafeError
    ) -> SessionCommitRecord:
        """Mark a session commit ``CONFLICT`` (design §17.7, §21.4; Task 3 Step 8).

        Records the conflict for operational alerting. Requires the
        current task claim so a stale Worker cannot mark a replacement
        attempt's commit conflicted. Uses ``check_deadline=False``
        because this is a voluntary release — the token/epoch check is
        sufficient fencing.
        """
        now_ms = _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms, check_deadline=False)
            self._conn.execute(
                """
                UPDATE session_commits
                SET state='CONFLICT', error_code=?, committed_at_ms=?
                WHERE session_commit_id=? AND state='PREPARED'
                """,
                (error.error_code, now_ms, commit_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE session_commit_id=?",
            (commit_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_session_commit(row)

    def read_session_commit(
        self, task_id: str, commit_kind: str
    ) -> SessionCommitRecord | None:
        """Read the session commit row for ``(task_id, commit_kind)``."""
        row = self._conn.execute(
            "SELECT * FROM session_commits WHERE task_id=? AND commit_kind=?",
            (task_id, commit_kind),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_session_commit(row)

    # ------------------------------------------------------------------
    # ExecutionJournal (design §11.2, §17.4–§17.6) — WP4
    # ------------------------------------------------------------------

    def load_restore_point(self, task_id: str) -> RestorePoint | None:
        """Load the latest durable restore point for ``task_id`` (design §17.4)."""
        task_row = self._conn.execute(
            "SELECT checkpoint_phase, run_segment, control_cursor, "
            "payload_blob_id, payload_hash FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            return None

        cp_row = self._conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE task_id=? "
            "ORDER BY run_segment DESC, ordinal DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        checkpoint_id = cp_row["checkpoint_id"] if cp_row is not None else None

        sc_row = self._conn.execute(
            "SELECT base_revision FROM session_commits "
            "WHERE task_id=? AND commit_kind='INBOUND'",
            (task_id,),
        ).fetchone()
        session_base_revision = (
            sc_row["base_revision"] if sc_row is not None else 0
        )

        completed_models = self.list_completed_models(task_id)
        completed_tools = self.list_completed_tools(task_id)

        return RestorePoint(
            checkpoint_id=checkpoint_id,
            phase=task_row["checkpoint_phase"],
            run_segment=task_row["run_segment"],
            session_base_revision=session_base_revision,
            completed_models=completed_models,
            completed_tools=completed_tools,
            control_cursor=task_row["control_cursor"],
            payload_blob_id=task_row["payload_blob_id"],
            payload_hash=task_row["payload_hash"],
        )

    def list_completed_models(
        self, task_id: str
    ) -> tuple[CompletedModelDecision, ...]:
        """Return all completed model attempts for ``task_id`` ordered by start."""
        rows = self._conn.execute(
            "SELECT * FROM model_attempts "
            "WHERE task_id=? AND state='COMPLETED' "
            "ORDER BY started_at_ms ASC",
            (task_id,),
        ).fetchall()
        return tuple(_row_to_model_attempt(r) for r in rows)

    def list_completed_tools(
        self, task_id: str
    ) -> tuple[CompletedToolDecision, ...]:
        """Return completed tool calls for ``task_id`` ordered by creation."""
        rows = self._conn.execute(
            "SELECT * FROM tool_calls "
            "WHERE task_id=? AND state IN ('SUCCEEDED','FAILED','OUTCOME_UNKNOWN','REJECTED') "
            "ORDER BY created_at_ms ASC",
            (task_id,),
        ).fetchall()
        return tuple(_row_to_completed_tool_decision(r) for r in rows)

    def read_tool_call(
        self, task_id: str, tool_call_id: str
    ) -> ToolCallRecord | None:
        """Read a single logical tool call by id (Task 6 Step 3).

        Used by :class:`ToolGateway._check_existing_call` to decide
        reuse / retry / unknown without invoking the tool again.
        """
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE task_id=? AND tool_call_id=?",
            (task_id, tool_call_id),
        ).fetchone()
        return _row_to_tool_call(row) if row else None

    def read_tool_result_content(self, result_blob_id: str) -> Any:
        """Decode a previously-stored tool result blob (Task 6 Step 3).

        Returns the original Python value (str or deserialized JSON) so
        the gateway can echo it as the in-memory ``content`` on reuse.
        Returns ``None`` when the blob is missing or externally stored.
        """
        raw = self.read_blob_content(result_blob_id)
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    def begin_model_attempt(
        self, claim: TaskClaim, value: ModelAttemptWrite
    ) -> str:
        """Durable-record the start of a Provider attempt (design §17.5)."""
        model_attempt_id = value.model_attempt_id or _new_uuid()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=value.started_at_ms)
            self._conn.execute(
                """
                INSERT INTO model_attempts (
                    model_attempt_id, task_id, logical_call_id, attempt_no,
                    provider_name, model_name, request_hash, state,
                    started_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'STARTED', ?)
                """,
                (
                    model_attempt_id,
                    claim.task_id,
                    value.logical_call_id,
                    value.attempt_no,
                    value.provider_name,
                    value.model_name,
                    value.request_hash,
                    value.started_at_ms,
                ),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="MODEL_STARTED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "model_attempt_id": model_attempt_id,
                        "logical_call_id": value.logical_call_id,
                        "attempt_no": value.attempt_no,
                    }
                ),
                now_ms=value.started_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return model_attempt_id

    def finish_model_attempt(
        self, claim: TaskClaim, attempt_id: str, value: ModelResultWrite
    ) -> None:
        """Durable-record a completed Provider attempt (design §17.5)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=value.finished_at_ms or _now_ms())
            self._conn.execute(
                """
                UPDATE model_attempts SET
                    state='COMPLETED',
                    response_blob_id=?,
                    response_hash=?,
                    input_tokens=?,
                    output_tokens=?,
                    finish_reason=?,
                    finished_at_ms=?
                WHERE model_attempt_id=?
                """,
                (
                    value.response_blob_id,
                    value.response_hash,
                    value.input_tokens,
                    value.output_tokens,
                    value.finish_reason,
                    value.finished_at_ms,
                    attempt_id,
                ),
            )
            self._conn.execute(
                "UPDATE tasks SET cumulative_input_tokens=cumulative_input_tokens+?, "
                "cumulative_output_tokens=cumulative_output_tokens+?, updated_at_ms=? "
                "WHERE task_id=?",
                (
                    value.input_tokens,
                    value.output_tokens,
                    value.finished_at_ms,
                    claim.task_id,
                ),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="MODEL_COMPLETED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "model_attempt_id": attempt_id,
                        "input_tokens": value.input_tokens,
                        "output_tokens": value.output_tokens,
                    }
                ),
                now_ms=value.finished_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def fail_model_attempt(
        self, claim: TaskClaim, attempt_id: str, error: SafeError, finished_at_ms: int
    ) -> None:
        """Durable-record a failed Provider attempt (design §17.5)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=finished_at_ms)
            self._conn.execute(
                """
                UPDATE model_attempts SET
                    state='FAILED',
                    error_code=?,
                    error_summary=?,
                    finished_at_ms=?
                WHERE model_attempt_id=?
                """,
                (error.error_code, error.error_summary, finished_at_ms, attempt_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="MODEL_FAILED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "model_attempt_id": attempt_id,
                        "error_code": error.error_code,
                    }
                ),
                now_ms=finished_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def prepare_tool_call(
        self, claim: TaskClaim, value: PreparedToolWrite
    ) -> ToolCallRecord:
        """Insert a logical tool call (idempotent) (design §17.6, §20)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=value.created_at_ms)
            existing = self._conn.execute(
                "SELECT * FROM tool_calls WHERE task_id=? AND tool_call_id=?",
                (claim.task_id, value.tool_call_id),
            ).fetchone()
            if existing is not None:
                if existing["arguments_hash"] != value.arguments_hash:
                    raise ValueError("TOOL_CALL_ID_CONFLICT")
                self._conn.execute("COMMIT")
                return _row_to_tool_call(existing)

            initial_state = (
                "WAITING_APPROVAL" if value.approval_policy == "ALWAYS" else "PREPARED"
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO tool_calls (
                    task_id, tool_call_id, tool_name, arguments_blob_id,
                    arguments_hash, effect_class, risk_class, idempotency_mode,
                    idempotency_key, approval_policy, recovery_policy,
                    concurrency_scope, state, attempt_count,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    claim.task_id,
                    value.tool_call_id,
                    value.tool_name,
                    value.arguments_blob_id,
                    value.arguments_hash,
                    value.effect_class,
                    value.risk_class,
                    value.idempotency_mode,
                    value.idempotency_key,
                    value.approval_policy,
                    value.recovery_policy,
                    value.concurrency_scope,
                    initial_state,
                    value.created_at_ms,
                    value.created_at_ms,
                ),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="TOOL_PREPARED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "tool_call_id": value.tool_call_id,
                        "tool_name": value.tool_name,
                        "state": initial_state,
                    }
                ),
                now_ms=value.created_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE task_id=? AND tool_call_id=?",
            (claim.task_id, value.tool_call_id),
        ).fetchone()
        assert row is not None
        return _row_to_tool_call(row)

    def begin_tool_attempt(
        self, claim: TaskClaim, value: ToolAttemptWrite
    ) -> ToolAttemptRecord:
        """Durable-record the start of a tool invocation attempt (design §17.6)."""
        tool_attempt_id = value.tool_attempt_id or _new_uuid()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=value.started_at_ms)
            self._conn.execute(
                """
                INSERT INTO tool_attempts (
                    tool_attempt_id, task_id, tool_call_id, attempt_no,
                    state, resource_token, started_at_ms
                ) VALUES (?, ?, ?, ?, 'STARTED', ?, ?)
                """,
                (
                    tool_attempt_id,
                    claim.task_id,
                    value.tool_call_id,
                    value.attempt_no,
                    value.resource_token,
                    value.started_at_ms,
                ),
            )
            self._conn.execute(
                "UPDATE tool_calls SET state='RUNNING', attempt_count=attempt_count+1, "
                "updated_at_ms=? WHERE task_id=? AND tool_call_id=?",
                (value.started_at_ms, claim.task_id, value.tool_call_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="TOOL_STARTED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "tool_attempt_id": tool_attempt_id,
                        "tool_call_id": value.tool_call_id,
                        "attempt_no": value.attempt_no,
                    }
                ),
                now_ms=value.started_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        row = self._conn.execute(
            "SELECT * FROM tool_attempts WHERE tool_attempt_id=?",
            (tool_attempt_id,),
        ).fetchone()
        assert row is not None
        return _row_to_tool_attempt(row)

    def finish_tool_attempt(
        self, claim: TaskClaim, attempt_id: str, value: ToolResultWrite
    ) -> ToolExecutionResult:
        """Durable-record the terminal state of a tool attempt (design §17.6)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=value.finished_at_ms or _now_ms())
            attempt_row = self._conn.execute(
                "SELECT tool_call_id FROM tool_attempts WHERE tool_attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt_row is None:
                raise RuntimeError(f"tool attempt not found: {attempt_id}")
            tool_call_id = attempt_row["tool_call_id"]

            error_code = value.error.error_code if value.error else None
            error_summary = value.error.error_summary if value.error else None

            self._conn.execute(
                """
                UPDATE tool_attempts SET
                    state=?,
                    effect_receipt_ref=?,
                    error_code=?,
                    error_summary=?,
                    finished_at_ms=?
                WHERE tool_attempt_id=?
                """,
                (
                    value.state,
                    value.effect_receipt_ref,
                    error_code,
                    error_summary,
                    value.finished_at_ms,
                    attempt_id,
                ),
            )
            self._conn.execute(
                """
                UPDATE tool_calls SET
                    state=?,
                    result_blob_id=?,
                    result_hash=?,
                    effect_receipt_ref=?,
                    error_code=?,
                    error_summary=?,
                    updated_at_ms=?
                WHERE task_id=? AND tool_call_id=?
                """,
                (
                    value.state,
                    value.result_blob_id,
                    value.result_hash,
                    value.effect_receipt_ref,
                    error_code,
                    error_summary,
                    value.finished_at_ms,
                    claim.task_id,
                    tool_call_id,
                ),
            )

            event_type = {
                "SUCCEEDED": "TOOL_COMPLETED",
                "FAILED": "TOOL_FAILED",
                "OUTCOME_UNKNOWN": "TOOL_OUTCOME_UNKNOWN",
            }.get(value.state, "TOOL_COMPLETED")
            self._append_event(
                task_id=claim.task_id,
                event_type=event_type,
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "tool_attempt_id": attempt_id,
                        "tool_call_id": tool_call_id,
                        "state": value.state,
                    }
                ),
                now_ms=value.finished_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return ToolExecutionResult(
            state=value.state,
            result_blob_id=value.result_blob_id,
            result_hash=value.result_hash,
            effect_receipt_ref=value.effect_receipt_ref,
            error=value.error,
        )

    def mark_tool_unknown(
        self, claim: TaskClaim, attempt_id: str, error: SafeError, finished_at_ms: int
    ) -> None:
        """Mark a tool attempt ``OUTCOME_UNKNOWN`` (design §17.6, §24)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=finished_at_ms)
            attempt_row = self._conn.execute(
                "SELECT tool_call_id FROM tool_attempts WHERE tool_attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt_row is None:
                raise RuntimeError(f"tool attempt not found: {attempt_id}")
            tool_call_id = attempt_row["tool_call_id"]

            self._conn.execute(
                """
                UPDATE tool_attempts SET
                    state='OUTCOME_UNKNOWN',
                    error_code=?,
                    error_summary=?,
                    finished_at_ms=?
                WHERE tool_attempt_id=?
                """,
                (error.error_code, error.error_summary, finished_at_ms, attempt_id),
            )
            self._conn.execute(
                "UPDATE tool_calls SET state='OUTCOME_UNKNOWN', updated_at_ms=? "
                "WHERE task_id=? AND tool_call_id=?",
                (finished_at_ms, claim.task_id, tool_call_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="TOOL_OUTCOME_UNKNOWN",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "tool_attempt_id": attempt_id,
                        "tool_call_id": tool_call_id,
                        "error_code": error.error_code,
                    }
                ),
                now_ms=finished_at_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def list_pending_controls(
        self, claim: TaskClaim, after_control_seq: int
    ) -> list[TaskControlRecord]:
        """Return pending controls for the task after ``after_control_seq``."""
        rows = self._conn.execute(
            "SELECT * FROM task_controls "
            "WHERE task_id=? AND control_seq > ? AND state='PENDING' "
            "ORDER BY control_seq ASC",
            (claim.task_id, after_control_seq),
        ).fetchall()
        return [_row_to_control(r) for r in rows]

    def acknowledge_control(
        self, claim: TaskClaim, outcome: ControlOutcomeWrite
    ) -> None:
        """Apply a control outcome and advance ``control_cursor`` (design §17.12)."""
        now_ms = outcome.applied_at_ms or _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)
            self._conn.execute(
                """
                UPDATE task_controls SET
                    state=?,
                    outcome_code=?,
                    applied_at_ms=?
                WHERE control_id=?
                """,
                (outcome.state, outcome.outcome_code, outcome.applied_at_ms, outcome.control_id),
            )
            self._conn.execute(
                "UPDATE tasks SET control_cursor="
                "(SELECT COALESCE(MAX(control_seq), 0) FROM task_controls "
                "WHERE task_id=? AND state IN ('APPLIED','REJECTED','EXPIRED')), "
                "state_version=state_version+1, updated_at_ms=? "
                "WHERE task_id=?",
                (claim.task_id, now_ms, claim.task_id),
            )
            self._append_event(
                task_id=claim.task_id,
                event_type="CONTROL_APPLIED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps(
                    {
                        "control_id": outcome.control_id,
                        "state": outcome.state,
                        "outcome_code": outcome.outcome_code,
                    }
                ),
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # ResourceLedger (design §11.2, §16.14) — WP4
    # ------------------------------------------------------------------

    def acquire_resource(self, request: ResourceLeaseRequest) -> ResourceLease | None:
        """Acquire or renew a resource lease (design §16.14)."""
        lease_token = request.lease_token or _new_lease_token()
        lease_until_ms = request.now_ms + request.lease_ms
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Delete expired holders for this resource_key.
            self._conn.execute(
                "DELETE FROM resource_leases WHERE resource_key=? AND lease_until_ms < ?",
                (request.resource_key, request.now_ms),
            )
            # Renew in place when the same holder already holds the resource.
            existing = self._conn.execute(
                "SELECT * FROM resource_leases "
                "WHERE resource_key=? AND holder_kind=? AND holder_id=?",
                (request.resource_key, request.holder_kind, request.holder_id),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE resource_leases SET units=?, lease_token=?, "
                    "lease_until_ms=?, updated_at_ms=? "
                    "WHERE resource_key=? AND holder_kind=? AND holder_id=?",
                    (
                        request.units,
                        lease_token,
                        lease_until_ms,
                        request.now_ms,
                        request.resource_key,
                        request.holder_kind,
                        request.holder_id,
                    ),
                )
                self._conn.execute("COMMIT")
            else:
                # WP4 simplicity: TASK holders take an exclusive lock
                # (capacity 1). Reject if any other unexpired lease exists.
                if request.holder_kind == "TASK" and request.units > 0:
                    conflict = self._conn.execute(
                        "SELECT 1 FROM resource_leases "
                        "WHERE resource_key=? AND lease_until_ms >= ? LIMIT 1",
                        (request.resource_key, request.now_ms),
                    ).fetchone()
                    if conflict is not None:
                        self._conn.execute("COMMIT")
                        return None
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO resource_leases (
                        resource_key, holder_kind, holder_id, units, lease_token,
                        lease_until_ms, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.resource_key,
                        request.holder_kind,
                        request.holder_id,
                        request.units,
                        lease_token,
                        lease_until_ms,
                        request.now_ms,
                        request.now_ms,
                    ),
                )
                self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return ResourceLease(
            resource_key=request.resource_key,
            holder_kind=request.holder_kind,
            holder_id=request.holder_id,
            units=request.units,
            lease_token=lease_token,
            lease_until_ms=lease_until_ms,
        )

    def renew_resource(self, lease: ResourceLease, until_ms: int) -> bool:
        """Renew a resource lease. Returns True if a row was updated."""
        now_ms = _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "UPDATE resource_leases SET lease_until_ms=?, updated_at_ms=? "
                "WHERE resource_key=? AND holder_kind=? AND holder_id=? AND lease_token=?",
                (
                    until_ms,
                    now_ms,
                    lease.resource_key,
                    lease.holder_kind,
                    lease.holder_id,
                    lease.lease_token,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount > 0

    def release_resource(self, lease: ResourceLease) -> bool:
        """Release a resource lease. Returns True if a row was deleted."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "DELETE FROM resource_leases "
                "WHERE resource_key=? AND holder_kind=? AND holder_id=? AND lease_token=?",
                (
                    lease.resource_key,
                    lease.holder_kind,
                    lease.holder_id,
                    lease.lease_token,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount > 0

    def read_resource_lease(
        self, resource_key: str, holder_kind: str, holder_id: str
    ) -> ResourceLeaseRecord | None:
        """Read a resource lease row by ``(resource_key, holder_kind, holder_id)``."""
        row = self._conn.execute(
            "SELECT * FROM resource_leases "
            "WHERE resource_key=? AND holder_kind=? AND holder_id=?",
            (resource_key, holder_kind, holder_id),
        ).fetchone()
        return _row_to_resource_lease(row) if row else None

    # ------------------------------------------------------------------
    # DeliveryLedger (Outbox Sender) — design §17.9, §17.13, WP5
    # ------------------------------------------------------------------

    def claim_next_delivery(
        self, sender_id: str, now_ms: int, lease_ms: int
    ) -> OutboxClaim | None:
        """Claim the earliest eligible outbox row (design §17.9).

        A row is claimable only when no earlier non-terminal row exists
        for the same ``(channel, channel_account, target_key)``. This
        prevents a retrying message from being overtaken by a later
        final reply (design §16.13).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Find the earliest claimable row: a PENDING or RETRY_WAIT row
            # whose target has no earlier non-terminal row.
            row = self._conn.execute(
                """
                SELECT o.* FROM outbox o
                WHERE o.state IN ('PENDING', 'RETRY_WAIT')
                  AND o.available_at_ms <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM outbox o2
                    WHERE o2.channel = o.channel
                      AND o2.channel_account = o.channel_account
                      AND o2.target_key = o.target_key
                      AND o2.outbox_id < o.outbox_id
                      AND o2.state IN (
                        'PENDING', 'SENDING', 'RETRY_WAIT', 'OUTCOME_UNKNOWN'
                      )
                  )
                ORDER BY o.outbox_id ASC
                LIMIT 1
                """,
                (now_ms,),
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None

            lease_token = _new_lease_token()
            lease_epoch = row["lease_epoch"] + 1
            lease_until_ms = now_ms + lease_ms
            attempt_count = row["attempt_count"] + 1

            self._conn.execute(
                """
                UPDATE outbox SET
                    state='SENDING',
                    leased_by=?,
                    lease_token=?,
                    lease_epoch=?,
                    lease_until_ms=?,
                    attempt_count=?
                WHERE outbox_id=?
                """,
                (sender_id, lease_token, lease_epoch, lease_until_ms, attempt_count, row["outbox_id"]),
            )
            self._conn.execute("COMMIT")
            # Re-read to get the updated row.
            updated = self._conn.execute(
                "SELECT * FROM outbox WHERE outbox_id=?", (row["outbox_id"],)
            ).fetchone()
            return _row_to_outbox_claim(updated)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def renew_delivery_lease(self, claim: OutboxClaim, until_ms: int) -> bool:
        """Renew the delivery lease for an in-progress send (design §17.9)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE outbox SET lease_until_ms=?
                WHERE outbox_id=? AND lease_token=? AND lease_epoch=?
                """,
                (until_ms, claim.outbox_id, claim.lease_token, claim.lease_epoch),
            )
            self._conn.execute("COMMIT")
            return cur.rowcount > 0
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def mark_delivered(self, claim: OutboxClaim, receipt: DeliveryReceipt) -> None:
        """Mark an outbox row as DELIVERED with the provider receipt (design §17.9)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE outbox SET
                    state='DELIVERED',
                    provider_receipt_ref=?,
                    delivered_at_ms=?,
                    lease_until_ms=NULL,
                    error_code=NULL,
                    error_summary=NULL
                WHERE outbox_id=? AND lease_token=? AND lease_epoch=?
                """,
                (
                    receipt.provider_message_id or receipt.receipt_ref,
                    _now_ms(),
                    claim.outbox_id,
                    claim.lease_token,
                    claim.lease_epoch,
                ),
            )
            if cur.rowcount == 0:
                self._conn.execute("ROLLBACK")
                raise StaleLeaseError(
                    str(claim.outbox_id), claim.lease_epoch, "delivery lease stale"
                )
            self._conn.execute("COMMIT")
        except StaleLeaseError:
            self._conn.execute("ROLLBACK")
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def retry_delivery(self, claim: OutboxClaim, retry: RetryDecision) -> None:
        """Schedule a retry with bounded backoff (design §17.9)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT max_attempts, attempt_count FROM outbox WHERE outbox_id=? "
                "AND lease_token=? AND lease_epoch=?",
                (claim.outbox_id, claim.lease_token, claim.lease_epoch),
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise StaleLeaseError(
                    str(claim.outbox_id), claim.lease_epoch, "delivery lease stale"
                )
            if row["attempt_count"] >= row["max_attempts"]:
                # Exhausted retries → permanent failure.
                self._conn.execute(
                    """
                    UPDATE outbox SET
                        state='FAILED',
                        error_code=?,
                        error_summary=?,
                        lease_until_ms=NULL
                    WHERE outbox_id=?
                    """,
                    (retry.error.error_code, retry.error.error_summary, claim.outbox_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE outbox SET
                        state='RETRY_WAIT',
                        available_at_ms=?,
                        error_code=?,
                        error_summary=?,
                        lease_until_ms=NULL
                    WHERE outbox_id=?
                    """,
                    (
                        retry.available_at_ms,
                        retry.error.error_code,
                        retry.error.error_summary,
                        claim.outbox_id,
                    ),
                )
            self._conn.execute("COMMIT")
        except StaleLeaseError:
            self._conn.execute("ROLLBACK")
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def fail_delivery(self, claim: OutboxClaim, error: SafeError) -> None:
        """Mark an outbox row as permanently FAILED (design §17.9)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE outbox SET
                    state='FAILED',
                    error_code=?,
                    error_summary=?,
                    lease_until_ms=NULL
                WHERE outbox_id=? AND lease_token=? AND lease_epoch=?
                """,
                (error.error_code, error.error_summary, claim.outbox_id, claim.lease_token, claim.lease_epoch),
            )
            if cur.rowcount == 0:
                self._conn.execute("ROLLBACK")
                raise StaleLeaseError(
                    str(claim.outbox_id), claim.lease_epoch, "delivery lease stale"
                )
            self._conn.execute("COMMIT")
        except StaleLeaseError:
            self._conn.execute("ROLLBACK")
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def resolve_unknown_delivery(
        self,
        outbox_id: int,
        resolution: DeliveryResolution,
        *,
        receipt: DeliveryReceipt | None = None,
        resolved_by: str,
    ) -> None:
        """Resolve an ``OUTCOME_UNKNOWN`` row (design §17.13).

        Requires an explicit operational action:
        - ``MARK_DELIVERED``: mark as DELIVERED with optional receipt.
        - ``RETRY``: return to PENDING for re-send.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                self._conn.execute("ROLLBACK")
                raise ValueError(f"outbox row {outbox_id} not found")
            if row["state"] != "OUTCOME_UNKNOWN":
                self._conn.execute("ROLLBACK")
                raise ValueError(
                    f"outbox row {outbox_id} is {row['state']}, not OUTCOME_UNKNOWN"
                )
            now_ms = _now_ms()
            if resolution == "MARK_DELIVERED":
                self._conn.execute(
                    """
                    UPDATE outbox SET
                        state='DELIVERED',
                        provider_receipt_ref=?,
                        delivered_at_ms=?,
                        error_code=NULL,
                        error_summary=NULL
                    WHERE outbox_id=?
                    """,
                    (
                        receipt.provider_message_id if receipt else None,
                        now_ms,
                        outbox_id,
                    ),
                )
            elif resolution == "RETRY":
                self._conn.execute(
                    """
                    UPDATE outbox SET
                        state='PENDING',
                        available_at_ms=?,
                        error_code=NULL,
                        error_summary=NULL
                    WHERE outbox_id=?
                    """,
                    (now_ms, outbox_id),
                )
            self._conn.execute("COMMIT")
        except ValueError:
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def read_outbox_record(self, outbox_id: int) -> OutboxRecord | None:
        """Read an outbox row by ID."""
        row = self._conn.execute(
            "SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        return _row_to_outbox_record(row) if row else None

    def read_final_reply(
        self,
        scope: RequestScope,
        task_id: str,
    ) -> DurableReply | None:
        """Read the final-reply content for a completed task (design Task 4).

        Joins ``tasks`` to the task's ``FINAL_REPLY`` Outbox row and
        ``runtime_blobs``. Verifies tenant, principal, agent, and workspace
        scope before returning content.

        Task 7 Step 7: reply reads are independent from delivery state.
        The protected final payload may be read for any Outbox state
        (including ``SENDING``, ``FAILED``, and ``OUTCOME_UNKNOWN``) so a
        stuck Channel cannot hide a completed reply. The Outbox state is
        surfaced in returned metadata as ``delivery_state`` so callers
        can distinguish delivered from in-flight/failed replies.
        Returns ``None`` when no final-reply Outbox row exists.
        """
        row = self._conn.execute(
            """
            SELECT t.state AS task_state,
                   o.outbox_id AS outbox_id,
                   o.state AS outbox_state,
                   b.inline_content AS inline_content,
                   b.encoding AS encoding
            FROM tasks t
            JOIN outbox o ON o.task_id = t.task_id
                AND o.message_kind = 'FINAL_REPLY'
            LEFT JOIN runtime_blobs b ON b.blob_id = o.payload_blob_id
            WHERE t.task_id = ?
              AND t.tenant_id = ?
              AND t.principal_id = ?
              AND t.agent_id = ?
              AND t.workspace_id = ?
            ORDER BY o.outbox_id DESC
            LIMIT 1
            """,
            (
                task_id,
                scope.tenant_id,
                scope.principal_id,
                scope.agent_id,
                scope.workspace_id,
            ),
        ).fetchone()
        if row is None:
            return None
        content = ""
        if row["inline_content"] is not None and row["encoding"] == "RAW_BYTES":
            # Task 7 Step 6: decode the versioned Outbox codec envelope.
            # Legacy raw-text FINAL_REPLY blobs fall back to the literal
            # UTF-8 string via decode_outbox_payload's FINAL_REPLY path.
            from miniunicorn.runtime.outbox_payload import decode_outbox_payload

            content = decode_outbox_payload(
                "FINAL_REPLY", bytes(row["inline_content"])
            ).content
        return DurableReply(
            content=content,
            outbox_id=int(row["outbox_id"]),
            metadata={
                "task_id": task_id,
                "state": row["task_state"],
                "outbox_id": int(row["outbox_id"]),
                "delivery_state": row["outbox_state"],
            },
        )

    def enqueue_message_tool_outbox(
        self,
        claim: TaskClaim,
        *,
        channel: str,
        channel_account: str,
        target_key: str,
        payload_blob_id: str,
        payload_hash: str,
        dedup_key: str,
        now_ms: int | None = None,
    ) -> int:
        """Enqueue a Message Tool outbox row under the task lease (design §20.6).

        Steps 2 and 3 of §20.6 occur in one Runtime Store transaction
        under the task lease.
        """
        now_ms = now_ms or _now_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._validate_lease(claim, now_ms=now_ms)
            cur = self._conn.execute(
                """
                INSERT INTO outbox (
                    task_id, channel, channel_account, target_key,
                    message_kind, payload_blob_id, payload_hash, dedup_key,
                    state, max_attempts, available_at_ms, lease_epoch,
                    created_at_ms
                ) VALUES (?, ?, ?, ?, 'MESSAGE_TOOL', ?, ?, ?, 'PENDING', ?, ?, 0, ?)
                """,
                (
                    claim.task_id,
                    channel,
                    channel_account,
                    target_key,
                    payload_blob_id,
                    payload_hash,
                    dedup_key,
                    8,
                    now_ms,
                    now_ms,
                ),
            )
            outbox_id = int(cur.lastrowid)
            self._append_event(
                task_id=claim.task_id,
                event_type="OUTBOX_ENQUEUED",
                phase=None,
                lease_epoch=claim.lease_epoch,
                safe_payload=json.dumps({"outbox_id": outbox_id, "kind": "MESSAGE_TOOL"}),
                now_ms=now_ms,
            )
            self._conn.execute("COMMIT")
            return outbox_id
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # MaintenanceLedger (design §11.2, §33.4, §16.16) — WP7
    # ------------------------------------------------------------------

    def list_retention_batch(
        self, policy: RetentionPolicy, now_ms: int
    ) -> RetentionBatch:
        """Select a bounded batch of terminal rows for retention deletion.

        Implements the deletion order from design §16.16: child rows
        (events, checkpoints, attempts) are deleted before their parent
        task, and delivered outbox rows before their parent task. Only
        terminal tasks older than the policy thresholds are selected.
        """
        success_cutoff = now_ms - policy.successful_task_age_days * 86_400_000
        failure_cutoff = now_ms - policy.failed_task_age_days * 86_400_000
        limit = policy.batch_size

        # Select terminal tasks that have NO non-terminal outbox rows
        # (design §16.16). A task with a pending/undelivered outbox row is
        # not yet retention-eligible because deleting it would violate the
        # outbox -> tasks FK constraint.
        rows = self._conn.execute(
            """
            SELECT task_id FROM tasks
            WHERE state IN ('COMPLETED', 'CANCELLED')
              AND completed_at_ms IS NOT NULL
              AND completed_at_ms < ?
              AND NOT EXISTS (
                  SELECT 1 FROM outbox o
                  WHERE o.task_id = tasks.task_id
                    AND o.state NOT IN ('DELIVERED', 'WAIVED', 'FAILED')
              )
            UNION
            SELECT task_id FROM tasks
            WHERE state = 'FAILED'
              AND completed_at_ms IS NOT NULL
              AND completed_at_ms < ?
              AND NOT EXISTS (
                  SELECT 1 FROM outbox o
                  WHERE o.task_id = tasks.task_id
                    AND o.state NOT IN ('DELIVERED', 'WAIVED', 'FAILED')
              )
            ORDER BY task_id
            LIMIT ?
            """,
            (success_cutoff, failure_cutoff, limit),
        ).fetchall()
        task_ids = tuple(r["task_id"] for r in rows)

        # Delivered or waived outbox rows for those tasks.
        if task_ids:
            outbox_ph = ",".join("?" for _ in task_ids)
            outbox_rows = self._conn.execute(
                f"""
                SELECT outbox_id FROM outbox
                WHERE task_id IN ({outbox_ph})
                AND state IN ('DELIVERED', 'WAIVED', 'FAILED')
                ORDER BY outbox_id
                LIMIT ?
                """,
                (*task_ids, limit),
            ).fetchall()
        else:
            outbox_rows = []
        outbox_ids = tuple(r["outbox_id"] for r in outbox_rows)

        # Unreferenced blobs (no remaining references from tasks,
        # outbox, checkpoints, events, or attempts).
        # tool_attempts has effect_receipt_ref (not a blob id), so it is
        # not included here; tool_calls.result_blob_id is the blob reference
        # for tool results.
        blob_rows = self._conn.execute(
            """
            SELECT b.blob_id FROM runtime_blobs b
            WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM model_attempts m WHERE m.response_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.result_blob_id = b.blob_id
                              OR tc.arguments_blob_id = b.blob_id)
            ORDER BY b.blob_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        blob_ids = tuple(r["blob_id"] for r in blob_rows)

        return RetentionBatch(
            task_ids=task_ids,
            outbox_ids=outbox_ids,
            blob_ids=blob_ids,
        )

    def delete_retention_batch(self, batch: RetentionBatch) -> RetentionResult:
        """Delete a retention batch in FK-safe order (design §16.16).

        Order: child rows first (events, checkpoints, attempts, controls),
        then outbox rows, then terminal tasks, then unreferenced blobs.
        Non-terminal tasks and non-terminal outbox rows are never deleted.
        Foreign-key failure aborts the batch and raises.
        """
        if not batch.task_ids and not batch.outbox_ids and not batch.blob_ids:
            return RetentionResult(
                deleted_tasks=0, deleted_outbox=0, deleted_blobs=0, skipped=0
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            deleted_tasks = 0
            deleted_outbox = 0
            deleted_blobs = 0
            skipped = 0

            # 1. Delete child rows for selected tasks (design §16.16).
            placeholders = ",".join("?" for _ in batch.task_ids)
            if batch.task_ids:
                # Verify all selected tasks are still terminal.
                non_terminal = self._conn.execute(
                    f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) "
                    f"AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                    batch.task_ids,
                ).fetchall()
                if non_terminal:
                    skipped += len(non_terminal)
                    safe_ids = tuple(
                        r["task_id"] for r in self._conn.execute(
                            f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) "
                            f"AND state IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                            batch.task_ids,
                        ).fetchall()
                    )
                    safe_ph = ",".join("?" for _ in safe_ids) if safe_ids else None
                else:
                    safe_ids = batch.task_ids
                    safe_ph = placeholders

                if safe_ids and safe_ph:
                    # Delete child rows in FK order. tool_attempts must be
                    # deleted before tool_calls (FK: tool_attempts -> tool_calls),
                    # and all child rows must be deleted before tasks.
                    self._conn.execute(
                        f"DELETE FROM task_events WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM checkpoints WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM model_attempts WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM tool_attempts WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM tool_calls WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM task_controls WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM session_commits WHERE task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    # Delete outbox rows for those tasks.
                    cur = self._conn.execute(
                        f"DELETE FROM outbox WHERE task_id IN ({safe_ph}) "
                        f"AND state IN ('DELIVERED', 'WAIVED', 'FAILED')",
                        safe_ids,
                    )
                    deleted_outbox += cur.rowcount
                    # Clear session slots.
                    self._conn.execute(
                        f"UPDATE session_slots SET active_task_id=NULL "
                        f"WHERE active_task_id IN ({safe_ph})",
                        safe_ids,
                    )
                    # Delete the terminal tasks.
                    cur = self._conn.execute(
                        f"DELETE FROM tasks WHERE task_id IN ({safe_ph}) "
                        f"AND state IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                        safe_ids,
                    )
                    deleted_tasks += cur.rowcount

            # 2. Delete standalone outbox rows (if any not covered above).
            if batch.outbox_ids:
                outbox_ph = ",".join("?" for _ in batch.outbox_ids)
                cur = self._conn.execute(
                    f"DELETE FROM outbox WHERE outbox_id IN ({outbox_ph}) "
                    f"AND state IN ('DELIVERED', 'WAIVED', 'FAILED')",
                    batch.outbox_ids,
                )
                deleted_outbox += cur.rowcount

            # 3. Delete unreferenced blobs (design §16.16 step 5).
            if batch.blob_ids:
                blob_ph = ",".join("?" for _ in batch.blob_ids)
                cur = self._conn.execute(
                    f"DELETE FROM runtime_blobs WHERE blob_id IN ({blob_ph})",
                    batch.blob_ids,
                )
                deleted_blobs += cur.rowcount

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return RetentionResult(
            deleted_tasks=deleted_tasks,
            deleted_outbox=deleted_outbox,
            deleted_blobs=deleted_blobs,
            skipped=skipped,
        )

    def list_unreferenced_blobs(self, limit: int) -> list[str]:
        """List blob IDs with no remaining references (design §16.16)."""
        rows = self._conn.execute(
            """
            SELECT b.blob_id FROM runtime_blobs b
            WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.payload_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM model_attempts m WHERE m.response_blob_id = b.blob_id)
              AND NOT EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.result_blob_id = b.blob_id
                              OR tc.arguments_blob_id = b.blob_id)
            ORDER BY b.blob_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [r["blob_id"] for r in rows]

    def delete_unreferenced_blobs(self, blob_ids: list[str]) -> int:
        """Delete blobs by ID. Returns the number of rows deleted."""
        if not blob_ids:
            return 0
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            ph = ",".join("?" for _ in blob_ids)
            cur = self._conn.execute(
                f"DELETE FROM runtime_blobs WHERE blob_id IN ({ph})",
                blob_ids,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount

    def checkpoint_wal(self) -> None:
        """Checkpoint the WAL during quiet periods (design §33.4)."""
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def backup_to(self, dest_path: str) -> None:
        """Online backup to ``dest_path`` (design §33.4).

        Uses SQLite's online backup API; never copies a WAL file on disk.
        """
        import sqlite3 as _sqlite3

        dest = _sqlite3.connect(dest_path)
        try:
            self._conn.backup(dest)
        finally:
            dest.close()


__all__ = ["SqliteRuntimeStore"]
