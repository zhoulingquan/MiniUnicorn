"""Façade-identity and transaction-boundary characterization (Task 12).

These tests pin two invariants that the upcoming SQLite store split
(Task 12 Steps 3-5) must preserve:

1. **Façade identity** — ``SqliteRuntimeStore`` remains the single public
   façade re-exported from ``miniunicorn.runtime.sqlite`` and implements
   every Runtime Store protocol.
2. **Transaction boundaries** — each mutating operation uses the same
   number and order of ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``
   statements before and after the internal module split. The split must
   move SQL without changing transaction boundaries.

No production behavior is changed by this file; it only characterizes
the current green baseline so regressions during the split are caught.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.contracts import (
    BlobStore,
    ClaimRequest,
    DeliveryLedger,
    DurableEventLog,
    ExecutionJournal,
    MaintenanceLedger,
    ResourceLedger,
    RuntimeStore,
    SessionCommitLedger,
    TaskIngressStore,
    WorkerLedger,
)
from miniunicorn.runtime.models import (
    BlobWrite,
    CompletionWrite,
    DeliveryReceipt,
    PreparedToolWrite,
    RequestScope,
    SessionCommitWrite,
    ToolAttemptWrite,
    ToolResultWrite,
)
from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection, run_migrations
from miniunicorn.runtime.sqlite.store import SqliteRuntimeStore as direct_store  # noqa: N813

public_store = SqliteRuntimeStore


# ---------------------------------------------------------------------------
# Step 1: Façade identity (design §7.3, Task 12 Step 1)
# ---------------------------------------------------------------------------


class TestSqliteStoreFacadeIdentity:
    """The public re-export and the direct class are the same object."""

    def test_public_is_direct(self) -> None:
        assert public_store is direct_store

    def test_facade_implements_all_runtime_protocols(self, store: Any) -> None:
        """The façade satisfies every narrow Runtime Store protocol."""
        for protocol in (
            TaskIngressStore,
            WorkerLedger,
            ExecutionJournal,
            SessionCommitLedger,
            DeliveryLedger,
            ResourceLedger,
            MaintenanceLedger,
            DurableEventLog,
            BlobStore,
            RuntimeStore,
        ):
            assert isinstance(store, protocol), (
                f"SqliteRuntimeStore must implement {protocol.__name__}"
            )

    def test_facade_exposes_expected_public_methods(self) -> None:
        """Every protocol-required method is present on the façade class."""
        required = {
            # TaskIngressStore
            "submit_task", "submit_internal", "append_control",
            "read_task", "read_task_snapshot", "read_final_reply",
            # WorkerLedger
            "claim_next", "mark_running", "renew_lease", "heartbeat",
            "checkpoint", "record_progress", "enter_retry_wait",
            "enter_waiting_user", "fail_task", "cancel_task",
            "complete_with_outbox", "complete_internal",
            "promote_due_retries", "reclaim_expired",
            # ExecutionJournal
            "load_restore_point", "list_completed_models",
            "list_completed_tools", "begin_model_attempt",
            "finish_model_attempt", "fail_model_attempt",
            "prepare_tool_call", "begin_tool_attempt",
            "finish_tool_attempt", "mark_tool_unknown",
            "read_tool_call", "read_tool_result_content",
            "list_pending_controls", "acknowledge_control",
            # SessionCommitLedger
            "prepare_session_commit", "confirm_session_commit",
            "mark_session_conflict", "read_session_commit",
            # DeliveryLedger
            "claim_next_delivery", "renew_delivery_lease",
            "mark_delivered", "retry_delivery", "fail_delivery",
            "resolve_unknown_delivery", "claim_expired_deliveries",
            "mark_delivery_outcome_unknown", "read_outbox_record",
            # ResourceLedger
            "acquire_resource", "renew_resource",
            "release_resource", "read_resource_lease",
            # MaintenanceLedger
            "list_retention_batch", "delete_retention_batch",
            "list_unreferenced_blobs", "delete_unreferenced_blobs",
            # DurableEventLog
            "list_events",
            # BlobStore
            "write_blob", "read_blob", "read_blob_content",
        }
        missing = {name for name in required if not hasattr(direct_store, name)}
        assert not missing, f"façade missing methods: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Step 2: Transaction-boundary characterization (Task 12 Step 2)
# ---------------------------------------------------------------------------


class _TraceCollector:
    """Collects SQLite transaction-control statements via trace callback.

    Only ``BEGIN IMMEDIATE``, ``COMMIT``, ``ROLLBACK`` (and plain
    ``BEGIN``) are retained so assertions focus on transaction
    boundaries rather than statement text.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, statement: str) -> None:
        upper = statement.strip().upper()
        if upper.startswith("BEGIN") or upper == "COMMIT" or upper == "ROLLBACK":
            self.statements.append(upper)

    def clear(self) -> None:
        self.statements.clear()


def _txn_summary(collector: _TraceCollector) -> dict[str, int]:
    counts: dict[str, int] = {"BEGIN IMMEDIATE": 0, "COMMIT": 0, "ROLLBACK": 0}
    for stmt in collector.statements:
        counts[stmt] = counts.get(stmt, 0) + 1
    return counts


_ONE_TXN = {"BEGIN IMMEDIATE": 1, "COMMIT": 1, "ROLLBACK": 0}
_TWO_TXN = {"BEGIN IMMEDIATE": 2, "COMMIT": 2, "ROLLBACK": 0}


@pytest.fixture
def traced(tmp_path: Path) -> tuple[SqliteRuntimeStore, _TraceCollector]:
    db_path = tmp_path / "trace.sqlite"
    conn = open_connection(db_path)
    run_migrations(conn)
    collector = _TraceCollector()
    conn.set_trace_callback(collector)
    store = SqliteRuntimeStore(conn)
    return store, collector


def _claim_and_run(
    store: SqliteRuntimeStore,
    scope: RequestScope,
    make_inbound_envelope: Any,
    *,
    channel_message_id: str,
    now_ms: int = 1_000_000,
) -> Any:
    env = make_inbound_envelope(scope, channel_message_id=channel_message_id)
    store.submit_task(env)
    result = store.claim_next(
        ClaimRequest(worker_id="w-1", now_ms=now_ms, lease_ms=60_000)
    )
    assert result.claimed is not None
    store.mark_running(result.claimed.claim, now_ms=now_ms + 1)
    return result.claimed.claim


class TestTransactionBoundaryCharacterization:
    """Each mutating operation uses one ``BEGIN IMMEDIATE`` + one ``COMMIT``.

    These counts are captured on the pre-split baseline and must remain
    identical after the internal module split. ROLLBACK must never appear
    on the success path.
    """

    def test_submit_task_uses_bounded_transactions(
        self,
        traced: tuple[SqliteRuntimeStore, _TraceCollector],
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        # submit_task writes the payload blob (1 txn) then inserts the
        # task row (1 txn). Both are BEGIN IMMEDIATE + COMMIT, no ROLLBACK.
        store, collector = traced
        env = make_inbound_envelope(
            sample_scope, channel_message_id="trace-submit-1"
        )
        collector.clear()
        store.submit_task(env)
        assert _txn_summary(collector) == _TWO_TXN

    def test_claim_next_one_transaction(
        self,
        traced: tuple[SqliteRuntimeStore, _TraceCollector],
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        store, collector = traced
        env = make_inbound_envelope(
            sample_scope, channel_message_id="trace-claim-1"
        )
        store.submit_task(env)
        collector.clear()
        store.claim_next(
            ClaimRequest(worker_id="w-2", now_ms=1_000_000, lease_ms=60_000)
        )
        assert _txn_summary(collector) == _ONE_TXN

    def test_complete_with_outbox_one_transaction(
        self,
        traced: tuple[SqliteRuntimeStore, _TraceCollector],
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        store, collector = traced
        claim = _claim_and_run(
            store, sample_scope, make_inbound_envelope,
            channel_message_id="trace-complete-1",
        )
        collector.clear()
        store.complete_with_outbox(
            claim,
            CompletionWrite(
                final_reply_blob_id=None,
                final_reply_hash=None,
                final_reply_dedup_key=None,
                suppress_final=True,
                completed_at_ms=1_000_002,
            ),
        )
        assert _txn_summary(collector) == _ONE_TXN

    def test_session_prepare_confirm_each_one_transaction(
        self,
        traced: tuple[SqliteRuntimeStore, _TraceCollector],
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        store, collector = traced
        claim = _claim_and_run(
            store, sample_scope, make_inbound_envelope,
            channel_message_id="trace-session-1",
        )
        write = SessionCommitWrite(
            session_key="test-session",
            commit_kind="INBOUND",
            base_revision=0,
            target_revision=1,
            content_hash="hash-1",
            payload_blob_id="",
            created_at_ms=1_000_000,
            session_commit_id="commit-1",
        )
        collector.clear()
        store.prepare_session_commit(claim, write)
        assert _txn_summary(collector) == _ONE_TXN

        collector.clear()
        store.confirm_session_commit(claim, "commit-1", 1, 1_000_003)
        assert _txn_summary(collector) == _ONE_TXN

    def test_tool_attempt_finish_one_transaction(
        self,
        traced: tuple[SqliteRuntimeStore, _TraceCollector],
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        store, collector = traced
        claim = _claim_and_run(
            store, sample_scope, make_inbound_envelope,
            channel_message_id="trace-tool-1",
        )
        args_bytes = json.dumps({"path": "/x"}, sort_keys=True).encode("utf-8")
        import hashlib

        args_hash = hashlib.sha256(args_bytes).hexdigest()
        blob = store.write_blob(
            BlobWrite(
                scope_key=f"task/{claim.task_id}",
                blob_kind="TOOL_ARGUMENTS",
                content_hash=args_hash,
                encoding="RAW_BYTES",
                inline_content=args_bytes,
                size_bytes=len(args_bytes),
            )
        )
        write = PreparedToolWrite(
            tool_call_id="tc-1",
            tool_name="read_file",
            arguments_blob_id=blob.blob_id,
            arguments_hash=args_hash,
            effect_class="READ",
            risk_class="LOW",
            idempotency_mode="IDEMPOTENT",
            idempotency_key="key:tc-1",
            approval_policy="NEVER",
            recovery_policy="REPLAY_SAFE",
            concurrency_scope="TASK",
            created_at_ms=1_000_000,
        )
        store.prepare_tool_call(claim, write)
        attempt = store.begin_tool_attempt(
            claim,
            ToolAttemptWrite(
                tool_call_id="tc-1",
                attempt_no=1,
                resource_token=None,
                started_at_ms=1_000_001,
            ),
        )
        collector.clear()
        store.finish_tool_attempt(
            claim,
            attempt.tool_attempt_id,
            ToolResultWrite(
                state="SUCCEEDED",
                result_blob_id=None,
                result_hash=None,
                finished_at_ms=1_000_002,
            ),
        )
        assert _txn_summary(collector) == _ONE_TXN

    def test_outbox_claim_and_result_each_one_transaction(
        self,
        traced: tuple[SqliteRuntimeStore, _TraceCollector],
        sample_scope: RequestScope,
        make_inbound_envelope: Any,
    ) -> None:
        store, collector = traced
        claim = _claim_and_run(
            store, sample_scope, make_inbound_envelope,
            channel_message_id="trace-outbox-1",
        )
        # A real final-reply blob + routing fields create an outbox row.
        reply_bytes = b'"final reply"'
        import hashlib

        reply_hash = hashlib.sha256(reply_bytes).hexdigest()
        reply_blob = store.write_blob(
            BlobWrite(
                scope_key=f"task/{claim.task_id}",
                blob_kind="FINAL_REPLY",
                content_hash=reply_hash,
                encoding="RAW_BYTES",
                inline_content=reply_bytes,
                size_bytes=len(reply_bytes),
            )
        )
        store.complete_with_outbox(
            claim,
            CompletionWrite(
                final_reply_blob_id=reply_blob.blob_id,
                final_reply_hash=reply_hash,
                final_reply_dedup_key=None,
                suppress_final=False,
                completed_at_ms=1_000_002,
                channel="websocket",
                channel_account="test-account",
                target_key="test-target",
            ),
        )

        collector.clear()
        delivery = store.claim_next_delivery(
            "sender-1", now_ms=1_000_003, lease_ms=60_000
        )
        assert delivery is not None
        assert _txn_summary(collector) == _ONE_TXN

        collector.clear()
        store.mark_delivered(
            delivery,
            DeliveryReceipt(
                status="DELIVERED",
                provider_message_id="pm-1",
            ),
        )
        assert _txn_summary(collector) == _ONE_TXN
