"""Durable TurnJournalPort adapter over the Runtime Store (design §29.4).

Implements :class:`~miniunicorn.agent.ports.TurnJournalPort` against the
Runtime Store. This adapter is the durable checkpoint adapter used by the
Worker Adapter when ``runtime.enabled=true``.

It does NOT touch the legacy ``runtime_checkpoint`` / ``pending_user_turn``
session metadata — those remain in :mod:`miniunicorn.agent.turn_persistence`
for migration and the legacy path.

The adapter wraps two Runtime Store views:

- :class:`~miniunicorn.runtime.contracts.ExecutionJournal` for model
  and tool facts (used by WP4 — Provider journaling and Tool Gateway);
- :class:`~miniunicorn.runtime.contracts.WorkerLedger` for checkpoint
  and progress writes (used by WP3 — restore/checkpoint outer phases).

WP3 scope: ``save_checkpoint`` and ``record_progress`` are functional
(needed for restore/checkpoint outer phases). The model attempt methods
are wired but only exercised in WP4 — until then the Worker's execution
callback runs the existing Agent loop without per-attempt journaling.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from miniunicorn.agent.ports import (
    AttemptIdentity,
    CheckpointIdentity,
    DurableProgress,
    ModelAttemptResult,
    ModelAttemptStarted,
    ModelDecision,
    RestorePoint,
    SafeError,
    TaskIdentity,
    TurnCheckpoint,
)

if TYPE_CHECKING:
    from miniunicorn.runtime.contracts import ExecutionJournal, WorkerLedger


class DurableTurnJournalAdapter:
    """Implements :class:`TurnJournalPort` against the Runtime Store.

    Design §29.4: this adapter is the durable checkpoint adapter.
    """

    def __init__(
        self,
        worker_ledger: "WorkerLedger",
        execution_journal: "ExecutionJournal | None" = None,
    ) -> None:
        self._ledger = worker_ledger
        self._journal = execution_journal

    # ------------------------------------------------------------------
    # Restore point (design §17.4, §18.2 RESTORE)
    # ------------------------------------------------------------------

    def load_restore_point(self, task: TaskIdentity) -> RestorePoint | None:
        """Load the latest durable restore point for ``task``."""
        if self._journal is None:
            return None
        return self._journal.load_restore_point(task.task_id)

    # ------------------------------------------------------------------
    # Model attempt journaling (design §17.5, §19) — WP4 scope
    # ------------------------------------------------------------------

    async def record_model_started(
        self, call: ModelAttemptStarted
    ) -> AttemptIdentity:
        """Durable-record that a Provider attempt started (design §19).

        Full implementation is WP4. WP3 returns a synthetic
        :class:`AttemptIdentity` so the existing Agent loop is unaffected.
        """
        if self._journal is not None:
            from miniunicorn.runtime.models import ModelAttemptWrite

            write = ModelAttemptWrite(
                logical_call_id=call.logical_call_id,
                attempt_no=call.attempt_no,
                provider_name=call.provider_name,
                model_name=call.model_name,
                request_hash=call.request_hash,
                started_at_ms=call.started_at_ms,
            )
            attempt_id = self._journal.begin_model_attempt(
                _claim_from_context(),  # type: ignore[arg-type]
                write,
            )
            return AttemptIdentity(
                task_id=_task_id_from_context(),
                model_attempt_id=attempt_id,
                logical_call_id=call.logical_call_id,
                attempt_no=call.attempt_no,
            )
        return AttemptIdentity(
            task_id=_task_id_from_context(),
            model_attempt_id=call.logical_call_id,
            logical_call_id=call.logical_call_id,
            attempt_no=call.attempt_no,
        )

    async def record_model_completed(
        self, attempt: AttemptIdentity, result: ModelAttemptResult
    ) -> ModelDecision:
        """Durable-record the completed attempt (design §19)."""
        if self._journal is not None:
            from miniunicorn.runtime.models import ModelResultWrite

            write = ModelResultWrite(
                response_blob_id=result.response_blob_id,
                response_hash=result.response_hash,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                finish_reason=result.finish_reason,
            )
            self._journal.finish_model_attempt(
                _claim_from_context(),  # type: ignore[arg-type]
                attempt.model_attempt_id,
                write,
            )
        return ModelDecision(
            attempt=attempt,
            response_blob_id=result.response_blob_id,
            response_hash=result.response_hash,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            finish_reason=result.finish_reason,
        )

    def write_response_blob(self, content: str, content_hash: str) -> str:
        """Write raw response text into a protected runtime blob (design §19).

        Returns the durable ``blob_id``. Used by the Provider attempt
        observer so the model attempt's ``response_blob_id`` FK resolves.
        """
        from miniunicorn.runtime.models import BlobWrite

        if self._journal is None:
            return f"inline:{content_hash[:16]}"
        blob = self._journal.write_blob(  # type: ignore[attr-defined]
            BlobWrite(
                scope_key=f"task/{_task_id_from_context()}",
                blob_kind="MODEL_RESPONSE",
                content_hash=content_hash,
                encoding="RAW_BYTES",
                inline_content=content.encode("utf-8"),
                size_bytes=len(content.encode("utf-8")),
            )
        )
        return blob.blob_id

    async def record_model_failed(
        self, attempt: AttemptIdentity, error: SafeError
    ) -> None:
        """Durable-record a failed Provider attempt (design §19)."""
        if self._journal is not None:
            self._journal.fail_model_attempt(
                _claim_from_context(),  # type: ignore[arg-type]
                attempt.model_attempt_id,
                error,
                int(time.time() * 1000),
            )

    # ------------------------------------------------------------------
    # Checkpoint and progress (design §17.4, §18.2 RESTORE/SAVE)
    # ------------------------------------------------------------------

    async def save_checkpoint(self, checkpoint: TurnCheckpoint) -> CheckpointIdentity:
        """Persist a checkpoint at a safe boundary (design §17.4)."""
        from miniunicorn.runtime.models import CheckpointWrite

        claim = _claim_from_context()
        if claim is None:
            raise RuntimeError(
                "DurableTurnJournalAdapter.save_checkpoint requires an active claim"
            )
        write = CheckpointWrite(
            phase=checkpoint.phase,
            run_segment=checkpoint.run_segment,
            inner_loop_iteration=checkpoint.inner_loop_iteration,
            session_base_revision=checkpoint.session_base_revision,
            payload_blob_id=checkpoint.payload_blob_id,
            payload_hash=checkpoint.payload_hash,
        )
        checkpoint_id = self._ledger.checkpoint(claim, write)
        return CheckpointIdentity(checkpoint_id=checkpoint_id, ordinal=0)

    async def record_progress(self, progress: DurableProgress) -> None:
        """Persist bounded, redacted progress at a safe boundary (design §17.4)."""
        claim = _claim_from_context()
        if claim is None:
            raise RuntimeError(
                "DurableTurnJournalAdapter.record_progress requires an active claim"
            )
        self._ledger.record_progress(
            claim,
            {
                "phase": progress.phase,
                "progress_summary": progress.progress_summary,
                "progress_at_ms": progress.progress_at_ms,
                "cumulative_input_tokens": progress.cumulative_input_tokens,
                "cumulative_output_tokens": progress.cumulative_output_tokens,
            },
            progress.progress_at_ms,
        )


# ---------------------------------------------------------------------------
# Context helpers — retrieve the active claim/task_id bound by the Worker
# ---------------------------------------------------------------------------


def _claim_from_context() -> Any:
    """Retrieve the active ``TaskClaim`` from the Worker's claim context.

    The Worker Adapter binds the claim via ``set_active_claim(task_id, claim)``
    before invoking Agent Core. We look it up by the ``task_id`` stored in
    the bound :class:`TurnRuntime`.
    """
    from miniunicorn.agent.turn_runtime import current_turn_runtime
    from miniunicorn.runtime.session_committer import _get_claim_for_task

    runtime = current_turn_runtime()
    if runtime is None or runtime.task_id is None:
        return None
    return _get_claim_for_task(runtime.task_id)


def _task_id_from_context() -> str:
    """Retrieve the durable ``task_id`` from the bound TurnRuntime."""
    from miniunicorn.agent.turn_runtime import current_turn_runtime

    runtime = current_turn_runtime()
    if runtime is None or runtime.task_id is None:
        return ""
    return runtime.task_id


class JournalProviderObserver:
    """Adapts :class:`TurnJournalPort` to :class:`ProviderAttemptObserver`
    (design §19).

    The Provider calls ``started/completed/failed`` for every network
    attempt. This adapter derives a deterministic ``logical_call_id`` from
    the bound task id plus a monotonic per-run model-call ordinal, tracks
    ``attempt_no`` within each logical call, and forwards the facts to the
    durable :class:`TurnJournalPort`.

    The Provider does not write SQLite directly; all durability flows
    through this adapter into the Runtime Store via the journal.
    """

    def __init__(self, journal: "TurnJournalPort") -> None:  # type: ignore[name-defined]
        self._journal = journal
        # Per-run monotonic counters (design §19: logical_call_id derives
        # from task_id + run_segment + inner_loop_iteration + ordinal).
        self._call_ordinal = 0
        self._attempt_no = 0
        self._logical_call_id = ""
        # Map provider attempt_id -> (AttemptIdentity) for completed/failed.
        self._attempts: dict[str, Any] = {}

    def begin_logical_call(self) -> None:
        """Start a new logical model call (called by Runner before _request_model)."""
        self._call_ordinal += 1
        self._attempt_no = 0
        task_id = _task_id_from_context()
        self._logical_call_id = f"{task_id}:{self._call_ordinal}"

    async def started(self, value: Any) -> str:
        from miniunicorn.agent.ports import (
            AttemptIdentity,
            ModelAttemptStarted,
        )

        self._attempt_no += 1
        call = ModelAttemptStarted(
            logical_call_id=self._logical_call_id,
            attempt_no=self._attempt_no,
            provider_name=value.provider_name,
            model_name=value.model_name,
            request_hash=value.request_hash,
            started_at_ms=value.started_at_ms,
        )
        attempt = await self._journal.record_model_started(call)
        self._attempts[attempt.model_attempt_id] = attempt
        return attempt.model_attempt_id

    async def completed(self, attempt_id: str, value: Any) -> None:
        from miniunicorn.agent.ports import ModelAttemptResult

        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return
        # Write the raw response into a protected blob so the model attempt's
        # response_blob_id FK resolves (design §19). The provider does not
        # write SQLite directly; the observer owns the blob write.
        blob_id = value.response_blob_id
        if value.content is not None and hasattr(self._journal, "write_response_blob"):
            blob_id = self._journal.write_response_blob(value.content, value.response_hash)
        result = ModelAttemptResult(
            response_blob_id=blob_id,
            response_hash=value.response_hash,
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            finish_reason=value.finish_reason,
        )
        await self._journal.record_model_completed(attempt, result)

    async def failed(self, attempt_id: str, value: Any) -> None:
        from miniunicorn.agent.ports import SafeError

        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return
        await self._journal.record_model_failed(
            attempt,
            SafeError(
                error_code=value.error_code,
                error_summary=value.error_summary,
            ),
        )


__all__ = ["DurableTurnJournalAdapter", "JournalProviderObserver"]
