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
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)

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
        response, prompt, eval_provider, eval_model,
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
    from miniunicorn.agent.tools.cron import CronTool
    from miniunicorn.agent.tools.message import MessageTool
    from miniunicorn.cli import commands

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
            response, reminder_note, agent.provider, agent.model,
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
    """Shared gateway runtime; ``open_browser_url`` opens a tab once channels are up."""
    # ``commands`` is used for late-binding lookups of names that tests patch
    # on the commands module (``sync_workspace_templates``,
    # ``_migrate_cron_store``, ``AgentLoop``).  The other imports below are
    # patched by tests on their own modules, so they can be resolved directly.
    from miniunicorn.agent.tools.message import MessageTool
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.channels.manager import ChannelManager
    from miniunicorn.channels.websocket import publish_runtime_model_update
    from miniunicorn.cli import commands
    from miniunicorn.cron.service import CronService
    from miniunicorn.providers.factory import build_provider_snapshot, load_provider_snapshot
    from miniunicorn.session.manager import SessionManager

    ws_cfg = getattr(config.channels, "websocket", None)
    if isinstance(ws_cfg, dict):
        ws_port = ws_cfg.get("port", 8765)
    elif ws_cfg is not None:
        ws_port = ws_cfg.port
    else:
        ws_port = 8765

    console.print(f"{__logo__} Starting MiniUnicorn gateway version {__version__} on port {ws_port}...")
    commands.sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    try:
        provider_snapshot = build_provider_snapshot(config)
    except ValueError as exc:
        console.print(f"[yellow]Warning: {exc}[/yellow]")
        console.print("[dim]Chat will not work until an API key is configured in Settings → BYOK.[/dim]")
        provider_snapshot = None
    session_manager = SessionManager(config.workspace_path)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        commands._migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = commands.AgentLoop.from_config(
        config, bus,
        provider=provider_snapshot.provider if provider_snapshot else None,
        model=provider_snapshot.model if provider_snapshot else None,
        context_window_tokens=provider_snapshot.context_window_tokens if provider_snapshot else None,
        cron_service=cron,
        session_manager=session_manager,
        provider_snapshot_loader=load_provider_snapshot,
        runtime_model_publisher=lambda model, preset: publish_runtime_model_update(
            bus,
            model,
            preset,
        ),
        provider_signature=provider_snapshot.signature if provider_snapshot else None,
    )

    from miniunicorn.agent.loop import UNIFIED_SESSION_KEY
    from miniunicorn.bus.events import OutboundMessage

    def _channel_session_key(channel: str, chat_id: str) -> str:
        return (
            UNIFIED_SESSION_KEY
            if config.agents.defaults.unified_session
            else f"{channel}:{chat_id}"
        )

    async def _deliver_to_channel(
        msg: OutboundMessage, *, record: bool = False, session_key: str | None = None,
    ) -> None:
        """Publish a user-visible message and mirror it into that channel's session."""
        metadata = dict(msg.metadata or {})
        record = record or bool(metadata.pop("_record_channel_delivery", False))
        if metadata != (msg.metadata or {}):
            msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                reply_to=msg.reply_to,
                media=msg.media,
                metadata=metadata,
                buttons=msg.buttons,
            )
        if (
            record
            and msg.channel != "cli"
            and msg.content.strip()
            and hasattr(session_manager, "get_or_create")
            and hasattr(session_manager, "save")
        ):
            key = session_key or _channel_session_key(msg.channel, msg.chat_id)
            session = session_manager.get_or_create(key)
            extra: dict[str, Any] = {"_channel_delivery": True}
            if msg.media:
                extra["media"] = list(msg.media)
            session.add_message("assistant", msg.content, **extra)
            session_manager.save(session)
        await bus.publish_outbound(msg)

    message_tool = getattr(agent, "tools", {}).get("message")
    if isinstance(message_tool, MessageTool):
        message_tool.set_send_callback(_deliver_to_channel)

    # Set cron callback (needs agent).  The extracted on_cron_job receives
    # all closure dependencies as explicit parameters; tests can still
    # mutate agent.provider / agent.model after gateway setup and have
    # on_cron_job observe the new values because `agent` is captured by
    # reference (not by value) inside this lambda.
    def _on_cron_job_wrapper(job: CronJob) -> "Any":
        return on_cron_job(
            job,
            agent=agent,
            config=config,
            hb_cfg=hb_cfg,
            message_tool=message_tool,
            deliver_to_channel=_deliver_to_channel,
            pick_heartbeat_target=_pick_heartbeat_target_local,
        )

    # Define the heartbeat-target picker as a thin closure that delegates
    # to the module-level function with the channels / session_manager it
    # captured from _run_gateway's scope.
    def _pick_heartbeat_target_local() -> tuple[str, str]:
        return _pick_heartbeat_target(channels, session_manager)

    cron.on_job = _on_cron_job_wrapper

    # `hb_cfg` is referenced by _on_cron_job_wrapper above; define it before
    # the cron loop actually fires (it is only read at call time, so order
    # of definition vs. the wrapper is fine, but keep it close for clarity).
    hb_cfg = config.gateway.heartbeat

    def _webui_runtime_model_name() -> str | None:
        model = getattr(agent, "model", None)
        if isinstance(model, str):
            stripped = model.strip()
            return stripped or None
        return None

    def _webui_provider_loader():
        # Returns the current LLMProvider (or None if not configured) so that
        # HTTP routes like /api/agents/generate can call the LLM directly.
        return getattr(agent, "provider", None)

    def _reload_cron_system_jobs() -> None:
        """Re-register heartbeat and dream system jobs after runtime config changes.

        Called by the WebSocket channel when heartbeat/dream intervals are updated
        from the WebUI, so the new interval takes effect without a gateway restart.
        """
        from miniunicorn.config.loader import load_config as _reload_config
        fresh = _reload_config()
        fresh_hb = fresh.gateway.heartbeat
        fresh_dream = fresh.agents.defaults.dream
        tz = fresh.agents.defaults.timezone
        if fresh_hb.enabled:
            cron.register_system_job(CronJob(
                id="heartbeat",
                name="heartbeat",
                schedule=CronSchedule(
                    kind="every",
                    every_ms=fresh_hb.interval_s * 1000,
                    tz=tz,
                ),
                payload=CronPayload(kind="system_event"),
            ))
        if fresh_dream.enabled:
            cron.register_system_job(CronJob(
                id="dream",
                name="dream",
                schedule=fresh_dream.build_schedule(tz),
                payload=CronPayload(kind="system_event"),
                catch_up_on_start=True,
            ))

    def _refresh_agent_runtime_model() -> None:
        """Refresh the running AgentLoop's model/provider from the latest config.

        Called by the WebSocket channel after model/provider settings are
        updated from the WebUI, so agent.model reflects the new selection
        immediately (bootstrap + runtime_model_updated carry the new value).
        """
        try:
            agent._refresh_provider_snapshot()
        except Exception:
            console.print("[yellow]Warning: failed to refresh agent runtime model[/yellow]")

    # Create channel manager (forwards SessionManager so the WebSocket channel
    # can serve the embedded webui's REST surface).
    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        webui_runtime_model_name=_webui_runtime_model_name,
        webui_static_dist=webui_static_dist,
        webui_runtime_surface=webui_runtime_surface,
        webui_runtime_capabilities=webui_runtime_capabilities,
        webui_provider_loader=_webui_provider_loader,
        webui_cron_reloader=_reload_cron_system_jobs,
        webui_agent_model_refresher=_refresh_agent_runtime_model,
        webui_cron_service=cron,
        webui_tool_registry=agent.tools,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    if hb_cfg.enabled:
        console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")
    else:
        console.print("[yellow]✗[/yellow] Heartbeat: disabled")

    # Register Dream system job (idempotent on restart)
    dream_cfg = config.agents.defaults.dream
    if dream_cfg.model_override:
        agent.dream.model = dream_cfg.model_override
    agent.dream.max_batch_size = dream_cfg.max_batch_size
    agent.dream.max_iterations = dream_cfg.max_iterations
    agent.dream.annotate_line_ages = dream_cfg.annotate_line_ages
    # 同步空闲触发器配置（方案B：会话间空闲自动触发 Dream）
    agent.dream_idle_trigger.update_config(
        enabled=dream_cfg.idle_trigger_enabled,
        min_idle_seconds=dream_cfg.idle_trigger_min_seconds,
        min_entries=dream_cfg.idle_trigger_min_entries,
        min_interval_s=dream_cfg.idle_trigger_min_interval_s,
    )
    # 标志位：启动时积压检查命中后，延迟到 run() 协程内调度 dream。
    # 这里不能直接 asyncio.create_task，因为此时还没有 running loop（asyncio.run 尚未执行）。
    need_dream_catchup = False
    if dream_cfg.enabled:
        cron.register_system_job(CronJob(
            id="dream",
            name="dream",
            schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
            payload=CronPayload(kind="system_event"),
            catch_up_on_start=True,
        ))
        # 方案C：启动时积压检查——若未处理历史超过阈值，立即后台触发一次 dream。
        # 解决"连续多天不开，catch_up 只补 1 次"的漏洞。
        if dream_cfg.startup_backlog_threshold > 0:
            try:
                cursor = agent.context.memory.get_last_dream_cursor()
                backlog = agent.context.memory.read_unprocessed_history(since_cursor=cursor)
            except Exception:
                backlog = []
            if len(backlog) >= dream_cfg.startup_backlog_threshold:
                console.print(
                    f"[yellow]![/yellow] Dream: {len(backlog)} backlog entries "
                    f"(threshold={dream_cfg.startup_backlog_threshold}), triggering immediate run"
                )
                need_dream_catchup = True
        console.print(f"[green]✓[/green] Dream: {dream_cfg.describe_schedule()}")
    else:
        console.print("[yellow]○[/yellow] Dream: disabled")

    # Register Heartbeat system job (idempotent on restart)
    if hb_cfg.enabled:
        cron.register_system_job(CronJob(
            id="heartbeat",
            name="heartbeat",
            schedule=CronSchedule(
                kind="every",
                every_ms=hb_cfg.interval_s * 1000,
                tz=config.agents.defaults.timezone,
            ),
            payload=CronPayload(kind="system_event"),
        ))

    async def _open_browser_when_ready() -> None:
        """Wait for the gateway to bind, then point the user's browser at the webui."""
        if not open_browser_url:
            return
        import webbrowser
        # Channels start asynchronously; a short poll lets us avoid racing the bind.
        for _ in range(40):  # ~4s max
            try:
                reader, writer = await asyncio.open_connection(
                    config.gateway.host or "127.0.0.1", ws_port
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        try:
            webbrowser.open(open_browser_url)
            console.print(f"[green]✓[/green] Opened browser at {open_browser_url}")
        except Exception as e:
            console.print(f"[yellow]Could not open browser ({e}); visit {open_browser_url}[/yellow]")

    async def run():
        try:
            await cron.start()
            # 启动时积压触发的 dream：此时已有 running loop，可安全调度后台任务。
            if need_dream_catchup:
                asyncio.create_task(agent.dream.run())
            tasks = [
                agent.run(),
                channels.start_all(),
            ]
            if open_browser_url:
                tasks.append(_open_browser_when_ready())
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            cron.stop()
            agent.stop()
            await channels.stop_all()
            # Flush all cached sessions to durable storage before exit.
            # This prevents data loss on filesystems with write-back
            # caching (rclone VFS, NFS, FUSE mounts, etc.).
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Shutdown: flushed {} session(s) to disk", flushed)

    asyncio.run(run())
