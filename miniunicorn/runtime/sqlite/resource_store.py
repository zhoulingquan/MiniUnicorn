"""Resource ledger mixin for the SQLite Runtime Store (design §16.14).

Holds :class:`ResourceStoreMixin` covering ``ResourceLedger``: acquire,
renew, release, and read of resource leases. The mixin shares
``self._conn`` with the other responsibility mixins via the façade
(design §7.3, §11.2, §16.14; Task 12 Steps 3-5).
"""

from __future__ import annotations

import sqlite3

from miniunicorn.runtime.models import (
    ResourceLease,
    ResourceLeaseRecord,
    ResourceLeaseRequest,
)
from miniunicorn.runtime.sqlite.base_store import _new_lease_token, _now_ms


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


class ResourceStoreMixin:
    """Resource lease acquire/renew/release/read operations."""

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
