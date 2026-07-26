"""签名媒体文件读取 handler(正则路由)。"""

from __future__ import annotations

from websockets.http11 import Response

from miniunicorn.webui.media_api import serve_signed_media

from .._http_router import RouteContext, router


@router.route(r"^/api/media/(?P<sig>[A-Za-z0-9_-]+)/(?P<payload>[A-Za-z0-9_-]+)$", regex=True)
def fetch_media(ctx: RouteContext) -> Response:
    """Serve a single media file previously signed via
    ``_sign_media_path``. Validates the signature, decodes the
    payload to a relative path, and streams the file bytes with a
    long-lived immutable cache header (the URL already encodes the
    file identity, so caches can be aggressive)."""
    return serve_signed_media(
        ctx.path_vars["sig"],
        ctx.path_vars["payload"],
        secret=ctx.deps.media_secret,
        request=ctx.request,
        media_dir=ctx.deps.get_media_dir,
    )
