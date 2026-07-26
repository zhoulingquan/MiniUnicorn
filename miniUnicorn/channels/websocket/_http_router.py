"""声明式 HTTP 路由注册表，用于 WebSocketChannel 旁路的 WebUI REST API。

设计目标:
- 把原本嵌在 ``WebSocketChannel`` 内部的 ``_dispatch_*``/``_handle_*`` 方法
  抽成独立 handler,通过装饰器 ``@router.route(...)`` 声明即注册;
- handler 不再持有 ``self``,改为接收一个 :class:`RouteContext`(打包了所有
  依赖与请求上下文),实现与 channel 实例解耦;
- 支持精确路径匹配与正则捕获两种模式,统一 async/sync 双轨调用;
- 未命中时返回 ``None``,交回 channel 继续走 WS 升级/静态文件/404 兜底。

依赖方向(无循环)::

    handlers/* → _http_router(同包) + _http_routes(同包) + webui/*_api(跨包单向)
    channel.py → _http_router + handlers(同包)
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from miniUnicorn.bus.queue import MessageBus

from ._http_routes import _parse_request_path

# handler 返回类型:同步 Response 或异步 Awaitable[Response]。
RouteResult = Union[Response, Awaitable[Response]]
# handler 签名:接收 RouteContext,返回 RouteResult。
RouteHandler = Callable[["RouteContext"], RouteResult]


@dataclass
class RouteDeps:
    """打包 handler 所需的全部依赖,替代原本的 ``self``。

    由 :meth:`WebSocketChannel._build_route_deps` 在每次请求时构造,
    把 channel 实例状态以显式字段的形式注入,handler 不再反向引用 channel。
    """

    # 路径与工作区
    workspace_path: Any  # pathlib.Path
    webui_workspaces: Any  # WebUIWorkspaceController
    # 服务依赖(可能为 None,handler 自行判断)
    session_manager: Any  # SessionManager | None
    cron_service: Any
    tool_registry: Any
    provider_loader: "Callable[[], Any] | None"
    runtime_model_name: "Callable[[], str | None] | None"
    # 运行时状态
    runtime_surface: str
    runtime_capabilities: dict[str, Any]
    media_secret: bytes
    bus: MessageBus
    logger: Any
    # 鉴权与连接判断(把 channel 方法以 callable 注入,避免 handler 持有 self)
    check_api_token: "Callable[[WsRequest], bool]"
    is_localhost_connection: "Callable[[Any], bool]"
    # Origin 校验回调:用于 POST/PUT/DELETE 等状态变更路由的 CSWSH 防护。
    # 空 Origin(非浏览器客户端)返回 True(放行),非空 Origin 严格匹配白名单。
    is_origin_allowed: "Callable[[Any], bool]"
    # 4 处副作用回调(触发 channel 侧的状态变更)
    # with_restart_state: 封装 restart-required 段维护 + payload 装饰,替代原
    #   ``self._with_settings_restart_state(payload, section=...)``。
    with_restart_state: "Callable[[dict, str | None], dict]"
    refresh_agent_model: "Callable[[], None]"
    reload_cron: "Callable[[], None]"
    reload_mcp: "Callable[[], None]"
    # rewind handler 触发:通知连接的 WS 客户端刷新会话视图(fire-and-forget)。
    notify_session_updated: "Callable[[str], None]"
    # bootstrap-file save 后清除 ContextBuilder 缓存(best-effort,mtime 兜底)。
    invalidate_bootstrap_cache: "Callable[[str], None]"
    # 媒体签名回调:把 channel 的 _sign_media_path/_sign_or_stage_media_path
    # 以 callable 注入,handler 不再直接导入 get_media_dir(测试通过 monkeypatch
    # ``channel.get_media_dir`` 拦截,必须经过 channel 模块才能生效)。
    sign_media_path: "Callable[[Any], str | None]"
    sign_or_stage_media_path: "Callable[[Any], dict[str, str] | None]"
    # media 目录解析器(测试通过 monkeypatch ``channel.get_media_dir`` 拦截,
    # 必须经过 channel 模块才能生效)。
    get_media_dir: "Callable[..., Any]"


@dataclass
class RouteContext:
    """单次请求的上下文,包含依赖与已解析的请求信息。

    handler 只需接收这一个参数,签名统一为 ``(ctx: RouteContext) -> Response``。
    """

    deps: RouteDeps
    connection: Any
    request: WsRequest
    query: dict[str, list[str]]
    got: str  # 已归一化的请求路径(不含 query),由 dispatch 解析后传入
    path_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class _HandlerEntry:
    """包装 handler,预计算是否为协程,统一同步/异步调用。"""

    fn: RouteHandler
    is_async: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_async = asyncio.iscoroutinefunction(self.fn)

    async def invoke(self, ctx: RouteContext) -> Response:
        result = self.fn(ctx)
        if self.is_async:
            result = await result  # type: ignore[misc]
        return result  # type: ignore[return-value]


class HttpRouter:
    """声明式路由注册表。

    用法::

        router = HttpRouter()

        @router.route("/api/skills")
        def list_skills(ctx: RouteContext) -> Response: ...

        @router.route(r"^/api/sessions/(?P<key>[^/]+)/messages$", regex=True)
        def session_messages(ctx: RouteContext) -> Response: ...
    """

    def __init__(self) -> None:
        self._exact: dict[str, _HandlerEntry] = {}
        self._regex: list[tuple[re.Pattern[str], _HandlerEntry]] = []

    def route(
        self,
        path: str,
        *,
        regex: bool = False,
    ) -> "Callable[[RouteHandler], RouteHandler]":
        """装饰器:注册一个 handler。

        Args:
            path: 精确路径字符串,或正则模式(当 ``regex=True`` 时)。
            regex: 为 True 时 ``path`` 视为正则,支持 ``(?P<name>...)`` 命名捕获组,
                捕获结果通过 ``ctx.path_vars`` 传给 handler。
        """

        def deco(fn: RouteHandler) -> RouteHandler:
            self.register(path, fn, regex=regex)
            return fn

        return deco

    def register(
        self,
        path: str,
        fn: RouteHandler,
        *,
        regex: bool = False,
    ) -> None:
        """编程式注册(供非装饰器场景使用)。"""
        entry = _HandlerEntry(fn)
        if regex:
            self._regex.append((re.compile(path), entry))
        else:
            self._exact[path] = entry

    async def dispatch(
        self,
        deps: RouteDeps,
        connection: Any,
        request: WsRequest,
    ) -> "Response | None":
        """按注册顺序分派请求,命中则返回 Response,未命中返回 None。

        分派顺序:先精确匹配,再正则匹配(按注册顺序)。未命中交回调用方
        (channel)继续走 WS 升级/静态文件/404 兜底。
        """
        got, query = _parse_request_path(request.path)

        # 1. 精确匹配
        entry = self._exact.get(got)
        if entry is not None:
            ctx = RouteContext(
                deps=deps,
                connection=connection,
                request=request,
                query=query,
                got=got,
            )
            return await entry.invoke(ctx)

        # 2. 正则匹配(按注册顺序,首个命中即返回)
        for pattern, entry in self._regex:
            m = pattern.match(got)
            if m is not None:
                ctx = RouteContext(
                    deps=deps,
                    connection=connection,
                    request=request,
                    query=query,
                    got=got,
                    path_vars=m.groupdict(),
                )
                return await entry.invoke(ctx)

        return None


# 全局单例:所有 handler 模块导入此对象并用 ``@router.route`` 注册。
router = HttpRouter()
