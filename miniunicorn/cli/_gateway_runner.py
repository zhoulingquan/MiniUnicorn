"""Gateway runtime (extracted from cli/commands.py).

``_run_gateway`` is a thin launcher: it delegates all Agent/Session/
Cron/Channel construction to ``build_supervised_runtime`` /
``build_lightweight_runtime``, then starts resources, optionally opens
a browser, and waits for shutdown.

Task 9 Step 6 removed the obsolete direct Cron-job dispatch helpers
(``on_cron_job``, ``_handle_dream_job``, ``_handle_heartbeat_job``,
``_handle_reminder_job``, ``_pick_heartbeat_target``). Cron-triggered
maintenance now enqueues durable internal tasks through
``_wire_maintenance_callbacks`` (design §22.3) and they dispatch through
the :class:`MaintenanceExecutor` inside each Worker — no Gateway/Cron
production handler may invoke Dream directly.
"""

import asyncio
from typing import Any


def _run_gateway(
    config: Any,
    *,
    runtime_mode: str = "supervised",
    open_browser_url: Any = None,
    webui_static_dist: bool = True,
    webui_runtime_surface: str = "browser",
    webui_runtime_capabilities: dict[str, Any] | None = None,
) -> None:
    """Start the MiniUnicorn gateway as a thin launcher.

    All Agent/Session/Cron/Channel construction lives in the runtime
    bootstrap (build_supervised_runtime / build_lightweight_runtime).
    The gateway just starts resources, optionally opens a browser, and
    waits for shutdown.
    """
    from miniunicorn.runtime.bootstrap import (
        build_lightweight_runtime,
        build_supervised_runtime,
    )

    surface = {
        "webui_static_dist": webui_static_dist,
        "webui_runtime_surface": webui_runtime_surface,
        "webui_runtime_capabilities": webui_runtime_capabilities or {},
    }

    if runtime_mode == "supervised":
        resources = build_supervised_runtime(config, surface=surface)
    else:
        resources = build_lightweight_runtime(config, gateway=True, surface=surface)

    async def run() -> None:
        await resources.start()
        try:
            if open_browser_url is not None:
                try:
                    open_browser_url()
                except Exception:
                    pass
            await resources.wait_for_shutdown()
        finally:
            await resources.stop()

    asyncio.run(run())
