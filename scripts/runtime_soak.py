#!/usr/bin/env python3
"""Runtime soak test: drives sustained load against a supervised runtime.

Creates an isolated workspace/database under a temp directory, starts a
production supervised runtime (Control Plane + 3 Workers) with an
OpenAIStubServer, drives USER_TURN tasks at the specified rate for the
specified duration, and writes periodic + final JSON summaries to the
output file.

Returns nonzero on:

- missing work (submitted tasks that never reached a terminal state)
- duplicate effects (DUPLICATE submit results)
- ordering violations (same-session completion order)
- unbounded queue age (tasks stuck in QUEUED at the end)
- child leaks (Worker/Control-Plane processes still alive after shutdown)

Always stops child processes and closes the temporary workspace
(try/finally).

Usage::

    python scripts/runtime_soak.py --duration-minutes 1 --sessions 20 \
        --rate-per-second 1 --seed 20260731 --output runtime-soak.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure the project root is on sys.path so both ``miniunicorn`` and
# ``tests.runtime.support.openai_stub`` are importable when the script is
# run directly from the scripts/ directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Config + summary helpers
# ---------------------------------------------------------------------------


def _make_config(workspace: Path, api_base: str) -> Any:
    """Build a root Config with supervised runtime + 3 workers."""
    from miniunicorn.config.schema import Config

    return Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "provider": "custom",
                    "model": "stub-model",
                    "contextWindowTokens": 32768,
                },
            },
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": api_base,
                    "apiType": "chat_completions",
                },
            },
            "runtime": {
                "mode": "supervised",
                "workerCount": 3,
                "heartbeatIntervalS": 2,
                "leaseTimeoutS": 30,
                "progressTimeoutS": 60,
                "queuePollMaxMs": 500,
                "leaseScanIntervalS": 5,
                "outboxLeaseTimeoutS": 30,
                "channelSendTimeoutS": 10,
                "sqliteBusyTimeoutMs": 1000,
                "shutdownGraceS": 5,
            },
        }
    )


def _write_summary(output_path: Path, summary: dict[str, Any]) -> None:
    """Write the JSON summary to *output_path* (atomic-ish overwrite)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(output_path)


def _collect_current_summary(
    conn: Any,
    *,
    total_submitted: int,
    duplicate_effects: int,
    elapsed_s: float,
    same_session_overlaps: int,
) -> dict[str, Any]:
    """Query the store for the current state and build a summary dict."""
    total_completed = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE state = 'COMPLETED'"
    ).fetchone()["n"]
    total_failed = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('FAILED', 'CANCELLED')"
    ).fetchone()["n"]
    final_queue_depth = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE state = 'QUEUED'"
    ).fetchone()["n"]
    final_outbox_depth = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE state NOT IN ('DELIVERED', 'FAILED')"
    ).fetchone()["n"]
    return {
        "total_submitted": total_submitted,
        "total_completed": total_completed,
        "total_failed": total_failed,
        "duplicate_effects": duplicate_effects,
        "same_session_overlaps": same_session_overlaps,
        "duration_seconds": round(elapsed_s, 2),
        "final_queue_depth": final_queue_depth,
        "final_outbox_depth": final_outbox_depth,
    }


def _compute_same_session_overlaps(conn: Any) -> int:
    """Count overlapping RUNNING-interval pairs within each session."""
    intervals: dict[str, tuple[int, int]] = {}
    running_starts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT task_id, event_type, created_at_ms FROM task_events "
        "WHERE event_type IN ('TASK_RUNNING', 'TASK_COMPLETED', "
        "                     'TASK_FAILED', 'TASK_CANCELLED') "
        "ORDER BY created_at_ms ASC"
    ).fetchall()
    for row in rows:
        tid = row["task_id"]
        etype = row["event_type"]
        ts = row["created_at_ms"]
        if etype == "TASK_RUNNING":
            if tid not in running_starts:
                running_starts[tid] = ts
        else:
            if tid in running_starts:
                intervals[tid] = (running_starts[tid], ts)

    task_sessions: dict[str, str] = {}
    for row in conn.execute("SELECT task_id, session_key FROM tasks").fetchall():
        task_sessions[row["task_id"]] = row["session_key"]

    by_session: dict[str, list[tuple[int, int]]] = {}
    for tid, (start, end) in intervals.items():
        session = task_sessions.get(tid)
        if session is None:
            continue
        by_session.setdefault(session, []).append((start, end))

    overlaps = 0
    for ivs in by_session.values():
        ivs.sort()
        for i in range(len(ivs)):
            for j in range(i + 1, len(ivs)):
                if ivs[i][1] > ivs[j][0]:
                    overlaps += 1
                else:
                    break
    return overlaps


def _compute_order_violations(conn: Any) -> int:
    """Count same-session completion-order violations."""
    rows = conn.execute(
        "SELECT session_key, session_sequence, completed_at_ms FROM tasks "
        "WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED') "
        "AND completed_at_ms IS NOT NULL "
        "ORDER BY session_key, session_sequence"
    ).fetchall()
    violations = 0
    prev_session: str | None = None
    prev_completed: int | None = None
    for row in rows:
        if row["session_key"] != prev_session:
            prev_session = row["session_key"]
            prev_completed = row["completed_at_ms"]
        else:
            if prev_completed is not None and row["completed_at_ms"] < prev_completed:
                violations += 1
            if prev_completed is None or row["completed_at_ms"] > prev_completed:
                prev_completed = row["completed_at_ms"]
    return violations


def _collect_worker_ids(conn: Any) -> list[str]:
    """Return the distinct Worker ids that leased at least one task.

    Sourced from the immutable ``TASK_LEASED`` task events, whose
    ``safe_payload`` records ``{"leased_by": worker_id}`` at claim time.
    The ``tasks.leased_by`` column is cleared on completion/reclaim, so
    only the event log retains the full set of Workers that executed
    work. A healthy supervised soak over three Workers must yield exactly
    three distinct ids.
    """
    rows = conn.execute(
        "SELECT DISTINCT json_extract(safe_payload_json, '$.leased_by') AS wid "
        "FROM task_events "
        "WHERE event_type = 'TASK_LEASED' "
        "AND json_extract(safe_payload_json, '$.leased_by') IS NOT NULL "
        "ORDER BY wid ASC"
    ).fetchall()
    return [row["wid"] for row in rows]


def _count_missing_final_replies(conn: Any) -> int:
    """Count COMPLETED tasks without a ``FINAL_REPLY`` outbox row.

    Every COMPLETED interactive (``USER_TURN``) task must enqueue exactly
    one ``FINAL_REPLY``; a missing row means the final reply was lost
    before delivery routing.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM tasks t "
        "WHERE t.state = 'COMPLETED' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM outbox o "
        "  WHERE o.task_id = t.task_id AND o.message_kind = 'FINAL_REPLY'"
        ")"
    ).fetchone()["n"]


def _count_stale_mutations(conn: Any) -> int:
    """Count durable fenced-lease diagnostics.

    Each ``LEASE_RECLAIMED`` task event records a lease that was broken
    by the reclaim scanner — the durable signal that a Worker's mutation
    was fenced off (stale lease). In a healthy soak with no Worker
    crashes this must be 0.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE event_type = 'LEASE_RECLAIMED'"
    ).fetchone()["n"]


def _count_unresolved_sqlite_busy(conn: Any) -> int:
    """Count terminal tasks whose failure records an unresolved SQLite busy.

    SQLite busy errors are retried inside the busy-timeout window; a
    terminal task whose ``error_code``/``error_summary`` records a busy
    failure means the retry budget was exhausted — a release-blocking
    durability fault.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM tasks "
        "WHERE state = 'FAILED' "
        "AND (error_code LIKE '%BUSY%' OR error_summary LIKE '%busy%' "
        "     OR error_code LIKE '%SQLITE_BUSY%')"
    ).fetchone()["n"]


# ---------------------------------------------------------------------------
# Release summary validator (pure, no I/O)
# ---------------------------------------------------------------------------


#: The authoritative set of release-soak summary keys. Every key must be
#: present in a report and every zero-guard must equal zero.
_RELEASE_SUMMARY_ZERO_GUARDS: tuple[str, ...] = (
    "missing_terminal",
    "missing_final_replies",
    "duplicate_effects",
    "same_session_overlaps",
    "same_session_order_violations",
    "stale_mutations",
    "unresolved_sqlite_busy",
    "children_alive_after_shutdown",
)


def assert_release_soak_summary(summary: dict[str, Any]) -> None:
    """Assert *summary* satisfies the release soak contract.

    Requires exactly three distinct ``worker_ids`` and every zero-guard
    key equal to zero. Each failure raises :class:`AssertionError` with a
    diagnostic that names the offending key.

    Pure: no I/O, sockets, or runtime dependencies — safe to unit-test
    and to run against a loaded JSON report.
    """
    worker_ids = summary["worker_ids"]
    assert len(set(worker_ids)) == 3, (
        f"worker_ids must contain exactly 3 distinct ids, got {worker_ids!r}"
    )
    for key in _RELEASE_SUMMARY_ZERO_GUARDS:
        value = summary[key]
        assert value == 0, f"{key}={value}, expected 0"


# ---------------------------------------------------------------------------
# Async main
# ---------------------------------------------------------------------------


async def _run_soak(args: argparse.Namespace) -> int:
    """Drive the supervised runtime and return a process exit code."""
    from miniunicorn.config.runtime import resolve_runtime_paths
    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.bootstrap import build_supervised_runtime
    from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection
    from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion

    # Isolated temp workspace.
    temp_root = Path(tempfile.mkdtemp(prefix="runtime-soak-"))
    workspace = temp_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).resolve()

    stub = OpenAIStubServer([chat_completion("ok")])
    stub.start()
    resources: Any = None
    test_conn: Any = None
    final_summary: dict[str, Any] | None = None
    exit_holder: dict[str, int] = {"exit_code": 0}
    try:
        config = _make_config(workspace, stub.api_base)
        resources = build_supervised_runtime(config)
        await resources.start()

        resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
        db_path = resolved.database_path_resolved or Path(resolved.database_path)
        test_conn = open_connection(db_path)
        test_store = SqliteRuntimeStore(test_conn)
        scope = local_request_scope(config)

        rng = random.Random(args.seed)
        total_submitted = 0
        duplicate_effects = 0

        duration_s = args.duration_minutes * 60
        rate = args.rate_per_second
        interval = 1.0 / rate if rate > 0 else 0.0
        start = time.monotonic()
        last_summary = start

        session_idx = 0

        # ---- Drive tasks at the specified rate ----
        while time.monotonic() - start < duration_s:
            session_key = f"soak-session-{session_idx % args.sessions:04d}"
            content = f"soak-{total_submitted:06d}-{rng.randint(0, 99999):05d}"
            request = RuntimeInboundRequest(
                content=content,
                media=(),
                metadata={},
                session_key=session_key,
                channel="cli",
                channel_account="soak-user",
                channel_message_id=None,
                scope=scope,
                target_key="direct",
            )
            envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
            result = test_store.submit_task(envelope)
            if result.status == "ACCEPTED":
                total_submitted += 1
            elif result.status == "DUPLICATE":
                duplicate_effects += 1

            session_idx += 1

            # Fan out a wake hint so Workers poll immediately.
            try:
                resources.host.notify_submit(task_id=result.task_id)
            except Exception:
                pass  # best-effort

            # Periodic summary every 10 seconds.
            now = time.monotonic()
            if now - last_summary >= 10.0:
                elapsed = now - start
                summary = _collect_current_summary(
                    test_conn,
                    total_submitted=total_submitted,
                    duplicate_effects=duplicate_effects,
                    elapsed_s=elapsed,
                    same_session_overlaps=0,  # computed in final summary
                )
                _write_summary(output_path, summary)
                print(
                    f"[soak] {elapsed:.0f}s: submitted={summary['total_submitted']} "
                    f"completed={summary['total_completed']} "
                    f"failed={summary['total_failed']} "
                    f"queue={summary['final_queue_depth']}",
                    file=sys.stderr,
                )
                last_summary = now

            if interval > 0:
                await asyncio.sleep(interval)

        # ---- Wait for all submitted tasks to reach a terminal state ----
        drain_deadline = time.monotonic() + max(120.0, duration_s)
        while time.monotonic() < drain_deadline:
            terminal = test_conn.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED')"
            ).fetchone()["n"]
            if terminal >= total_submitted:
                break
            await asyncio.sleep(1.0)

        # ---- Collect final metrics ----
        same_session_overlaps = _compute_same_session_overlaps(test_conn)
        order_violations = _compute_order_violations(test_conn)
        missing_final_replies = _count_missing_final_replies(test_conn)
        stale_mutations = _count_stale_mutations(test_conn)
        unresolved_sqlite_busy = _count_unresolved_sqlite_busy(test_conn)
        worker_ids = _collect_worker_ids(test_conn)

        final_summary = _collect_current_summary(
            test_conn,
            total_submitted=total_submitted,
            duplicate_effects=duplicate_effects,
            elapsed_s=time.monotonic() - start,
            same_session_overlaps=same_session_overlaps,
        )
        final_summary["same_session_order_violations"] = order_violations
        # Task 26 Step 2: release-evidence keys sourced from durable facts.
        final_summary["worker_ids"] = worker_ids
        final_summary["missing_terminal"] = max(
            0,
            total_submitted - (final_summary["total_completed"] + final_summary["total_failed"]),
        )
        final_summary["missing_final_replies"] = missing_final_replies
        final_summary["stale_mutations"] = stale_mutations
        final_summary["unresolved_sqlite_busy"] = unresolved_sqlite_busy
        # children_alive_after_shutdown is populated after stop() in finally.
        final_summary["children_alive_after_shutdown"] = 0
        _write_summary(output_path, final_summary)

        print(
            json.dumps(final_summary, indent=2, sort_keys=True),
            file=sys.stderr,
        )

        # ---- Determine exit code ----
        exit_code = 0
        if final_summary["total_completed"] + final_summary["total_failed"] < total_submitted:
            missing = total_submitted - (
                final_summary["total_completed"] + final_summary["total_failed"]
            )
            print(f"ERROR: missing work: {missing} tasks not terminal", file=sys.stderr)
            exit_code = 1
        if duplicate_effects > 0:
            print(f"ERROR: duplicate effects: {duplicate_effects}", file=sys.stderr)
            exit_code = 1
        if order_violations > 0:
            print(f"ERROR: ordering violations: {order_violations}", file=sys.stderr)
            exit_code = 1
        if final_summary["final_queue_depth"] > 0:
            print(
                f"ERROR: unbounded queue age: {final_summary['final_queue_depth']} "
                f"tasks still QUEUED",
                file=sys.stderr,
            )
            exit_code = 1
        if same_session_overlaps > 0:
            print(
                f"ERROR: same-session overlaps: {same_session_overlaps}",
                file=sys.stderr,
            )
            exit_code = 1
        # Task 26: release-evidence guards.
        if len(set(worker_ids)) != 3:
            print(
                f"ERROR: expected 3 distinct worker ids, got {worker_ids}",
                file=sys.stderr,
            )
            exit_code = 1
        if missing_final_replies > 0:
            print(
                f"ERROR: missing final replies: {missing_final_replies}",
                file=sys.stderr,
            )
            exit_code = 1
        if stale_mutations > 0:
            print(f"ERROR: stale mutations (LEASE_RECLAIMED): {stale_mutations}", file=sys.stderr)
            exit_code = 1
        if unresolved_sqlite_busy > 0:
            print(
                f"ERROR: unresolved sqlite busy: {unresolved_sqlite_busy}",
                file=sys.stderr,
            )
            exit_code = 1

        exit_holder["exit_code"] = exit_code
    finally:
        if test_conn is not None:
            test_conn.close()
        if resources is not None:
            try:
                await resources.stop()
            except Exception as exc:
                print(f"WARNING: error during resources.stop(): {exc}", file=sys.stderr)
        stub.close()

        # Check for child leaks after shutdown (Task 26: durable evidence).
        children_alive_after_shutdown = 0
        if resources is not None:
            try:
                snap = resources.host.snapshot()
                alive = [c for c in snap.get("children", []) if c.get("alive")]
                children_alive_after_shutdown = len(alive)
                if alive:
                    print(
                        f"ERROR: child leaks: {len(alive)} child processes "
                        f"still alive after shutdown",
                        file=sys.stderr,
                    )
                    # Force-terminate survivors.
                    try:
                        resources.host.terminate()
                    except Exception:
                        pass
            except Exception as exc:
                print(
                    f"WARNING: could not check child leaks: {exc}",
                    file=sys.stderr,
                )

        # Record child liveness into the final summary and re-write it so
        # the saved report carries the complete release-evidence contract.
        if final_summary is not None:
            final_summary["children_alive_after_shutdown"] = children_alive_after_shutdown
            _write_summary(output_path, final_summary)
            if children_alive_after_shutdown > 0:
                exit_holder["exit_code"] = 1

        # Clean up the temp workspace.
        shutil.rmtree(temp_root, ignore_errors=True)

    return exit_holder["exit_code"]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drive sustained load against a supervised runtime.",
    )
    parser.add_argument(
        "--duration-minutes",
        type=int,
        default=5,
        help="How long to drive traffic (default: 5 minutes).",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=20,
        help="Number of distinct session keys to cycle through (default: 20).",
    )
    parser.add_argument(
        "--rate-per-second",
        type=float,
        default=1.0,
        help="Task submission rate in tasks/second (default: 1.0).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260731,
        help="Deterministic seed for task content generation (default: 20260731).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="runtime-soak.json",
        help="Path to write the JSON summary (default: runtime-soak.json).",
    )
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_run_soak(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
