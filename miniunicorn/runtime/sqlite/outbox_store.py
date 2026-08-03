"""Delivery (Outbox) ledger mixin for the SQLite Runtime Store (design §17.9, §17.13).

Holds :class:`OutboxStoreMixin` covering ``DeliveryLedger``: outbox claim,
renew, mark delivered, retry, fail, unknown-resolution, expired recovery,
read, final-reply read, and message-tool enqueue. The mixin shares
``self._conn`` with the other responsibility mixins via the façade and
calls shared lease-validation helpers on the base (design §7.3, §11.2,
§17; Task 12 Steps 3-5).
"""

from __future__ import annotations

import json
import sqlite3

from miniunicorn.agent.ports import SafeError
from miniunicorn.runtime.contracts import StaleLeaseError, TaskClaim
from miniunicorn.runtime.models import (
    DeliveryReceipt,
    DeliveryResolution,
    DurableReply,
    OutboxClaim,
    OutboxRecord,
    RequestScope,
    RetryDecision,
)
from miniunicorn.runtime.sqlite.base_store import _new_lease_token, _now_ms


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


class OutboxStoreMixin:
    """Outbox delivery claim/renew/mark/retry/fail/resolve/read operations."""

    def claim_next_delivery(self, sender_id: str, now_ms: int, lease_ms: int) -> OutboxClaim | None:
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
                (
                    sender_id,
                    lease_token,
                    lease_epoch,
                    lease_until_ms,
                    attempt_count,
                    row["outbox_id"],
                ),
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
                raise StaleLeaseError(
                    str(claim.outbox_id), claim.lease_epoch, "delivery lease stale"
                )
            self._conn.execute("COMMIT")
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
                (
                    error.error_code,
                    error.error_summary,
                    claim.outbox_id,
                    claim.lease_token,
                    claim.lease_epoch,
                ),
            )
            if cur.rowcount == 0:
                raise StaleLeaseError(
                    str(claim.outbox_id), claim.lease_epoch, "delivery lease stale"
                )
            self._conn.execute("COMMIT")
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
                raise ValueError(f"outbox row {outbox_id} not found")
            if row["state"] != "OUTCOME_UNKNOWN":
                raise ValueError(f"outbox row {outbox_id} is {row['state']}, not OUTCOME_UNKNOWN")
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
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def claim_expired_deliveries(
        self,
        sender_id: str,
        now_ms: int,
        lease_ms: int,
        limit: int = 10,
    ) -> tuple[OutboxClaim, ...]:
        """Claim expired ``SENDING`` rows for recovery (Task 8, design §17.13).

        Selects bounded rows whose lease has expired and re-fences them
        with a fresh lease token and epoch so only the recovery caller
        may write the result. The row stays in ``SENDING`` so no other
        sender can claim it concurrently.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """
                SELECT * FROM outbox
                WHERE state='SENDING'
                  AND lease_until_ms IS NOT NULL
                  AND lease_until_ms < ?
                ORDER BY outbox_id
                LIMIT ?
                """,
                (now_ms, limit),
            ).fetchall()
            claims: list[OutboxClaim] = []
            for row in rows:
                lease_token = _new_lease_token()
                lease_epoch = row["lease_epoch"] + 1
                lease_until_ms = now_ms + lease_ms
                self._conn.execute(
                    """
                    UPDATE outbox SET
                        leased_by=?,
                        lease_token=?,
                        lease_epoch=?,
                        lease_until_ms=?
                    WHERE outbox_id=?
                    """,
                    (sender_id, lease_token, lease_epoch, lease_until_ms, row["outbox_id"]),
                )
                updated = self._conn.execute(
                    "SELECT * FROM outbox WHERE outbox_id=?", (row["outbox_id"],)
                ).fetchone()
                claims.append(_row_to_outbox_claim(updated))
            self._conn.execute("COMMIT")
            return tuple(claims)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def mark_delivery_outcome_unknown(self, claim: OutboxClaim, error: SafeError) -> None:
        """Transition a claimed delivery to ``OUTCOME_UNKNOWN`` (Task 8, design §17.13).

        Used by the recovery path when the Channel recovery policy is
        ``NONE`` or a ``QUERYABLE_RECEIPT`` query cannot reconcile the
        send. Fenced by the recovery claim's lease token and epoch.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE outbox SET
                    state='OUTCOME_UNKNOWN',
                    error_code=?,
                    error_summary=?,
                    lease_until_ms=NULL
                WHERE outbox_id=? AND lease_token=? AND lease_epoch=?
                """,
                (
                    error.error_code,
                    error.error_summary,
                    claim.outbox_id,
                    claim.lease_token,
                    claim.lease_epoch,
                ),
            )
            if cur.rowcount == 0:
                raise StaleLeaseError(
                    str(claim.outbox_id), claim.lease_epoch, "delivery lease stale"
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def read_outbox_record(self, outbox_id: int) -> OutboxRecord | None:
        """Read an outbox row by ID."""
        row = self._conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
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
        scope_predicate, scope_params = self._task_scope_predicate(scope, alias="t")
        row = self._conn.execute(
            f"""
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
              AND {scope_predicate}
            ORDER BY o.outbox_id DESC
            LIMIT 1
            """,
            (task_id, *scope_params),
        ).fetchone()
        if row is None:
            return None
        content = ""
        if row["inline_content"] is not None and row["encoding"] == "RAW_BYTES":
            # Task 7 Step 6: decode the versioned Outbox codec envelope.
            # Legacy raw-text FINAL_REPLY blobs fall back to the literal
            # UTF-8 string via decode_outbox_payload's FINAL_REPLY path.
            from miniunicorn.runtime.outbox_payload import decode_outbox_payload

            content = decode_outbox_payload("FINAL_REPLY", bytes(row["inline_content"])).content
        return DurableReply(
            content=content,
            outbox_id=int(row["outbox_id"]),
            metadata={
                "task_id": task_id,
                "task_state": row["task_state"],
                "delivery_state": row["outbox_state"],
                "outbox_id": int(row["outbox_id"]),
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
