"""Gateway application composition root.

Static composition root for the gateway runtime (``cli gateway``,
``cli desktop-gateway``).  The ``GatewayApplication`` class owns the full
object-creation and wiring that used to live inline in
``cli/_gateway_runner.py::_run_gateway`` (lines 353-691 of the pre-split
module), preserving the exact creation order and the exact reverse-order
shutdown sequence of the original cleanup code.

All heavyweight collaborators are resolved at call time (inside
``__init__``) so that test patches on their own modules
(``miniunicorn.bus.queue.MessageBus``, ``miniunicorn.cron.service.CronService``,
``miniunicorn.session.manager.SessionManager``,
``miniunicorn.providers.factory.build_provider_snapshot``,
``miniunicorn.cli.commands.AgentLoop`` …) keep taking effect — the same
late-binding pattern the gateway runner already documents.  This also avoids
import cycles between the composition root and the CLI entry modules.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from loguru import logger

from miniunicorn import __logo__, __version__
from miniunicorn.cli._terminal_render import console
from miniunicorn.config.paths import is_default_workspace
from miniunicorn.config.schema import Config
from miniunicorn.cron.types import CronJob, CronPayload, CronSchedule


class GatewayApplication:
    """Assembled gateway application: bus, sessions, cron, agent, channels.

    ``__init__`` performs the full object wiring in the same order as the
    legacy ``_run_gateway`` (MessageBus → provider snapshot → SessionManager →
    CronService → AgentLoop → MessageTool send callback → cron.on_job →
    ChannelManager → Dream/heartbeat system jobs).  ``start()`` runs the
    gateway event loop and ``stop()`` shuts everything down in reverse order
    with the exact same layered-cleanup semantics as before.
    """

    def __init__(
        self,
        config: Config,
        *,
        open_browser_url: str | None = None,
        webui_static_dist: bool = True,
        webui_runtime_surface: str = "browser",
        webui_runtime_capabilities: dict[str, Any] | None = None,
    ) -> None:
        # Late imports: tests patch these names on their own modules, and
        # ``commands``/``_gateway_runner`` must not be imported at module load
        # (circular with the CLI entry modules).
        from miniunicorn.agent.tools.message import MessageTool
        from miniunicorn.bus.queue import MessageBus
        from miniunicorn.channels.manager import ChannelManager
        from miniunicorn.channels.websocket import publish_runtime_model_update
        from miniunicorn.cli import commands
        from miniunicorn.cli._gateway_runner import (
            _dream_backlog_total,
        )
        from miniunicorn.cron.service import CronService
        from miniunicorn.providers.factory import build_provider_snapshot, load_provider_snapshot
        from miniunicorn.session.manager import SessionManager

        self.config = config
        self._open_browser_url = open_browser_url

        ws_cfg = getattr(config.channels, "websocket", None)
        if isinstance(ws_cfg, dict):
            ws_port = ws_cfg.get("port", 8765)
        elif ws_cfg is not None:
            ws_port = ws_cfg.port
        else:
            ws_port = 8765
        self._ws_port = ws_port

        console.print(
            f"{__logo__} Starting MiniUnicorn gateway version {__version__} on port {ws_port}..."
        )
        commands.sync_workspace_templates(config.workspace_path)
        self.bus = MessageBus()
        try:
            provider_snapshot = build_provider_snapshot(config)
        except ValueError as exc:
            console.print(f"[yellow]Warning: {exc}[/yellow]")
            console.print(
                "[dim]Chat will not work until an API key is configured in Settings → BYOK.[/dim]"
            )
            provider_snapshot = None
        self.session_manager = SessionManager(config.workspace_path)

        # Preserve existing single-workspace installs, but keep custom workspaces clean.
        if is_default_workspace(config.workspace_path):
            commands._migrate_cron_store(config)

        # Create cron service with workspace-scoped store
        cron_store_path = config.workspace_path / "cron" / "jobs.json"
        self.cron = CronService(cron_store_path)

        # Create agent with cron service
        self.agent = commands.AgentLoop.from_config(
            config,
            self.bus,
            provider=provider_snapshot.provider if provider_snapshot else None,
            model=provider_snapshot.model if provider_snapshot else None,
            context_window_tokens=provider_snapshot.context_window_tokens
            if provider_snapshot
            else None,
            cron_service=self.cron,
            session_manager=self.session_manager,
            provider_snapshot_loader=load_provider_snapshot,
            runtime_model_publisher=lambda model, preset: publish_runtime_model_update(
                self.bus,
                model,
                preset,
            ),
            provider_signature=provider_snapshot.signature if provider_snapshot else None,
        )

        # hb_cfg is referenced by _on_cron_job; defined here and read at call
        # time, keeping the legacy definition point (before cron.on_job wiring).
        hb_cfg = config.gateway.heartbeat
        self._hb_cfg = hb_cfg

        message_tool = getattr(self.agent, "tools", {}).get("message")
        self._message_tool = message_tool
        if isinstance(message_tool, MessageTool):
            message_tool.set_send_callback(self._deliver_to_channel)

        # Set cron callback (needs agent). ``agent`` is captured on ``self``
        # so tests can still mutate agent.provider / agent.model after setup
        # and have _on_cron_job observe the new values at call time.
        self.cron.on_job = self._on_cron_job

        # Create channel manager (forwards SessionManager so the WebSocket
        # channel can serve the embedded webui's REST surface).
        self.channels = ChannelManager(
            config,
            self.bus,
            session_manager=self.session_manager,
            webui_runtime_model_name=self._webui_runtime_model_name,
            webui_static_dist=webui_static_dist,
            webui_runtime_surface=webui_runtime_surface,
            webui_runtime_capabilities=webui_runtime_capabilities,
            webui_provider_loader=self._webui_provider_loader,
            webui_cron_reloader=self._reload_cron_system_jobs,
            webui_agent_model_refresher=self._refresh_agent_runtime_model,
            webui_cron_service=self.cron,
            webui_tool_registry=self.agent.tools,
        )

        if self.channels.enabled_channels:
            console.print(
                f"[green]✓[/green] Channels enabled: "
                f"{', '.join(self.channels.enabled_channels)}"
            )
        else:
            console.print("[yellow]Warning: No channels enabled[/yellow]")

        cron_status = self.cron.status()
        if cron_status["jobs"] > 0:
            console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

        if hb_cfg.enabled:
            console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")
        else:
            console.print("[yellow]✗[/yellow] Heartbeat: disabled")

        # Register Dream system job (idempotent on restart)
        dream_cfg = config.agents.defaults.dream
        if dream_cfg.model_override:
            self.agent.dream.model = dream_cfg.model_override
        self.agent.dream.max_batch_size = dream_cfg.max_batch_size
        # 同步空闲触发器配置（方案B：会话间空闲自动触发 Dream）
        self.agent.dream_idle_trigger.update_config(
            enabled=dream_cfg.idle_trigger_enabled,
            min_idle_seconds=dream_cfg.idle_trigger_min_seconds,
            min_entries=dream_cfg.idle_trigger_min_entries,
            min_interval_s=dream_cfg.idle_trigger_min_interval_s,
        )
        # 标志位：启动时积压检查命中后，延迟到 run() 协程内调度 dream。
        # 这里不能直接 asyncio.create_task，因为此时还没有 running loop。
        self._need_dream_catchup = False
        if dream_cfg.enabled:
            self.cron.register_system_job(
                CronJob(
                    id="dream",
                    name="dream",
                    schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
                    payload=CronPayload(kind="system_event"),
                    catch_up_on_start=True,
                )
            )
            # 方案C：启动时积压检查——若未处理历史超过阈值，立即后台触发一次 dream。
            if dream_cfg.startup_backlog_threshold > 0:
                try:
                    stores = self.agent.context.memory_registry.known_stores()
                    total_backlog = _dream_backlog_total(stores)
                except Exception:
                    total_backlog = 0
                if total_backlog >= dream_cfg.startup_backlog_threshold:
                    console.print(
                        f"[yellow]![/yellow] Dream: {total_backlog} backlog entries "
                        f"(threshold={dream_cfg.startup_backlog_threshold}), "
                        "triggering immediate run"
                    )
                    self._need_dream_catchup = True
            console.print(f"[green]✓[/green] Dream: {dream_cfg.describe_schedule()}")
        else:
            console.print("[yellow]○[/yellow] Dream: disabled")

        # Register Heartbeat system job (idempotent on restart)
        if hb_cfg.enabled:
            self.cron.register_system_job(
                CronJob(
                    id="heartbeat",
                    name="heartbeat",
                    schedule=CronSchedule(
                        kind="every",
                        every_ms=hb_cfg.interval_s * 1000,
                        tz=config.agents.defaults.timezone,
                    ),
                    payload=CronPayload(kind="system_event"),
                )
            )

    # ------------------------------------------------------------------
    # Wiring callbacks (kept as methods; called through bound references
    # exactly where the legacy _run_gateway closures were invoked)
    # ------------------------------------------------------------------

    def _channel_session_key(self, channel: str, chat_id: str) -> str:
        from miniunicorn.agent.loop import UNIFIED_SESSION_KEY

        if self.config.agents.defaults.unified_session:
            return UNIFIED_SESSION_KEY
        return f"{channel}:{chat_id}"

    async def _deliver_to_channel(
        self,
        msg: Any,
        *,
        record: bool = False,
        session_key: str | None = None,
    ) -> None:
        """Publish a user-visible message and mirror it into that channel's session."""
        from miniunicorn.bus.events import OutboundMessage

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
            and hasattr(self.session_manager, "get_or_create")
            and hasattr(self.session_manager, "save")
        ):
            key = session_key or self._channel_session_key(msg.channel, msg.chat_id)
            session = self.session_manager.get_or_create(key)
            extra: dict[str, Any] = {"_channel_delivery": True}
            if msg.media:
                extra["media"] = list(msg.media)
            self.session_manager.writes.record_message(session, "assistant", msg.content, **extra)
        await self.bus.publish_outbound(msg)

    def _pick_heartbeat_target(self) -> tuple[str, str]:
        from miniunicorn.cli._gateway_runner import _pick_heartbeat_target as _pick_target

        return _pick_target(self.channels, self.session_manager)

    def _on_cron_job(self, job: CronJob) -> "Any":
        from miniunicorn.cli._gateway_runner import on_cron_job

        return on_cron_job(
            job,
            agent=self.agent,
            config=self.config,
            hb_cfg=self._hb_cfg,
            message_tool=self._message_tool,
            deliver_to_channel=self._deliver_to_channel,
            pick_heartbeat_target=self._pick_heartbeat_target,
        )

    def _webui_runtime_model_name(self) -> str | None:
        model = getattr(self.agent, "model", None)
        if isinstance(model, str):
            stripped = model.strip()
            return stripped or None
        return None

    def _webui_provider_loader(self):
        # Returns the current LLMProvider (or None if not configured) so that
        # HTTP routes like /api/agents/generate can call the LLM directly.
        return getattr(self.agent, "provider", None)

    def _reload_cron_system_jobs(self) -> None:
        """Re-register heartbeat and dream system jobs after runtime config changes.

        Called by the WebSocket channel when heartbeat/dream intervals are
        updated from the WebUI, so the new interval takes effect without a
        gateway restart.
        """
        from miniunicorn.config.loader import load_config as _reload_config

        fresh = _reload_config()
        fresh_hb = fresh.gateway.heartbeat
        fresh_dream = fresh.agents.defaults.dream
        tz = fresh.agents.defaults.timezone
        if fresh_hb.enabled:
            self.cron.register_system_job(
                CronJob(
                    id="heartbeat",
                    name="heartbeat",
                    schedule=CronSchedule(
                        kind="every",
                        every_ms=fresh_hb.interval_s * 1000,
                        tz=tz,
                    ),
                    payload=CronPayload(kind="system_event"),
                )
            )
        if fresh_dream.enabled:
            self.cron.register_system_job(
                CronJob(
                    id="dream",
                    name="dream",
                    schedule=fresh_dream.build_schedule(tz),
                    payload=CronPayload(kind="system_event"),
                    catch_up_on_start=True,
                )
            )

    def _refresh_agent_runtime_model(self) -> None:
        """Refresh the running AgentLoop's model/provider from the latest config.

        Called by the WebSocket channel after model/provider settings are
        updated from the WebUI, so agent.model reflects the new selection
        immediately (bootstrap + runtime_model_updated carry the new value).
        """
        try:
            self.agent._refresh_provider_snapshot()
        except Exception:
            console.print("[yellow]Warning: failed to refresh agent runtime model[/yellow]")

    async def _open_browser_when_ready(self) -> None:
        """Wait for the gateway to bind, then point the user's browser at the webui."""
        if not self._open_browser_url:
            return
        import webbrowser

        # Channels start asynchronously; a short poll lets us avoid racing the bind.
        for _ in range(40):  # ~4s max
            try:
                reader, writer = await asyncio.open_connection(
                    self.config.gateway.host or "127.0.0.1", self._ws_port
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        try:
            webbrowser.open(self._open_browser_url)
            console.print(f"[green]✓[/green] Opened browser at {self._open_browser_url}")
        except Exception as e:
            console.print(
                f"[yellow]Could not open browser ({e}); visit {self._open_browser_url}[/yellow]"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Run the gateway (blocks until shutdown), then stop on exit."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        try:
            await self.cron.start()
            # 启动时积压触发的 dream：此时已有 running loop，可安全调度后台任务。
            if self._need_dream_catchup:
                asyncio.create_task(self.agent.run_all_dreams())
            tasks = [
                self.agent.run(),
                self.channels.start_all(),
            ]
            if self._open_browser_url:
                tasks.append(self._open_browser_when_ready())
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Shut the gateway down.

        Layered try/finally: each cleanup step is wrapped so a failure in one
        step does NOT skip subsequent steps. Critically, ``flush_all()`` must
        run even if ``channels.stop_all()`` or ``agent.close_mcp()`` raised
        (including CancelledError during shutdown). The first captured
        exception is re-raised at the end so cancellation semantics are
        preserved. Order is kept exactly as the legacy ``_run_gateway``
        cleanup (do not "optimise" it).
        """
        pending_exc: BaseException | None = None
        try:
            await self.agent.close_mcp()
        except BaseException as exc:  # noqa: BLE001 — re-raised later
            pending_exc = exc
            logger.warning("Error during agent.close_mcp() shutdown: {}", exc)
        try:
            self.cron.stop()
            # Await cancellation of in-flight cron jobs so their state
            # (cancelled status, recomputed next_run_at_ms) is persisted
            # before we flush sessions.
            try:
                await self.cron.await_stop()
            except Exception as exc:
                logger.warning("Error during cron.await_stop(): {}", exc)
        except BaseException as exc:  # noqa: BLE001 — re-raised later
            if pending_exc is None:
                pending_exc = exc
            logger.warning("Error during cron.stop(): {}", exc)
        try:
            self.agent.stop()
        except BaseException as exc:  # noqa: BLE001 — re-raised later
            if pending_exc is None:
                pending_exc = exc
            logger.warning("Error during agent.stop(): {}", exc)
        try:
            await self.agent._resources.shutdown()
        except BaseException as exc:  # noqa: BLE001 — re-raised later
            if pending_exc is None:
                pending_exc = exc
            logger.warning("Error during agent._resources.shutdown(): {}", exc)
        try:
            await self.channels.stop_all()
        except BaseException as exc:  # noqa: BLE001 — re-raised later
            if pending_exc is None:
                pending_exc = exc
            logger.warning("Error during channels.stop_all(): {}", exc)
        # Flush all cached sessions to durable storage before exit.
        # This prevents data loss on filesystems with write-back
        # caching (rclone VFS, NFS, FUSE mounts, etc.).
        try:
            flushed = self.agent.sessions.flush_all()
            if flushed:
                logger.info("Shutdown: flushed {} session(s) to disk", flushed)
        except BaseException as exc:  # noqa: BLE001 — re-raised later
            if pending_exc is None:
                pending_exc = exc
            logger.warning("Error during sessions.flush_all(): {}", exc)
        # Re-raise the first captured exception (typically CancelledError
        # during shutdown) so the caller's asyncio.run sees the cancellation.
        if pending_exc is not None:
            raise pending_exc


def build_gateway_application(
    config: Config,
    *,
    open_browser_url: str | None = None,
    webui_static_dist: bool = True,
    webui_runtime_surface: str = "browser",
    webui_runtime_capabilities: dict[str, Any] | None = None,
) -> GatewayApplication:
    """Construct a fully wired ``GatewayApplication``.

    Thin factory alias kept for the composition-root public surface.
    """
    return GatewayApplication(
        config,
        open_browser_url=open_browser_url,
        webui_static_dist=webui_static_dist,
        webui_runtime_surface=webui_runtime_surface,
        webui_runtime_capabilities=webui_runtime_capabilities,
    )
