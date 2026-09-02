"""Gateway runtime (extracted from cli/commands.py).

Owns the ``_run_gateway`` shared gateway runtime plus the cron-job
handling that used to live as nested closures inside it:

- ``on_cron_job`` — top-level dispatcher for a single ``CronJob``.
- ``_pick_heartbeat_target`` — picks a routable (channel, chat_id) for
  heartbeat-driven messages.
- ``_handle_dream_job`` — runs the dream consolidation directly.
- ``_handle_heartbeat_job`` — heartbeat branch: reads HEARTBEAT.md,
  optionally swaps in a heartbeat-specific provider, runs the agent,
  evaluates the response, and delivers it.
- ``_handle_reminder_job`` — reminder branch: runs the agent with cron /
  message tool flags flipped, then optionally delivers.

The extracted helpers receive as parameters the values they previously
captured as closures (``agent``, ``config``, ``hb_cfg``,
``message_tool``, ``deliver_to_channel``, ``pick_heartbeat_target``).

A handful of names that tests patch on the ``miniunicorn.cli.commands``
module namespace (``commands.evaluate_response``,
``commands.sync_workspace_templates``, ``commands._migrate_cron_store``,
``commands.AgentLoop``) are looked up through ``commands.<name>`` at
call time (late binding) so those patches continue to take effect
without changing the tests.
"""

from typing import Any

from loguru import logger

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.cli._heartbeat import (
    _HEARTBEAT_LIGHT_PREAMBLE,
    _HEARTBEAT_PREAMBLE,
    _build_heartbeat_provider,
    _heartbeat_template,
    _is_within_active_hours,
)
from miniunicorn.config.schema import Config
from miniunicorn.cron.types import CronJob
from miniunicorn.memory import count_pending_dream_entries

# ---------------------------------------------------------------------------
# Module-level helpers extracted from the body of _run_gateway
# ---------------------------------------------------------------------------


def _dream_backlog_total(stores) -> int:
    """Return the combined cursor-visible Dream backlog for all stores."""
    return sum(count_pending_dream_entries(store) for store in stores)


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
        await agent.run_all_dreams()
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
    """Heartbeat branch: check HEARTBEAT.md, run the agent, evaluate, deliver.

    Was the ``if job.name == "heartbeat":`` branch of ``on_cron_job``.
    Closure dependencies (``config``, ``hb_cfg``, ``agent``,
    ``_pick_heartbeat_target``, ``_deliver_to_channel``,
    ``_build_heartbeat_provider``, ``_heartbeat_template``,
    ``_HEARTBEAT_PREAMBLE``, ``evaluate_response``) are now either passed
    explicitly or imported at module load.  ``evaluate_response`` is
    resolved through ``commands.evaluate_response`` so that test patches
    on that path continue to work.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from miniunicorn.cli import commands

    # activeHours 检查:不在活跃时段内则跳过(借鉴 OpenClaw activeHours)
    tz_name = config.agents.defaults.timezone or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    if not _is_within_active_hours(hb_cfg.active_hours, tz):
        logger.debug("Heartbeat: outside active_hours, skipping")
        return None

    heartbeat_file = config.workspace_path / "HEARTBEAT.md"
    try:
        content = heartbeat_file.read_text(encoding="utf-8")
    except OSError:
        content = ""
    is_template = bool(content) and content == _heartbeat_template()
    has_tasks = bool(content) and not is_template

    channel, chat_id = pick_heartbeat_target()
    if channel == "cli":
        return None

    # 构造 prompt:有任务时用完整 preamble + HEARTBEAT.md;无任务时用轻量巡检 preamble。
    # 这修复了"空 HEARTBEAT.md 直接短路"的问题——即使没有用户任务,
    # agent 仍可做一次轻量巡检(检查 cron/dream 产物等)。
    if has_tasks:
        prompt = (
            _HEARTBEAT_PREAMBLE
            + f"Review the following HEARTBEAT.md and report any active tasks:\n\n{content}"
        )
    else:
        prompt = _HEARTBEAT_LIGHT_PREAMBLE

    # isolatedSession:每次心跳用独立 session_key,不累积历史
    if hb_cfg.isolated_session:
        session_key = f"heartbeat_{int(datetime.now().timestamp())}"
    else:
        session_key = "heartbeat"

    # 若配置了 heartbeat 专用 model_preset,临时切换 agent 的 provider/model,
    # 调用结束后在 finally 中恢复,避免影响主对话。
    hb_override = _build_heartbeat_provider(hb_cfg, config)
    orig_provider = agent.provider
    orig_model = agent.model
    orig_runner_provider = agent.runner.provider
    orig_generation = getattr(agent.runner.provider, "generation", None)
    if hb_override is not None:
        hb_provider, hb_model = hb_override
        # 继承主 provider 的 generation 设置(temperature/max_tokens 等)
        if orig_generation is not None:
            hb_provider.generation = orig_generation
        agent.provider = hb_provider
        agent.model = hb_model
        agent.runner.provider = hb_provider
    # lightContext:跳过 bootstrap 文件注入,省 token(由 build_messages 读取)
    orig_light_context = getattr(agent, "_light_context", False)
    agent._light_context = hb_cfg.light_context
    try:

        async def _silent(*_args, **_kwargs):
            pass

        resp = await agent.process_direct(
            prompt,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )
    finally:
        agent.provider = orig_provider
        agent.model = orig_model
        agent.runner.provider = orig_runner_provider
        agent._light_context = orig_light_context
    response = resp.content if resp else ""

    # Keep a small tail of heartbeat history so the loop stays bounded.
    # isolatedSession 模式下不裁剪固定会话(每次都是新会话,无历史累积)。
    if not hb_cfg.isolated_session:
        session = agent.sessions.get_or_create("heartbeat")
        agent.sessions.writes.trim_to_recent(session, hb_cfg.keep_recent_messages)

    if not response:
        return None

    # evaluate_response 也走 heartbeat 专用 provider(若已配置)
    if hb_override is not None:
        eval_provider = hb_override[0]
        eval_model = hb_override[1]
    else:
        eval_provider = agent.provider
        eval_model = agent.model
    should_notify = await commands.evaluate_response(
        response,
        prompt,
        eval_provider,
        eval_model,
    )
    if should_notify:
        logger.info("Heartbeat: completed, delivering response")
        await deliver_to_channel(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response),
            record=True,
        )
    else:
        logger.info("Heartbeat: silenced by post-run evaluation")
    return response


async def _handle_reminder_job(
    job: CronJob,
    *,
    agent,
    message_tool,
    deliver_to_channel,
) -> str | None:
    """Reminder branch: deliver a scheduled reminder through the agent loop.

    Was the trailing fall-through branch of ``on_cron_job``.  Closure
    dependencies (``agent``, ``message_tool``, ``_deliver_to_channel``,
    ``evaluate_response``) are now either passed explicitly or resolved
    through ``commands.evaluate_response`` for patch compatibility.
    """
    from miniunicorn.cli import commands
    from miniunicorn.tools.cron import CronTool
    from miniunicorn.tools.message import MessageTool

    async def _silent(*_args, **_kwargs):
        pass

    reminder_note = (
        "The scheduled time has arrived. Deliver this reminder to the user now, "
        "as a brief and natural message in their language. Speak directly to them — "
        "do not narrate progress, summarize, include user IDs, or add status reports "
        "like 'Done' or 'Reminded'.\n\n"
        f"Reminder: {job.payload.message}"
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)

    message_record_token = None
    if isinstance(message_tool, MessageTool):
        message_record_token = message_tool.set_record_channel_delivery(True)

    try:
        resp = await agent.process_direct(
            reminder_note,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            on_progress=_silent,
        )
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)
        if isinstance(message_tool, MessageTool) and message_record_token is not None:
            message_tool.reset_record_channel_delivery(message_record_token)

    response = resp.content if resp else ""

    if job.payload.deliver and isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
        return response

    if job.payload.deliver and job.payload.to and response:
        should_notify = await commands.evaluate_response(
            response,
            reminder_note,
            agent.provider,
            agent.model,
        )
        if should_notify:
            await deliver_to_channel(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                    metadata=dict(job.payload.channel_meta),
                ),
                record=True,
                session_key=job.payload.session_key,
            )
    return response


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
    config: Config,
    *,
    open_browser_url: str | None = None,
    webui_static_dist: bool = True,
    webui_runtime_surface: str = "browser",
    webui_runtime_capabilities: dict[str, Any] | None = None,
) -> None:
    """Shared gateway runtime; ``open_browser_url`` opens a tab once channels are up.

    Thin caller: all assembly lives in ``GatewayApplication`` (the static
    composition root).  The full object wiring, ordering, CLI output and the
    reverse-order shutdown sequence are preserved there verbatim.
    """
    from miniunicorn.composition.gateway import GatewayApplication

    app = GatewayApplication(
        config,
        open_browser_url=open_browser_url,
        webui_static_dist=webui_static_dist,
        webui_runtime_surface=webui_runtime_surface,
        webui_runtime_capabilities=webui_runtime_capabilities,
    )
    # start() blocks until shutdown; the gateway's layered cleanup runs as
    # part of start() (see GatewayApplication.start / stop).
    app.start()
