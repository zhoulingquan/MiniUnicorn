"""Durable task-to-Agent Worker Adapter (design §8.3, §17, §18).

``AgentTaskWorker`` adapts one claimed durable task to the existing
Agent Core. It:

- loads the protected task payload;
- commits the inbound user message (INBOUND session commit);
- runs the Agent execution through a pluggable execution callback;
- commits the final session mutation (FINAL session commit);
- checkpoints progress at safe boundaries;
- maps terminal outcomes to Runtime Store operations.

One Worker handles one root task at a time. The design's full Agent-owned
port injection (TurnJournalPort, ToolExecutionPort, etc.) is progressively
wired; WP3 provides a functional path that can submit, crash, restart,
and finish one task in lightweight mode (design §30 WP3 exit).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from loguru import logger

from miniunicorn.agent.ports import (
    SafeError,
    SessionCommitRequest,
    SessionCommitResult,
    SessionMutation,
)
from miniunicorn.runtime.containment import (
    ContainmentScope,
    NullContainmentScope,
    ProcessContainmentScope,
    bind_containment_scope,
    reset_containment_scope,
)
from miniunicorn.runtime.contracts import (
    ClaimedTask,
    CompletionResult,
    LeaseLostError,
    StaleLeaseError,
    WorkerLedger,
)
from miniunicorn.runtime.models import (
    BlobWrite,
    CompletionWrite,
    InternalCompletionWrite,
    RetryDecision,
    TaskFailure,
)
from miniunicorn.runtime.scheduler import Scheduler
from miniunicorn.runtime.session_committer import (
    SessionCommitter,
    set_active_claim,
    clear_active_claim,
    set_active_delivery_ledger,
    clear_active_delivery_ledger,
)

# ---------------------------------------------------------------------------
# Execution callback protocol
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class WorkerTaskPayload:
    """Decoded task payload provided to the execution callback."""

    task_id: str
    session_key: str
    turn_id: str | None
    channel: str | None
    channel_account: str | None
    channel_message_id: str | None
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Task 7 Step 2: immutable delivery target for the final reply.
    target_key: str = ""


@dataclass(slots=True, frozen=True)
class WorkerExecutionResult:
    """Structured result returned by the execution callback.

    ``final_content`` is the complete assistant response. ``messages`` are
    the assistant/tool messages produced after the inbound commit (for the
    FINAL session commit). ``suppress_final`` indicates the Message Tool
    already sent the reply and no separate Outbox row is needed.
    """

    final_content: str | None
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata_updates: dict[str, Any] = field(default_factory=dict)
    suppress_final: bool = False
    error: SafeError | None = None


class ExecutionCallback(Protocol):
    """Pluggable Agent execution entry point (design §8.3, §29.3).

    The Worker calls this with the decoded task payload and the current
    session base revision. The callback runs the existing Agent Core
    (TurnExecutor / AgentLoop) and returns the structured result.

    In WP3 the callback is provided by the LightweightHost and wraps
    ``AgentLoop._process_message`` or ``TurnDispatcher.process_direct``.
    """

    async def __call__(
        self,
        payload: WorkerTaskPayload,
        session_base_revision: int,
    ) -> WorkerExecutionResult: ...


# ---------------------------------------------------------------------------
# AgentTaskWorker
# ---------------------------------------------------------------------------


class AgentTaskWorker:
    """Adapts one claimed durable task to the existing Agent Core (design §8.3).

    The Worker is NOT thread-safe; one Worker handles one task at a time.
    The LightweightHost runs multiple Worker coroutines for concurrency
    (design §9.1).
    """

    def __init__(
        self,
        worker_id: str,
        scheduler: Scheduler,
        worker_ledger: WorkerLedger,
        session_committer: SessionCommitter,
        execution_callback: ExecutionCallback,
        *,
        heartbeat_interval_s: float = 15.0,
        containment_factory: Callable[[str], ContainmentScope] | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._scheduler = scheduler
        self._ledger = worker_ledger
        self._committer = session_committer
        self._execution_callback = execution_callback
        self._heartbeat_interval_s = heartbeat_interval_s
        self._containment_factory = containment_factory
        self._running = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the Worker loop: claim → execute → complete, repeat.

        The loop exits when ``stop()`` is called or when no task is
        eligible after polling. The LightweightHost restarts the loop
        when new work is submitted.
        """
        self._running = True
        while self._running:
            outcome = self._scheduler.claim_next(self._worker_id)
            if outcome.claimed is None:
                # Nothing to do — wait briefly before retrying.
                await asyncio.sleep(0.1)
                continue

            try:
                await self._execute_task(outcome.claimed)
            except LeaseLostError as exc:
                logger.warning(
                    "worker {} heartbeat lease lost for task {}: {}",
                    self._worker_id,
                    exc.task_id,
                    exc,
                )
            except StaleLeaseError as exc:
                logger.warning(
                    "worker {} lost lease for task {}: {}",
                    self._worker_id,
                    exc.task_id,
                    exc,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "worker {} unhandled error for task: {}",
                    self._worker_id,
                    exc,
                )
            finally:
                clear_active_claim()

    def stop(self) -> None:
        """Signal the Worker loop to stop after the current task."""
        self._running = False

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    async def _execute_task(self, claimed: ClaimedTask) -> None:
        """Execute one claimed task through the full lifecycle."""
        record = claimed.record
        claim = claimed.claim
        task_id = record.task_id

        logger.info(
            "worker {} claimed task {} (session={}, seq={}, phase={})",
            self._worker_id,
            task_id,
            record.session_key,
            record.session_sequence,
            record.checkpoint_phase,
        )

        # Mark RUNNING (design §17.3).
        now_ms = _now_ms()
        try:
            self._ledger.mark_running(claim, now_ms)
        except StaleLeaseError:
            logger.warning("worker {} lost lease before mark_running for {}", self._worker_id, task_id)
            return

        # Set the active claim for SessionCommitter (design §17.7).
        set_active_claim(task_id, claim)

        # Bind the delivery ledger so MessageTool can enqueue Outbox rows
        # (design §20.6, WP5 task 5). The WorkerLedger and DeliveryLedger
        # are implemented by the same SqliteRuntimeStore object.
        set_active_delivery_ledger(task_id, self._ledger)

        # Create and bind the task's child-process containment scope
        # (design §20.7, WP4 task 9). On Worker termination, cancellation,
        # or hard timeout, the entire child tree is terminated before the
        # task lease is released.
        containment = (
            self._containment_factory(task_id)
            if self._containment_factory is not None
            else NullContainmentScope()
        )
        containment_token = bind_containment_scope(containment)

        # Start heartbeat renewal.
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(claim, task_id),
            name=f"heartbeat-{task_id}",
        )

        try:
            # Decode payload.
            payload = self._decode_payload(record)

            # Check if INBOUND commit already happened (crash recovery).
            inbound_result = await self._commit_inbound(claim, record, payload)
            if inbound_result.state == "IO_FAILURE":
                self._fail_task(claim, task_id, "INBOUND_COMMIT_IO_FAILURE", str(inbound_result.error))
                return
            if not _session_commit_succeeded(inbound_result):
                self._enter_session_retry(claim, task_id, inbound_result)
                return

            # Get the session base revision after INBOUND commit.
            session_base_revision = inbound_result.revision

            # Race the Agent execution against lease loss: a rejected
            # heartbeat must cancel execution before it can reach session
            # or completion writes (Task 2 Step 7).
            execution_task = asyncio.create_task(
                self._execution_callback(payload, session_base_revision),
                name=f"execute-{task_id}",
            )
            done, _pending = await asyncio.wait(
                [execution_task, heartbeat_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if heartbeat_task in done:
                # Heartbeat raised LeaseLostError (or returned). Cancel the
                # in-flight execution before any commit can happen.
                execution_task.cancel()
                with suppress_exception(asyncio.CancelledError):
                    await execution_task
                exc = heartbeat_task.exception()
                if isinstance(exc, LeaseLostError):
                    raise exc
                raise LeaseLostError(task_id)

            result = execution_task.result()

            if result.error is not None:
                self._fail_task(claim, task_id, result.error.error_code, result.error.error_summary)
                return

            # Commit the FINAL session mutation (design §17.7, §18.2 SAVE).
            final_result = await self._commit_final(claim, record, result, session_base_revision)
            if final_result.state == "IO_FAILURE":
                self._fail_task(claim, task_id, "FINAL_COMMIT_IO_FAILURE", str(final_result.error))
                return
            if not _session_commit_succeeded(final_result):
                self._enter_session_retry(claim, task_id, final_result)
                return

            # Complete the task (design §17.8).
            self._complete_task(claim, record, result, final_result.revision)

            logger.info(
                "worker {} completed task {} (session={})",
                self._worker_id,
                task_id,
                record.session_key,
            )
        except LeaseLostError as exc:
            logger.warning(
                "worker {} lease lost for task {}: {}",
                self._worker_id,
                task_id,
                exc,
            )
            self._fail_task(claim, task_id, "LEASE_LOST", str(exc))
            raise
        except StaleLeaseError as exc:
            logger.warning("worker {} fenced during task {}: {}", self._worker_id, task_id, exc)
            raise
        except asyncio.CancelledError:
            logger.info("worker {} cancelled during task {}", self._worker_id, task_id)
            raise
        except Exception as exc:
            logger.exception("worker {} error during task {}", self._worker_id, task_id)
            self._fail_task(claim, task_id, "WORKER_UNHANDLED_EXCEPTION", str(exc)[:200])
            raise
        finally:
            heartbeat_task.cancel()
            with suppress_exception(asyncio.CancelledError):
                await heartbeat_task
            # Terminate the task's child-process tree before releasing
            # the lease (design §20.7).
            try:
                containment.close()
            except Exception:  # noqa: BLE001
                logger.warning("worker {}: containment close failed for {}", self._worker_id, task_id)
            reset_containment_scope(containment_token)
            clear_active_claim(task_id)
            clear_active_delivery_ledger(task_id)

    # ------------------------------------------------------------------
    # Session commits
    # ------------------------------------------------------------------

    async def _commit_inbound(
        self,
        claim: Any,
        record: Any,
        payload: WorkerTaskPayload,
    ) -> SessionCommitResult:
        """Commit the inbound user message (design §17.7, §18.2 RESTORE).

        The Worker commits the triggering user message after claim and
        before Agent execution. This prevents a later queued message from
        advancing the transcript revision while an earlier task is still
        preparing its final session commit (design §17.1).
        """
        commit_id = _derive_commit_id(record.task_id, "INBOUND")
        content_hash = _hash_content(payload.content)

        # Build the normalized inbound mutation.
        user_message: dict[str, Any] = {
            "role": "user",
            "content": payload.content,
        }
        if payload.metadata:
            user_message["metadata"] = payload.metadata

        # Read the actual filesystem revision so the INBOUND commit targets
        # the correct next revision (Task 3 Step 5). A hardcoded 0 would
        # conflict on the second turn of the same session.
        base_revision = self._committer.current_revision(record.session_key)
        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=commit_id,
            commit_kind="INBOUND",
            base_revision=base_revision,
            target_revision=base_revision + 1,
            mutation=SessionMutation(
                messages=[user_message],
                metadata_updates={},
            ),
            content_hash=content_hash,
        )
        return await self._committer.commit_turn(request)

    async def _commit_final(
        self,
        claim: Any,
        record: Any,
        result: WorkerExecutionResult,
        base_revision: int,
    ) -> SessionCommitResult:
        """Commit the final session mutation (design §17.7, §18.2 SAVE).

        Appends only messages produced after the inbound message —
        assistant and tool transcript entries. Never appends the
        triggering user message a second time (design §17.7).
        """
        if not result.messages:
            # No messages to commit (e.g., system shortcut). Skip.
            return SessionCommitResult(
                state="COMMITTED",
                revision=base_revision,
            )

        commit_id = _derive_commit_id(record.task_id, "FINAL")
        content_hash = _hash_content(json.dumps(result.messages, sort_keys=True, default=str))

        request = SessionCommitRequest(
            task_id=record.task_id,
            session_key=record.session_key,
            commit_id=commit_id,
            commit_kind="FINAL",
            base_revision=base_revision,
            target_revision=base_revision + 1,
            mutation=SessionMutation(
                messages=result.messages,
                metadata_updates=result.metadata_updates,
            ),
            content_hash=content_hash,
        )
        return await self._committer.commit_turn(request)

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def _complete_task(
        self,
        claim: Any,
        record: Any,
        result: WorkerExecutionResult,
        session_revision: int,
    ) -> None:
        """Complete the task with outbox enqueue (design §17.8)."""
        now_ms = _now_ms()
        if result.suppress_final:
            # Message Tool already sent the reply; no separate Outbox row.
            completion = InternalCompletionWrite(
                result_ref=f"revision:{session_revision}",
                completed_at_ms=now_ms,
            )
            self._ledger.complete_internal(claim, completion)
            return

        # Write the final reply as a protected blob, then complete with outbox.
        # Task 7 Step 6: the payload is the versioned Outbox codec envelope
        # (not raw UTF-8 bytes) so OutboxSender can decode content+media+metadata.
        final_content = result.final_content or ""
        from miniunicorn.runtime.outbox_payload import encode_outbox_payload

        final_bytes = encode_outbox_payload(content=final_content)
        final_hash = hashlib.sha256(final_bytes).hexdigest()
        scope_key = f"{record.tenant_id}/{record.principal_id}"
        blob = self._ledger.write_blob(
            BlobWrite(
                scope_key=scope_key,
                blob_kind="OUTBOX_PAYLOAD",
                content_hash=final_hash,
                encoding="RAW_BYTES",
                inline_content=final_bytes,
                size_bytes=len(final_bytes),
                created_at_ms=now_ms,
            )
        )
        # Task 7 Step 3: durable delivery routing copied verbatim from the
        # immutable claimed TaskRecord (design §17.8). The Agent's final
        # text cannot choose or alter channel/account/target.
        completion = CompletionWrite(
            final_reply_blob_id=blob.blob_id,
            final_reply_hash=final_hash,
            final_reply_dedup_key=None,
            suppress_final=False,
            completed_at_ms=now_ms,
            channel=record.channel or "",
            channel_account=record.channel_account or "",
            target_key=record.target_key or "",
        )
        self._ledger.complete_with_outbox(claim, completion)

    def _fail_task(self, claim: Any, task_id: str, error_code: str, error_summary: str) -> None:
        """Fail the task terminally (design §17.10)."""
        try:
            self._ledger.fail_task(
                claim,
                TaskFailure(
                    error=SafeError(
                        error_code=error_code,
                        error_summary=error_summary[:500],
                    ),
                    failed_at_ms=_now_ms(),
                ),
            )
        except StaleLeaseError:
            logger.warning("worker {} could not fail task {} (stale lease)", self._worker_id, task_id)

    def _enter_session_retry(
        self, claim: Any, task_id: str, result: SessionCommitResult
    ) -> None:
        """Enter bounded RETRY_WAIT after a session commit conflict (Task 3 Step 6).

        A ``REVISION_CONFLICT`` means the session changed before the
        commit could land. The task enters a short bounded retry so the
        next attempt re-reads the fresh revision (design §17.7, §23.4).
        """
        try:
            self._ledger.enter_retry_wait(
                claim,
                RetryDecision(
                    kind="TRANSIENT",
                    available_at_ms=_now_ms() + 250,
                    error=SafeError(
                        error_code="SESSION_REVISION_CONFLICT",
                        error_summary="session changed before commit; retry from fresh revision",
                    ),
                ),
            )
        except StaleLeaseError:
            logger.warning(
                "worker {} could not enter retry for task {} (stale lease)",
                self._worker_id,
                task_id,
            )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, claim: Any, task_id: str) -> None:
        """Renew the lease periodically while the task is running (design §6.11).

        Raises :class:`LeaseLostError` when a renewal is rejected so the
        active Agent execution is cancelled before it can reach session or
        completion writes (Task 2 Step 7).
        """
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_s)
                if not self._scheduler.heartbeat(claim):
                    logger.warning(
                        "worker {} heartbeat rejected (stale lease) for task {}",
                        self._worker_id,
                        task_id,
                    )
                    raise LeaseLostError(task_id)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Payload decoding
    # ------------------------------------------------------------------

    def _decode_payload(self, record: Any) -> WorkerTaskPayload:
        """Decode the protected task payload from the Runtime Store.

        WP3 uses a simple JSON payload stored in the blob. The full
        design (§17.1) stores the raw user content as a protected blob;
        we decode it here.
        """
        from miniunicorn.runtime.contracts import BlobStore

        # The store implements BlobStore.
        blob_store: BlobStore = self._ledger  # type: ignore[assignment]
        blob = blob_store.read_blob(record.payload_blob_id)
        if blob is None:
            raise RuntimeError(f"payload blob not found: {record.payload_blob_id}")

        content_bytes = blob_store.read_blob_content(record.payload_blob_id)
        if content_bytes is None:
            raise RuntimeError(f"payload content not readable: {record.payload_blob_id}")

        try:
            payload_data = json.loads(content_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"payload decode failed: {exc}") from exc

        return WorkerTaskPayload(
            task_id=record.task_id,
            session_key=record.session_key,
            turn_id=record.turn_id,
            channel=record.channel,
            channel_account=record.channel_account,
            channel_message_id=record.channel_message_id,
            content=payload_data.get("content", ""),
            media=payload_data.get("media", []),
            metadata=payload_data.get("metadata", {}),
            target_key=record.target_key or "",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Current UTC Unix milliseconds (design §12.2)."""
    return int(time.time() * 1000)


def _derive_commit_id(task_id: str, commit_kind: str) -> str:
    """Derive a stable commit id from task id and commit kind (design §17.7)."""
    return hashlib.sha256(f"{task_id}:{commit_kind}".encode("utf-8")).hexdigest()[:32]


def _hash_content(content: str) -> str:
    """SHA-256 hex of content (design §16.15)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _session_commit_succeeded(result: SessionCommitResult) -> bool:
    """Return ``True`` only for ``COMMITTED`` or ``ALREADY_COMMITTED`` (Task 3 Step 6).

    A ``REVISION_CONFLICT`` or ``IO_FAILURE`` is not a success; the
    Worker must enter bounded retry or fail rather than complete.
    """
    return result.state in {"COMMITTED", "ALREADY_COMMITTED"}


class suppress_exception:
    """Context manager that suppresses a specific exception type."""

    def __init__(self, exc_type: type[BaseException]) -> None:
        self._exc_type = exc_type

    def __enter__(self) -> "suppress_exception":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *args: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, self._exc_type)


__all__ = [
    "AgentTaskWorker",
    "WorkerTaskPayload",
    "WorkerExecutionResult",
    "ExecutionCallback",
]
