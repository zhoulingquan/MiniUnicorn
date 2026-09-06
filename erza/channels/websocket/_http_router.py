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

from erza.bus.queue import MessageBus

from ._http_routes import _merge_sensitive_values_header, _parse_request_path

# handler 返回类型:同步 Response 或异步 Awaitable[Response]。
RouteResult = Union[Response, Awaitable[Response]]
# handler 签名:接收 RouteContext,返回 RouteResult。
RouteHandler = Callable[["RouteContext"], RouteResult]

# 状态变更 HTTP method 集合:这些 method 默认触发 Origin 校验(CSWSH 防护)。
# GET/HEAD/OPTIONS 是安全方法,不触发 Origin 校验(但非公开路由仍需 token)。
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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


@dataclass(frozen=True)
class RouteMeta:
    """路由元数据:HTTP method 集合、是否公开、是否校验 Origin。

    - ``methods``:允许的 HTTP method 集合,默认 ``{"GET"}``。未匹配的 method
      返回 405。
    - ``public``:是否显式公开(免 token)。默认 ``False``——安全边界默认拒绝,
      公开入口必须显式声明(如签名 media 路由)。
    - ``origin_check``:是否要求 Origin 校验。默认 ``True``;仅在请求 method
      属于状态变更集合(POST/PUT/PATCH/DELETE)时实际执行。GET 等安全方法
      不触发 Origin 校验,但非公开路由仍需 token。
    """

    methods: frozenset[str] = frozenset({"GET"})
    public: bool = False
    origin_check: bool = True


@dataclass
class _HandlerEntry:
    """包装 handler + 元数据,预计算是否为协程,统一同步/异步调用。"""

    fn: RouteHandler
    meta: RouteMeta = field(default_factory=RouteMeta)
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
        methods: "frozenset[str] | set[str] | None" = None,
        public: bool = False,
        origin_check: bool = True,
    ) -> "Callable[[RouteHandler], RouteHandler]":
        """装饰器:注册一个 handler。

        Args:
            path: 精确路径字符串,或正则模式(当 ``regex=True`` 时)。
            regex: 为 True 时 ``path`` 视为正则,支持 ``(?P<name>...)`` 命名捕获组,
                捕获结果通过 ``ctx.path_vars`` 传给 handler。
            methods: 允许的 HTTP method 集合,默认 ``{"GET"}``。未匹配返回 405。
            public: 是否显式公开(免 token)。默认 ``False``。
            origin_check: 是否校验 Origin(仅对状态变更 method 生效)。默认 ``True``。
        """
        meta = RouteMeta(
            methods=frozenset(methods) if methods is not None else frozenset({"GET"}),
            public=public,
            origin_check=origin_check,
        )

        def deco(fn: RouteHandler) -> RouteHandler:
            self.register(path, fn, regex=regex, meta=meta)
            return fn

        return deco

    def register(
        self,
        path: str,
        fn: RouteHandler,
        *,
        regex: bool = False,
        methods: "frozenset[str] | set[str] | None" = None,
        public: bool = False,
        origin_check: bool = True,
        meta: "RouteMeta | None" = None,
    ) -> None:
        """编程式注册(供非装饰器场景使用)。"""
        if meta is None:
            meta = RouteMeta(
                methods=frozenset(methods) if methods is not None else frozenset({"GET"}),
                public=public,
                origin_check=origin_check,
            )
        entry = _HandlerEntry(fn, meta=meta)
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

        命中后,在调用 handler 前统一执行三层安全校验:

        1. **method 校验**:请求 method 必须在 ``RouteMeta.methods`` 内,否则 405。
        2. **token 校验**:非 ``public`` 路由必须携带有效 API token,否则 401。
        3. **Origin 校验**:请求 method 属于状态变更集合(POST/PUT/PATCH/DELETE)
           且 ``origin_check=True`` 时,Origin 头必须在白名单内,否则 403。

        敏感字段可通过 ``x-erza-Values`` chunked JSON header 传递,在此处
        解析并合并进 ``ctx.query``,handler 无需感知 header 与 query 的差异。
        """
        from ._http_routes import _http_error

        got, query = _parse_request_path(request.path)
        method = (getattr(request, "method", "GET") or "GET").upper()

        # 1. 精确匹配
        entry = self._exact.get(got)
        if entry is None:
            # 2. 正则匹配(按注册顺序,首个命中即返回)
            for pattern, regex_entry in self._regex:
                m = pattern.match(got)
                if m is not None:
                    entry = regex_entry
                    path_vars = m.groupdict()
                    break
            else:
                return None
        else:
            path_vars = {}

        meta = entry.meta

        # method 校验:未匹配返回 405。
        # 注意:websockets HTTP 层只支持 GET,所有请求的 method 都是 GET。
        # routes 声明 methods={"GET","POST"} 表示"状态变更路由"(POST 仅为语义标记,
        # 不会实际到达)。GET-only 路由的 methods={"GET"} 自然匹配。
        if method not in meta.methods:
            allow = ", ".join(sorted(meta.methods))
            return _http_error(
                405,
                f"Method Not Allowed (allowed: {allow})",
            )

        # token 校验:非公开路由必须携带有效 token。
        if not meta.public and not deps.check_api_token(request):
            return _http_error(401, "Unauthorized")

        # Origin 校验:状态变更路由(accept POST)默认校验 Origin,防御 CSRF/CSWSH。
        # 由于 websockets HTTP 层只支持 GET,状态变更请求实际以 GET 发送,
        # 但跨源浏览器 GET 仍能触发状态变更,因此 Origin 校验必须覆盖这些路由。
        # _STATE_CHANGING_METHODS 仍保留用于未来支持 POST 时的 method 级校验。
        if meta.origin_check and (method in _STATE_CHANGING_METHODS or "POST" in meta.methods):
            if not deps.is_origin_allowed(request):
                return _http_error(403, "Origin not allowed")

        # 敏感字段 header 合并:把 x-erza-Values(JSON)合并进 query,
        # 让 handler 透明地从 query 读取 api_key/config 等敏感字段,避免它们
        # 出现在 URL query 字符串(进而避免进入日志/Referer/浏览器历史)。
        merged_query = _merge_sensitive_values_header(request, query)

        ctx = RouteContext(
            deps=deps,
            connection=connection,
            request=request,
            query=merged_query,
            got=got,
            path_vars=path_vars,
        )
        return await entry.invoke(ctx)


# 全局单例:所有 handler 模块导入此对象并用 ``@router.route`` 注册。
router = HttpRouter()
