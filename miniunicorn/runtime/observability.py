"""Runtime observability: health, status, and metrics (design §34, WP8).

Provides liveness/readiness probes, a detailed status snapshot, and a
Prometheus-style metrics export. All data is derived from the Runtime
Store and the Host state — no external dependencies.

Design references:

- §34 acceptance criteria 26: logs and telemetry pass content-redaction
  tests (no prompt text, tool args, or credentials in metrics).
- §27: configuration fields for alert thresholds.
- §9.1, §9.2: Host state (lightweight vs supervised).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from miniunicorn.runtime.contracts import RuntimeStore


# ---------------------------------------------------------------------------
# Health (liveness + readiness)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeHealth:
    """Liveness and readiness probe result (design §34, WP8).

    ``alive`` means the process is running. ``ready`` means the Runtime
    Store is migrated and accepting work. Both are derived from the
    store connection and schema version — no external calls.
    """

    alive: bool
    ready: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.alive and self.ready else "degraded",
            "alive": self.alive,
            "ready": self.ready,
            "detail": self.detail,
        }


def check_health(store: "RuntimeStore | None") -> RuntimeHealth:
    """Check liveness and readiness of the runtime (design §34, WP8).

    Liveness is always true if this function is called. Readiness
    requires the store to be migrated and queryable.
    """
    if store is None:
        return RuntimeHealth(alive=True, ready=False, detail="runtime store not initialized")
    try:
        # A simple query verifies the connection is live and the schema
        # is migrated.
        conn = _get_conn(store)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        ).fetchone()
        if row is None or row["n"] == 0:
            return RuntimeHealth(alive=True, ready=False, detail="schema not migrated")
        return RuntimeHealth(alive=True, ready=True)
    except Exception as exc:
        return RuntimeHealth(alive=True, ready=False, detail=f"store error: {exc}")


# ---------------------------------------------------------------------------
# Status (detailed snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeStatus:
    """Detailed runtime status snapshot (design §34, WP8).

    Includes task queue depth by state, outbox depth by state, lease
    statistics, and (optionally) host/supervisor state.
    """

    schema_version: int
    tasks_by_state: dict[str, int] = field(default_factory=dict)
    outbox_by_state: dict[str, int] = field(default_factory=dict)
    active_leases: int = 0
    expired_leases: int = 0
    blob_count: int = 0
    database_size_bytes: int = 0
    host_mode: str = "unknown"
    host_started: bool = False
    host_snapshot: dict[str, Any] | None = None
    timestamp_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tasks_by_state": dict(self.tasks_by_state),
            "outbox_by_state": dict(self.outbox_by_state),
            "active_leases": self.active_leases,
            "expired_leases": self.expired_leases,
            "blob_count": self.blob_count,
            "database_size_bytes": self.database_size_bytes,
            "host_mode": self.host_mode,
            "host_started": self.host_started,
            "host_snapshot": self.host_snapshot,
            "timestamp_ms": self.timestamp_ms,
        }


def collect_status(
    store: "RuntimeStore | None",
    *,
    host_mode: str = "unknown",
    host_started: bool = False,
    host_snapshot: dict[str, Any] | None = None,
) -> RuntimeStatus:
    """Collect a detailed runtime status snapshot (design §34, WP8).

    All data is derived from synchronous store queries — no external
    calls, no prompt or tool-arg content in the output (design §34
    criterion 26).
    """
    now_ms = int(time.time() * 1000)
    if store is None:
        return RuntimeStatus(
            schema_version=0,
            host_mode=host_mode,
            host_started=host_started,
            host_snapshot=host_snapshot,
            timestamp_ms=now_ms,
        )

    try:
        conn = _get_conn(store)

        # Schema version.
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        schema_version = row["v"] if row and row["v"] else 0

        # Tasks by state.
        tasks_by_state: dict[str, int] = {}
        for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
        ).fetchall():
            tasks_by_state[r["state"]] = r["n"]

        # Outbox by state.
        outbox_by_state: dict[str, int] = {}
        for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM outbox GROUP BY state"
        ).fetchall():
            outbox_by_state[r["state"]] = r["n"]

        # Active and expired leases.
        active_leases = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('LEASED', 'RUNNING')"
        ).fetchone()["n"]
        expired_leases = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('LEASED', 'RUNNING') "
            "AND lease_until_ms IS NOT NULL AND lease_until_ms < ?",
            (now_ms,),
        ).fetchone()["n"]

        # Blob count.
        blob_count = conn.execute(
            "SELECT COUNT(*) AS n FROM runtime_blobs"
        ).fetchone()["n"]

        # Database size (page_count * page_size).
        db_size = 0
        try:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            db_size = page_count * page_size
        except Exception:
            pass

        return RuntimeStatus(
            schema_version=schema_version,
            tasks_by_state=tasks_by_state,
            outbox_by_state=outbox_by_state,
            active_leases=active_leases,
            expired_leases=expired_leases,
            blob_count=blob_count,
            database_size_bytes=db_size,
            host_mode=host_mode,
            host_started=host_started,
            host_snapshot=host_snapshot,
            timestamp_ms=now_ms,
        )
    except Exception:
        return RuntimeStatus(
            schema_version=0,
            host_mode=host_mode,
            host_started=host_started,
            host_snapshot=host_snapshot,
            timestamp_ms=now_ms,
        )


# ---------------------------------------------------------------------------
# Metrics (Prometheus-style text format)
# ---------------------------------------------------------------------------


def collect_metrics_text(
    store: "RuntimeStore | None",
    *,
    host_mode: str = "unknown",
    host_started: bool = False,
) -> str:
    """Export metrics in Prometheus text exposition format (design §34, WP8).

    No prompt text, tool arguments, or credentials are included (design
    §34 criterion 26). Only aggregate counts and durations.
    """
    status = collect_status(store, host_mode=host_mode, host_started=host_started)
    lines: list[str] = []

    # Helper to emit a gauge.
    def _gauge(name: str, value: int | float, help_text: str = "") -> None:
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    _gauge(
        "miniunicorn_runtime_schema_version",
        status.schema_version,
        "Runtime Store schema version",
    )
    _gauge(
        "miniunicorn_runtime_host_started",
        1 if status.host_started else 0,
        "Whether the host is started (1) or stopped (0)",
    )
    _gauge(
        "miniunicorn_runtime_active_leases",
        status.active_leases,
        "Number of tasks in LEASED or RUNNING state",
    )
    _gauge(
        "miniunicorn_runtime_expired_leases",
        status.expired_leases,
        "Number of leased/running tasks with expired lease_until_ms",
    )
    _gauge(
        "miniunicorn_runtime_blob_count",
        status.blob_count,
        "Total number of runtime blobs",
    )
    _gauge(
        "miniunicorn_runtime_database_size_bytes",
        status.database_size_bytes,
        "Runtime Store database size in bytes",
    )

    # Tasks by state.
    for state, count in sorted(status.tasks_by_state.items()):
        lines.append(
            f'miniunicorn_runtime_tasks_by_state{{state="{state}"}} {count}'
        )

    # Outbox by state.
    for state, count in sorted(status.outbox_by_state.items()):
        lines.append(
            f'miniunicorn_runtime_outbox_by_state{{state="{state}"}} {count}'
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_conn(store: "RuntimeStore") -> sqlite3.Connection:
    """Get the SQLite connection from the store (internal helper)."""
    # SqliteRuntimeStore stores its connection as self._conn.
    return store._conn  # type: ignore[attr-defined]


__all__ = [
    "RuntimeHealth",
    "RuntimeStatus",
    "check_health",
    "collect_status",
    "collect_metrics_text",
]
