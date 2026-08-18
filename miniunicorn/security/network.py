"""Network security utilities — SSRF protection and internal URL detection.

The ``create_ssrf_safe_client`` factory and ``_SSRFSafeRequestHook`` borrow
the design of Reasonix's ``ssrfGuardedTransport``: outbound HTTP requests are
intercepted just before dial so IP-literal targets can be blocked at the
transport layer. Hostname targets are validated pre-flight by
``validate_url_target`` in the redirect-safe fetch wrappers (covering both
initial requests and any explicit redirects). When a proxy is configured,
hostnames are forwarded to the proxy for resolution (matching Reasonix's
GFW-friendly behaviour); IP-literal targets are still checked client-side.

DNS rebinding 防护:``validate_url_target`` 解析 hostname 后会把
``hostname → frozenset(已校验 IP)`` 写入 ContextVar(``_pinned_dns_var``);
``_ssrf_request_hook`` 在 dial 前重新解析 hostname,如果任一解析到的 IP 不
在 pin 集合中(说明 DNS 在两次查询间被改写),拒绝请求。pin 记录有 30 秒
TTL,过期后允许 DNS 正常变更生效。
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import re
import socket
import time
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

import httpx

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local v6
]

# Networks that are ALWAYS blocked, even if the operator adds them to the
# SSRF whitelist via ``configure_ssrf_whitelist``.  These cover cloud metadata
# endpoints (169.254.0.0/16 — AWS/GCP/Azure IMDS) and loopback (127.0.0.0/8,
# ::1) which must never be reachable from a server-side fetch context.
_HARD_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("::1/128"),
]

# Networks blocked even in ``allow_private`` mode: cloud metadata endpoints
# (link-local) are never legitimate targets for outbound clients such as
# user-configured local MCP servers, while loopback/RFC1918 stay reachable.
_METADATA_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("fe80::/10"),  # link-local v6
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

# 已知 HTTP 客户端命令名(小写,含 .exe 后缀)。这些工具对无 scheme 的
# 主机/IP 参数会默认补 http://,因此 SSRF 检查必须覆盖这种形式。
# 例如 `curl example.com` / `wget 169.254.169.254` 都会发起 HTTP 请求。
_HTTP_CLIENT_BINARIES: frozenset[str] = frozenset(
    {
        "curl",
        "curl.exe",
        "wget",
        "wget.exe",
    }
)

# schemeless 目标参数的可接受首字符(用于在命令行中识别 URL candidate)。
# 排除 '-'(选项标志)、数字开头的重定向(如 2>)、以及 shell 元字符。
_SCHEMELESS_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:"
    r"localhost"  # localhost (no dot)
    r"|"
    r"(?:[A-Za-z][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}"  # domain.tld
    r"|"
    r"(?:\d{1,3}\.){3}\d{1,3}"  # IPv4 literal
    r")"
    r"(?::\d+)?"  # optional :port
    r"(?:/[^\s\"'`;|<>]*)?"  # optional path
)

# SSRF 白名单使用 ContextVar 而非模块级 list 保存。
# 同一进程内可能运行多个实例(如多 agent / 多请求上下文),若用模块级全局,
# 一个实例调用 configure_ssrf_whitelist 会覆盖其它实例的白名单;改用 ContextVar
# 后,白名单绑定到当前 async 上下文,各实例互不干扰。
# _HARD_BLOCKED_NETWORKS 保持模块级常量,不应被覆盖。
_allowed_networks_var: contextvars.ContextVar[
    tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
] = contextvars.ContextVar("_allowed_networks_var", default=())

# DNS rebinding 防护:hostname → (已校验 IP 集合, 时间戳)。
# validate_url_target 解析成功后写入;_ssrf_request_hook dial 前重新解析并比对。
# TTL 30 秒,过期后允许 DNS 正常变更生效。
# 注意:ContextVar 不支持 default 工厂(不像 dataclass 的 default_factory),
# 因此 default 设为 None,由 _pin_dns_resolution / _check_dns_pin 在读取时
# 统一处理 None → 空 dict,避免共享 dict 类本身作为默认值。
_DNS_PIN_TTL_S: float = 30.0
_pinned_dns_var: contextvars.ContextVar[dict[str, tuple[frozenset[str], float]] | None] = (
    contextvars.ContextVar("_pinned_dns_var", default=None)
)


def _pin_dns_resolution(hostname: str, ips: list[str]) -> None:
    """把已校验过的 hostname → IP 集合写入当前 context 的 pin 表。

    用于 DNS rebinding 防御:后续 dial 前会重新解析并比对,如果 IP 集合
    发生变化(说明 DNS 被改写),拒绝请求。
    """
    if not hostname or not ips:
        return
    pinned = _pinned_dns_var.get()
    # 复制一份再写,避免共享底层 dict;None 时初始化为空 dict
    pinned = dict(pinned) if pinned else {}
    pinned[hostname.lower()] = (frozenset(ips), time.monotonic())
    _pinned_dns_var.set(pinned)


def _check_dns_pin(hostname: str, current_ips: list[str]) -> tuple[bool, str]:
    """检查重新解析得到的 IP 是否都在 pin 集合内。

    Returns: (ok, error_message)。如果 hostname 不在 pin 表中,返回 (True, "")
    (无 pin 记录时不强制检查,兼容旧调用方)。如果任一 IP 不在 pin 集合中,
    返回 (False, 原因)。
    """
    if not hostname or not current_ips:
        return True, ""
    pinned = _pinned_dns_var.get()
    if not pinned:
        return True, ""
    entry = pinned.get(hostname.lower())
    if entry is None:
        return True, ""
    pinned_ips, pinned_at = entry
    # TTL 过期:允许 DNS 变更,不强制检查
    if time.monotonic() - pinned_at > _DNS_PIN_TTL_S:
        return True, ""
    current_set = set(current_ips)
    new_ips = current_set - set(pinned_ips)
    if new_ips:
        return False, (
            f"DNS rebinding suspected: {hostname} resolved to new IP(s) "
            f"{sorted(new_ips)} not in pinned set {sorted(pinned_ips)}"
        )
    return True, ""


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10).

    白名单写入当前 context 的 ContextVar,因此不同 async 上下文可拥有各自独立的
    白名单,避免多实例共享同一进程时互相覆盖。函数签名保持向后兼容。
    """
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks_var.set(tuple(nets))


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses to their IPv4 form.

    ``::ffff:127.0.0.1`` is semantically identical to ``127.0.0.1`` but
    Python's ipaddress treats it as an IPv6Address that matches neither
    ``127.0.0.0/8`` nor ``::1/128``.  Converting it to IPv4 ensures
    blocklist/allowlist checks work correctly.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_hard_blocked(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return True for networks that must never be reachable regardless of
    operator whitelists (cloud metadata, loopback)."""
    normalized = _normalize_addr(addr)
    return any(normalized in net for net in _HARD_BLOCKED_NETWORKS)


def _is_metadata_blocked(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return True for cloud metadata / link-local networks only.

    Used by ``allow_private`` out-bound clients (e.g. local MCP servers):
    loopback and RFC1918 stay reachable, IMDS endpoints never do.
    """
    normalized = _normalize_addr(addr)
    return any(normalized in net for net in _METADATA_NETWORKS)


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_addr(addr)
    # Hard-blocked networks (cloud metadata, loopback) can never be bypassed
    # by the SSRF whitelist — this prevents an operator mistake from exposing
    # IMDS endpoints to server-side fetches.
    if _is_hard_blocked(normalized):
        return True
    # 从当前 context 读取白名单(避免多实例共享进程时互相覆盖)
    allowed = _allowed_networks_var.get()
    if allowed and any(normalized in net for net in allowed):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def validate_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    ``allow_loopback`` is intentionally narrow: it only permits literal
    loopback hosts (localhost, 127.0.0.0/8, ::1) when every resolved address is
    loopback. It does not allow RFC1918, link-local, metadata, or public DNS
    names that happen to resolve to loopback.

    Returns (ok, error_message).  When ok is True, error_message is empty.

    解析成功后会把 hostname → IP 集合写入 ContextVar(``_pinned_dns_var``),
    供 ``_ssrf_request_hook`` 在 dial 前比对,防御 DNS rebinding。
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        # 即使是 loopback,也要 pin DNS,防止后续被改写为非 loopback
        _pin_dns_resolution(hostname, [str(a) for a in addrs])
        return True, ""
    for addr in addrs:
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    # 解析通过校验,把 hostname → IP 集合写入 pin 表,供 _ssrf_request_hook 比对
    _pin_dns_resolution(hostname, [str(a) for a in addrs])
    return True, ""


async def validate_url_target_async(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """``validate_url_target`` 的异步版本。

    DNS 解析(``socket.getaddrinfo``)通过 ``asyncio.to_thread`` 放到线程池
    执行,避免阻塞事件循环。供 web_fetch、channel 媒体下载等异步代码路径
    使用;逻辑与同步版本一致,调用方可逐步从 ``validate_url_target`` 迁移
    到本函数。同步调用方(如 ``contains_internal_url``)继续使用原函数。

    解析成功后同样写入 DNS pin,防御 DNS rebinding。
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        # 同步 getaddrinfo 放到线程池执行,避免阻塞事件循环
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        _pin_dns_resolution(hostname, [str(a) for a in addrs])
        return True, ""
    for addr in addrs:
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    _pin_dns_resolution(hostname, [str(a) for a in addrs])
    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS.

    Fail-closed: unparsable URLs, non-http(s) schemes, missing hostnames and
    DNS resolution failures are all rejected — a security validator must not
    silently allow a target it could not inspect.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, f"invalid URL: {e}"

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False, f"Cannot resolve hostname: {hostname}"
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def contains_internal_url(command: str, *, allow_loopback: bool = False) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address.

    覆盖两种形式:
    1. 显式 ``http://`` / ``https://`` URL(原行为)。
    2. 已知 HTTP 客户端(curl/wget 等)的无 scheme 主机/IP 参数 —— 这些工具
       会默认补 ``http://``,因此 ``curl example.com`` 与 ``curl http://example.com``
       等价,必须进入同一 ``validate_url_target`` 流程,否则可绕过 SSRF 检查。
    """
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True

    # 检测已知 HTTP 客户端的无 scheme 参数。通过简单的 token 分词识别命令名,
    # 后续非选项 token(不以 ``-`` 开头)若匹配域名/IPv4 形式,补 ``http://``
    # 后再次校验。这样可避免对任意包含点号的命令(如 ``git config user.name``)
    # 产生误报。
    for target in _extract_schemeless_http_targets(command):
        candidate = f"http://{target}"
        ok, _ = validate_url_target(candidate, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


def _extract_schemeless_http_targets(command: str) -> list[str]:
    """从命令中提取已知 HTTP 客户端(curl/wget 等)的无 scheme URL 参数。

    只在这些客户端的命令上下文中提取,避免对 ``git config user.email`` 等无关
    命令误报。返回的 target 不含 scheme,调用方负责补全。
    """
    targets: list[str] = []
    # 按空白/管道/重定向分词,识别命令名位置(管道后的第一个 token 也是命令)。
    # 简单分词已足够:我们只需要在 curl/wget 之后找到非选项的 host-like token。
    tokens = re.split(r"[\s|;&<>`()\[\]{}]+", command)
    seen_client = False
    for tok in tokens:
        if not tok:
            seen_client = False
            continue
        # 命令名:取 basename(去路径前缀),小写比对。
        bare = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if bare in _HTTP_CLIENT_BINARIES:
            seen_client = True
            continue
        # 遇到新的命令分隔(另一个非选项 token 但不是 client)时,停止当前 client 上下文。
        if not seen_client:
            continue
        # 选项标志(-x / --option)跳过,但选项值不在此简单解析范围内;
        # 误校验选项值只会产生 false positive(更安全),可接受。
        if tok.startswith("-"):
            continue
        # 检查是否是 host-like target(域名或 IPv4)。
        if _SCHEMELESS_TARGET_RE.fullmatch(tok) or _SCHEMELESS_TARGET_RE.match(tok):
            # 取匹配到的 host 部分(可能含 :port / path)
            m = _SCHEMELESS_TARGET_RE.match(tok)
            if m:
                targets.append(m.group(0))
        # 一个 client 命令可能有多个 URL 参数(罕见),继续扫描。
    return targets


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False


def _ssrf_request_hook(proxy: str | None, *, allow_private: bool = False) -> Any:
    """Build an httpx request event hook that blocks SSRF at dial time.

    Defence-in-depth complement to the pre-flight ``validate_url_target``
    call in ``_get_with_safe_redirects`` / ``_stream_with_safe_redirects``.

    - For IP-literal hosts: always checked against the private/internal
      blocklist (this is the path that catches a re-bound dial even when
      a proxy is configured).
    - For hostname hosts without a proxy: re-validated via
      ``validate_url_target_async`` so any redirect followed by httpx
      internally is also covered.异步版本通过 ``asyncio.to_thread`` 在线程池
      执行 DNS 解析,避免阻塞事件循环。
    - For hostname hosts with a proxy: the proxy resolves DNS (matches
      Reasonix's behaviour for GFW-friendly operation); we skip local
      resolution to avoid leaking the queried hostname through a
      side-channel DNS lookup.
    - DNS rebinding 防护:无论是否有代理,只要 hostname 在 pin 表中
      (即之前被 validate_url_target 校验过),dial 前会重新解析并比对
      IP 集合。如果出现新 IP,拒绝请求。

    ``allow_private=True`` 放宽为仅封锁云元数据网段(169.254.0.0/16、
    fe80::/10),允许回环/RFC1918 私网地址 —— 供用户显式配置了本地/局域网
    服务端点的出站客户端使用(如本地 MCP server);重定向到云元数据端点
    依然被拦。
    """

    async def _hook(request: httpx.Request) -> None:
        host = request.url.host
        if not host:
            return
        # IP-literal hosts are checked directly regardless of proxy.
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            # hostname host
            if not proxy:
                if allow_private:
                    ok, err = await _validate_not_metadata_async(str(request.url))
                else:
                    # 使用异步版本,避免同步 getaddrinfo 阻塞事件循环
                    ok, err = await validate_url_target_async(str(request.url))
                if not ok:
                    raise httpx.ConnectError(
                        f"SSRF blocked: {err}",
                        request=request,
                    )
            # DNS rebinding 防护:重新解析 hostname 并比对 pin 集合。
            # 即使配置了代理(proxy 模式下 validate_url_target_async 被跳过),
            # 也执行 pin 检查 — 但 pin 表只在 validate_url_target 写入,
            # 代理场景下无 pin 记录,_check_dns_pin 会返回 (True, "") 不强制。
            pin_ok, pin_err = await _check_dns_pin_async(host)
            if not pin_ok:
                raise httpx.ConnectError(
                    f"SSRF blocked: {pin_err}",
                    request=request,
                )
            return
        blocked = _is_metadata_blocked(addr) if allow_private else _is_private(addr)
        if blocked:
            raise httpx.ConnectError(
                f"SSRF blocked: target {host} is a private/internal address",
                request=request,
            )

    return _hook


async def _validate_not_metadata_async(url: str) -> tuple[bool, str]:
    """``allow_private`` 模式下的拨号前校验:仅拒绝云元数据/链路本地网段。

    与 ``validate_url_target_async`` 的区别:允许回环/RFC1918 等私网地址
    (用户显式配置的本地服务端点),但云元数据(169.254.0.0/16、fe80::/10)
    目标在任何模式下都不可达。
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)
    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_metadata_blocked(addr):
            return False, f"Blocked: {hostname} resolves to blocked address {addr}"
    return True, ""


async def _check_dns_pin_async(hostname: str) -> tuple[bool, str]:
    """重新解析 hostname 并比对 pin 集合(异步版本)。

    如果 hostname 不在 pin 表中或 pin 已过期,返回 (True, "")。
    如果重新解析得到的 IP 集合包含 pin 表中没有的新 IP,返回 (False, 原因)。
    """
    pinned = _pinned_dns_var.get()
    if not pinned or hostname.lower() not in pinned:
        return True, ""
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        # pin 检查 fail-closed:hostname 已被 pin(先前校验过可解析),dial 前
        # 却无法解析 —— DNS 被改写或链路异常,拒绝请求而非放行
        return False, f"DNS pin check failed: cannot resolve {hostname}"
    current_ips: list[str] = []
    for info in infos:
        try:
            current_ips.append(info[4][0])
        except (IndexError, ValueError):
            continue
    return _check_dns_pin(hostname, current_ips)


def create_ssrf_safe_client(
    *,
    proxy: str | None = None,
    timeout: float | httpx.Timeout = 10.0,
    allow_private: bool = False,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient that blocks SSRF attacks on every request.

    Installs a request event hook (Reasonix ``ssrfGuardedTransport`` style)
    that re-validates each outbound request's target IP just before dial.
    This catches redirect-based SSRF that httpx might follow internally, on
    top of the explicit redirect-safe wrappers used by WebFetchTool.

    When ``proxy`` is set, hostnames are forwarded to the proxy for DNS
    resolution (no local lookup, GFW-friendly) but IP-literal hosts are
    still blocked client-side — matching Reasonix's IP-literal check path.

    When ``allow_private`` is True, only cloud metadata / link-local targets
    (169.254.0.0/16, fe80::/10) are blocked; loopback and RFC1918 stay
    reachable. Use this ONLY for clients whose targets are explicitly
    configured by the operator (e.g. local MCP servers) — never for
    fetching model-controlled URLs.

    SSRF 白名单通过 ``configure_ssrf_whitelist`` 设置到当前 context 的
    ContextVar(``_allowed_networks_var``);本工厂创建的 client 在每次请求
    时通过 ``_is_private`` 读取当前 async 上下文的白名单。同一进程内不同
    实例可拥有各自独立的白名单,互不覆盖。
    """
    hook = _ssrf_request_hook(proxy, allow_private=allow_private)
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        event_hooks={"request": [hook]},
        **kwargs,
    )
