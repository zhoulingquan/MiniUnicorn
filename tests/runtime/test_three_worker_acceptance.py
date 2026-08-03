"""Three-Worker acceptance test for supervised mode (design Task 8).

Verifies that ``build_supervised_runtime`` starts the Control Plane plus
exactly three Workers using the PRODUCTION child entrypoints (not stubs).
Readiness must not call an external model — the test only checks process
orchestration, not Agent turn execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from miniunicorn.runtime.bootstrap import build_supervised_runtime


@pytest.fixture
def runtime_root_config(tmp_path: Path) -> Any:
    """A root Config with a temporary workspace and supervised runtime."""
    from miniunicorn.config.schema import Config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "provider": "custom",
                    "model": "fake-model",
                    "contextWindowTokens": 8192,
                },
            },
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "http://localhost:1/v1",
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
    return config


@pytest.mark.slow
@pytest.mark.asyncio
async def test_supervised_default_starts_three_workers(
    runtime_root_config: Any,
) -> None:
    """Production entrypoints start exactly one Control Plane + 3 ready Workers.

    Task 24 Step 1: assert the snapshot contains exactly four children
    (one ``role == "control"`` and three ``role == "worker"``); all are
    ready/alive; Worker ids equal ``{"worker-0", "worker-1", "worker-2"}``;
    instance ids are distinct; and ``ready_workers() == 3``.
    """
    resources = build_supervised_runtime(runtime_root_config)
    await resources.start()
    try:
        snapshot = resources.host.snapshot()
        children = snapshot["children"]
        # Exactly four children total.
        assert len(children) == 4, f"expected 4 children, got {len(children)}"

        control = [c for c in children if c["role"] == "control"]
        workers = [c for c in children if c["role"] == "worker"]
        # Exactly one Control Plane and three Workers.
        assert len(control) == 1, f"expected 1 control child, got {len(control)}"
        assert len(workers) == 3, f"expected 3 worker children, got {len(workers)}"

        # All children must be ready and alive.
        assert all(c["ready"] for c in children), f"not all children ready: {children}"
        assert all(c["alive"] for c in children), f"not all children alive: {children}"

        # Worker ids must be exactly the expected set.
        worker_ids = {c["id"] for c in workers}
        assert worker_ids == {"worker-0", "worker-1", "worker-2"}, (
            f"unexpected worker ids: {worker_ids}"
        )

        # Instance ids (per-spawn) must be distinct across all children.
        instance_ids = [c["instance_id"] for c in children]
        assert len(set(instance_ids)) == len(instance_ids), (
            f"duplicate instance ids: {instance_ids}"
        )

        # Each Worker record must resolve through child_record() and have
        # a live process with a positive pid (real spawned process, not a
        # stub).
        supervisor = resources.host.supervisor
        for wid in ("worker-0", "worker-1", "worker-2"):
            record = supervisor.child_record(wid)
            assert record is not None, f"child_record({wid!r}) returned None"
            assert record.role == "worker", f"{wid} role={record.role!r}, expected 'worker'"
            assert record.process is not None, f"{wid} has no process"
            assert record.process.is_alive(), f"{wid} process not alive"
            assert record.process.pid is not None and record.process.pid > 0, f"{wid} has no pid"

        # ready_workers() must report exactly 3.
        assert resources.host.ready_workers() == 3, (
            f"ready_workers()={resources.host.ready_workers()}, expected 3"
        )
    finally:
        await resources.stop()
