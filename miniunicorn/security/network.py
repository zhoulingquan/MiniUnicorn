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
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

# Networks that are ALWAYS blocked, even if the operator adds them to the
# SSRF whitelist via ``configure_ssrf_whitelist``.  These cover cloud metadata
# endpoints (169.254.0.0/16 — AWS/GCP/Azure IMDS) and loopback (127.0.0.0/8,
# ::1) which must never be reachable from a server-side fetch context.
_HARD_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("::1/128"),
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

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
_pinned_dns_var: contextvars.ContextVar[
    dict[str, tuple[frozenset[str], float]] | None
] = contextvars.ContextVar("_pinned_dns_var", default=None)


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


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_addr(addr)
    # Hard-blocked networks (cloud metadata, loopback) can never be bypassed
    # by the SSRF whitelist — this prevents an operator mistake from exposing
    # IMDS endpoints to server-side fetches.
    if any(normalized in net for net in _HARD_BLOCKED_NETWORKS):
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


async def validate_url_target_async(
    url: str, *, allow_loopback: bool = False
) -> tuple[bool, str]:
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
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS."""
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def contains_internal_url(command: str, *, allow_loopback: bool = False) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address."""
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


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


def _ssrf_request_hook(proxy: str | None) -> Any:
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
        if _is_private(addr):
            raise httpx.ConnectError(
                f"SSRF blocked: target {host} is a private/internal address",
                request=request,
            )

    return _hook


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
        # 解析失败时不阻止请求(让 httpx 自己报错),pin 检查只是防御层
        return True, ""
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

    SSRF 白名单通过 ``configure_ssrf_whitelist`` 设置到当前 context 的
    ContextVar(``_allowed_networks_var``);本工厂创建的 client 在每次请求
    时通过 ``_is_private`` 读取当前 async 上下文的白名单。同一进程内不同
    实例可拥有各自独立的白名单,互不覆盖。
    """
    hook = _ssrf_request_hook(proxy)
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        event_hooks={"request": [hook]},
        **kwargs,
    )
