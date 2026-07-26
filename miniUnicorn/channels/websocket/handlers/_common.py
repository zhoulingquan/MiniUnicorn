"""handler 共享的小工具。

鉴权失败响应、服务不可用响应等高频样板,集中在此避免各 handler 重复。
"""

from __future__ import annotations

import asyncio
import functools

from websockets.http11 import Response

from .._http_routes import _http_error


def unauthorized() -> Response:
    """401 Unauthorized 标准响应。"""
    return _http_error(401, "Unauthorized")


def forbidden(message: str = "Forbidden") -> Response:
    """403 Forbidden 标准响应,用于 Origin 校验失败等场景。"""
    return _http_error(403, message)


def service_unavailable(message: str = "service unavailable") -> Response:
    """503 Service Unavailable,用于 session_manager / cron_service 等未注入时。"""
    return _http_error(503, message)


def require_auth(fn):
    """装饰器:在 handler 执行前校验 API token,失败则返回 401。

    兼容 sync 和 async handler。用法::

        @router.route("/api/foo")
        @require_auth
        def foo(ctx: RouteContext) -> Response: ...
    """
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(ctx, *args, **kwargs):
            if not ctx.deps.check_api_token(ctx.request):
                return unauthorized()
            return await fn(ctx, *args, **kwargs)
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(ctx, *args, **kwargs):
        if not ctx.deps.check_api_token(ctx.request):
            return unauthorized()
        return fn(ctx, *args, **kwargs)
    return sync_wrapper


def require_auth_with_origin(fn):
    """装饰器:在 handler 执行前校验 API token + Origin,失败则返回 401/403。

    比 ``require_auth`` 多一层 Origin 校验,用于 POST/PUT/DELETE 等状态变更路由,
    防御 CSWSH(Cross-Site WebSocket/HTTP)攻击:恶意网页通过表单/fetch 发起
    跨域请求时,浏览器会携带 Origin 头,此处拦截非白名单来源。

    - 空 Origin(非浏览器客户端,如 curl/httpx)→ 放行,保持向后兼容。
    - 非空 Origin → 必须在白名单内,否则返回 403。

    兼容 sync 和 async handler。用法::

        @router.route("/api/foo", method="POST")
        @require_auth_with_origin
        def foo(ctx: RouteContext) -> Response: ...
    """
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(ctx, *args, **kwargs):
            if not ctx.deps.check_api_token(ctx.request):
                return unauthorized()
            if not ctx.deps.is_origin_allowed(ctx.request):
                return forbidden("Origin not allowed")
            return await fn(ctx, *args, **kwargs)
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(ctx, *args, **kwargs):
        if not ctx.deps.check_api_token(ctx.request):
            return unauthorized()
        if not ctx.deps.is_origin_allowed(ctx.request):
            return forbidden("Origin not allowed")
        return fn(ctx, *args, **kwargs)
    return sync_wrapper
