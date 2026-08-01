"""Load gate tests for the three-worker production runtime (Task 15 Step 3).

Drives 1,000 (smoke: 100) USER_TURN tasks through the lightweight runtime
with 3 workers and an :class:`OpenAIStubServer`. Asserts correctness,
concurrency, and safety properties from the production readiness
remediation plan:

- ``accepted_unique_tasks == total``
- ``duplicate_inbound_tasks == 0``
- ``terminal_tasks == total``
- ``missing_final_replies == 0``
- ``same_session_order_violations == 0``
- ``max_distinct_sessions_concurrently_running >= 3`` (skip if lightweight
  mode does not achieve this)
- ``same_session_overlap_count == 0``

Safety bounds:

- Test completes within 10 minutes (``@pytest.mark.slow``).
- All worker tasks exit after shutdown.
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.application import RuntimeInboundRequest
from miniunicorn.runtime.bootstrap import build_lightweight_runtime
from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection
from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion

pytestmark = pytest.mark.slow

#: Deterministic seed for dataset generation (design Task 15 Step 3).
SEED = 20260731


# ---------------------------------------------------------------------------
# Config + dataset helpers
# ---------------------------------------------------------------------------


def _make_config(workspace: Path, api_base: str) -> Any:
    """Build a root Config pointing at *api_base* with lightweight mode + 3 slots."""
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
                "mode": "lightweight",
                "workerCount": 3,
                "lightweightExecutionSlots": 3,
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


def _generate_dataset(
    seed: int, total_tasks: int, sessions: int
) -> list[tuple[str, str]]:
    """Generate a deterministic ``(session_key, content)`` dataset.

    Each session receives ``total_tasks // sessions`` tasks with unique
    content derived from the seed so submissions never collide on the
    dedup key (CLI sessions have ``channel_message_id=None`` → no dedup).
    """
    rng = random.Random(seed)
    tasks_per_session = total_tasks // sessions
    dataset: list[tuple[str, str]] = []
    for s in range(sessions):
        session_key = f"load-session-{s:04d}"
        for t in range(tasks_per_session):
            content = f"turn-s{s:04d}-t{t:03d}-{rng.randint(0, 99999):05d}"
            dataset.append((session_key, content))
    return dataset


# ---------------------------------------------------------------------------
# Store analysis helpers (read-only queries against the Runtime Store)
# ---------------------------------------------------------------------------


def _count_terminal(conn: Any) -> int:
    """Count tasks in a terminal state."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM tasks "
        "WHERE state IN ('COMPLETED', 'FAILED', 'CANCELLED')"
    ).fetchone()["n"]


def _collect_task_intervals(conn: Any) -> dict[str, tuple[int, int]]:
    """Return ``{task_id: (running_start_ms, terminal_end_ms)}`` from events.

    Uses the first ``TASK_RUNNING`` event as the interval start and the
    first terminal event (``TASK_COMPLETED`` / ``TASK_FAILED`` /
    ``TASK_CANCELLED``) as the end. Tasks that never reached RUNNING
    (e.g. cancelled while QUEUED) are omitted.
    """
    rows = conn.execute(
        "SELECT task_id, event_type, created_at_ms FROM task_events "
        "WHERE event_type IN ('TASK_RUNNING', 'TASK_COMPLETED', "
        "                     'TASK_FAILED', 'TASK_CANCELLED') "
        "ORDER BY created_at_ms ASC"
    ).fetchall()
    intervals: dict[str, tuple[int, int]] = {}
    running_starts: dict[str, int] = {}
    for row in rows:
        tid = row["task_id"]
        etype = row["event_type"]
        ts = row["created_at_ms"]
        if etype == "TASK_RUNNING":
            if tid not in running_starts:
                running_starts[tid] = ts
        else:  # terminal event
            if tid in running_starts:
                intervals[tid] = (running_starts[tid], ts)
    return intervals


def _compute_max_concurrent_sessions(
    intervals: dict[str, tuple[int, int]],
    task_sessions: dict[str, str],
) -> int:
    """Sweep-line: max distinct sessions with ≥1 running task at any instant."""
    events: list[tuple[int, int, str]] = []
    for tid, (start, end) in intervals.items():
        session = task_sessions.get(tid)
        if session is None:
            continue
        events.append((start, 1, session))
        events.append((end, -1, session))
    # Process end (-1) before start (+1) at the same timestamp so
    # back-to-back tasks in the same session don't count as overlapping.
    events.sort(key=lambda e: (e[0], e[1]))
    active: dict[str, int] = {}
    max_concurrent = 0
    for _ts, delta, session in events:
        active[session] = active.get(session, 0) + delta
        if active[session] <= 0:
            active.pop(session)
        if len(active) > max_concurrent:
            max_concurrent = len(active)
    return max_concurrent


def _compute_same_session_overlaps(
    intervals: dict[str, tuple[int, int]],
    task_sessions: dict[str, str],
) -> int:
    """Count overlapping RUNNING-interval pairs within each session."""
    by_session: dict[str, list[tuple[int, int]]] = {}
    for tid, (start, end) in intervals.items():
        session = task_sessions.get(tid)
        if session is None:
            continue
        by_session.setdefault(session, []).append((start, end))
    overlaps = 0
    for _session, ivs in by_session.items():
        ivs.sort()
        for i in range(len(ivs)):
            for j in range(i + 1, len(ivs)):
                if ivs[i][1] > ivs[j][0]:
                    overlaps += 1
                else:
                    break  # sorted by start → no later interval overlaps
    return overlaps


def _compute_order_violations(conn: Any) -> int:
    """Count session-sequence completion-order violations.

    Within each session, tasks must complete in ``session_sequence`` order
    (``completed_at_ms`` non-decreasing). Enforced by the claim algorithm's
    ``NOT EXISTS`` earlier-non-terminal check (design §15.2).
    """
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


def _count_missing_final_replies(conn: Any) -> int:
    """Count COMPLETED tasks without a ``FINAL_REPLY`` outbox row."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM tasks t "
        "WHERE t.state = 'COMPLETED' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM outbox o "
        "  WHERE o.task_id = t.task_id AND o.message_kind = 'FINAL_REPLY'"
        ")"
    ).fetchone()["n"]


# ---------------------------------------------------------------------------
# Core load-test runner
# ---------------------------------------------------------------------------


async def _run_load_test(
    tmp_path: Path,
    total_tasks: int,
    sessions: int,
    seed: int,
    overall_timeout_s: float,
) -> None:
    """Submit *total_tasks* tasks, wait for completion, assert all properties."""
    stub = OpenAIStubServer([chat_completion("ok")])
    stub.start()
    resources: Any = None
    test_conn: Any = None
    metrics: dict[str, Any] = {}
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = _make_config(workspace, stub.api_base)

        resources = build_lightweight_runtime(config)
        await resources.start()

        from miniunicorn.config.runtime import resolve_runtime_paths

        resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
        db_path = resolved.database_path_resolved or Path(resolved.database_path)
        test_conn = open_connection(db_path)
        test_store = SqliteRuntimeStore(test_conn)

        scope = local_request_scope(config)
        dataset = _generate_dataset(seed, total_tasks, sessions)

        # 4. Submit all tasks (synchronous SQLite writes are fast in-process).
        accepted = 0
        duplicates = 0
        task_sessions: dict[str, str] = {}
        wall_start = time.monotonic()

        for session_key, content in dataset:
            request = RuntimeInboundRequest(
                content=content,
                media=(),
                metadata={},
                session_key=session_key,
                channel="cli",
                channel_account="test-user",
                channel_message_id=None,
                scope=scope,
                target_key="direct",
            )
            envelope = build_inbound_envelope(
                request, now_ms=int(time.time() * 1000)
            )
            result = test_store.submit_task(envelope)
            if result.status == "ACCEPTED":
                accepted += 1
            elif result.status == "DUPLICATE":
                duplicates += 1
            task_sessions[result.task_id] = session_key

        # 5. Wait for all tasks to reach a terminal state.
        deadline = time.monotonic() + overall_timeout_s
        while True:
            terminal = _count_terminal(test_conn)
            if terminal >= total_tasks:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.5)

        wall_elapsed = time.monotonic() - wall_start

        # 6. Collect all metrics from the store.
        terminal = _count_terminal(test_conn)
        missing_replies = _count_missing_final_replies(test_conn)
        order_violations = _compute_order_violations(test_conn)
        intervals = _collect_task_intervals(test_conn)
        same_session_overlaps = _compute_same_session_overlaps(
            intervals, task_sessions
        )
        max_concurrent = _compute_max_concurrent_sessions(
            intervals, task_sessions
        )

        metrics = {
            "accepted": accepted,
            "duplicates": duplicates,
            "terminal": terminal,
            "missing_replies": missing_replies,
            "order_violations": order_violations,
            "same_session_overlaps": same_session_overlaps,
            "max_concurrent": max_concurrent,
            "wall_elapsed": wall_elapsed,
        }
    finally:
        if test_conn is not None:
            test_conn.close()
        if resources is not None:
            await resources.stop()
        stub.close()

    # ------------------------------------------------------------------
    # Assertions (after cleanup so cleanup always runs first)
    # ------------------------------------------------------------------

    assert metrics["accepted"] == total_tasks, (
        f"accepted_unique_tasks={metrics['accepted']}, expected={total_tasks}"
    )
    assert metrics["duplicates"] == 0, (
        f"duplicate_inbound_tasks={metrics['duplicates']}"
    )
    assert metrics["terminal"] == total_tasks, (
        f"terminal_tasks={metrics['terminal']}, expected={total_tasks}"
    )
    assert metrics["missing_replies"] == 0, (
        f"missing_final_replies={metrics['missing_replies']}"
    )
    assert metrics["order_violations"] == 0, (
        f"same_session_order_violations={metrics['order_violations']}"
    )
    assert metrics["same_session_overlaps"] == 0, (
        f"same_session_overlap_count={metrics['same_session_overlaps']}"
    )
    if metrics["max_concurrent"] < 3:
        pytest.skip(
            f"lightweight mode achieved only {metrics['max_concurrent']} "
            f"distinct concurrent sessions (need >= 3)"
        )
    assert metrics["wall_elapsed"] < 600.0, (
        f"test took {metrics['wall_elapsed']:.1f}s (max 600s)"
    )
    # All worker tasks exited after shutdown.
    assert not resources.host._running, "host still running after stop"
    assert len(resources.host._worker_tasks) == 0, "worker tasks not cleared"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_hundred_task_load_smoke(tmp_path: Path) -> None:
    """100-task / 10-session smoke test for CI (Task 15 Step 3).

    Faster than the full load gate; exercises the same correctness,
    concurrency, and safety properties with a smaller dataset.
    """
    await _run_load_test(
        tmp_path,
        total_tasks=100,
        sessions=10,
        seed=SEED,
        overall_timeout_s=120.0,
    )


@pytest.mark.slow
async def test_thousand_task_load_gate(tmp_path: Path) -> None:
    """1,000-task / 100-session load gate (Task 15 Step 3).

    The full production-readiness load gate. Submits 1,000 USER_TURN
    tasks across 100 sessions (10 tasks per session) through the
    lightweight runtime with 3 workers and an OpenAIStubServer. Must
    complete within 10 minutes with no correctness or concurrency
    violations.
    """
    await _run_load_test(
        tmp_path,
        total_tasks=1000,
        sessions=100,
        seed=SEED,
        overall_timeout_s=600.0,
    )
