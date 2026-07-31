"""Task ingress + worker ledger mixin for the SQLite Runtime Store.

Holds :class:`TaskStoreMixin` covering ``TaskIngressStore`` (submit,
dedup, control append, reads) and ``WorkerLedger`` (claim, renew,
heartbeat, checkpoint, progress, retry/wait, fail, cancel, complete,
reclaim). The mixin shares ``self._conn`` with the other responsibility
mixins via the façade and calls shared helpers on the base
(design §7.3, §11.2, §15, §17; Task 12 Steps 3-5).
"""

from __future__ import annotations

import json

from miniunicorn.agent.ports import SafeError
from miniunicorn.runtime.contracts import (
    ClaimedTask,
    ClaimRequest,
    ClaimResult,
    CompletionResult,
    ControlResult,
    ReclaimResult,
    StaleLeaseError,
    SubmitResult,
    TaskClaim,
)
from miniunicorn.runtime.models import (
    TERMINAL_TASK_STATES,
    BlobWrite,
    CheckpointWrite,
    CompletionWrite,
    InboundTaskEnvelope,
    InternalCompletionWrite,
    InternalTaskEnvelope,
    RequestScope,
    RetryDecision,
    TaskControlRequest,
    TaskFailure,
    TaskRecord,
    TaskSnapshot,
    WaitDecision,
    WaitResult,
)
from miniunicorn.runtime.sqlite.base_store import (
    _dedup_key_hash,
    _new_lease_token,
    _new_uuid,
    _now_ms,
    _row_to_task_record,
    _task_snapshot,
)


class TaskStoreMixin:
    """Task ingress and worker ledger operations."""

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
        predicate, scope_params = self._task_scope_predicate(scope)
        row = self._conn.execute(
            f"SELECT * FROM tasks WHERE task_id = ? AND {predicate}",
            (task_id, *scope_params),
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
