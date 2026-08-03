#!/usr/bin/env python3
"""Three-worker runtime topology verifier (Task 24 Step 4).

Starts a production supervised runtime (one Control Plane + exactly three
Workers) against the process-safe local OpenAI stub, resolves each Worker
id through ``resources.host.supervisor.child_record(worker_id)`` and
records its live ``process.pid``, submits a ``"three-worker-proof"``
task, waits for ``COMPLETED``, queries durable facts (session content,
Outbox FINAL_REPLY, COMPLETED model attempts, Provider request count)
through a separate SQLite connection, stops the runtime in ``finally``,
verifies no child process survives, and writes ``three-worker-evidence.json``.

Task 25 extends this script with a ``--kill-owner`` crash-recovery mode;
this file implements only the topology-proof mode required by Task 24.

Usage::

    uv run python scripts/verify_three_worker_runtime.py \
        --workspace .three-worker-evidence-workspace \
        --output three-worker-evidence.json

Expected exit 0 with ``control_count=1``, ``worker_count=3``, three
distinct Worker ids, ``task_state="COMPLETED"``, ``provider_requests=1``,
and ``children_alive_after_stop=0``.
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


def _write_evidence(output_path: Path, evidence: dict[str, Any]) -> None:
    """Write the JSON evidence to *output_path* (atomic-ish overwrite)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(output_path)


async def _run_verify(args: argparse.Namespace) -> int:
    """Start the supervised runtime, prove topology, return exit code."""
    from miniunicorn.config.runtime import resolve_runtime_paths
    from miniunicorn.runtime.application import RuntimeInboundRequest
    from miniunicorn.runtime.bootstrap import build_supervised_runtime
    from miniunicorn.runtime.ingress import build_inbound_envelope, local_request_scope
    from miniunicorn.runtime.sqlite import SqliteRuntimeStore, open_connection
    from miniunicorn.runtime.task_service import TaskService
    from miniunicorn.session.manager import SessionManager
    from tests.runtime.support.openai_stub import OpenAIStubServer, chat_completion

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

    stub = OpenAIStubServer([chat_completion("proof-complete")])
    stub.start()
    resources: Any = None
    test_conn: Any = None
    # Evidence is populated incrementally; fields are filled in as each
    # phase completes so the finally block can always write a partial
    # evidence file even when an early phase fails.
    evidence: dict[str, Any] = {
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
    }
    exit_code = 1  # pessimistic default; set to 0 only when all checks pass
    try:
        config = _make_config(workspace, stub.api_base)
        resources = build_supervised_runtime(config)
        await resources.start()

        # ---- 1. Topology proof ------------------------------------------
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
            return 1
        if not evidence["instance_ids_distinct"]:
            print(
                f"FAIL: duplicate instance ids: {evidence['instance_ids']}",
                file=sys.stderr,
            )
            return 1
        if any(pid is None or pid <= 0 for pid in worker_pids.values()):
            print(
                f"FAIL: missing/invalid worker pids: {worker_pids}",
                file=sys.stderr,
            )
            return 1

        # ---- 2. Submit the proof task ----------------------------------
        resolved = resolve_runtime_paths(config.runtime, config.workspace_path)
        db_path = resolved.database_path_resolved or Path(resolved.database_path)
        test_conn = open_connection(db_path)
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

        # ---- 3. Wait for COMPLETED -------------------------------------
        task_snapshot = await task_service.wait_terminal(scope, handle.task_id, timeout_s=30)
        evidence["task_state"] = task_snapshot.state
        if evidence["task_state"] != "COMPLETED":
            print(
                f"FAIL: task did not complete: state={evidence['task_state']}",
                file=sys.stderr,
            )
            return 1

        # ---- 4. Query durable facts through a separate SQLite conn -----
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
            return 1
        if evidence["outbox_count"] != 1 or evidence["outbox_kinds"] != ["FINAL_REPLY"]:
            print(
                f"FAIL: expected exactly 1 FINAL_REPLY outbox row, "
                f"got count={evidence['outbox_count']} "
                f"kinds={evidence['outbox_kinds']}",
                file=sys.stderr,
            )
            return 1
        if evidence["completed_model_attempts"] != 1:
            print(
                f"FAIL: expected exactly 1 COMPLETED model attempt, "
                f"got {evidence['completed_model_attempts']}",
                file=sys.stderr,
            )
            return 1
        if not evidence["session_has_user_then_assistant"]:
            print(
                f"FAIL: durable session missing user-then-assistant: roles={session_roles}",
                file=sys.stderr,
            )
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

        # ---- 5. Verify no child survives after stop() ------------------
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
        description="Prove the production three-worker runtime topology.",
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
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_run_verify(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
