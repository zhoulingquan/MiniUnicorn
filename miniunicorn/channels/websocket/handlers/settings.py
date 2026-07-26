"""Provider/MCP/搜索等配置 handler(18 个端点,含 async)。"""

from __future__ import annotations

import asyncio

from websockets.http11 import Response

from .._http_routes import (
    _http_error,
    _http_json_response,
    _parse_mcp_settings_query,
    _MCP_PRESET_ACTIONS_BY_PATH,
)
from .._http_router import RouteContext, router
from ._common import require_auth, unauthorized
from miniunicorn.webui.settings_api import (
    WebUISettingsError,
    create_model_configuration,
    delete_all_providers,
    delete_model_configuration,
    delete_provider_settings,
    list_provider_models,
    login_oauth_provider,
    logout_oauth_provider,
    settings_payload,
    update_agent_settings,
    update_image_generation_settings,
    update_model_configuration,
    update_network_safety_settings,
    update_provider_settings,
    update_runtime_settings,
    update_web_fetch_settings,
    update_web_search_settings,
)
from miniunicorn.webui.mcp_presets_api import mcp_presets_settings_action


@router.route("/api/settings")
@require_auth
def settings(ctx: RouteContext) -> Response:
    return _http_json_response(
        ctx.deps.with_restart_state(
            settings_payload(
                surface=ctx.deps.runtime_surface,
                runtime_capability_overrides=ctx.deps.runtime_capabilities,
            ),
            section=None,
        )
    )


@router.route("/api/settings/update")
@require_auth
def settings_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_agent_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    ctx.deps.refresh_agent_model()
    return _http_json_response(
        ctx.deps.with_restart_state(payload, section="runtime")
    )


@router.route("/api/settings/model-configurations/create")
@require_auth
def model_config_create(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = create_model_configuration(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    ctx.deps.refresh_agent_model()
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/model-configurations/update")
@require_auth
def model_config_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_model_configuration(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    ctx.deps.refresh_agent_model()
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/model-configurations/delete")
@require_auth
def model_config_delete(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = delete_model_configuration(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    ctx.deps.refresh_agent_model()
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/provider/update")
@require_auth
def provider_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_provider_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/provider/delete")
@require_auth
def provider_delete(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = delete_provider_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/providers/delete-all")
@require_auth
def providers_delete_all(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = delete_all_providers(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/provider/models")
@require_auth
async def provider_models(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = await list_provider_models(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        ctx.deps.logger.warning("provider models fetch failed: {}", exc)
        return _http_error(500, f"Failed to fetch models: {exc}")
    return _http_json_response(payload)


async def _provider_oauth(ctx: RouteContext, action: str) -> Response:
    if not ctx.deps.check_api_token(ctx.request):
        return unauthorized()
    query = ctx.query
    try:
        if action == "login":
            payload = await asyncio.to_thread(login_oauth_provider, query)
        else:
            payload = await asyncio.to_thread(logout_oauth_provider, query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section=None))


@router.route("/api/settings/provider/oauth-login")
async def provider_oauth_login(ctx: RouteContext) -> Response:
    return await _provider_oauth(ctx, "login")


@router.route("/api/settings/provider/oauth-logout")
async def provider_oauth_logout(ctx: RouteContext) -> Response:
    return await _provider_oauth(ctx, "logout")


@router.route("/api/settings/web-fetch/update")
@require_auth
def web_fetch_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_web_fetch_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section="browser"))


@router.route("/api/settings/web-search/update")
@require_auth
def web_search_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_web_search_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section="browser"))


@router.route("/api/settings/image-generation/update")
@require_auth
def image_generation_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_image_generation_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section="runtime"))


@router.route("/api/settings/network-safety/update")
@require_auth
def network_safety_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_network_safety_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    return _http_json_response(ctx.deps.with_restart_state(payload, section="runtime"))


@router.route("/api/settings/runtime/update")
@require_auth
def runtime_update(ctx: RouteContext) -> Response:
    query = ctx.query
    try:
        payload = update_runtime_settings(query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    ctx.deps.reload_cron()
    return _http_json_response(ctx.deps.with_restart_state(payload, section="runtime"))


@router.route("/api/settings/cli-apps")
@require_auth
def cli_apps(ctx: RouteContext) -> Response:
    # Import from channel module so tests can monkeypatch
    # ``channel.cli_apps_payload`` and intercept the call.
    from miniunicorn.channels.websocket.channel import cli_apps_payload

    try:
        payload = cli_apps_payload()
    except Exception:
        ctx.deps.logger.exception("failed to load CLI Apps payload")
        return _http_error(500, "failed to load CLI Apps")
    return _http_json_response(payload)


async def _cli_apps_action(ctx: RouteContext, action: str) -> Response:
    if not ctx.deps.check_api_token(ctx.request):
        return unauthorized()
    query = ctx.query
    # Import from channel module so tests can monkeypatch
    # ``channel.cli_apps_action`` and intercept the call.
    from miniunicorn.channels.websocket.channel import cli_apps_action

    try:
        payload = await asyncio.to_thread(cli_apps_action, action, query)
    except WebUISettingsError as e:
        return _http_error(e.status, e.message)
    except Exception as e:
        status = getattr(e, "status", 500)
        message = getattr(e, "message", str(e))
        if status >= 500:
            ctx.deps.logger.exception("CLI Apps action '{}' failed", action)
        return _http_error(status, message)
    return _http_json_response(payload)


@router.route("/api/settings/cli-apps/install")
async def cli_apps_install(ctx: RouteContext) -> Response:
    return await _cli_apps_action(ctx, "install")


@router.route("/api/settings/cli-apps/update")
async def cli_apps_update(ctx: RouteContext) -> Response:
    return await _cli_apps_action(ctx, "update")


@router.route("/api/settings/cli-apps/uninstall")
async def cli_apps_uninstall(ctx: RouteContext) -> Response:
    return await _cli_apps_action(ctx, "uninstall")


@router.route("/api/settings/cli-apps/test")
async def cli_apps_test(ctx: RouteContext) -> Response:
    return await _cli_apps_action(ctx, "test")


async def _mcp_presets_handler(ctx: RouteContext, action: str | None) -> Response:
    if not ctx.deps.check_api_token(ctx.request):
        return unauthorized()
    try:
        payload = await mcp_presets_settings_action(
            action,
            _parse_mcp_settings_query(ctx.request),
            reload_mcp=ctx.deps.reload_mcp,
        )
    except Exception as e:
        status = getattr(e, "status", 500)
        message = getattr(e, "message", str(e))
        if status >= 500:
            ctx.deps.logger.exception("MCP preset action '{}' failed", action or "list")
        return _http_error(status, message)
    if action is None:
        return _http_json_response(payload)
    return _http_json_response(
        ctx.deps.with_restart_state(payload, section="runtime")
    )


@router.route("/api/settings/mcp-presets")
async def mcp_presets(ctx: RouteContext) -> Response:
    return await _mcp_presets_handler(ctx, None)


def _make_mcp_preset_action_handler(action: str):
    async def _handler(ctx: RouteContext) -> Response:
        return await _mcp_presets_handler(ctx, action)

    return _handler


for _path, _action in _MCP_PRESET_ACTIONS_BY_PATH.items():
    router.register(_path, _make_mcp_preset_action_handler(_action))
