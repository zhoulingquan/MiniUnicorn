"""CLI Apps helpers for the WebUI HTTP and message surfaces."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from miniunicorn.config.loader import load_config

from ._query import _clip_ws_string, _query_first
from ._runtime import QueryParams

if TYPE_CHECKING:
    from miniunicorn.apps.cli import CliAppError, CliAppManager
    from miniunicorn.config.schema import Config

_CLI_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_CLI_APP_ATTACHMENT_KEYS = (
    "name",
    "display_name",
    "category",
    "entry_point",
    "logo_url",
    "brand_color",
)


def normalize_cli_app_mentions(raw: Any) -> list[dict[str, str]]:
    """Sanitize structured CLI app mentions sent by the WebUI."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        name = _clip_ws_string(item.get("name"), 64)
        if not name or _CLI_APP_NAME_RE.match(name) is None:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, str] = {"name": key}
        for field in _CLI_APP_ATTACHMENT_KEYS[1:]:
            value = _clip_ws_string(item.get(field), 512 if field == "logo_url" else 160)
            if value:
                row[field] = value
        out.append(row)
    return out


def _cli_apps_config() -> Config:
    return load_config()


def cli_apps_enabled(config: Config | None = None) -> bool:
    """Whether the optional CLI Apps ecosystem is enabled (``tools.cliApps.enabled``)."""
    cfg = config if config is not None else _cli_apps_config()
    return bool(cfg.tools.cli_apps.enabled)


def _manager(config: Config | None = None) -> CliAppManager:
    # Imported lazily so the WebUI/channel modules can be imported at gateway
    # startup without loading the ``apps`` service when the feature is off.
    from miniunicorn.apps.cli import CliAppManager, CliAppsRuntimeConfig

    if config is None:
        config = _cli_apps_config()
    cli_cfg = config.tools.cli_apps
    return CliAppManager(
        workspace=config.workspace_path,
        runtime=CliAppsRuntimeConfig(
            install_timeout=cli_cfg.install_timeout,
            run_timeout=cli_cfg.run_timeout,
            catalog_ttl_seconds=cli_cfg.catalog_ttl_seconds,
        ),
    )


def cli_apps_payload() -> dict[str, Any]:
    if not cli_apps_enabled():
        return {
            "enabled": False,
            "apps": [],
            "installed_count": 0,
            "catalog_updated_at": None,
        }
    payload = _manager().payload()
    payload["enabled"] = True
    return payload


def _cli_app_error(message: str, *, status: int = 400) -> CliAppError:
    # Lazy import keeps the ``apps`` service unloaded until first real use.
    from miniunicorn.apps.cli import CliAppError

    return CliAppError(message, status=status)


def cli_apps_action(action: str, query: QueryParams) -> dict[str, Any]:
    if not cli_apps_enabled():
        raise _cli_app_error(
            "CLI Apps are disabled; set tools.cliApps.enabled=true to enable",
            status=403,
        )
    name = (_query_first(query, "name") or "").strip()
    if not name:
        raise _cli_app_error("missing CLI app name")
    manager = _manager()
    if action == "install":
        return manager.install(name)
    if action == "update":
        return manager.update(name)
    if action == "uninstall":
        return manager.uninstall(name)
    if action == "test":
        return manager.test(name)
    raise _cli_app_error(f"unknown CLI app action '{action}'", status=404)
