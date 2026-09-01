"""搜索后端抽象基类。

每个后端只需继承 SearchBackend,实现 search() 方法。
aggregator 负责降级链与并发聚合,后端只关心单次查询。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from miniunicorn.tools.web_search.circuit_breaker import BackendCircuitBreaker
from miniunicorn.tools.web_search.config import WebSearchConfig


def _read_system_proxy() -> str | None:
    """读取系统代理环境变量。

    当 config.proxy 为 None 时回退到此函数,让 web_search 自动复用
    用户已有的系统代理(如 HTTP_PROXY/HTTPS_PROXY 环境变量)。

    优先级: HTTPS_PROXY > HTTP_PROXY > https_proxy > http_proxy
    """
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


@dataclass
class SearchResult:
    """单条搜索结果。"""

    title: str
    url: str
    snippet: str = ""
    source_backend: str = ""  # 标记来源后端,聚合时区分

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source_backend,
        }


@dataclass
class BackendResponse:
    """后端响应封装。"""

    backend: str
    results: list[SearchResult] = field(default_factory=list)
    error: str = ""  # 非空表示失败
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


class SearchBackend(ABC):
    """搜索后端抽象基类。"""

    # 子类必须覆盖
    name: str = "base"
    # 是否需要 API Key
    requires_api_key: bool = False
    # 对应的环境变量名(供 get_api_key 自动读取)
    env_var: str = ""
    # 是否需要代理才能在国内使用
    needs_proxy_in_cn: bool = False
    # 该后端的推荐默认超时(秒);子类可覆盖。
    # 用户未显式配置 backends[name].timeout 时使用此值。
    default_timeout: int = 30

    def __init__(self, config: WebSearchConfig) -> None:
        self.config = config
        self.timeout = self._resolve_timeout()
        # proxy 为 None 时回退到系统代理环境变量(HTTP_PROXY/HTTPS_PROXY),
        # 让 web_search 自动复用用户已有的代理配置,海外后端无需额外设置
        self.proxy = config.proxy or _read_system_proxy()
        self.user_agent = _DEFAULT_USER_AGENT
        # 每个后端实例持有独立的熔断器(独立计数,互不影响)
        self.circuit_breaker = BackendCircuitBreaker(name=self.name)

    def _resolve_timeout(self) -> float:
        # 优先级:backend_cfg.timeout(用户显式配置) > 子类 default_timeout > config.timeout(全局)
        backend_cfg = self.config.get_backend_config(self.name)
        return float(backend_cfg.timeout or self.default_timeout or self.config.timeout)

    def get_api_key(self) -> str:
        """获取该后端的 API Key。"""
        return self.config.get_api_key(self.name, self.env_var)

    def make_client(
        self, *, timeout: float | None = None, follow_redirects: bool = True
    ) -> httpx.AsyncClient:
        """创建带 SSRF 防护的 httpx 客户端。

        SSRF 钩子会在每次请求(含重定向)时触发,因此 follow_redirects 安全。
        """
        from miniunicorn.security.network import create_ssrf_safe_client

        return create_ssrf_safe_client(
            proxy=self.proxy,
            timeout=timeout or self.timeout,
            follow_redirects=follow_redirects,
        )

    @abstractmethod
    async def search(self, query: str, count: int) -> BackendResponse:
        """执行搜索,返回 BackendResponse。

        实现要点:
        - 抓取失败时返回 error 字段,不要抛异常(降级链依赖此约定)
        - 每条结果填充 source_backend = self.name
        """
        ...

    async def safe_search(self, query: str, count: int) -> BackendResponse:
        """带熔断器的搜索包装。

        - open 状态下直接返回错误响应(不发网络请求)
        - 成功时重置熔断器
        - 失败时记录,达到阈值后打开熔断器

        aggregator 应调用本方法而非直接调 ``search()``,
        以获得熔断保护。子类无需覆盖本方法。
        """
        cb = self.circuit_breaker
        if not cb.allow():
            return BackendResponse(
                backend=self.name,
                error=f"{self.name}: circuit breaker open (status={cb.status()}, retry later)",
            )
        resp = await self.search(query, count)
        if resp.ok:
            cb.record_success()
        else:
            cb.record_failure()
        return resp


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
