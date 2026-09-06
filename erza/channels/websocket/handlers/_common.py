"""handler 共享的小工具。

鉴权失败响应、服务不可用响应等高频样板,集中在此避免各 handler 重复。

注意:token 与 Origin 校验现在由 :meth:`HttpRouter.dispatch` 统一执行(基于
``RouteMeta``)。``require_auth`` / ``require_auth_with_origin`` 保留为向后兼容
的 pass-through 装饰器,不再重复校验——路由必须通过 ``@router.route(...)``
的 ``methods`` / ``public`` / ``origin_check`` 参数声明安全边界。
"""

from __future__ import annotations

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
    """向后兼容的 pass-through 装饰器。

    token 校验已由 ``HttpRouter.dispatch`` 基于 ``RouteMeta.public`` 统一执行,
    此装饰器不再重复校验,仅保留以兼容现有 handler 的 ``@require_auth`` 写法。
    新代码应直接在 ``@router.route(...)`` 上声明 ``public=False``(默认值)。
    """
    return fn


def require_auth_with_origin(fn):
    """向后兼容的 pass-through 装饰器。

    token + Origin 校验已由 ``HttpRouter.dispatch`` 基于 ``RouteMeta`` 统一执行,
    此装饰器不再重复校验。新代码应在 ``@router.route(...)`` 上声明
    ``origin_check=True``(默认值)并使用状态变更 method(POST/PUT/PATCH/DELETE)。
    """
    return fn
