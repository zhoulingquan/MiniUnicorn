"""CLI commands for MiniUnicorn.

This module owns the Typer command surface (``app`` and its subcommands) and
re-exports the helpers that used to live here as top-level definitions.  The
heavy lifting has been split into three sibling modules:

- ``_terminal_render.py`` — prompt_toolkit / Rich output helpers
  (``_print_agent_response``, ``_ReasoningBuffer``, ``_print_cli_reasoning``,
  ``_print_interactive_progress_line`` …).
- ``_heartbeat.py`` — heartbeat preamble text, cached HEARTBEAT.md template
  loader, and the heartbeat-specific provider builder.
- ``_gateway_runner.py`` — the shared ``_run_gateway`` runtime plus the
  cron-job dispatcher (``on_cron_job``) and its branch helpers.

A few names are looked up by tests via ``unittest.mock.patch`` on this
module's namespace (e.g. ``patch("miniunicorn.cli.commands.PromptSession",
...)`` or ``patch("miniunicorn.cli.commands.evaluate_response", ...)``).
Those names must remain importable as attributes of ``commands``; the
helpers in the sibling modules honour the patches through late binding
(``from miniunicorn.cli import commands; commands.<name>(...)``).

The mutable prompt_toolkit session state (``_PROMPT_SESSION`` and
``_SAVED_TERM_ATTRS``) stays on this module for the same reason: tests
patch ``commands._PROMPT_SESSION`` and the helpers reach it through
``commands._PROMPT_SESSION``.
"""

import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        # 运行时设置 PYTHONIOENCODING 已无效（解释器启动时已读取），直接 reconfigure
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click
import typer
from loguru import logger

# Remove default handler and re-add with unified MiniUnicorn format
logger.remove()
_log_handler_id = logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <5}</level> | "
        "<cyan>{extra[channel]}</cyan> | "
        "<level>{message}</level>"
    ),
    level="INFO",
    colorize=None,
    filter=lambda record: record["extra"].setdefault("channel", "-") or True,
)

from prompt_toolkit import PromptSession
from rich.table import Table

from miniunicorn import __logo__, __version__
from miniunicorn.agent.loop import AgentLoop
from miniunicorn.cli._gateway_runner import _run_gateway
from miniunicorn.cli._terminal_render import (
    _flush_cli_reasoning,
    _flush_pending_tty_input,
    _init_prompt_session,
    _is_exit_command,
    _maybe_print_interactive_progress,
    _print_agent_response,
    _print_cli_progress_line,
    _print_cli_reasoning,
    _print_interactive_response,
    _read_interactive_input_async,
    _ReasoningBuffer,
    _restore_terminal,
    _sanitize_surrogates,
    console,
)
from miniunicorn.cli.stream import StreamRenderer, ThinkingSpinner
from miniunicorn.config.paths import get_workspace_path, is_default_workspace
from miniunicorn.config.runtime import RuntimeMode, resolve_runtime_mode
from miniunicorn.config.schema import Config, validate_api_bind_security
from miniunicorn.utils.helpers import sync_workspace_templates
from miniunicorn.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
    should_show_cli_restart_notice,
)

app = typer.Typer(
    name="miniunicorn",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} MiniUnicorn - Personal AI Assistant",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit session state (lives on the commands module so
# that test patches like `patch("miniunicorn.cli.commands._PROMPT_SESSION",
# ...)` continue to work after the split).  The helpers in
# `_terminal_render.py` reach these via `commands._PROMPT_SESSION` /
# `commands._SAVED_TERM_ATTRS` (late binding).
# ---------------------------------------------------------------------------
_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} MiniUnicorn v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """MiniUnicorn - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================

# System directories that --config must never point at (prevents accidental
# overwrites of OS files when a user passes e.g. --config /etc).
_BLOCKED_CONFIG_PATHS: frozenset[str] = frozenset(
    {"/etc", "/usr", "/bin", "/sbin", "/var", "/boot", "/sys", "/proc"}
)


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """Initialize MiniUnicorn configuration and workspace."""
    from miniunicorn.config.loader import get_config_path, load_config, save_config, set_config_path
    from miniunicorn.config.schema import Config

    if config:
        config_path = Path(config).expanduser().resolve()
        # 校验路径不在系统目录，避免误操作覆盖系统文件
        config_str = str(config_path)
        if config_str in _BLOCKED_CONFIG_PATHS or any(
            config_str.startswith(p + "/") for p in _BLOCKED_CONFIG_PATHS
        ):
            raise click.UsageError(f"--config 路径不允许指向系统目录: {config_path}")
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print(
                "  [bold]y[/bold] = overwrite with defaults (existing values will be lost)"
            )
            console.print(
                "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
            )
            if typer.confirm("Overwrite?"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(
                    f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
                )
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from miniunicorn.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print(
                "[yellow]Please run 'miniunicorn onboard' again to complete setup.[/yellow]"
            )
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'miniunicorn agent -m "Hello!"'
    gateway_cmd = "miniunicorn gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} MiniUnicorn is ready!")
    console.print("\nNext steps:")
    if wizard:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
    console.print(
        "\n[dim]Want more channels? See the documentation for available integrations.[/dim]"
    )


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json
    import tempfile

    from miniunicorn.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    # 原子写：先写入临时文件，再 os.replace 覆盖目标，避免写入中途崩溃导致配置损坏
    tmp_fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, prefix=".tmp_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, config_path)
    except Exception:
        # 写入失败时清理临时文件，避免残留
        with suppress(Exception):
            os.unlink(tmp_path)
        raise


def _model_display(config: Config) -> tuple[str, str]:
    """Return (resolved_model_name, preset_tag) for display strings."""
    resolved = config.resolve_preset()
    name = config.agents.defaults.model_preset
    tag = f" (preset: {name})" if name else ""
    return resolved.model, tag


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from miniunicorn.config.loader import load_config, resolve_config_env_vars, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    try:
        loaded = resolve_config_env_vars(load_config(config_path))
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from miniunicorn.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


def _resolve_runtime_mode(
    config: "Config",
    cli_value: str | None,
    launcher_default: RuntimeMode,
) -> RuntimeMode:
    """Validate and resolve the runtime mode from CLI, env, config, or default."""
    if cli_value is not None and cli_value not in ("lightweight", "supervised"):
        console.print(f"[red]Error: invalid --runtime-mode {cli_value!r}[/red]")
        raise typer.Exit(1)
    return resolve_runtime_mode(
        configured=config.runtime.mode,
        cli_value=cli_value,  # type: ignore[arg-type]
        environment=os.getenv("MINIUNICORN_RUNTIME_MODE"),  # type: ignore[arg-type]
        launcher_default=launcher_default,
    )


def _migrate_cron_store(config: "Config") -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from miniunicorn.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# ============================================================================
# OpenAI-Compatible API Server
# ============================================================================


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", "-p", help="API server port"),
    host: str | None = typer.Option(None, "--host", "-H", help="Bind address"),
    timeout: float | None = typer.Option(
        None, "--timeout", "-t", help="Per-request timeout (seconds)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show MiniUnicorn runtime logs"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    runtime_mode: str | None = typer.Option(
        None, "--runtime-mode", help="Runtime mode: lightweight or supervised"
    ),
):
    """Start the OpenAI-compatible API server (/v1/chat/completions)."""
    try:
        from aiohttp import web  # noqa: F401
    except ImportError:
        console.print(
            "[red]aiohttp is required. Install with: pip install 'miniunicorn-ai[api]'[/red]"
        )
        raise typer.Exit(1)

    from loguru import logger

    from miniunicorn.api.server import create_app
    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.session.manager import SessionManager

    if verbose:
        logger.enable("miniunicorn")
    else:
        logger.disable("miniunicorn")

    runtime_config = _load_runtime_config(config, workspace)
    _resolve_runtime_mode(runtime_config, runtime_mode, "lightweight")
    api_cfg = runtime_config.api
    host = host if host is not None else api_cfg.host
    port = port if port is not None else api_cfg.port
    timeout = timeout if timeout is not None else api_cfg.timeout
    try:
        validate_api_bind_security(
            host,
            api_cfg.api_key,
            api_cfg.allow_insecure_public_bind,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    sync_workspace_templates(runtime_config.workspace_path)
    bus = MessageBus()
    session_manager = SessionManager(runtime_config.workspace_path)
    try:
        agent_loop = AgentLoop.from_config(
            runtime_config,
            bus,
            session_manager=session_manager,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    model_name, preset_tag = _model_display(runtime_config)
    api_key = (api_cfg.api_key or "").strip()
    console.print(f"{__logo__} Starting OpenAI-compatible API server")
    console.print(f"  [cyan]Endpoint[/cyan] : http://{host}:{port}/v1/chat/completions")
    console.print(f"  [cyan]Model[/cyan]    : {model_name}{preset_tag}")
    console.print("  [cyan]Session[/cyan]  : api:default")
    console.print(f"  [cyan]Timeout[/cyan]  : {timeout}s")
    if api_key:
        console.print("  [cyan]Auth[/cyan]     : Bearer token required for /v1/*")
    else:
        console.print(
            "  [yellow]Auth[/yellow]     : disabled (set api.api_key to require Bearer token)"
        )
    if host in {"0.0.0.0", "::"}:
        if not api_key:
            console.print(
                "[red]Danger:[/red] API is bound to all interfaces WITHOUT authentication. "
                "Set api.api_key in config before exposing to a network."
            )
        else:
            console.print(
                "[yellow]Warning:[/yellow] API is bound to all interfaces. "
                "Only do this behind a trusted network boundary, firewall, or reverse proxy."
            )
    console.print()

    api_app = create_app(
        agent_loop,
        model_name=model_name,
        request_timeout=timeout,
        api_key=api_key,
    )

    async def on_startup(_app):
        await agent_loop._connect_mcp()

    async def on_cleanup(_app):
        await agent_loop.close_mcp()

    api_app.on_startup.append(on_startup)
    api_app.on_cleanup.append(on_cleanup)

    web.run_app(api_app, host=host, port=port, print=lambda msg: logger.info(msg))


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    runtime_mode: str | None = typer.Option(
        None, "--runtime-mode", help="Runtime mode: lightweight or supervised"
    ),
):
    """Start the MiniUnicorn gateway."""
    if verbose:
        logger.remove(_log_handler_id)
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <5}</level> | "
                "<cyan>{extra[channel]}</cyan> | "
                "<level>{message}</level>"
            ),
            level="DEBUG",
            colorize=None,
            filter=lambda record: record["extra"].setdefault("channel", "-") or True,
        )
    cfg = _load_runtime_config(config, workspace)
    _resolve_runtime_mode(cfg, runtime_mode, "supervised")
    _ensure_local_allow_from(cfg)
    _run_gateway(cfg)


def _ensure_local_allow_from(cfg: Config) -> None:
    """For local dev (``miniunicorn gateway``), auto-allow all clients on the
    configured WebSocket channel so the WebUI can connect without manual
    pairing. Only applied when ``allow_from`` is unset and the host is
    loopback, preserving explicit user configuration otherwise.

    Since the QwenPaw-style channel refactor, ``websocket`` is an explicit
    field on ``ChannelsConfig`` (not in ``__pydantic_extra__``), so we read
    it directly from ``cfg.channels.websocket`` and write back in place."""
    ws_cfg = cfg.channels.websocket
    if not isinstance(ws_cfg, dict):
        return
    host = ws_cfg.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        return
    if ws_cfg.get("allow_from"):
        return
    ws_cfg["allow_from"] = ["*"]
    cfg.channels.websocket = ws_cfg


def _load_or_create_desktop_config(config: str | None, workspace: str | None) -> Config:
    """Load the desktop-owned config, creating it on first launch."""
    from miniunicorn.config.loader import (
        get_config_path,
        load_config,
        resolve_config_env_vars,
        save_config,
        set_config_path,
    )
    from miniunicorn.config.schema import Config as MiniunicornConfig

    config_path = Path(config).expanduser().resolve() if config else get_config_path()
    set_config_path(config_path)
    created = False
    if config_path.exists():
        try:
            loaded = resolve_config_env_vars(load_config(config_path))
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
    else:
        loaded = MiniunicornConfig()
        created = True

    if workspace:
        workspace_path = Path(workspace).expanduser()
        loaded.agents.defaults.workspace = str(workspace_path)
        created = True

    if created:
        save_config(loaded, config_path)
    return loaded


def _configure_desktop_gateway(
    config: Config,
    *,
    webui_port: int,
    webui_socket: str | None,
    token_issue_secret: str,
) -> None:
    """Force a local WebSocket-only gateway for the desktop app process."""
    config.gateway.host = "127.0.0.1"
    config.gateway.heartbeat.enabled = False

    extras = dict(getattr(config.channels, "__pydantic_extra__", None) or {})
    for name, section in list(extras.items()):
        if name == "websocket":
            continue
        if isinstance(section, dict):
            extras[name] = {**section, "enabled": False}
        else:
            with suppress(Exception):
                setattr(section, "enabled", False)
            extras[name] = section

    websocket_cfg = extras.get("websocket")
    if not isinstance(websocket_cfg, dict):
        websocket_cfg = {}
    websocket_cfg.update(
        {
            "enabled": True,
            "host": "127.0.0.1",
            "port": webui_port,
            "unix_socket_path": webui_socket or "",
            "path": "/",
            "token_issue_secret": token_issue_secret,
            "websocket_requires_token": True,
            "allow_from": ["*"],
            "streaming": True,
        }
    )
    extras["websocket"] = websocket_cfg
    config.channels.__pydantic_extra__ = extras


@app.command("desktop-gateway", hidden=True)
def desktop_gateway(
    webui_port: int = typer.Option(0, "--webui-port", min=0, max=65535),
    webui_socket: str | None = typer.Option(
        None, "--webui-socket", help="Unix socket path for desktop IPC"
    ),
    token_issue_secret: str = typer.Option(..., "--token-issue-secret"),
    workspace: str | None = typer.Option(
        None, "--workspace", "-w", help="Desktop workspace directory"
    ),
    config: str | None = typer.Option(None, "--config", "-c", help="Desktop config file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    runtime_mode: str | None = typer.Option(
        None, "--runtime-mode", help="Runtime mode: lightweight or supervised"
    ),
):
    """Start the private local gateway used by MiniUnicorn Desktop."""
    if not token_issue_secret.strip():
        console.print("[red]Error: --token-issue-secret is required[/red]")
        raise typer.Exit(1)
    if webui_port <= 0 and not (webui_socket or "").strip():
        console.print("[red]Error: --webui-port or --webui-socket is required[/red]")
        raise typer.Exit(1)
    if verbose:
        logger.remove(_log_handler_id)
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <5}</level> | "
                "<cyan>{extra[channel]}</cyan> | "
                "<level>{message}</level>"
            ),
            level="DEBUG",
            colorize=None,
            filter=lambda record: record["extra"].setdefault("channel", "-") or True,
        )
    cfg = _load_or_create_desktop_config(config, workspace)
    _resolve_runtime_mode(cfg, runtime_mode, "supervised")
    _configure_desktop_gateway(
        cfg,
        webui_port=webui_port,
        webui_socket=webui_socket,
        token_issue_secret=token_issue_secret,
    )
    _run_gateway(
        cfg,
        webui_static_dist=False,
        webui_runtime_surface="native",
        webui_runtime_capabilities={
            "can_restart_engine": True,
            "can_pick_folder": True,
            "can_open_logs": True,
            "can_export_diagnostics": True,
        },
    )


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(
        True, "--markdown/--no-markdown", help="Render assistant output as Markdown"
    ),
    logs: bool = typer.Option(
        False, "--logs/--no-logs", help="Show MiniUnicorn runtime logs during chat"
    ),
    runtime_mode: str | None = typer.Option(
        None, "--runtime-mode", help="Runtime mode: lightweight or supervised"
    ),
):
    """Interact with the agent directly."""
    from loguru import logger

    from miniunicorn.bus.queue import MessageBus
    from miniunicorn.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    _resolve_runtime_mode(config, runtime_mode, "lightweight")
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("miniunicorn")
    else:
        logger.disable("miniunicorn")

    try:
        agent_loop = AgentLoop.from_config(
            config,
            bus,
            cron_service=cron,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    restart_notice = consume_restart_notice_from_env()
    if restart_notice and should_show_cli_restart_notice(restart_notice, session_id):
        _print_agent_response(
            format_restart_completed_message(restart_notice.started_at_raw),
            render_markdown=False,
        )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    def _make_progress(renderer: StreamRenderer | None = None):
        reasoning_buffer = _ReasoningBuffer()

        async def _cli_progress(
            content: str, *, tool_hint: bool = False, reasoning: bool = False, **_kwargs: Any
        ) -> None:
            ch = agent_loop.channels_config

            if _kwargs.get("reasoning_end"):
                if ch and not ch.show_reasoning:
                    reasoning_buffer.clear()
                else:
                    _flush_cli_reasoning(reasoning_buffer, _thinking, renderer)
                return

            if reasoning:
                if ch and not ch.show_reasoning:
                    reasoning_buffer.clear()
                    return
                text = reasoning_buffer.add(content)
                if text:
                    _print_cli_reasoning(text, _thinking, renderer)
                return
            if ch and tool_hint and not ch.send_tool_hints:
                return
            if ch and not tool_hint and not ch.send_progress:
                return
            _print_cli_progress_line(content, _thinking, renderer)

        return _cli_progress

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(
                render_markdown=markdown,
                bot_name=config.agents.defaults.bot_name,
                bot_icon=config.agents.defaults.bot_icon,
            )
            response = await agent_loop.process_direct(
                message,
                session_id,
                on_progress=_make_progress(renderer),
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                print_kwargs: dict[str, Any] = {}
                if renderer.header_printed:
                    print_kwargs["show_header"] = False
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                    **print_kwargs,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from miniunicorn.bus.events import InboundMessage

        _init_prompt_session()
        _model, _preset_tag = _model_display(config)
        console.print(
            f"{__logo__} Interactive mode [bold blue]({_model})[/bold blue]{_preset_tag} — type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
        )

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []
            renderer: StreamRenderer | None = None
            reasoning_buffer = _ReasoningBuffer()

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        if msg.metadata.get("_stream_delta"):
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            turn_done.set()
                            continue

                        if await _maybe_print_interactive_progress(
                            msg,
                            renderer,
                            agent_loop.channels_config,
                            renderer,
                            reasoning_buffer,
                        ):
                            continue

                        if not turn_done.is_set():
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        # Stop spinner before user input to avoid prompt_toolkit conflicts
                        if renderer:
                            renderer.stop_for_input()
                        user_input = _sanitize_surrogates(await _read_interactive_input_async())
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        reasoning_buffer.clear()
                        renderer = StreamRenderer(
                            render_markdown=markdown,
                            bot_name=config.agents.defaults.bot_name,
                            bot_icon=config.agents.defaults.bot_icon,
                        )

                        await bus.publish_inbound(
                            InboundMessage(
                                channel=cli_channel,
                                sender_id="user",
                                chat_id=cli_chat_id,
                                content=user_input,
                                metadata={"_wants_stream": True},
                            )
                        )

                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                print_kwargs: dict[str, Any] = {}
                                if renderer and renderer.header_printed:
                                    print_kwargs["show_header"] = False
                                _print_agent_response(
                                    content,
                                    render_markdown=markdown,
                                    metadata=meta,
                                    **print_kwargs,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show channel status."""
    from miniunicorn.channels.registry import discover_all
    from miniunicorn.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(
        ..., help="Channel name (use 'miniunicorn channels list' to see available)"
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Force re-authentication even if already logged in"
    ),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from miniunicorn.channels.registry import discover_all
    from miniunicorn.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from miniunicorn.channels.registry import discover_all, discover_channel_names
    from miniunicorn.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show MiniUnicorn status."""
    from miniunicorn.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} MiniUnicorn Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from miniunicorn.providers.registry import PROVIDERS

        _model, _preset_tag = _model_display(config)
        console.print(f"Model: {_model}{_preset_tag}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(
                    f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}"
                )


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


def _resolve_oauth_provider(provider: str):
    """Resolve and validate an OAuth provider configuration."""
    console.print(
        f"[red]Unknown OAuth provider: {provider}[/red]  No OAuth providers available in this build."
    )
    raise typer.Exit(1)


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider"),
):
    """Authenticate with an OAuth provider."""
    _resolve_oauth_provider(provider)


@provider_app.command("logout")
def provider_logout(
    provider: str = typer.Argument(..., help="OAuth provider"),
):
    """Log out from an OAuth provider."""
    _resolve_oauth_provider(provider)


# === 向后兼容 re-export ===
# 这些符号已迁移到专门模块,但测试通过 monkeypatch / unittest.mock.patch 替换
# commands 模块上的属性(例如 patch("miniunicorn.cli.commands.evaluate_response", ...)),
# 因此在此 re-export 保持兼容。调用方应通过 commands.<name> 走 late binding 访问,
# 这样替换才能生效。
#
# 注意:下方 import 中已剔除文件顶部从 _terminal_render 引入过的符号
# (_print_agent_response / _print_cli_reasoning / _ReasoningBuffer / _sanitize_surrogates),
# 避免重复 import。它们仍属于本模块的兼容导出表面(见 __all___compat__)。
from miniunicorn.cli._terminal_render import (  # noqa: F401 — compat re-exports
    FileHistory,
    SafeFileHistory,
    _print_interactive_line,
    _print_interactive_progress_line,
    _render_interactive_ansi,
    _response_renderable,
    patch_stdout,
)
from miniunicorn.utils.evaluator import evaluate_response  # noqa: F401 — compat re-export

# 显式声明 __all__ 之外的兼容导出(避免被 linter 当作未使用而移除)。
# 同时列出文件顶部已 import 的符号,以便集中查阅兼容表面。
__all___compat__ = (
    "FileHistory",
    "SafeFileHistory",
    "patch_stdout",
    "_print_agent_response",
    "_print_cli_reasoning",
    "_print_interactive_line",
    "_print_interactive_progress_line",
    "_ReasoningBuffer",
    "_render_interactive_ansi",
    "_response_renderable",
    "_sanitize_surrogates",
    "evaluate_response",
)


if __name__ == "__main__":
    app()
