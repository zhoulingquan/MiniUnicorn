"""统一的频道扫码登录处理器（参考 QwenPaw QrcodeAuthHandler）。

每个支持 WebUI 扫码登录的频道实现一个具体的 ``QRCodeAuthHandler`` 并在
``QRCODE_AUTH_HANDLERS`` 注册。路由层（``channels_api.begin_channel_qr_login``
与 ``poll_channel_qr_status``）通过 channel name 路由到对应 handler。

典型流程
--------
1. ``begin_channel_qr_login(name)``
   → ``handler.fetch_qrcode(query)``
   → 返回 ``{"qrcode_image": "<base64 PNG>", "poll_token": "...", "expires_in": ...}``
2. ``poll_channel_qr_status(name, poll_token)``
   → ``handler.poll_status(poll_token, query)``
   → 返回 ``{"status": "succeeded"|"pending"|"failed", "credentials": {...}}``

所有 handler 都是无状态纯函数（仅依赖 httpx 调用第三方 HTTP 端点），
不依赖 channel 实例 —— WebUI API 层拿不到运行中的 channel 实例，因此
必须避开实例方法。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import os
import secrets
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple
from urllib.parse import quote, urlencode

import segno
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from miniunicorn.security.network import create_ssrf_safe_client, validate_url_target

from ._query import _query_first

# 项目标识，传给第三方扫码授权端点作为 source 参数（参考 QwenPaw PROJECT_NAME）。
PROJECT_NAME = "MiniUnicorn"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------


@dataclass
class QRCodeResult:
    """``fetch_qrcode`` 返回值。"""

    scan_url: str
    poll_token: str
    expires_in: int = 600
    interval: int = 5


@dataclass
class PollResult:
    """``poll_status`` 返回值。"""

    status: str  # "waiting" | "success" | "fail" | "expired"
    credentials: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class QRCodeAuthHandler(ABC):
    """频道扫码登录处理器的抽象基类。"""

    @abstractmethod
    async def fetch_qrcode(self, query: dict[str, list[str]]) -> QRCodeResult:
        """获取扫码 URL 与后续轮询用的 token。"""

    @abstractmethod
    async def poll_status(
        self, token: str, query: dict[str, list[str]]
    ) -> PollResult:
        """轮询扫码状态。成功时返回的 ``credentials`` 字段名将与本
        频道的 Config 类字段对齐，便于直接持久化。"""

    def normalize_status(self, raw: str) -> str:
        """把各平台返回的状态字符串统一到 ``waiting/success/fail/expired``。"""
        s = (raw or "").strip().lower()
        if s in {"success", "succeeded", "ok", "0"}:
            return "success"
        if s in {"expired", "expired_token", "timeout", "3"}:
            return "expired"
        if s in {"fail", "failed", "access_denied", "invalid_grant", "2"}:
            return "fail"
        # waiting / authorization_pending / slow_down / 1 / -1 / 空
        return "waiting"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def generate_qrcode_image(scan_url: str) -> str:
    """生成 base64 编码的 PNG 二维码图片（参考 QwenPaw ``segno`` 实现）。"""
    qr = segno.make(scan_url, error="M")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# AES-256-GCM 辅助（QQ 平台需要解密 secret）
# ---------------------------------------------------------------------------

_AES_KEY_LENGTH = 32  # 256 bits

# ---------------------------------------------------------------------------
# poll_token 服务端存储 —— 避免 AES key 通过 poll_token 泄露
# ---------------------------------------------------------------------------
# task_id -> {"key": aes_key, "expires_at": monotonic_ts}
# 仅在进程内存中保存（扫码登录流程短，无需跨进程持久化）。
_POLL_TOKEN_TTL = 600  # 10 分钟，覆盖 QQ 二维码 5 分钟有效期 + 轮询余量
_MAX_POLL_TOKENS = 10_000  # 与 websocket channel._MAX_ISSUED_TOKENS 对齐
# 定期清理间隔:TTL 的一半,保证过期条目在 1 个周期内被回收。
_POLL_TOKEN_CLEANUP_INTERVAL = _POLL_TOKEN_TTL // 2
# 每进程独立的 HMAC secret（fork 多进程时各自不同，对本场景无影响）。
_POLL_TOKEN_SECRET: bytes = secrets.token_bytes(32)
_poll_tokens: dict[str, dict] = {}

# 后台清理线程:惰性启动,daemon=True 不阻塞进程退出。
_poll_token_cleanup_thread: threading.Thread | None = None
_poll_token_cleanup_lock = threading.Lock()


def _purge_expired_poll_tokens() -> None:
    """清理过期的 poll token 条目，参考 ``_issued_tokens`` 模式。"""
    now = time.monotonic()
    for tid, entry in list(_poll_tokens.items()):
        if now > entry["expires_at"]:
            _poll_tokens.pop(tid, None)


def _poll_token_cleanup_loop() -> None:
    """后台清理循环:周期性调用 ``_purge_expired_poll_tokens``。

    使用 daemon 线程而非 asyncio task,因为本模块是无状态纯函数,
    没有 asyncio 生命周期可挂载。daemon=True 保证进程退出时不阻塞。
    """
    while True:
        time.sleep(_POLL_TOKEN_CLEANUP_INTERVAL)
        try:
            _purge_expired_poll_tokens()
        except Exception:
            logger.warning("poll token 后台清理失败", exc_info=True)


def _ensure_poll_token_cleanup_started() -> None:
    """惰性启动后台清理线程(只在首次创建 token 时触发)。

    使用锁避免多线程并发时重复启动。
    """
    global _poll_token_cleanup_thread
    if _poll_token_cleanup_thread is not None:
        return
    with _poll_token_cleanup_lock:
        if _poll_token_cleanup_thread is not None:
            return
        _poll_token_cleanup_thread = threading.Thread(
            target=_poll_token_cleanup_loop,
            daemon=True,
            name="qr-poll-token-cleanup",
        )
        _poll_token_cleanup_thread.start()


def _generate_bind_key() -> str:
    """生成 base64 编码的 256-bit AES key。"""
    return base64.b64encode(os.urandom(_AES_KEY_LENGTH)).decode()


def _decrypt_secret(
    encrypted_base64: str,
    key_base64: str,
    associated_data: bytes | None = None,
) -> str:
    """AES-256-GCM 解密（base64 密文）。"""
    key = base64.b64decode(key_base64)
    raw = base64.b64decode(encrypted_base64)
    if len(raw) < 28:  # 12-byte IV + 至少 16 字节 (ciphertext+tag)
        raise ValueError(f"ciphertext too short: {len(raw)} bytes (min 28)")
    iv = raw[:12]
    ciphertext_with_tag = raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext_with_tag, associated_data)
    return plaintext.decode("utf-8")


def _encode_poll_token(task_id: str, aes_key: str) -> str:
    """生成 HMAC 签名的 task_id token，AES key 仅保留在服务端内存。

    安全说明：
    - 旧实现把 ``{"task_id":..., "key": aes_key}`` base64 编码后返回给客户端，
      导致 AES key 明文泄露。新实现只把 HMAC 签名过的 ``task_id`` 暴露给客户端，
      ``aes_key`` 存到模块级 ``_poll_tokens`` 中，仅在解密成功时一次性消费。
    - HMAC 防止客户端篡改 task_id；服务端字典 TTL 清理避免内存膨胀。
    """
    _purge_expired_poll_tokens()
    # 惰性启动后台清理线程,确保即使没有后续操作,
    # 过期 token 也能被定期回收。
    _ensure_poll_token_cleanup_started()
    if len(_poll_tokens) >= _MAX_POLL_TOKENS:
        # 与 websocket channel 的 _issued_tokens 一致：超过上限直接拒绝，
        # 防止恶意调用方耗尽内存。
        raise RuntimeError("too many outstanding poll tokens")
    _poll_tokens[task_id] = {
        "key": aes_key,
        "expires_at": time.monotonic() + _POLL_TOKEN_TTL,
    }
    sig = hmac.new(_POLL_TOKEN_SECRET, task_id.encode(), hashlib.sha256).hexdigest()
    return f"{task_id}.{sig}"


def _decode_poll_token(token: str) -> Tuple[str, str]:
    """校验 HMAC 签名并从服务端内存取出 (task_id, aes_key)。

    注意：扫码轮询场景下客户端会多次调用 ``poll_status``，因此本函数
    **不**删除条目（仅校验+返回）。AES key 的真正“一次性消费”由
    ``_consume_poll_token`` 在解密成功后执行；过期条目由 TTL 清理负责。
    """
    try:
        task_id, sig = token.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError(f"invalid poll token format: {exc}") from exc

    expected = hmac.new(
        _POLL_TOKEN_SECRET, task_id.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("invalid poll token signature")

    entry = _poll_tokens.get(task_id)
    if entry is None:
        raise ValueError("poll token unknown or already consumed")
    if time.monotonic() > entry["expires_at"]:
        # 顺手清理过期项，避免堆积。
        _poll_tokens.pop(task_id, None)
        raise ValueError("poll token expired")
    return task_id, entry["key"]


def _consume_poll_token(task_id: str) -> None:
    """在解密成功后一次性删除 poll token 对应的服务端条目。

    防止同一个 token 在拿到 secret 后被重复利用（重放保护）。
    """
    _poll_tokens.pop(task_id, None)


# ---------------------------------------------------------------------------
# Feishu / Lark（device-code flow，与 channel 模块的辅助函数保持一致）
# ---------------------------------------------------------------------------


_FEISHU_ACCOUNTS_DOMAIN = "https://accounts.feishu.cn"
_LARK_ACCOUNTS_DOMAIN = "https://accounts.larksuite.com"
_FEISHU_REGISTER_ENDPOINT = "/oauth/v1/app/registration"


def _feishu_accounts_domain(domain: str) -> str:
    return _LARK_ACCOUNTS_DOMAIN if domain == "lark" else _FEISHU_ACCOUNTS_DOMAIN


class FeishuQRCodeAuthHandler(QRCodeAuthHandler):
    """飞书 / Lark device-code flow 扫码登录。

    与 QwenPaw 一致，使用 OAuth 2.0 Device Authorization Grant (RFC 8628)：
    1. action=init   → 获取支持的 auth methods
    2. action=begin  → device_code + verification_uri_complete
    3. action=poll   → 成功返回 client_id + client_secret
    """

    async def _resolve_domain(self, query: dict[str, list[str]]) -> str:
        qp_domain = (_query_first(query, "domain") or "").strip().lower()
        if qp_domain in ("feishu", "lark"):
            return qp_domain
        # fallback 到 config 中已保存的 domain
        try:
            from miniunicorn.config.loader import load_config

            cfg = load_config()
            feishu_cfg = getattr(cfg.channels, "feishu", None)
            if feishu_cfg is not None:
                domain_val = (
                    feishu_cfg.get("domain")
                    if isinstance(feishu_cfg, dict)
                    else getattr(feishu_cfg, "domain", None)
                )
                if domain_val in ("feishu", "lark"):
                    return domain_val
        except Exception:
            pass
        return "feishu"

    async def fetch_qrcode(self, query: dict[str, list[str]]) -> QRCodeResult:
        domain = await self._resolve_domain(query)
        endpoint = _feishu_accounts_domain(domain) + _FEISHU_REGISTER_ENDPOINT

        try:
            # 走 SSRF 防护客户端：拦截指向私有/loopback IP 的拨号。
            async with create_ssrf_safe_client(timeout=15) as client:
                # step 1: init
                init_resp = await client.post(
                    endpoint,
                    content=urlencode({"action": "init"}),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                init_resp.raise_for_status()
                init_data = init_resp.json()
                methods = init_data.get("supported_auth_methods", [])
                if "client_secret" not in methods:
                    raise RuntimeError(
                        f"feishu unsupported auth methods: {methods}"
                    )

                # step 2: begin
                begin_resp = await client.post(
                    endpoint,
                    content=urlencode(
                        {
                            "action": "begin",
                            "archetype": "PersonalAgent",
                            "auth_method": "client_secret",
                            "request_user_info": "open_id",
                        }
                    ),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                begin_resp.raise_for_status()
                begin_data = begin_resp.json()
        except Exception as exc:
            raise RuntimeError(f"feishu QR fetch failed: {exc}") from exc

        device_code = begin_data.get("device_code", "")
        verification_uri = begin_data.get("verification_uri_complete", "")
        if not device_code or not verification_uri:
            raise RuntimeError("feishu: missing device_code or QR URL")

        if "?" in verification_uri:
            scan_url = f"{verification_uri}&source={PROJECT_NAME}"
        else:
            scan_url = f"{verification_uri}?source={PROJECT_NAME}"

        return QRCodeResult(
            scan_url=scan_url,
            poll_token=device_code,
            expires_in=int(begin_data.get("expire_in") or 600),
            interval=int(begin_data.get("interval") or 5),
        )

    async def poll_status(
        self, token: str, query: dict[str, list[str]]
    ) -> PollResult:
        domain = await self._resolve_domain(query)
        endpoint = _feishu_accounts_domain(domain) + _FEISHU_REGISTER_ENDPOINT

        try:
            async with create_ssrf_safe_client(timeout=10) as client:
                resp = await client.post(
                    endpoint,
                    content=urlencode(
                        {"action": "poll", "device_code": token, "tp": "ob_app"}
                    ),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"feishu status check failed: {exc}") from exc

        user_info = data.get("user_info") or {}
        tenant_brand = user_info.get("tenant_brand")
        if tenant_brand == "lark":
            domain = "lark"

        if data.get("client_id") and data.get("client_secret"):
            return PollResult(
                status="success",
                credentials={
                    "app_id": data["client_id"],
                    "app_secret": data["client_secret"],
                    "domain": domain,
                },
            )

        error = data.get("error", "")
        if error in ("expired_token", "invalid_grant"):
            return PollResult(status="expired", credentials={"fail_reason": error})
        if error == "access_denied":
            return PollResult(status="fail", credentials={"fail_reason": error})
        if error and error not in ("authorization_pending", "slow_down"):
            return PollResult(status="fail", credentials={"fail_reason": error})

        return PollResult(status="waiting")


# ---------------------------------------------------------------------------
# WeChat / 微信（iLink Bot API）
# ---------------------------------------------------------------------------


class WeixinQRCodeAuthHandler(QRCodeAuthHandler):
    """个人微信 iLink Bot 扫码登录。

    端点：
      1. GET /ilink/bot/get_bot_qrcode?bot_type=3 → qrcode + qrcode_img_content
      2. GET /ilink/bot/get_qrcode_status?qrcode=<qrcode> → 状态轮询

    复用 ``miniunicorn.channels.weixin`` 中的 ``ILinkClient``（如果可用），
    否则直接走 httpx。base_url 从 query.domain 或 config 读取，默认
    ``https://ilinkai.weixin.qq.com``。
    """

    _DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

    async def _resolve_base_url(self, query: dict[str, list[str]]) -> str:
        qp_base = (_query_first(query, "base_url") or "").strip()
        if qp_base:
            return qp_base
        try:
            from miniunicorn.config.loader import load_config

            cfg = load_config()
            weixin_cfg = getattr(cfg.channels, "weixin", None)
            if weixin_cfg is not None:
                base = (
                    weixin_cfg.get("base_url")
                    if isinstance(weixin_cfg, dict)
                    else getattr(weixin_cfg, "base_url", None)
                )
                if base:
                    return base
        except Exception:
            pass
        return self._DEFAULT_BASE_URL

    async def fetch_qrcode(self, query: dict[str, list[str]]) -> QRCodeResult:
        base_url = await self._resolve_base_url(query)
        # base_url 可来自 query 参数，必须做 SSRF 校验，拒绝私有/loopback IP。
        ok, err = validate_url_target(base_url)
        if not ok:
            raise RuntimeError(f"weixin base_url blocked by SSRF guard: {err}")
        try:
            async with create_ssrf_safe_client(timeout=15) as client:
                resp = await client.get(
                    f"{base_url}/ilink/bot/get_bot_qrcode",
                    params={"bot_type": 3},
                )
                resp.raise_for_status()
                qr_data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"weixin QR fetch failed: {exc}") from exc

        qrcode = qr_data.get("qrcode", "")
        qrcode_img_content = qr_data.get("qrcode_img_content", "")
        if not qrcode and not qrcode_img_content:
            raise RuntimeError("weixin returned empty QR code data")

        if qrcode_img_content.startswith("http"):
            scan_url = qrcode_img_content
        else:
            scan_url = (
                f"https://liteapp.weixin.qq.com/q/7GiQu1"
                f"?qrcode={qrcode}&bot_type=3"
            )

        return QRCodeResult(
            scan_url=scan_url,
            poll_token=qrcode,
            expires_in=120,
            interval=3,
        )

    async def poll_status(
        self, token: str, query: dict[str, list[str]]
    ) -> PollResult:
        base_url = await self._resolve_base_url(query)
        # 同 fetch_qrcode：base_url 可被客户端覆盖，必须 SSRF 校验。
        ok, err = validate_url_target(base_url)
        if not ok:
            raise RuntimeError(f"weixin base_url blocked by SSRF guard: {err}")
        try:
            async with create_ssrf_safe_client(timeout=10) as client:
                resp = await client.get(
                    f"{base_url}/ilink/bot/get_qrcode_status",
                    params={"qrcode": token},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"weixin status check failed: {exc}") from exc

        # iLink 状态：1=等待扫码，2=已扫码待确认，3=已确认，4=已过期，5=失败
        status_code = data.get("status", 1)
        if status_code == 3:
            return PollResult(
                status="success",
                credentials={
                    "token": data.get("bot_token", ""),
                    "base_url": data.get("baseurl", base_url),
                },
            )
        if status_code == 4:
            return PollResult(status="expired", credentials={})
        if status_code == 5:
            return PollResult(
                status="fail",
                credentials={"fail_reason": data.get("msg", "wechat rejected")},
            )
        return PollResult(status="waiting")


# ---------------------------------------------------------------------------
# WeCom / 企业微信（AI Bot 扫码授权）
# ---------------------------------------------------------------------------


_WECOM_AUTH_ORIGIN = "https://work.weixin.qq.com"
_WECOM_SOURCE = PROJECT_NAME.lower()


class WecomQRCodeAuthHandler(QRCodeAuthHandler):
    """企业微信 AI Bot 扫码授权。

    流程：
      1. GET  /ai/qc/gen?source=<source>&state=<state>      → 扫码授权 HTML
      2. 解析页面 ``window.settings`` JSON → scode + auth_url
      3. GET  /ai/qc/query_result?scode=<scode>             → 轮询结果
    """

    async def fetch_qrcode(self, query: dict[str, list[str]]) -> QRCodeResult:
        import json
        import re

        state = secrets.token_urlsafe(16)
        gen_url = (
            f"{_WECOM_AUTH_ORIGIN}/ai/qc/gen"
            f"?source={_WECOM_SOURCE}&state={state}"
            f"&timestamp={int(time.time() * 1000)}"
        )

        try:
            async with create_ssrf_safe_client(timeout=15, follow_redirects=True) as client:
                resp = await client.get(gen_url)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            raise RuntimeError(f"wecom auth page fetch failed: {exc}") from exc

        settings_match = re.search(r"window\.settings\s*=\s*(\{[^<]+\})", html)
        if not settings_match:
            raise RuntimeError("failed to parse wecom auth page settings")

        try:
            settings = json.loads(settings_match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"failed to parse wecom settings JSON: {exc}") from exc

        scode = settings.get("scode", "")
        auth_url = settings.get("auth_url", "")
        if not scode or not auth_url:
            raise RuntimeError("wecom returned empty scode or auth_url")

        return QRCodeResult(
            scan_url=auth_url,
            poll_token=scode,
            expires_in=300,
            interval=3,
        )

    async def poll_status(
        self, token: str, query: dict[str, list[str]]
    ) -> PollResult:
        query_url = f"{_WECOM_AUTH_ORIGIN}/ai/qc/query_result?scode={quote(token)}"
        try:
            async with create_ssrf_safe_client(timeout=10) as client:
                resp = await client.get(query_url)
                resp.raise_for_status()
                result = resp.json()
        except Exception as exc:
            raise RuntimeError(f"wecom status check failed: {exc}") from exc

        data = result.get("data", {})
        bot_info = data.get("bot_info", {})
        status_raw = data.get("status", "waiting")

        if self.normalize_status(status_raw) == "success":
            bot_id = bot_info.get("botid", "")
            secret = bot_info.get("secret", "")
            if bot_id and secret:
                return PollResult(
                    status="success",
                    credentials={"bot_id": bot_id, "secret": secret},
                )
            return PollResult(
                status="fail",
                credentials={"fail_reason": "missing bot_id or secret"},
            )
        if self.normalize_status(status_raw) == "expired":
            return PollResult(status="expired", credentials={})
        if self.normalize_status(status_raw) == "fail":
            return PollResult(
                status="fail",
                credentials={"fail_reason": data.get("msg", "wecom rejected")},
            )
        return PollResult(status="waiting")


# ---------------------------------------------------------------------------
# DingTalk（OAuth 2.0 Device Authorization Grant）
# ---------------------------------------------------------------------------


_DINGTALK_API_BASE = "https://oapi.dingtalk.com"
_DINGTALK_SOURCE = "QWENPAW"


class DingtalkQRCodeAuthHandler(QRCodeAuthHandler):
    """钉钉 device flow 扫码注册。

    流程：
      1. POST /app/registration/init   → nonce (5 min TTL)
      2. POST /app/registration/begin  → device_code + verification_uri_complete
      3. POST /app/registration/poll   → SUCCESS 返回 client_id + client_secret
    """

    async def fetch_qrcode(self, query: dict[str, list[str]]) -> QRCodeResult:
        try:
            async with create_ssrf_safe_client(timeout=15) as client:
                init_resp = await client.post(
                    f"{_DINGTALK_API_BASE}/app/registration/init",
                    json={"source": _DINGTALK_SOURCE},
                )
                init_resp.raise_for_status()
                init_data = init_resp.json()
                if init_data.get("errcode", -1) != 0:
                    raise RuntimeError(
                        f"dingtalk init failed: {init_data.get('errmsg', 'unknown')}"
                    )
                nonce = init_data.get("nonce", "")
                if not nonce:
                    raise RuntimeError("dingtalk returned empty nonce")

                begin_resp = await client.post(
                    f"{_DINGTALK_API_BASE}/app/registration/begin",
                    json={"nonce": nonce},
                )
                begin_resp.raise_for_status()
                begin_data = begin_resp.json()
                if begin_data.get("errcode", -1) != 0:
                    raise RuntimeError(
                        f"dingtalk begin failed: {begin_data.get('errmsg', 'unknown')}"
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"dingtalk QR fetch failed: {exc}") from exc

        device_code = begin_data.get("device_code", "")
        scan_url = begin_data.get("verification_uri_complete", "")
        if not device_code or not scan_url:
            raise RuntimeError("dingtalk: missing device_code or scan URL")

        return QRCodeResult(
            scan_url=scan_url,
            poll_token=device_code,
            expires_in=300,
            interval=3,
        )

    async def poll_status(
        self, token: str, query: dict[str, list[str]]
    ) -> PollResult:
        try:
            async with create_ssrf_safe_client(timeout=10) as client:
                resp = await client.post(
                    f"{_DINGTALK_API_BASE}/app/registration/poll",
                    json={"device_code": token},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"dingtalk status check failed: {exc}") from exc

        status = (data.get("status") or "WAITING").upper()
        if status == "SUCCESS":
            return PollResult(
                status="success",
                credentials={
                    "client_id": data.get("client_id", ""),
                    "client_secret": data.get("client_secret", ""),
                },
            )
        if status == "FAIL":
            return PollResult(
                status="fail",
                credentials={"fail_reason": data.get("fail_reason", "")},
            )
        if status == "EXPIRED":
            return PollResult(status="expired", credentials={})
        return PollResult(status="waiting")


# ---------------------------------------------------------------------------
# QQ（q.qq.com bind task + AES-256-GCM 解密 secret）
# ---------------------------------------------------------------------------


class QQQRCodeAuthHandler(QRCodeAuthHandler):
    """QQ Bot 通过 q.qq.com 扫码绑定任务授权。

    流程：
      1. POST /lite/create_bind_task   → task_id（携带本地生成的 AES key）
      2. 用户访问 /qqbot/openclaw/connect.html?task_id=... 扫码授权
      3. POST /lite/poll_bind_result   → status=2 时返回 bot_appid +
         bot_encrypt_secret，用 AES key 解密得到明文 secret
    """

    # 固定为官方域名，避免通过环境变量劫持到任意主机（SSRF 防护）。
    _PORTAL_HOST: str = "q.qq.com"
    _CREATE_PATH: str = "/lite/create_bind_task"
    _POLL_PATH: str = "/lite/poll_bind_result"
    _FRONTEND_PATH: str = "/qqbot/openclaw/connect.html"

    async def fetch_qrcode(self, query: dict[str, list[str]]) -> QRCodeResult:
        aes_key = _generate_bind_key()
        url = f"https://{self._PORTAL_HOST}{self._CREATE_PATH}"

        try:
            async with create_ssrf_safe_client(timeout=15) as client:
                resp = await client.post(
                    url,
                    json={"key": aes_key},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"QQ create_bind_task failed: {exc}") from exc

        if data.get("retcode") != 0:
            raise RuntimeError(
                f"QQ create_bind_task error: {data.get('msg', '')}"
            )

        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError("QQ create_bind_task returned empty task_id")

        params = urlencode(
            {"task_id": task_id, "_wv": "2", "source": PROJECT_NAME}
        )
        scan_url = f"https://{self._PORTAL_HOST}{self._FRONTEND_PATH}?{params}"
        poll_token = _encode_poll_token(task_id, aes_key)

        return QRCodeResult(
            scan_url=scan_url,
            poll_token=poll_token,
            expires_in=300,
            interval=3,
        )

    async def poll_status(
        self, token: str, query: dict[str, list[str]]
    ) -> PollResult:
        try:
            task_id, aes_key = _decode_poll_token(token)
        except ValueError as exc:
            raise RuntimeError(f"invalid poll token: {exc}") from exc

        url = f"https://{self._PORTAL_HOST}{self._POLL_PATH}"
        try:
            async with create_ssrf_safe_client(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={"task_id": task_id},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"QQ poll_bind_result failed: {exc}") from exc

        if data.get("retcode") != 0:
            return PollResult(
                status="fail",
                credentials={"fail_reason": data.get("msg", "unknown")},
            )

        result_data = data.get("data", {}) or {}
        status = result_data.get("status", -1)

        if status == 2:
            raw_appid = result_data.get("bot_appid")
            encrypted_secret = result_data.get("bot_encrypt_secret", "")
            if not raw_appid or not encrypted_secret:
                return PollResult(
                    status="fail",
                    credentials={"fail_reason": "missing app_id or secret"},
                )
            try:
                secret = _decrypt_secret(encrypted_secret, aes_key)
            except Exception:
                return PollResult(
                    status="fail",
                    credentials={"fail_reason": "secret decryption failed"},
                )
            # AES key 已用于解密并返回明文 secret，立即销毁服务端条目，
            # 防止同一 poll_token 被重放再次取到 secret。
            _consume_poll_token(task_id)
            return PollResult(
                status="success",
                credentials={
                    "app_id": str(raw_appid),
                    "secret": secret,
                    "user_openid": str(result_data.get("user_openid", "")),
                },
            )
        if status == 3:
            return PollResult(status="expired", credentials={})
        return PollResult(status="waiting")


# ---------------------------------------------------------------------------
# 注册表 —— 新增频道在此注册
# ---------------------------------------------------------------------------

QRCODE_AUTH_HANDLERS: Dict[str, QRCodeAuthHandler] = {
    "feishu": FeishuQRCodeAuthHandler(),
    "weixin": WeixinQRCodeAuthHandler(),
    "wecom": WecomQRCodeAuthHandler(),
    "dingtalk": DingtalkQRCodeAuthHandler(),
    "qq": QQQRCodeAuthHandler(),
}


def get_qr_handler(channel: str) -> QRCodeAuthHandler | None:
    """获取指定频道的扫码登录处理器（不存在返回 None）。"""
    return QRCODE_AUTH_HANDLERS.get(channel)
