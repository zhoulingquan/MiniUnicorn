"""共享 httpx 异步客户端工厂。

适配器可选复用此客户端以共享连接池; 也可自行创建临时客户端。
工具层负责管理共享客户端的生命周期 (一次 execute 内复用)。
"""

from __future__ import annotations

import httpx

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)


def build_async_client(
    *,
    timeout: float | None = None,
    proxy: str | None = None,
    verify: bool = True,
) -> httpx.AsyncClient:
    """构造一个共享 httpx.AsyncClient。

    Args:
        timeout: 请求超时秒数; None 时使用默认 120s
        proxy: HTTP/HTTPS 代理; None 时复用系统环境变量
        verify: 是否校验 TLS 证书; 调试场景可关闭
    """
    return httpx.AsyncClient(
        timeout=timeout or _DEFAULT_TIMEOUT,
        limits=_DEFAULT_LIMITS,
        proxy=proxy,
        verify=verify,
        follow_redirects=True,
    )
