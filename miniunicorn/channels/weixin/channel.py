"""Personal WeChat (微信) channel using HTTP long-poll API.

Uses the ilinkai.weixin.qq.com API for personal WeChat messaging.
No WebSocket, no local WeChat client needed — just HTTP requests with a
bot token obtained via QR code login.

Protocol reverse-engineered from ``@tencent-weixin/openclaw-weixin`` v1.0.3.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.channels.base import BaseChannel
from miniunicorn.channels.weixin.api_client import (
    BASE_INFO,
    ILINK_APP_CLIENT_VERSION,  # noqa: F401  — re-exported via __init__
    ILINK_APP_ID,  # noqa: F401  — re-exported via __init__
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    MESSAGE_STATE_FINISH,
    MESSAGE_TYPE_BOT,
    WEIXIN_CHANNEL_VERSION,  # noqa: F401  — re-exported via __init__
    WeixinApiClient,
)
from miniunicorn.channels.weixin.crypto import (
    decrypt_aes_ecb as _decrypt_aes_ecb,  # noqa: F401  — re-exported via __init__
)
from miniunicorn.channels.weixin.crypto import (
    encrypt_aes_ecb as _encrypt_aes_ecb,  # noqa: F401  — re-exported via __init__
)
from miniunicorn.channels.weixin.crypto import (
    parse_aes_key as _parse_aes_key,  # noqa: F401  — re-exported via __init__
)
from miniunicorn.channels.weixin.crypto import (
    pkcs7_unpad_safe as _pkcs7_unpad_safe,  # noqa: F401  — re-exported via __init__
)
from miniunicorn.channels.weixin.media import (
    WeixinMediaService,
    _has_downloadable_media_locator,
)
from miniunicorn.config.paths import get_media_dir, get_runtime_subdir
from miniunicorn.config.schema import Base
from miniunicorn.utils.helpers import split_message

# Retry / timing constants (matching the reference plugin's monitor.ts)
ERRCODE_SESSION_EXPIRED = -14
SESSION_PAUSE_DURATION_S = 60 * 60
CONTEXT_TOKEN_MAX_AGE_S = 60
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2
MAX_QR_REFRESH_COUNT = 3
TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2
TYPING_TICKET_TTL_S = 24 * 60 * 60
TYPING_KEEPALIVE_INTERVAL_S = 5
CONFIG_CACHE_INITIAL_RETRY_S = 2
CONFIG_CACHE_MAX_RETRY_S = 60 * 60
DEFAULT_LONG_POLL_TIMEOUT_S = 35
WEIXIN_MAX_MESSAGE_LEN = 4000


class WeixinConfig(Base):
    """Personal WeChat channel configuration."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list)
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    route_tag: str | int | None = None
    token: str = ""  # Manually set token, or obtained via QR login
    state_dir: str = ""  # Default: ~/.miniunicorn/weixin/
    poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_S  # seconds for long-poll
    # Default on: WeChat iLink has no native incremental delivery (send_delta is
    # buffered and the final answer is still sent in one shot), so streaming has
    # zero user-facing effect here — it only switches the LLM call to the
    # streaming API. That avoids upstream Anthropic relays that drop tool_use
    # id/name/input on the non-streaming Messages path (a common third-party
    # relay bug). Set to false only if a relay's streaming/SSE path is broken.
    streaming: bool = True


class WeixinChannel(BaseChannel):
    """Personal WeChat channel using HTTP long-poll via ilinkai.weixin.qq.com."""

    name = "weixin"
    display_name = "WeChat"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WeixinConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WeixinConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WeixinConfig = config

        self._client: httpx.AsyncClient | None = None
        self._get_updates_buf: str = ""
        self._context_tokens: dict[str, str] = {}  # from_user_id -> context_token
        self._state_dir: Path | None = None
        self._token: str = ""
        self._poll_task: asyncio.Task | None = None
        self._next_poll_timeout_s: int = DEFAULT_LONG_POLL_TIMEOUT_S
        self._session_pause_until: float = 0.0
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._typing_tickets: dict[str, dict[str, Any]] = {}
        self._context_token_at: dict[str, float] = {}
        self._pending_tool_hints: dict[str, list[str]] = {}
        self._stream_buffers: dict[str, list[str]] = {}
        self._api: WeixinApiClient | None = None
        self._media: WeixinMediaService | None = None

    # -- Service initialization --

    def _ensure_services(self) -> None:
        """Lazily build the API and media services around the current client."""
        if self._api is not None:
            return
        assert self._client is not None
        self._api = WeixinApiClient(
            client=self._client,
            base_url=self.config.base_url,
            token_getter=lambda: self._token,
            route_tag=self.config.route_tag,
        )
        self._media = WeixinMediaService(
            api_client=self._api,
            cdn_client=self._client,
            cdn_base_url=self.config.cdn_base_url,
            media_dir=get_media_dir("weixin"),
        )

    # -- State persistence --

    def _get_state_dir(self) -> Path:
        if self._state_dir:
            return self._state_dir
        if self.config.state_dir:
            d = Path(self.config.state_dir).expanduser()
        else:
            d = get_runtime_subdir("weixin")
        d.mkdir(parents=True, exist_ok=True)
        self._state_dir = d
        return d

    def _load_state(self) -> bool:
        """Load saved account state. Returns True if a valid token was found."""
        state_file = self._get_state_dir() / "account.json"
        if not state_file.exists():
            return False
        try:
            data = json.loads(state_file.read_text())
            self._token = data.get("token", "")
            self._get_updates_buf = data.get("get_updates_buf", "")
            context_tokens = data.get("context_tokens", {})
            if isinstance(context_tokens, dict):
                self._context_tokens = {
                    str(user_id): str(token)
                    for user_id, token in context_tokens.items()
                    if str(user_id).strip() and str(token).strip()
                }
            else:
                self._context_tokens = {}
            typing_tickets = data.get("typing_tickets", {})
            if isinstance(typing_tickets, dict):
                self._typing_tickets = {
                    str(user_id): ticket
                    for user_id, ticket in typing_tickets.items()
                    if str(user_id).strip() and isinstance(ticket, dict)
                }
            else:
                self._typing_tickets = {}
            base_url = data.get("base_url", "")
            if base_url:
                self.config.base_url = base_url
            return bool(self._token)
        except Exception:
            self.logger.error("Failed to load Weixin account state", exc_info=True)
            return False

    def _save_state(self) -> None:
        state_file = self._get_state_dir() / "account.json"
        with suppress(Exception):
            data = {
                "token": self._token,
                "get_updates_buf": self._get_updates_buf,
                "context_tokens": self._context_tokens,
                "typing_tickets": self._typing_tickets,
                "base_url": self.config.base_url,
            }
            state_file.write_text(json.dumps(data, ensure_ascii=False))

    # -- HTTP helpers (delegated to WeixinApiClient) --

    async def _api_get(
        self,
        endpoint: str,
        params: dict | None = None,
        *,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        self._ensure_services()
        assert self._api is not None
        return await self._api.get(endpoint, params, auth=auth, extra_headers=extra_headers)

    async def _api_get_with_base(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict | None = None,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        self._ensure_services()
        assert self._api is not None
        return await self._api.get_with_base(
            base_url=base_url,
            endpoint=endpoint,
            params=params,
            auth=auth,
            extra_headers=extra_headers,
        )

    async def _api_post(
        self, endpoint: str, body: dict | None = None, *, auth: bool = True
    ) -> dict:
        self._ensure_services()
        assert self._api is not None
        return await self._api.post(endpoint, body, auth=auth)

    def _make_headers(self, *, auth: bool = True) -> dict[str, str]:
        """Compatibility delegate to WeixinApiClient._make_headers."""
        self._ensure_services()
        assert self._api is not None
        return self._api._make_headers(auth=auth)

    # -- QR Code Login (matches login-qr.ts) --

    async def _fetch_qr_code(self) -> tuple[str, str]:
        """Fetch a fresh QR code. Returns (qrcode_id, scan_url)."""
        data = await self._api_get(
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            auth=False,
        )
        qrcode_img_content = data.get("qrcode_img_content", "")
        qrcode_id = data.get("qrcode", "")
        if not qrcode_id:
            raise RuntimeError(f"Failed to get QR code from WeChat API: {data}")
        return qrcode_id, (qrcode_img_content or qrcode_id)

    async def _qr_login(self) -> bool:
        """Perform QR code login flow. Returns True on success."""
        try:
            refresh_count = 0
            qrcode_id, scan_url = await self._fetch_qr_code()
            self._print_qr_code(scan_url)
            current_poll_base_url = self.config.base_url

            while self._running:
                try:
                    status_data = await self._api_get_with_base(
                        base_url=current_poll_base_url,
                        endpoint="ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode_id},
                        auth=False,
                    )
                except Exception as e:
                    if self._is_retryable_qr_poll_error(e):
                        await asyncio.sleep(1)
                        continue
                    raise

                if not isinstance(status_data, dict):
                    await asyncio.sleep(1)
                    continue

                status = status_data.get("status", "")
                if status == "confirmed":
                    token = status_data.get("bot_token", "")
                    bot_id = status_data.get("ilink_bot_id", "")
                    base_url = status_data.get("baseurl", "")
                    user_id = status_data.get("ilink_user_id", "")
                    if token:
                        self._token = token
                        if base_url:
                            self.config.base_url = base_url
                        self._save_state()
                        self.logger.info(
                            "login successful! bot_id={} user_id={}",
                            bot_id,
                            user_id,
                        )
                        return True
                    else:
                        self.logger.error("Login confirmed but no bot_token in response")
                        return False
                elif status == "scaned_but_redirect":
                    redirect_host = str(status_data.get("redirect_host", "") or "").strip()
                    if redirect_host:
                        if redirect_host.startswith("http://") or redirect_host.startswith(
                            "https://"
                        ):
                            redirected_base = redirect_host
                        else:
                            redirected_base = f"https://{redirect_host}"
                        if redirected_base != current_poll_base_url:
                            current_poll_base_url = redirected_base
                elif status == "expired":
                    refresh_count += 1
                    if refresh_count > MAX_QR_REFRESH_COUNT:
                        self.logger.warning(
                            "QR code expired too many times ({}/{}), giving up.",
                            refresh_count - 1,
                            MAX_QR_REFRESH_COUNT,
                        )
                        return False
                    qrcode_id, scan_url = await self._fetch_qr_code()
                    current_poll_base_url = self.config.base_url
                    self._print_qr_code(scan_url)
                    continue
                # status == "wait" — keep polling

                await asyncio.sleep(1)

        except Exception:
            self.logger.exception("QR login failed")

        return False

    @staticmethod
    def _is_retryable_qr_poll_error(err: Exception) -> bool:
        if isinstance(err, httpx.TimeoutException | httpx.TransportError):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            status_code = err.response.status_code if err.response is not None else 0
            if status_code >= 500:
                return True
        return False

    @staticmethod
    def _print_qr_code(url: str) -> None:
        try:
            import qrcode as qr_lib

            qr = qr_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"\nLogin URL: {url}\n")

    # -- Channel lifecycle --

    async def login(self, force: bool = False) -> bool:
        """Perform QR code login and save token. Returns True on success."""
        if force:
            self._token = ""
            self._get_updates_buf = ""
            state_file = self._get_state_dir() / "account.json"
            if state_file.exists():
                state_file.unlink()
        if self._token or self._load_state():
            return True

        # Initialize HTTP client for the login flow
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=30),
            follow_redirects=True,
        )
        self._running = True  # Enable polling loop in _qr_login()
        try:
            return await self._qr_login()
        finally:
            self._running = False
            if self._client:
                await self._client.aclose()
                self._client = None
            self._api = None
            self._media = None

    async def start(self) -> None:
        self._running = True
        self._next_poll_timeout_s = self.config.poll_timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._next_poll_timeout_s + 10, connect=30),
            follow_redirects=True,
        )

        if self.config.token:
            self._token = self.config.token
        elif not self._load_state():
            if not await self._qr_login():
                self.logger.error(
                    "login failed. Run 'miniunicorn channels login weixin' to authenticate."
                )
                self._running = False
                return

        self.logger.info("channel starting with long-poll...")

        consecutive_failures = 0
        while self._running:
            try:
                await self._poll_once()
                consecutive_failures = 0
            except httpx.TimeoutException:
                # Normal for long-poll, just retry
                continue
            except Exception:
                if not self._running:
                    break
                self.logger.exception("WeChat poll loop error")
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    await asyncio.sleep(BACKOFF_DELAY_S)
                else:
                    await asyncio.sleep(RETRY_DELAY_S)

    async def stop(self) -> None:
        self._running = False
        self._pending_tool_hints.clear()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        for chat_id in list(self._typing_tasks):
            await self._stop_typing(chat_id, clear_remote=False)
        if self._client:
            await self._client.aclose()
            self._client = None
        self._api = None
        self._media = None
        self._save_state()

    # -- Polling (matches monitor.ts monitorWeixinProvider) --

    def _pause_session(self, duration_s: int = SESSION_PAUSE_DURATION_S) -> None:
        self._session_pause_until = time.time() + duration_s

    def _session_pause_remaining_s(self) -> int:
        remaining = int(self._session_pause_until - time.time())
        if remaining <= 0:
            self._session_pause_until = 0.0
            return 0
        return remaining

    def _assert_session_active(self) -> None:
        remaining = self._session_pause_remaining_s()
        if remaining > 0:
            remaining_min = max((remaining + 59) // 60, 1)
            raise RuntimeError(
                f"WeChat session paused, {remaining_min} min remaining (errcode {ERRCODE_SESSION_EXPIRED})"
            )

    async def _poll_once(self) -> None:
        remaining = self._session_pause_remaining_s()
        if remaining > 0:
            await asyncio.sleep(remaining)
            return

        body: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": BASE_INFO,
        }

        # Adjust httpx timeout to match the current poll timeout
        assert self._client is not None
        self._client.timeout = httpx.Timeout(self._next_poll_timeout_s + 10, connect=30)

        data = await self._api_post("ilink/bot/getupdates", body)

        # Check for API-level errors (monitor.ts checks both ret and errcode)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)

        is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

        if is_error:
            if errcode == ERRCODE_SESSION_EXPIRED or ret == ERRCODE_SESSION_EXPIRED:
                self._pause_session()
                remaining = self._session_pause_remaining_s()
                self.logger.warning(
                    "session expired (errcode {}). Pausing {} min.",
                    errcode,
                    max((remaining + 59) // 60, 1),
                )
                return
            raise RuntimeError(
                f"getUpdates failed: ret={ret} errcode={errcode} errmsg={data.get('errmsg', '')}"
            )

        # Honour server-suggested poll timeout (monitor.ts:102-105)
        server_timeout_ms = data.get("longpolling_timeout_ms")
        if server_timeout_ms and server_timeout_ms > 0:
            self._next_poll_timeout_s = max(server_timeout_ms // 1000, 5)

        # Update cursor
        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._save_state()

        # Process messages (WeixinMessage[] from types.ts)
        msgs: list[dict] = data.get("msgs", []) or []
        for msg in msgs:
            try:
                await self._process_message(msg)
            except Exception:
                self.logger.exception("Failed to process WeChat message")

    # -- Inbound message processing (matches inbound.ts + process-message.ts) --

    async def _process_message(self, msg: dict) -> None:
        """Process a single WeixinMessage from getUpdates."""
        # Skip bot's own messages (message_type 2 = BOT)
        if msg.get("message_type") == MESSAGE_TYPE_BOT:
            return

        msg_id = str(msg.get("message_id", "") or msg.get("seq", ""))
        if not msg_id:
            msg_id = f"{msg.get('from_user_id', '')}_{msg.get('create_time_ms', '')}"

        from_user_id = msg.get("from_user_id", "") or ""
        if not from_user_id:
            return

        # Deduplication by message_id
        if not self._dedup_message(msg_id):
            return

        ctx_token = msg.get("context_token", "")
        if not self.is_allowed(from_user_id):
            if from_user_id.endswith("@chatroom"):
                await self._handle_message(
                    sender_id=from_user_id,
                    chat_id=from_user_id,
                    content="",
                    metadata={"message_id": msg_id},
                    is_dm=False,
                )
                return

            if not ctx_token:
                self.logger.warning(
                    "Access denied for sender {}; cannot send WeChat pairing code without context_token",
                    from_user_id,
                )
                return

            had_ctx_token = from_user_id in self._context_tokens
            previous_ctx_token = self._context_tokens.get(from_user_id, "")
            had_ctx_token_at = from_user_id in self._context_token_at
            previous_ctx_token_at = self._context_token_at.get(from_user_id, 0.0)
            self._context_tokens[from_user_id] = ctx_token
            self._context_token_at[from_user_id] = time.time()
            try:
                await self._handle_message(
                    sender_id=from_user_id,
                    chat_id=from_user_id,
                    content="",
                    metadata={"message_id": msg_id},
                    is_dm=True,
                )
            finally:
                if had_ctx_token:
                    self._context_tokens[from_user_id] = previous_ctx_token
                else:
                    self._context_tokens.pop(from_user_id, None)
                if had_ctx_token_at:
                    self._context_token_at[from_user_id] = previous_ctx_token_at
                else:
                    self._context_token_at.pop(from_user_id, None)
            return

        # Cache context_token (required for all replies — inbound.ts:23-27)
        if ctx_token:
            self._context_tokens[from_user_id] = ctx_token
            self._context_token_at[from_user_id] = time.time()
            self._save_state()

        # Parse item_list (WeixinMessage.item_list — types.ts:161)
        item_list: list[dict] = msg.get("item_list") or []
        content_parts: list[str] = []
        media_paths: list[str] = []
        has_top_level_downloadable_media = False

        for item in item_list:
            item_type = item.get("type", 0)

            if item_type == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if text:
                    # Handle quoted/ref messages (inbound.ts:86-98)
                    ref = item.get("ref_msg")
                    if ref:
                        ref_item = ref.get("message_item")
                        # If quoted message is media, just pass the text
                        if ref_item and ref_item.get("type", 0) in (
                            ITEM_IMAGE,
                            ITEM_VOICE,
                            ITEM_FILE,
                            ITEM_VIDEO,
                        ):
                            content_parts.append(text)
                        else:
                            parts: list[str] = []
                            if ref.get("title"):
                                parts.append(ref["title"])
                            if ref_item:
                                ref_text = (ref_item.get("text_item") or {}).get("text", "")
                                if ref_text:
                                    parts.append(ref_text)
                            if parts:
                                content_parts.append(f"[引用: {' | '.join(parts)}]\n{text}")
                            else:
                                content_parts.append(text)
                    else:
                        content_parts.append(text)

            elif item_type == ITEM_IMAGE:
                image_item = item.get("image_item") or {}
                if _has_downloadable_media_locator(image_item.get("media")):
                    has_top_level_downloadable_media = True
                file_path = await self._download_media_item(image_item, "image")
                if file_path:
                    content_parts.append(f"[image]\n[Image: source: {file_path}]")
                    media_paths.append(file_path)
                else:
                    content_parts.append("[image]")

            elif item_type == ITEM_VOICE:
                voice_item = item.get("voice_item") or {}
                # Voice-to-text provided by WeChat (inbound.ts:101-103)
                voice_text = voice_item.get("text", "")
                if voice_text:
                    content_parts.append(f"[voice] {voice_text}")
                else:
                    if _has_downloadable_media_locator(voice_item.get("media")):
                        has_top_level_downloadable_media = True
                    file_path = await self._download_media_item(voice_item, "voice")
                    if file_path:
                        transcription = await self.transcribe_audio(file_path)
                        if transcription:
                            content_parts.append(f"[voice] {transcription}")
                        else:
                            content_parts.append(f"[voice]\n[Audio: source: {file_path}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append("[voice]")

            elif item_type == ITEM_FILE:
                file_item = item.get("file_item") or {}
                if _has_downloadable_media_locator(file_item.get("media")):
                    has_top_level_downloadable_media = True
                file_name = file_item.get("file_name", "unknown")
                file_path = await self._download_media_item(
                    file_item,
                    "file",
                    file_name,
                )
                if file_path:
                    content_parts.append(f"[file: {file_name}]\n[File: source: {file_path}]")
                    media_paths.append(file_path)
                else:
                    content_parts.append(f"[file: {file_name}]")

            elif item_type == ITEM_VIDEO:
                video_item = item.get("video_item") or {}
                if _has_downloadable_media_locator(video_item.get("media")):
                    has_top_level_downloadable_media = True
                file_path = await self._download_media_item(video_item, "video")
                if file_path:
                    content_parts.append(f"[video]\n[Video: source: {file_path}]")
                    media_paths.append(file_path)
                else:
                    content_parts.append("[video]")

        # Fallback: when no top-level media was downloaded, try quoted/referenced media.
        # This aligns with the reference plugin behavior that checks ref_msg.message_item
        # when main item_list has no downloadable media.
        if not media_paths and not has_top_level_downloadable_media:
            ref_media_item: dict[str, Any] | None = None
            for item in item_list:
                if item.get("type", 0) != ITEM_TEXT:
                    continue
                ref = item.get("ref_msg") or {}
                candidate = ref.get("message_item") or {}
                if candidate.get("type", 0) in (ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO):
                    ref_media_item = candidate
                    break

            if ref_media_item:
                ref_type = ref_media_item.get("type", 0)
                if ref_type == ITEM_IMAGE:
                    image_item = ref_media_item.get("image_item") or {}
                    file_path = await self._download_media_item(image_item, "image")
                    if file_path:
                        content_parts.append(f"[image]\n[Image: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == ITEM_VOICE:
                    voice_item = ref_media_item.get("voice_item") or {}
                    file_path = await self._download_media_item(voice_item, "voice")
                    if file_path:
                        transcription = await self.transcribe_audio(file_path)
                        if transcription:
                            content_parts.append(f"[voice] {transcription}")
                        else:
                            content_parts.append(f"[voice]\n[Audio: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == ITEM_FILE:
                    file_item = ref_media_item.get("file_item") or {}
                    file_name = file_item.get("file_name", "unknown")
                    file_path = await self._download_media_item(file_item, "file", file_name)
                    if file_path:
                        content_parts.append(f"[file: {file_name}]\n[File: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == ITEM_VIDEO:
                    video_item = ref_media_item.get("video_item") or {}
                    file_path = await self._download_media_item(video_item, "video")
                    if file_path:
                        content_parts.append(f"[video]\n[Video: source: {file_path}]")
                        media_paths.append(file_path)

        content = "\n".join(content_parts)
        if not content:
            return

        self.logger.info(
            "inbound: from={} items={} bodyLen={}",
            from_user_id,
            ",".join(str(i.get("type", 0)) for i in item_list),
            len(content),
        )

        await self._start_typing(from_user_id, ctx_token)

        await self._handle_message(
            sender_id=from_user_id,
            chat_id=from_user_id,
            content=content,
            media=media_paths or None,
            metadata={"message_id": msg_id},
        )

    # -- Media download (delegated to WeixinMediaService) --

    async def _download_media_item(
        self,
        typed_item: dict,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """Download + AES-decrypt a media item. Returns local path or None."""
        self._ensure_services()
        assert self._media is not None
        return await self._media.download(typed_item, media_type, filename)

    # -- Outbound (matches send.ts buildTextMessageReq + sendMessageWeixin) --

    async def _get_typing_ticket(self, user_id: str, context_token: str = "") -> str:
        """Get typing ticket with per-user refresh + failure backoff cache."""
        now = time.time()
        entry = self._typing_tickets.get(user_id)
        if entry and now < float(entry.get("next_fetch_at", 0)):
            return str(entry.get("ticket", "") or "")

        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "context_token": context_token or None,
            "base_info": BASE_INFO,
        }
        data = await self._api_post("ilink/bot/getconfig", body)
        if data.get("ret", 0) == 0:
            ticket = str(data.get("typing_ticket", "") or "")
            self._typing_tickets[user_id] = {
                "ticket": ticket,
                "ever_succeeded": True,
                "next_fetch_at": now + (random.random() * TYPING_TICKET_TTL_S),
                "retry_delay_s": CONFIG_CACHE_INITIAL_RETRY_S,
            }
            return ticket

        prev_delay = (
            float(entry.get("retry_delay_s", CONFIG_CACHE_INITIAL_RETRY_S))
            if entry
            else CONFIG_CACHE_INITIAL_RETRY_S
        )
        next_delay = min(prev_delay * 2, CONFIG_CACHE_MAX_RETRY_S)
        if entry:
            entry["next_fetch_at"] = now + next_delay
            entry["retry_delay_s"] = next_delay
            return str(entry.get("ticket", "") or "")

        self._typing_tickets[user_id] = {
            "ticket": "",
            "ever_succeeded": False,
            "next_fetch_at": now + CONFIG_CACHE_INITIAL_RETRY_S,
            "retry_delay_s": CONFIG_CACHE_INITIAL_RETRY_S,
        }
        return ""

    async def _refresh_context_token_if_stale(self, chat_id: str, context_token: str) -> str:
        """Return a fresh context_token if the cached one is too old."""
        if not context_token:
            return context_token

        now = time.time()
        cached_at = self._context_token_at.get(chat_id, 0)
        age = now - cached_at

        if age < CONTEXT_TOKEN_MAX_AGE_S:
            return context_token

        self.logger.debug(
            "WeChat context_token for {} is {:.0f}s old; refreshing via getconfig",
            chat_id,
            age,
        )

        body: dict[str, Any] = {
            "ilink_user_id": chat_id,
            "context_token": context_token,
            "base_info": BASE_INFO,
        }
        try:
            data = await self._api_post("ilink/bot/getconfig", body)
        except Exception as e:
            self.logger.warning("WeChat getconfig failed for {}: {}", chat_id, e)
            return context_token

        if data.get("ret", 0) != 0:
            self.logger.warning(
                "WeChat getconfig returned ret={} for {}: {}",
                data.get("ret"),
                chat_id,
                data.get("errmsg", ""),
            )
            return context_token

        new_token = str(data.get("context_token", "") or "")
        if new_token and new_token != context_token:
            self.logger.info(
                "WeChat context_token refreshed for {} (age {:.0f}s -> fresh)",
                chat_id,
                age,
            )
            self._context_tokens[chat_id] = new_token
            self._context_token_at[chat_id] = now
            self._save_state()
            return new_token

        return context_token

    async def _flush_tool_hints(self, chat_id: str) -> None:
        """Send buffered tool hints as one message (rate-limit coalescing)."""
        hints = self._pending_tool_hints.pop(chat_id, None)
        if not hints:
            return

        self.logger.info(
            "Flushing {} buffered tool hint(s) for {}",
            len(hints),
            chat_id,
        )

        ctx_token = self._context_tokens.get(chat_id, "")
        ctx_token = await self._refresh_context_token_if_stale(chat_id, ctx_token)
        if not ctx_token:
            self.logger.warning(
                "Dropped {} buffered tool hint(s) for {}: no context_token",
                len(hints),
                chat_id,
            )
            return

        try:
            await self._send_text(chat_id, "\n\n".join(hints), ctx_token)
        except Exception:
            self.logger.exception("Failed to flush buffered tool hints for {}", chat_id)

    async def _send_typing(self, user_id: str, typing_ticket: str, status: int) -> None:
        """Best-effort sendtyping wrapper."""
        if not typing_ticket:
            return
        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": BASE_INFO,
        }
        await self._api_post("ilink/bot/sendtyping", body)

    async def _typing_keepalive_loop(
        self, user_id: str, typing_ticket: str, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
            if stop_event.is_set():
                break
            with suppress(Exception):
                await self._send_typing(user_id, typing_ticket, TYPING_STATUS_TYPING)

    async def send(self, msg: OutboundMessage) -> None:
        if not self._client or not self._token:
            raise RuntimeError("WeChat client not initialized or not authenticated")
        self._assert_session_active()

        progress_event = msg.metadata.get("_progress")
        is_progress = bool(progress_event)

        if progress_event and msg.metadata.get("_tool_hint"):
            if not self.send_tool_hints:
                return
            self._pending_tool_hints.setdefault(msg.chat_id, []).append(msg.content)
            self.logger.debug(
                "Buffered tool hint for {} (count={})",
                msg.chat_id,
                len(self._pending_tool_hints[msg.chat_id]),
            )
            return

        if progress_event and (
            msg.metadata.get("_reasoning_delta") or msg.metadata.get("_reasoning")
        ):
            self.logger.debug("Dropped invisible reasoning delta for {}", msg.chat_id)
            return

        content = msg.content.strip()

        if is_progress and not content and not (msg.media or []):
            self.logger.debug(
                "Skipped empty progress message for {} (no visible content)",
                msg.chat_id,
            )
            return

        await self._flush_tool_hints(msg.chat_id)

        if not is_progress:
            await self._stop_typing(msg.chat_id, clear_remote=True)

        ctx_token = self._context_tokens.get(msg.chat_id, "")
        ctx_token = await self._refresh_context_token_if_stale(msg.chat_id, ctx_token)
        if not ctx_token:
            raise RuntimeError(
                f"WeChat context_token missing for chat_id={msg.chat_id}, cannot send"
            )

        typing_ticket = ""
        with suppress(Exception):
            typing_ticket = await self._get_typing_ticket(msg.chat_id, ctx_token)

        if typing_ticket:
            with suppress(Exception):
                await self._send_typing(msg.chat_id, typing_ticket, TYPING_STATUS_TYPING)

        typing_keepalive_stop = asyncio.Event()
        typing_keepalive_task: asyncio.Task | None = None
        if typing_ticket:
            typing_keepalive_task = asyncio.create_task(
                self._typing_keepalive_loop(msg.chat_id, typing_ticket, typing_keepalive_stop)
            )

        try:
            for media_path in msg.media or []:
                try:
                    await self._send_media_file(msg.chat_id, media_path, ctx_token)
                except (httpx.TimeoutException, httpx.TransportError):
                    self.logger.opt(exception=True).warning(
                        "Network error sending media {}", media_path
                    )
                    raise
                except httpx.HTTPStatusError as http_err:
                    sc = http_err.response.status_code if http_err.response else 0
                    if sc >= 500:
                        self.logger.exception("Server error sending media {}", media_path)
                        raise
                    filename = Path(media_path).name
                    self.logger.exception("Failed to send media {}", media_path)
                    await self._send_text(msg.chat_id, f"[Failed to send: {filename}]", ctx_token)
                except Exception:
                    filename = Path(media_path).name
                    self.logger.exception("Failed to send media {}", media_path)
                    await self._send_text(msg.chat_id, f"[Failed to send: {filename}]", ctx_token)

            if not content:
                return

            chunks = split_message(content, WEIXIN_MAX_MESSAGE_LEN)
            for chunk in chunks:
                await self._send_text(msg.chat_id, chunk, ctx_token)
        except Exception:
            self.logger.exception("Error sending message")
            raise
        finally:
            if typing_keepalive_task:
                typing_keepalive_stop.set()
                typing_keepalive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_keepalive_task

            if typing_ticket and not is_progress:
                with suppress(Exception):
                    await self._send_typing(msg.chat_id, typing_ticket, TYPING_STATUS_CANCEL)

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        """Deliver a streamed reply: accumulate deltas, flush at stream end."""
        meta = metadata or {}
        if meta.get("_reasoning_delta") or meta.get("_reasoning"):
            return
        is_end = stream_end or bool(meta.get("_stream_end"))
        buffer_key = stream_id or chat_id
        if delta and not is_end:
            self._stream_buffers.setdefault(buffer_key, []).append(delta)
        if not is_end:
            return
        full = ("".join(self._stream_buffers.get(buffer_key, [])) + (delta or "")).strip()
        await self._flush_tool_hints(chat_id)
        if full:
            await self.send(OutboundMessage(channel=self.name, chat_id=chat_id, content=full))
        self._stream_buffers.pop(buffer_key, None)

    async def _start_typing(self, chat_id: str, context_token: str = "") -> None:
        """Start typing indicator immediately when a message is received."""
        if not self._client or not self._token or not chat_id:
            return
        await self._stop_typing(chat_id, clear_remote=False)
        try:
            ticket = await self._get_typing_ticket(chat_id, context_token)
            if not ticket:
                return
            await self._send_typing(chat_id, ticket, TYPING_STATUS_TYPING)
        except Exception as e:
            self.logger.debug("typing indicator start failed for {}: {}", chat_id, e)
            return

        stop_event = asyncio.Event()

        async def keepalive() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
                if stop_event.is_set():
                    break
                with suppress(Exception):
                    await self._send_typing(chat_id, ticket, TYPING_STATUS_TYPING)

        task = asyncio.create_task(keepalive())
        task._typing_stop_event = stop_event  # type: ignore[attr-defined]
        self._typing_tasks[chat_id] = task

    async def _stop_typing(self, chat_id: str, *, clear_remote: bool) -> None:
        """Stop typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            stop_event = getattr(task, "_typing_stop_event", None)
            if stop_event:
                stop_event.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if not clear_remote:
            return
        entry = self._typing_tickets.get(chat_id)
        ticket = str(entry.get("ticket", "") or "") if isinstance(entry, dict) else ""
        if not ticket:
            return
        try:
            await self._send_typing(chat_id, ticket, TYPING_STATUS_CANCEL)
        except Exception as e:
            self.logger.debug("typing clear failed for {}: {}", chat_id, e)

    async def _send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> None:
        """Send a text message matching the exact protocol from send.ts."""
        import uuid

        client_id = f"miniunicorn-{uuid.uuid4().hex[:12]}"

        item_list: list[dict] = []
        if text:
            item_list.append({"type": ITEM_TEXT, "text_item": {"text": text}})

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
        }
        if item_list:
            weixin_msg["item_list"] = item_list
        if context_token:
            weixin_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": BASE_INFO,
        }

        data = await self._api_post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send text error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )

    async def _send_media_file(
        self,
        to_user_id: str,
        media_path: str,
        context_token: str,
    ) -> None:
        """Upload a local file to WeChat CDN and send it as a media message."""
        self._ensure_services()
        assert self._media is not None
        await self._media.send_file(to_user_id, media_path, context_token)
