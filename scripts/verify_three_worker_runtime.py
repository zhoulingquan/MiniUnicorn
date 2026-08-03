#!/usr/bin/env python3
"""Three-worker runtime topology and crash-recovery verifier.

Task 24: proves the production supervised runtime starts one Control
Plane + exactly three Workers, executes a durable task, and leaves no
child process alive after stop().

Task 25: adds ``--kill-owner`` crash-recovery mode. When that flag is
present the stub is configured with a bounded delayed first response,
the task is submitted, the real lease-owning Worker (resolved from
``TaskRecord.leased_by``) is terminated, and the script waits for
terminal completion. The output schema records the killed worker id,
recovery attempt count, exactly one logical Provider decision, no
duplicate MODEL_STARTED events, no duplicate final replies, and no
child alive after stop().

Usage::

    # Topology proof (Task 24)
    uv run python scripts/verify_three_worker_runtime.py \
        --workspace .three-worker-evidence-workspace \
        --output three-worker-evidence.json

    # Crash-recovery proof (Task 25)
    uv run python scripts/verify_three_worker_runtime.py \
        --workspace .three-worker-crash-workspace \
        --output three-worker-crash-evidence.json --kill-owner
"""

from __future__ import annotations

import argparse
import asyncio
import json
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

EXPECTED_WORKER_IDS = ("worker-0", "worker-1", "worker-2")
TASK_CONTENT = "three-worker-proof"
SESSION_KEY = "three-worker-proof-session"


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


def _make_crash_config(workspace: Path, api_base: str) -> Any:
    """Build a supervised Config with faster lease/scan timers for crash recovery.

    The crash-recovery scenario needs the lease to expire quickly so the
    killed Worker's task is reclaimed by another Worker within the
    verifier's bounded wait. These timers mirror the
    ``test_task_recovers_after_worker_kill`` test configuration.
    """
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
                "heartbeatIntervalS": 1,
                "leaseTimeoutS": 5,
                "progressTimeoutS": 30,
                "queuePollMaxMs": 200,
                "leaseScanIntervalS": 1,
                "outboxLeaseTimeoutS": 30,
                "channelSendTimeoutS": 10,
                "sqliteBusyTimeoutMs": 1000,
                "shutdownGraceS": 3,
            },
        }
    )


def _write_evidence(output_path: Path, evidence: dict[str, Any]) -> None:
    """Write the JSON evidence to *output_path* (atomic-ish overwrite)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(output_path)


def _prove_topology(resources: Any, evidence: dict[str, Any]) -> bool:
    """Assert the runtime has exactly one Control Plane + three ready Workers.

    Populates topology fields in *evidence* and returns True when the
    topology is correct. On failure, prints a diagnostic and returns
    False.
    """
    snapshot = resources.host.snapshot()
    children = snapshot["children"]
    control = [c for c in children if c["role"] == "control"]
    workers = [c for c in children if c["role"] == "worker"]

    evidence["control_count"] = len(control)
    evidence["worker_count"] = len(workers)
    evidence["worker_ids"] = [c["id"] for c in workers]
    worker_ids_set = set(evidence["worker_ids"])
    evidence["worker_ids_set"] = sorted(worker_ids_set)
    evidence["instance_ids"] = [c["instance_id"] for c in children]
    evidence["instance_ids_distinct"] = len(set(evidence["instance_ids"])) == len(
        evidence["instance_ids"]
    )
    evidence["ready_workers"] = resources.host.ready_workers()

    # Resolve each Worker's live pid through child_record().
    worker_pids: dict[str, int | None] = {}
    supervisor = resources.host.supervisor
    for wid in EXPECTED_WORKER_IDS:
        record = supervisor.child_record(wid)
        if record is None or record.process is None:
            worker_pids[wid] = None
        else:
            worker_pids[wid] = record.process.pid
    evidence["worker_pids"] = worker_pids

    topology_ok = (
        evidence["control_count"] == 1
        and evidence["worker_count"] == 3
        and worker_ids_set == set(EXPECTED_WORKER_IDS)
        and all(c["ready"] and c["alive"] for c in children)
        and evidence["ready_workers"] == 3
    )
    if not topology_ok:
        print(
            f"FAIL: topology check failed: control_count={evidence['control_count']} "
            f"worker_count={evidence['worker_count']} "
            f"worker_ids={evidence['worker_ids']} "
            f"ready_workers={evidence['ready_workers']}",
            file=sys.stderr,
        )
        return False
    if not evidence["instance_ids_distinct"]:
        print(
            f"FAIL: duplicate instance ids: {evidence['instance_ids']}",
            file=sys.stderr,
        )
        return False
    if any(pid is None or pid <= 0 for pid in worker_pids.values()):
        print(
            f"FAIL: missing/invalid worker pids: {worker_pids}",
            file=sys.stderr,
        )
        return False
    return True


async def _run_basic_proof(
    resources: Any,
    config: Any,
    workspace: Path,
    stub: Any,
    test_conn: Any,
    evidence: dict[str, Any],
) -> bool:
    """Submit a proof task, wait for COMPLETED, and verify durable facts.

    Returns True when all postconditions hold.
    """
    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore
    from miniunicorn.runtime.task_service import TaskService
    from miniunicorn.session.manager import SessionManager

    test_store = SqliteRuntimeStore(test_conn)
    task_service = TaskService(test_store)
    scope = local_request_scope(config)

    request = RuntimeInboundRequest(
        content=TASK_CONTENT,
        media=(),
        metadata={},
        session_key=SESSION_KEY,
        channel="cli",
        channel_account="proof-user",
        channel_message_id=None,
        scope=scope,
        target_key="direct",
    )
    envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
    handle = await task_service.submit(envelope)
    evidence["task_id"] = handle.task_id

    task_snapshot = await task_service.wait_terminal(scope, handle.task_id, timeout_s=30)
    evidence["task_state"] = task_snapshot.state
    if evidence["task_state"] != "COMPLETED":
        print(
            f"FAIL: task did not complete: state={evidence['task_state']}",
            file=sys.stderr,
        )
        return False

    evidence["provider_requests"] = len(stub.requests)

    outbox_row = test_conn.execute(
        "SELECT COUNT(*) AS n, message_kind FROM outbox WHERE task_id=? GROUP BY message_kind",
        (handle.task_id,),
    ).fetchall()
    evidence["outbox_count"] = sum(r["n"] for r in outbox_row)
    evidence["outbox_kinds"] = sorted({r["message_kind"] for r in outbox_row})

    evidence["completed_model_attempts"] = test_conn.execute(
        "SELECT COUNT(*) AS n FROM model_attempts WHERE task_id=? AND state='COMPLETED'",
        (handle.task_id,),
    ).fetchone()["n"]

    session_snapshot = SessionManager(workspace).load_fresh(SESSION_KEY)
    session_roles = [m.get("role") for m in session_snapshot.messages]
    evidence["session_roles"] = session_roles
    evidence["session_has_user_then_assistant"] = (
        "user" in session_roles
        and "assistant" in session_roles
        and session_roles.index("user") < session_roles.index("assistant")
    )

    if evidence["provider_requests"] != 1:
        print(
            f"FAIL: expected exactly 1 provider request, got {evidence['provider_requests']}",
            file=sys.stderr,
        )
        return False
    if evidence["outbox_count"] != 1 or evidence["outbox_kinds"] != ["FINAL_REPLY"]:
        print(
            f"FAIL: expected exactly 1 FINAL_REPLY outbox row, "
            f"got count={evidence['outbox_count']} kinds={evidence['outbox_kinds']}",
            file=sys.stderr,
        )
        return False
    if evidence["completed_model_attempts"] != 1:
        print(
            f"FAIL: expected exactly 1 COMPLETED model attempt, "
            f"got {evidence['completed_model_attempts']}",
            file=sys.stderr,
        )
        return False
    if not evidence["session_has_user_then_assistant"]:
        print(
            f"FAIL: durable session missing user-then-assistant: roles={session_roles}",
            file=sys.stderr,
        )
        return False
    return True


async def _run_crash_proof(
    resources: Any,
    config: Any,
    workspace: Path,
    stub: Any,
    test_conn: Any,
    evidence: dict[str, Any],
) -> bool:
    """Submit a task, kill its lease-owning Worker, and verify recovery.

    Populates crash-evidence fields in *evidence* and returns True when
    every recovery postcondition holds.
    """
    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore
    from miniunicorn.runtime.task_service import TaskService
    from tests.runtime.support.supervised import terminate_worker, wait_for_task_owner

    test_store = SqliteRuntimeStore(test_conn)
    task_service = TaskService(test_store)
    scope = local_request_scope(config)

    request = RuntimeInboundRequest(
        content=TASK_CONTENT,
        media=(),
        metadata={},
        session_key=SESSION_KEY,
        channel="cli",
        channel_account="proof-user",
        channel_message_id=None,
        scope=scope,
        target_key="direct",
    )
    envelope = build_inbound_envelope(request, now_ms=int(time.time() * 1000))
    handle = await task_service.submit(envelope)
    evidence["task_id"] = handle.task_id

    # Resolve the real lease-owning Worker from durable state. The helper
    # fails (not skips) when the task never reaches RUNNING — that is the
    # correct signal that the crash scenario was not established.
    task, owner = await wait_for_task_owner(
        test_store,
        handle.task_id,
        timeout_s=15.0,
    )
    evidence["killed_worker_id"] = owner
    assert task.leased_by == owner, f"leased_by={task.leased_by!r} != owner={owner!r}"

    # Record the owner's live pid before termination.
    supervisor = resources.host.supervisor
    owner_record = supervisor.child_record(owner)
    assert owner_record is not None and owner_record.process is not None, (
        f"no live process for owner {owner}"
    )
    evidence["killed_worker_pid"] = owner_record.process.pid

    # Terminate the exact Worker that holds the lease.
    killed_pid = terminate_worker(supervisor, owner)
    assert killed_pid > 0, f"terminate_worker returned non-positive pid {killed_pid}"

    # Wait for the task to complete (recovered by another Worker).
    task_snapshot = await task_service.wait_terminal(scope, handle.task_id, timeout_s=60)
    evidence["task_state"] = task_snapshot.state
    if evidence["task_state"] != "COMPLETED":
        print(
            f"FAIL: task did not recover after Worker kill: state={evidence['task_state']}",
            file=sys.stderr,
        )
        return False

    final_task = test_store.read_task(handle.task_id)
    assert final_task is not None, "final task record missing"
    evidence["recovery_attempts"] = final_task.root_attempt_count

    # Exactly one logical Provider decision: exactly one COMPLETED model
    # attempt for this task.
    evidence["logical_provider_decisions"] = test_conn.execute(
        "SELECT COUNT(*) AS n FROM model_attempts WHERE task_id=? AND state='COMPLETED'",
        (handle.task_id,),
    ).fetchone()["n"]

    # duplicate_model_started_events: MODEL_STARTED events beyond the
    # first. With attempt reuse (Task 5), recovery must NOT start a new
    # model attempt — it reuses the existing COMPLETED row. So there
    # should be exactly 1 MODEL_STARTED event and 0 duplicates.
    model_started_count = test_conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id=? AND event_type='MODEL_STARTED'",
        (handle.task_id,),
    ).fetchone()["n"]
    evidence["model_started_events"] = model_started_count
    evidence["duplicate_model_started_events"] = max(0, model_started_count - 1)

    # duplicate_final_replies: outbox rows beyond the first. Exactly one
    # FINAL_REPLY outbox row must exist (recovery must not duplicate the
    # final reply enqueue).
    outbox_count = test_conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE task_id=?",
        (handle.task_id,),
    ).fetchone()["n"]
    evidence["outbox_count"] = outbox_count
    evidence["duplicate_final_replies"] = max(0, outbox_count - 1)

    evidence["provider_requests"] = len(stub.requests)

    # Postcondition checks.
    if evidence["recovery_attempts"] < 2:
        print(
            f"FAIL: expected >=2 recovery attempts, got {evidence['recovery_attempts']}",
            file=sys.stderr,
        )
        return False
    if evidence["logical_provider_decisions"] != 1:
        print(
            f"FAIL: expected exactly 1 logical Provider decision, "
            f"got {evidence['logical_provider_decisions']}",
            file=sys.stderr,
        )
        return False
    if evidence["duplicate_model_started_events"] != 0:
        print(
            f"FAIL: expected 0 duplicate MODEL_STARTED events, "
            f"got {evidence['duplicate_model_started_events']}",
            file=sys.stderr,
        )
        return False
    if evidence["duplicate_final_replies"] != 0:
        print(
            f"FAIL: expected 0 duplicate final replies, got {evidence['duplicate_final_replies']}",
            file=sys.stderr,
        )
        return False
    return True


async def _run_verify(args: argparse.Namespace) -> int:
    """Start the supervised runtime, prove topology/crash-recovery, return exit code."""
    from miniunicorn.config.runtime import resolve_runtime_paths
    from miniunicorn.runtime.bootstrap import build_supervised_runtime
    from miniunicorn.runtime.sqlite import open_connection
    from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion

    kill_owner = bool(args.kill_owner)

    # Workspace: explicit path (default) or temp dir.
    if args.workspace:
        workspace_root = Path(args.workspace).resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        cleanup_workspace = False
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="three-worker-proof-"))
        cleanup_workspace = True
    workspace = workspace_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).resolve()

    # Crash mode uses a delayed stub so the task is in-flight (RUNNING
    # with a lease) when the Worker is killed. Two responses are queued
    # in case the first is interrupted by the kill.
    if kill_owner:
        stub = OpenAIStubServer(
            [chat_completion("recovered answer"), chat_completion("recovered answer")],
            delay_s=2.0,
        )
    else:
        stub = OpenAIStubServer([chat_completion("proof-complete")])
    stub.start()
    resources: Any = None
    test_conn: Any = None
    # Evidence is populated incrementally; fields are filled in as each
    # phase completes so the finally block can always write a partial
    # evidence file even when an early phase fails.
    evidence: dict[str, Any] = {
        "mode": "crash" if kill_owner else "topology",
        "control_count": 0,
        "worker_count": 0,
        "worker_ids": [],
        "worker_ids_set": [],
        "worker_pids": {},
        "instance_ids": [],
        "instance_ids_distinct": False,
        "ready_workers": 0,
        "task_id": None,
        "task_state": "UNKNOWN",
        "provider_requests": 0,
        "outbox_count": 0,
        "outbox_kinds": [],
        "completed_model_attempts": 0,
        "session_has_user_then_assistant": False,
        "session_roles": [],
        "children_alive_after_stop": -1,
        # Crash-mode fields (populated only when --kill-owner).
        "killed_worker_id": None,
        "killed_worker_pid": None,
        "recovery_attempts": 0,
        "logical_provider_decisions": 0,
        "model_started_events": 0,
        "duplicate_model_started_events": 0,
        "duplicate_final_replies": 0,
    }
    exit_code = 1  # pessimistic default; set to 0 only when all checks pass
    try:
        config = (
            _make_crash_config(workspace, stub.api_base)
            if kill_owner
            else _make_config(workspace, stub.api_base)
        )
        resources = build_supervised_runtime(config)
        await resources.start()

        # ---- 1. Topology proof (shared by both modes) ------------------
        if not _prove_topology(resources, evidence):
            return 1

        # ---- 2. Open a separate SQLite connection for task submission --
        resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
        db_path = resolved.database_path_resolved or Path(resolved.database_path)
        test_conn = open_connection(db_path)

        # ---- 3. Branch: crash recovery vs basic proof ------------------
        if kill_owner:
            ok = await _run_crash_proof(resources, config, workspace, stub, test_conn, evidence)
        else:
            ok = await _run_basic_proof(resources, config, workspace, stub, test_conn, evidence)
        if not ok:
            return 1

        # All durable-fact checks passed; the final exit code depends on
        # the post-stop liveness check in the finally block.
        exit_code = 0
        return 0
    finally:
        if test_conn is not None:
            test_conn.close()
        if resources is not None:
            try:
                await resources.stop()
            except Exception as exc:
                print(f"WARNING: error during resources.stop(): {exc}", file=sys.stderr)
        stub.close()

        # ---- 4. Verify no child survives after stop() ------------------
        children_alive_after_stop = -1
        if resources is not None:
            try:
                post_snapshot = resources.host.snapshot()
                alive = [c for c in post_snapshot["children"] if c.get("alive")]
                children_alive_after_stop = len(alive)
                if alive:
                    print(
                        f"FAIL: {len(alive)} child processes still alive after stop(): {alive}",
                        file=sys.stderr,
                    )
                    try:
                        resources.host.terminate()
                    except Exception:
                        pass
            except Exception as exc:
                print(
                    f"WARNING: could not check child liveness: {exc}",
                    file=sys.stderr,
                )
        evidence["children_alive_after_stop"] = children_alive_after_stop

        _write_evidence(output_path, evidence)
        print(json.dumps(evidence, indent=2, sort_keys=True), file=sys.stderr)

        if cleanup_workspace:
            shutil.rmtree(workspace_root, ignore_errors=True)

        # A return inside finally overrides any return from try. The exit
        # code is 0 only when every durable-fact check passed AND no child
        # survived after stop().
        if exit_code == 0 and children_alive_after_stop == 0:
            return 0
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prove the production three-worker runtime topology and crash recovery.",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default="",
        help="Workspace root directory (default: a fresh temp dir).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="three-worker-evidence.json",
        help="Path to write the JSON evidence (default: three-worker-evidence.json).",
    )
    parser.add_argument(
        "--kill-owner",
        action="store_true",
        help="Crash-recovery mode: kill the lease-owning Worker and verify recovery.",
    )
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_run_verify(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
