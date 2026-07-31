"""Stateless Scheduler library over WorkerLedger (design §8.2, §15).

The Scheduler is a stateless library that atomically claims the
highest-priority eligible session head, issues random lease tokens,
renews valid leases, releases or reclaims expired work, and promotes
elapsed ``RETRY_WAIT`` tasks back to ``QUEUED``.

There is no central in-memory dispatch queue. In supervised mode each
Worker calls the same Scheduler against SQLite. SQLite serializes the
short claim transaction. Wake IPC only reduces polling latency
(design §8.2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from miniunicorn.runtime.contracts import (
    ClaimRequest,
    ClaimResult,
    ClaimedTask,
    ReclaimResult,
    StaleLeaseError,
    WorkerLedger,
)


@dataclass(slots=True, frozen=True)
class ClaimOutcome:
    """Result of a scheduler claim attempt.

    ``claimed`` is the claimed task and claim handle, or ``None`` when
    nothing is eligible. When ``NothingEligible`` is the reason, the
    caller may sleep before retrying.
    """

    claimed: ClaimedTask | None
    reason: str  # "CLAIMED", "NOTHING_ELIGIBLE", "LEASE_RECLAIMED"


class Scheduler:
    """Stateless scheduler over :class:`WorkerLedger` (design §8.2, §15).

    The Scheduler does not own connections, queues, or Worker processes.
    Each call is a single short transaction against the Runtime Store.
    """

    def __init__(
        self,
        worker_ledger: WorkerLedger,
        *,
        lease_ms: int = 180_000,
        max_root_attempts: int = 3,
    ) -> None:
        self._ledger = worker_ledger
        self._lease_ms = lease_ms
        self._max_root_attempts = max_root_attempts

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def claim_next(self, worker_id: str, *, now_ms: int | None = None) -> ClaimOutcome:
        """Claim the highest-priority eligible session head (design §15.2, §15.3).

        Promotes due ``RETRY_WAIT`` tasks and reclaims expired leases
        before attempting a claim. Returns a :class:`ClaimOutcome` with
        ``claimed`` set when a task was claimed, or ``None`` when nothing
        is eligible.
        """
        now = now_ms if now_ms is not None else _now_ms()

        # Promote due retries and reclaim expired leases before claiming
        # (design §15.3 step 1).
        self._ledger.promote_due_retries(now, limit=100)
        self._ledger.reclaim_expired(now, limit=100)

        request = ClaimRequest(
            worker_id=worker_id,
            now_ms=now,
            lease_ms=self._lease_ms,
            max_root_attempts=self._max_root_attempts,
        )
        result = self._ledger.claim_next(request)
        if result.claimed is None:
            return ClaimOutcome(claimed=None, reason="NOTHING_ELIGIBLE")
        return ClaimOutcome(claimed=result.claimed, reason="CLAIMED")

    # ------------------------------------------------------------------
    # Lease management
    # ------------------------------------------------------------------

    def renew_lease(self, claim: object, *, now_ms: int | None = None) -> bool:
        """Renew a task lease (design §6.11, §14.4).

        Returns ``True`` if the lease was renewed, ``False`` if the
        claim is stale (token/epoch mismatch or expired deadline).
        """
        now = now_ms if now_ms is not None else _now_ms()
        try:
            return self._ledger.renew_lease(  # type: ignore[arg-type]
                claim,
                now + self._lease_ms,
                now_ms=now,
            )
        except StaleLeaseError:
            return False

    def heartbeat(self, claim: object, *, now_ms: int | None = None) -> bool:
        """Send a heartbeat that renews the lease (design §6.11, Task 2 Step 4).

        A successful heartbeat atomically sets both ``lease_until_ms`` and
        ``last_heartbeat_at_ms``. Returns ``True`` if the lease was renewed,
        ``False`` if the claim is stale.
        """
        return self.renew_lease(claim, now_ms=now_ms)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def reclaim_expired(self, *, now_ms: int | None = None, limit: int = 100) -> ReclaimResult:
        """Reclaim expired leases (design §24.2)."""
        now = now_ms if now_ms is not None else _now_ms()
        return self._ledger.reclaim_expired(now, limit)

    def promote_due_retries(self, *, now_ms: int | None = None, limit: int = 100) -> int:
        """Promote due ``RETRY_WAIT`` tasks back to ``QUEUED`` (design §17.5)."""
        now = now_ms if now_ms is not None else _now_ms()
        return self._ledger.promote_due_retries(now, limit)


def _now_ms() -> int:
    """Current UTC Unix milliseconds (design §12.2)."""
    return int(time.time() * 1000)


__all__ = ["Scheduler", "ClaimOutcome"]
