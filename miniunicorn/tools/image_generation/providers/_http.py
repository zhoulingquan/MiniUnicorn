"""共享 httpx 异步客户端工厂。

适配器可选复用此客户端以共享连接池; 也可自行创建临时客户端。
工具层负责管理共享客户端的生命周期 (一次 execute 内复用)。
"""

from __future__ import annotations

import httpx

from miniunicorn.security.network import create_ssrf_safe_client

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)


def build_async_client(
    *,
    timeout: float | None = None,
    proxy: str | None = None,
) -> httpx.AsyncClient:
    """构造一个共享的 SSRF 防护 httpx.AsyncClient。

    每次出站请求在拨号前重新校验目标 IP (含 httpx 内部跟随的重定向),
    防止 api_base 配置或 provider 重定向被用于探测内网。目标端点由操作者
    在 model_preset 中显式配置, 可能指向本机 ComfyUI/SD WebUI 等私有服务,
    因此 allow_private=True (仅拦截云元数据/链路本地网段)。TLS 证书校验
    始终开启, 不提供关闭开关。

    Args:
        timeout: 请求超时秒数; None 时使用默认 120s
        proxy: HTTP/HTTPS 代理; None 时复用系统环境变量
    """
    return create_ssrf_safe_client(
        proxy=proxy,
        timeout=timeout or _DEFAULT_TIMEOUT,
        allow_private=True,
        follow_redirects=True,
        limits=_DEFAULT_LIMITS,
    )
