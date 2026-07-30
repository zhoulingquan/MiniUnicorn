"""Session Committer — prepare/apply/confirm coordinator (design §17.7, §21).

The Session Committer is the runtime adapter that implements
:class:`~miniunicorn.agent.ports.SessionCommitPort`. It coordinates an
idempotent session commit between two stores that cannot share a
transaction: the SQLite Runtime Store and the filesystem-based Session
Manager.

Protocol (design §17.7):

1. build the normalized session mutation outside SQLite;
2. derive the stable commit id from task id and commit kind;
3. transactionally validate the task lease, insert or verify
   ``session_commits(PREPARED)``, and append ``SESSION_COMMIT_PREPARED``;
4. call ``SessionManager.commit_turn()`` (filesystem commit with OS lock,
   revision check, and commit-id idempotency);
5. transactionally revalidate the task lease, mark the commit
   ``COMMITTED``, advance the compatible task phase, and append
   ``SESSION_COMMITTED``.

Crash behavior (design §17.7):

- before step 3: repeat from task payload or checkpoint;
- after step 3 but before filesystem commit: repeat ``commit_turn``;
- after filesystem commit but before step 5: ``commit_turn`` detects the
  commit id and returns ``ALREADY_COMMITTED``; then step 5 is repeated;
- if the Worker is fenced after the filesystem commit, it stops; the next
  lease holder detects the same commit id and performs step 5;
- revision mismatch without the same commit id: never overwrite; record
  ``CONFLICT`` and follow design §23.4.
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

from miniunicorn.agent.ports import (
    CommitKind,
    SafeError,
    SessionCommitRequest,
    SessionCommitResult,
    SessionMutation,
)
from miniunicorn.runtime.models import SessionCommitWrite

if TYPE_CHECKING:
    from miniunicorn.runtime.contracts import SessionCommitLedger, TaskClaim
    from miniunicorn.session.manager import SessionManager


def _derive_commit_id(task_id: str, commit_kind: CommitKind) -> str:
    """Stable commit id from ``(task_id, commit_kind)`` (design §17.7 step 2).

    The id is a SHA-256 hex of ``f"{task_id}:{commit_kind}"``. This makes
    it deterministic: a retry of the same commit kind for the same task
    reuses the same id, enabling idempotency in both the Runtime Store
    and the Session Manager sidecar.
    """
    return hashlib.sha256(f"{task_id}:{commit_kind}".encode("utf-8")).hexdigest()


def _compute_content_hash(mutation: SessionMutation) -> str:
    """SHA-256 hex over the normalized mutation (design §17.7 step 1).

    The hash covers the messages list and the metadata_updates dict,
    serialized as canonical JSON (sorted keys) to ensure determinism.
    """
    import json

    payload = json.dumps(
        {
            "messages": mutation.messages,
            "metadata_updates": mutation.metadata_updates,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SessionCommitter:
    """Implements ``SessionCommitPort`` via prepare/apply/confirm (design §17.7).

    The committer holds references to:
    - ``SessionCommitLedger`` (Runtime Store view) for SQLite bookkeeping;
    - ``SessionManager`` for filesystem commit.

    It is stateless between calls — all durability lives in the two stores.
    """

    def __init__(
        self,
        ledger: "SessionCommitLedger",
        session_manager: "SessionManager",
    ) -> None:
        self._ledger = ledger
        self._session_manager = session_manager

    async def commit_turn(self, request: SessionCommitRequest) -> SessionCommitResult:
        """Execute the 5-step prepare/apply/confirm protocol.

        Returns a :class:`SessionCommitResult` with one of:
        ``COMMITTED``, ``ALREADY_COMMITTED``, ``REVISION_CONFLICT``,
        ``IO_FAILURE``.
        """
        # Step 1: the mutation is already normalized by the caller.
        mutation = request.mutation
        content_hash = request.content_hash or _compute_content_hash(mutation)

        # Step 2: commit id is already derived by the caller, but verify it
        # matches the deterministic derivation. Use the request's commit_id
        # as the source of truth (it may have been pre-derived).
        commit_id = request.commit_id or _derive_commit_id(
            request.task_id, request.commit_kind
        )

        # Step 3: prepare in SQLite (insert or verify PREPARED row).
        now_ms = int(time.time() * 1000)
        write = SessionCommitWrite(
            session_key=request.session_key,
            commit_kind=request.commit_kind,
            base_revision=request.base_revision,
            target_revision=request.target_revision,
            content_hash=content_hash,
            payload_blob_id="",  # not used by SessionCommitLedger.prepare
            created_at_ms=now_ms,
            session_commit_id=commit_id,
        )

        # The ledger's prepare_session_commit expects a TaskClaim for lease
        # validation. The request carries task_id but not the full claim;
        # the caller (Worker) must have already established the claim. We
        # pass a synthetic claim derived from the request. In production,
        # the Worker passes the real claim via a closure or bound method.
        claim = _get_claim_for_task(request.task_id)
        if claim is None:
            return SessionCommitResult(
                state="IO_FAILURE",
                revision=request.base_revision,
                error=SafeError(
                    error_code="NO_CLAIM_CONTEXT",
                    error_summary="SessionCommitter has no active claim for this task",
                ),
            )

        prepared = self._ledger.prepare_session_commit(claim, write)

        # If already prepared/committed by a previous attempt, check state.
        if prepared.state == "COMMITTED":
            # The filesystem commit was already confirmed by a prior run.
            return SessionCommitResult(
                state="ALREADY_COMMITTED",
                revision=prepared.target_revision,
            )

        # Step 4: filesystem commit via SessionManager.commit_turn().
        outcome = self._session_manager.commit_turn(
            session_key=request.session_key,
            commit_id=commit_id,
            commit_kind=request.commit_kind,
            base_revision=request.base_revision,
            mutation_messages=mutation.messages,
            mutation_metadata_updates=mutation.metadata_updates,
            content_hash=content_hash,
        )

        if outcome.state == "ALREADY_COMMITTED":
            # Filesystem already has this commit (crash after step 4 but
            # before step 5). Proceed to step 5 to confirm in SQLite.
            pass
        elif outcome.state == "REVISION_CONFLICT":
            # Record the conflict in SQLite (design §21.4).
            self._ledger.mark_session_conflict(
                claim,
                commit_id,
                SafeError(
                    error_code="SESSION_REVISION_CONFLICT",
                    error_summary=f"base_revision={request.base_revision} "
                    f"does not match disk revision={outcome.revision}",
                ),
            )
            return SessionCommitResult(
                state="REVISION_CONFLICT",
                revision=outcome.revision,
            )
        elif outcome.state == "IO_FAILURE":
            return SessionCommitResult(
                state="IO_FAILURE",
                revision=outcome.revision,
                error=SafeError(
                    error_code="SESSION_IO_FAILURE",
                    error_summary=outcome.error or "filesystem commit failed",
                ),
            )
        # outcome.state == "COMMITTED" → proceed to step 5.

        # Step 5: confirm in SQLite (mark COMMITTED + append event).
        committed_at_ms = int(time.time() * 1000)
        self._ledger.confirm_session_commit(
            claim, commit_id, outcome.revision, committed_at_ms
        )

        return SessionCommitResult(
            state="COMMITTED",
            revision=outcome.revision,
        )


# ---------------------------------------------------------------------------
# Claim context — how the Worker passes its TaskClaim to the committer
# ---------------------------------------------------------------------------

# The SessionCommitter needs the active TaskClaim for lease validation.
# Because SessionCommitPort.commit_turn() only receives a
# SessionCommitRequest (which has task_id but not the full claim), the
# Worker must bind the claim before calling. This is done via a
# context-local variable set by the Worker adapter.

_claim_context: dict[str, "TaskClaim"] = {}


def set_active_claim(task_id: str, claim: "TaskClaim") -> None:
    """Bind a ``TaskClaim`` as the active claim for ``task_id``.

    Called by the Worker adapter before invoking Agent Core methods that
    may call ``SessionCommitPort.commit_turn()``.
    """
    _claim_context[task_id] = claim


def clear_active_claim(task_id: str | None = None) -> None:
    """Remove the active claim for ``task_id`` after the Worker finishes.

    If ``task_id`` is ``None``, this is a no-op safety net — the Worker
    should always pass the explicit ``task_id`` from its claimed task.
    """
    if task_id is not None:
        _claim_context.pop(task_id, None)


def _get_claim_for_task(task_id: str) -> "TaskClaim | None":
    """Retrieve the active claim for ``task_id``, or ``None``."""
    return _claim_context.get(task_id)


# ---------------------------------------------------------------------------
# Delivery ledger context — how the Worker passes the DeliveryLedger to
# tools that need to enqueue Outbox rows (design §20.6, WP5 task 5).
# ---------------------------------------------------------------------------

# The MessageTool enqueues to the Outbox instead of calling the Channel
# callback directly when running under the durable runtime. The Worker
# binds the DeliveryLedger (Runtime Store view) so the MessageTool can
# call ``enqueue_message_tool_outbox`` and ``write_blob`` without a hard
# dependency on the runtime package.

_delivery_ledger_context: dict[str, Any] = {}
_tool_call_id_context: dict[str, str] = {}


def set_active_delivery_ledger(task_id: str, ledger: Any) -> None:
    """Bind a ``DeliveryLedger`` as the active store for ``task_id``.

    Called by the Worker adapter before invoking Agent Core methods that
    may call ``MessageTool`` (design §20.6, WP5 task 5).
    """
    _delivery_ledger_context[task_id] = ledger


def clear_active_delivery_ledger(task_id: str | None = None) -> None:
    """Remove the active delivery ledger for ``task_id`` after the Worker finishes."""
    if task_id is not None:
        _delivery_ledger_context.pop(task_id, None)


def _get_delivery_ledger_for_task(task_id: str) -> Any | None:
    """Retrieve the active delivery ledger for ``task_id``, or ``None``."""
    return _delivery_ledger_context.get(task_id)


def set_active_tool_call_id(task_id: str, tool_call_id: str) -> None:
    """Bind the current ``tool_call_id`` for ``task_id`` (design §20.6).

    Called by the ToolGateway before invoking a tool so the MessageTool
    can compute the stable dedup key ``sha256(task_id:tool_call_id:message)``.
    """
    _tool_call_id_context[task_id] = tool_call_id


def clear_active_tool_call_id(task_id: str | None = None) -> None:
    """Remove the active tool_call_id for ``task_id`` after the tool call."""
    if task_id is not None:
        _tool_call_id_context.pop(task_id, None)


def _get_active_tool_call_id(task_id: str) -> str | None:
    """Retrieve the active tool_call_id for ``task_id``, or ``None``."""
    return _tool_call_id_context.get(task_id)


__all__ = [
    "SessionCommitter",
    "set_active_claim",
    "clear_active_claim",
    "set_active_delivery_ledger",
    "clear_active_delivery_ledger",
    "set_active_tool_call_id",
    "clear_active_tool_call_id",
]
