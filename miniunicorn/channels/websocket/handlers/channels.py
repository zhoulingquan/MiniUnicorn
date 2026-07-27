"""频道配置 + 飞书扫码登录 handler。"""

from __future__ import annotations

from websockets.http11 import Response

from .._http_router import RouteContext, router
from .._http_routes import _http_error, _http_json_response
from ._common import require_auth


@router.route("/api/channels", methods={"GET"})
@require_auth
def list(ctx: RouteContext) -> Response:
    """List all available channels and their current configuration."""
    from miniunicorn.webui.channels_api import list_channels

    try:
        payload = list_channels()
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/channels/update", methods={"GET", "POST"})
@require_auth
def update(ctx: RouteContext) -> Response:
    """Create or update a single channel's configuration."""
    from miniunicorn.webui.channels_api import WebUIChannelsError, update_channel_config

    query = ctx.query
    try:
        payload = update_channel_config(query)
    except WebUIChannelsError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/channels/delete", methods={"GET", "POST"})
@require_auth
def delete(ctx: RouteContext) -> Response:
    """Remove a channel's configuration."""
    from miniunicorn.webui.channels_api import WebUIChannelsError, delete_channel_config

    query = ctx.query
    try:
        payload = delete_channel_config(query)
    except WebUIChannelsError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/channels/qrcode", methods={"GET", "POST"})
@require_auth
def qrcode_begin(ctx: RouteContext) -> Response:
    """Begin a QR code login flow for a channel (currently feishu only)."""
    from miniunicorn.webui.channels_api import (
        WebUIChannelsError,
        begin_channel_qr_login,
    )

    query = ctx.query
    try:
        payload = begin_channel_qr_login(query)
    except WebUIChannelsError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/channels/qrcode/status", methods={"GET"})
@require_auth
def qrcode_status(ctx: RouteContext) -> Response:
    """Poll the status of a QR code login flow."""
    from miniunicorn.webui.channels_api import (
        WebUIChannelsError,
        poll_channel_qr_status,
    )

    query = ctx.query
    try:
        payload = poll_channel_qr_status(query)
    except WebUIChannelsError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)
