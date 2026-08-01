"""Execution journal mixin for the SQLite Runtime Store (design §17.4–§17.6).

Holds :class:`ExecutionStoreMixin` covering ``ExecutionJournal``: restore
point loading, model attempts, tool calls/attempts, pending control reads,
and control acknowledgement. The mixin shares ``self._conn`` with the
other responsibility mixins via the façade and calls shared
lease-validation helpers on the base (design §7.3, §11.2, §17; Task 12
Steps 3-5).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from miniunicorn.agent.ports import (
    CompletedModelDecision,
    CompletedToolDecision,
    RestorePoint,
    SafeError,
    ToolExecutionResult,
)
from miniunicorn.runtime.contracts import TaskClaim
from miniunicorn.runtime.models import (
    ControlOutcomeWrite,
    ModelAttemptWrite,
    ModelResultWrite,
    PreparedToolWrite,
    TaskControlRecord,
    ToolAttemptRecord,
    ToolAttemptWrite,
    ToolCallRecord,
    ToolResultWrite,
)
from miniunicorn.runtime.sqlite.base_store import (
    _new_uuid,
    _now_ms,
    _row_to_control,
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


class ExecutionStoreMixin:
    """Execution journal operations (model/tool attempts, controls)."""

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
