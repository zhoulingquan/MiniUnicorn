"""Gateway runtime (extracted from cli/commands.py).

Owns the ``_run_gateway`` thin launcher plus the cron-job handling
helpers that will be wired into the Control Plane in Task 10:

- ``on_cron_job`` — top-level dispatcher for a single ``CronJob``.
- ``_pick_heartbeat_target`` — picks a routable (channel, chat_id) for
  heartbeat-driven messages.
- ``_handle_dream_job`` — runs the dream consolidation directly.
- ``_handle_heartbeat_job`` — heartbeat branch (deferred to Task 10).
- ``_handle_reminder_job`` — reminder branch (deferred to Task 10).

``_run_gateway`` is a thin launcher: it delegates all Agent/Session/
Cron/Channel construction to ``build_supervised_runtime`` /
``build_lightweight_runtime``, then starts resources, optionally opens
a browser, and waits for shutdown.
"""

import asyncio
from contextlib import suppress
from typing import Any

from loguru import logger

from miniunicorn import __logo__, __version__
from miniunicorn.bus.events import OutboundMessage
from miniunicorn.cli._heartbeat import (
    _HEARTBEAT_LIGHT_PREAMBLE,
    _HEARTBEAT_PREAMBLE,
    _build_heartbeat_provider,
    _heartbeat_template,
    _is_within_active_hours,
)
from miniunicorn.cli._terminal_render import console
from miniunicorn.config.paths import is_default_workspace
from miniunicorn.config.schema import Config
from miniunicorn.cron.types import CronJob, CronPayload, CronSchedule

# ---------------------------------------------------------------------------
# Module-level helpers extracted from the body of _run_gateway
# ---------------------------------------------------------------------------


def _pick_heartbeat_target(channels, session_manager) -> tuple[str, str]:
    """Pick a routable channel/chat target for heartbeat-triggered messages.

    Was a nested closure inside ``_run_gateway``; now parameterised on
    ``channels`` and ``session_manager`` (its only closure dependencies).
    """
    enabled = set(channels.enabled_channels)
    for item in session_manager.list_sessions():
        key = item.get("key") or ""
        if ":" not in key:
            continue
        channel, chat_id = key.split(":", 1)
        if channel in {"cli", "system"}:
            continue
        if channel in enabled and chat_id:
            return channel, chat_id
    return "cli", "direct"


async def _handle_dream_job(job: CronJob, agent) -> None:
    """Dream is an internal job — run directly, not through the agent loop.

    Was the ``if job.name == "dream":`` branch of ``on_cron_job``.
    """
    try:
        await agent.dream.run()
        logger.info("Dream cron job completed")
    except Exception:
        logger.exception("Dream cron job failed")


async def _handle_heartbeat_job(
    job: CronJob,
    *,
    agent,
    config: Config,
    hb_cfg,
    pick_heartbeat_target,
    deliver_to_channel,
) -> str | None:
    """Heartbeat branch (deferred to Task 10).

    Will be wired to submit internal tasks through the Control Plane's
    ``TaskService.submit_internal`` once the cron→runtime bridge lands.
    """
    logger.warning(
        "Heartbeat cron job handler not yet wired to Control Plane (Task 10); skipping job {}",
        job.id,
    )
    return None


async def _handle_reminder_job(
    job: CronJob,
    *,
    agent,
    message_tool,
    deliver_to_channel,
) -> str | None:
    """Reminder branch (deferred to Task 10).

    Will be wired to submit internal tasks through the Control Plane's
    ``TaskService.submit_internal`` once the cron→runtime bridge lands.
    """
    logger.warning(
        "Reminder cron job handler not yet wired to Control Plane (Task 10); skipping job {}",
        job.id,
    )
    return None


async def on_cron_job(
    job: CronJob,
    *,
    agent,
    config: Config,
    hb_cfg,
    message_tool,
    deliver_to_channel,
    pick_heartbeat_target,
) -> str | None:
    """Execute a cron job through the agent.

    Extracted from a nested closure inside ``_run_gateway``.  The closure
    dependencies are passed explicitly so that tests can:

    - swap ``agent.provider`` / ``agent.model`` after gateway setup and
      have ``on_cron_job`` read the new values at call time (``agent`` is
      captured by reference, not by value);
    - patch ``commands.evaluate_response`` and have the heartbeat /
      reminder branches pick up the patched function via late binding.
    """
    # Dream is an internal job — run directly, not through the agent loop.
    if job.name == "dream":
        await _handle_dream_job(job, agent)
        return None

    # Heartbeat is a system job that checks HEARTBEAT.md for active tasks.
    if job.name == "heartbeat":
        return await _handle_heartbeat_job(
            job,
            agent=agent,
            config=config,
            hb_cfg=hb_cfg,
            pick_heartbeat_target=pick_heartbeat_target,
            deliver_to_channel=deliver_to_channel,
        )

    return await _handle_reminder_job(
        job,
        agent=agent,
        message_tool=message_tool,
        deliver_to_channel=deliver_to_channel,
    )


# ---------------------------------------------------------------------------
# _run_gateway itself
# ---------------------------------------------------------------------------


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
    import asyncio
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
