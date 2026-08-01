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
    """Production entrypoints start Control Plane + 3 ready Workers."""
    resources = build_supervised_runtime(runtime_root_config)
    await resources.start()
    try:
        snapshot = resources.host.snapshot()
        workers = [row for row in snapshot["children"] if row["role"] == "worker"]
        assert len(workers) == 3
        assert all(row["ready"] and row["alive"] for row in workers)
        assert resources.host.ready_workers() == 3
    finally:
        await resources.stop()
