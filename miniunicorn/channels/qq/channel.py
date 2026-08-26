"""QQ channel implementation using botpy SDK.

Inbound:
- Parse QQ botpy messages (C2C / Group)
- Download attachments to media dir using chunked streaming write (memory-safe)
- Publish to miniunicorn bus via BaseChannel._handle_message()
- Content includes a clear, actionable "Received files:" list with local paths

Outbound:
- Send attachments (msg.media) first via QQ rich media API (base64 upload + msg_type=7)
- Then send text (plain or markdown)
- msg.media supports local paths, file:// paths, and http(s) URLs

Notes:
- QQ restricts many audio/video formats. We conservatively classify as image vs file.
- Attachment structures differ across botpy versions; we try multiple field candidates.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlparse

import aiohttp
from loguru import logger
from pydantic import Field

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.channels._media_common import (
    _IMAGE_EXTS,
)
from miniunicorn.channels._media_common import (
    sanitize_filename as _sanitize_filename,
)
from miniunicorn.channels.base import BaseChannel
from miniunicorn.config.schema import Base
from miniunicorn.security.network import validate_url_target
from miniunicorn.utils.logging_bridge import redirect_lib_logging

try:
    from miniunicorn.config.paths import get_media_dir
except Exception:  # pragma: no cover
    get_media_dir = None  # type: ignore

try:
    import botpy
    from botpy.http import Route

    QQ_AVAILABLE = True
except ImportError:  # pragma: no cover
    QQ_AVAILABLE = False
    botpy = None
    Route = None

if TYPE_CHECKING:
    from botpy.message import BaseMessage, C2CMessage, GroupMessage
    from botpy.types.message import Media


# QQ rich media file_type: 1=image, 4=file
# (2=voice, 3=video are restricted; we only use image vs file)
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_FILE = 4

# _IMAGE_EXTS / _sanitize_filename 由 miniunicorn.channels._media_common 提供，
# 与 wecom / dingtalk 共享同一份实现，避免历史跨频道重复定义。


def _is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _guess_send_file_type(filename: str) -> int:
    """Conservative send type: images -> 1, else -> 4."""
    ext = Path(filename).suffix.lower()
    mime, _ = mimetypes.guess_type(filename)
    if ext in _IMAGE_EXTS or (mime and mime.startswith("image/")):
        return QQ_FILE_TYPE_IMAGE
    return QQ_FILE_TYPE_FILE


# 下载流式写入的分块大小（256KB）与单附件最大字节（200MB）。
# 与其他频道保持一致：内部常量，不暴露给用户配置。
_QQ_DOWNLOAD_CHUNK_SIZE = 1024 * 256
_QQ_DOWNLOAD_MAX_BYTES = 1024 * 1024 * 200


def _make_bot_class(channel: QQChannel) -> type[botpy.Client]:
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log — miniunicorn uses loguru; default "botpy.log" fails on read-only fs
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: C2CMessage):
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: GroupMessage):
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            await channel._on_message(message, is_group=False)

    return _Bot


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    msg_format: Literal["plain", "markdown"] = "plain"
    ack_message: str = "⏳ Processing..."


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config

        self._client: botpy.Client | None = None
        self._http: aiohttp.ClientSession | None = None
        # 保护 _http 懒初始化的异步锁:防止并发协程同时创建多个 ClientSession,
        # 导致早期创建的 session 泄漏(未关闭连接池)。start() 中的初始化在
        # 事件循环启动前完成,无需加锁;此处保护 download 路径的懒初始化。
        self._http_lock: asyncio.Lock = asyncio.Lock()

        self._msg_seq: int = 1  # used to avoid QQ API dedup
        # chat_id -> chat_type 缓存。chat_type 对同一会话基本不变, 用 OrderedDict
        # 实现 FIFO 有界缓存, 防止长生命周期网关下按 chat_id 无界累积。
        self._chat_type_cache: OrderedDict[str, str] = OrderedDict()

        self._media_root: Path = self._init_media_root()

    # ---------------------------
    # Lifecycle
    # ---------------------------

    def _init_media_root(self) -> Path:
        """Choose a directory for saving inbound attachments."""
        if get_media_dir:
            try:
                root = Path(get_media_dir("qq"))
            except Exception:
                root = Path.home() / ".miniunicorn" / "media" / "qq"
        else:
            root = Path.home() / ".miniunicorn" / "media" / "qq"

        root.mkdir(parents=True, exist_ok=True)
        self.logger.info("media directory: {}", str(root))
        return root

    async def start(self) -> None:
        """Start the QQ bot with auto-reconnect loop."""
        redirect_lib_logging("botpy", level="WARNING")
        if not QQ_AVAILABLE:
            self.logger.error("SDK not installed. Run: miniunicorn plugins enable qq")
            return

        if not self.config.app_id or not self.config.secret:
            self.logger.error("app_id and secret not configured")
            return

        self._running = True
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        self._client = _make_bot_class(self)()
        self.logger.info("bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                self.logger.warning("bot error: {}", e)
            if self._running:
                self.logger.info("Reconnecting bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop bot and cleanup resources."""
        self._running = False
        if self._client:
            with suppress(Exception):
                await self._client.close()
        self._client = None

        if self._http:
            with suppress(Exception):
                await self._http.close()
        self._http = None

        self.logger.info("bot stopped")

    # ---------------------------
    # Outbound (send)
    # ---------------------------

    def _remember_chat_type(self, chat_id: str, chat_type: str, *, max_size: int = 1024) -> None:
        """记录 chat_id 的会话类型, FIFO 有界缓存防止无界累积。"""
        self._chat_type_cache[chat_id] = chat_type
        while len(self._chat_type_cache) > max_size:
            self._chat_type_cache.popitem(last=False)

    async def send(self, msg: OutboundMessage) -> None:
        """Send attachments first, then text."""
        try:
            if not self._client:
                raise RuntimeError("QQ client not initialized")

            msg_id = msg.metadata.get("message_id")
            chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")
            is_group = chat_type == "group"

            # 1) Send media
            for media_ref in msg.media or []:
                ok = await self._send_media(
                    chat_id=msg.chat_id,
                    media_ref=media_ref,
                    msg_id=msg_id,
                    is_group=is_group,
                )
                if not ok:
                    filename = (
                        os.path.basename(urlparse(media_ref).path)
                        or os.path.basename(media_ref)
                        or "file"
                    )
                    await self._send_text_only(
                        chat_id=msg.chat_id,
                        is_group=is_group,
                        msg_id=msg_id,
                        content=f"[Attachment send failed: {filename}]",
                    )

            # 2) Send text
            if msg.content and msg.content.strip():
                await self._send_text_only(
                    chat_id=msg.chat_id,
                    is_group=is_group,
                    msg_id=msg_id,
                    content=msg.content.strip(),
                )
        except (aiohttp.ClientError, OSError):
            # Network / transport errors — propagate so ChannelManager can retry
            raise
        except Exception:
            self.logger.exception("Error sending message to chat_id={}", msg.chat_id)
            raise

    async def _send_text_only(
        self,
        chat_id: str,
        is_group: bool,
        msg_id: str | None,
        content: str,
    ) -> None:
        """Send a plain/markdown text message."""
        if not self._client:
            return

        self._msg_seq += 1
        use_markdown = self.config.msg_format == "markdown"
        payload: dict[str, Any] = {
            "msg_type": 2 if use_markdown else 0,
            "msg_id": msg_id,
            "msg_seq": self._msg_seq,
        }
        if use_markdown:
            payload["markdown"] = {"content": content}
        else:
            payload["content"] = content

        if is_group:
            await self._client.api.post_group_message(group_openid=chat_id, **payload)
        else:
            await self._client.api.post_c2c_message(openid=chat_id, **payload)

    async def _send_media(
        self,
        chat_id: str,
        media_ref: str,
        msg_id: str | None,
        is_group: bool,
    ) -> bool:
        """Read bytes -> base64 upload -> msg_type=7 send."""
        if not self._client:
            return False

        data, filename = await self._read_media_bytes(media_ref)
        if not data or not filename:
            return False

        try:
            file_type = _guess_send_file_type(filename)
            file_data_b64 = base64.b64encode(data).decode()

            media_obj = await self._post_base64file(
                chat_id=chat_id,
                is_group=is_group,
                file_type=file_type,
                file_data=file_data_b64,
                file_name=filename,
                srv_send_msg=False,
            )
            if not media_obj:
                self.logger.error("media upload failed: empty response")
                return False

            self._msg_seq += 1
            if is_group:
                await self._client.api.post_group_message(
                    group_openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )

            self.logger.info("media sent: {}", filename)
            return True
        except (aiohttp.ClientError, OSError) as e:
            # Network / transport errors — propagate for retry by caller
            self.logger.warning("send media network error filename={} err={}", filename, e)
            raise
        except Exception:
            # API-level or other non-network errors — return False so send() can fallback
            self.logger.exception("send media failed filename={}", filename)
            return False

    async def _read_media_bytes(self, media_ref: str) -> tuple[bytes | None, str | None]:
        """Read bytes from http(s) or local file path; return (data, filename)."""
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return None, None

        # Local file: plain path or file:// URI
        if not media_ref.startswith("http://") and not media_ref.startswith("https://"):
            try:
                if media_ref.startswith("file://"):
                    parsed = urlparse(media_ref)
                    # Windows: path in netloc; Unix: path in path
                    raw = parsed.path or parsed.netloc
                    local_path = Path(unquote(raw))
                else:
                    local_path = Path(os.path.expanduser(media_ref))

                if not local_path.is_file():
                    self.logger.warning("outbound media file not found: {}", str(local_path))
                    return None, None

                data = await asyncio.to_thread(local_path.read_bytes)
                return data, local_path.name
            except Exception as e:
                self.logger.warning("outbound media read error ref={} err={}", media_ref, e)
                return None, None

        # Remote URL
        ok, err = validate_url_target(media_ref)
        if not ok:
            self.logger.warning(
                "outbound media URL validation failed url={} err={}", media_ref, err
            )
            return None, None

        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        try:
            async with self._http.get(media_ref, allow_redirects=True) as resp:
                if resp.status >= 400:
                    self.logger.warning(
                        "outbound media download failed status={} url={}",
                        resp.status,
                        media_ref,
                    )
                    return None, None
                data = await resp.read()
                if not data:
                    return None, None
                filename = os.path.basename(urlparse(media_ref).path) or "file.bin"
                return data, filename
        except Exception as e:
            self.logger.warning("outbound media download error url={} err={}", media_ref, e)
            return None, None

    # https://github.com/tencent-connect/botpy/issues/198
    # https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/rich-media.html
    async def _post_base64file(
        self,
        chat_id: str,
        is_group: bool,
        file_type: int,
        file_data: str,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> Media:
        """Upload base64-encoded file and return Media object."""
        if not self._client:
            raise RuntimeError("QQ client not initialized")

        if is_group:
            endpoint = "/v2/groups/{group_openid}/files"
            id_key = "group_openid"
        else:
            endpoint = "/v2/users/{openid}/files"
            id_key = "openid"

        payload: dict[str, Any] = {
            id_key: chat_id,
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": srv_send_msg,
        }
        # Only pass file_name for non-image types (file_type=4).
        # Passing file_name for images causes QQ client to render them as
        # file attachments instead of inline images.
        if file_type != QQ_FILE_TYPE_IMAGE and file_name:
            payload["file_name"] = file_name

        route = Route("POST", endpoint, **{id_key: chat_id})
        result = await self._client.api._http.request(route, json=payload)

        # Extract only the file_info field to avoid extra fields (file_uuid, ttl, etc.)
        # that may confuse QQ client when sending the media object.
        if isinstance(result, dict) and "file_info" in result:
            return {"file_info": result["file_info"]}
        return result

    # ---------------------------
    # Inbound (receive)
    # ---------------------------

    async def _on_message(self, data: C2CMessage | GroupMessage, is_group: bool = False) -> None:
        """Parse inbound message, download attachments, and publish to the bus."""
        try:
            if is_group:
                chat_id = data.group_openid
                user_id = data.author.member_openid
                chat_type = "group"
            else:
                chat_id = str(
                    getattr(data.author, "id", None)
                    or getattr(data.author, "user_openid", "unknown")
                )
                user_id = chat_id
                chat_type = "c2c"

            content = (data.content or "").strip()

            # 复用基类 _dedup_message (OrderedDict, O(1) 查重 + FIFO 淘汰);
            # 此前对 deque 做 O(n) 线性扫描, 高峰消息量下开销放大。
            if not self._dedup_message(data.id):
                return
            self._remember_chat_type(chat_id, chat_type)

            # Early permission check — avoid attachment downloads and ack side
            # effects for unauthorized users. Access is managed exclusively
            # via the allowFrom config; C2C and group senders are denied alike.
            if not self.is_allowed(user_id):
                self.logger.warning(
                    "Access denied for sender {}. "
                    "Add them to allowFrom list in config to grant access.",
                    user_id,
                )
                return

            # the data used by tests don't contain attachments property
            # so we use getattr with a default of [] to avoid AttributeError in tests
            attachments = getattr(data, "attachments", None) or []
            media_paths, recv_lines, att_meta = await self._handle_attachments(attachments)

            # Compose content that always contains actionable saved paths
            if recv_lines:
                tag = (
                    "[Image]"
                    if any(_is_image_name(Path(p).name) for p in media_paths)
                    else "[File]"
                )
                file_block = "Received files:\n" + "\n".join(recv_lines)
                content = (
                    f"{content}\n\n{file_block}".strip() if content else f"{tag}\n{file_block}"
                )

            if not content and not media_paths:
                return

            if self.config.ack_message:
                try:
                    await self._send_text_only(
                        chat_id=chat_id,
                        is_group=is_group,
                        msg_id=data.id,
                        content=self.config.ack_message,
                    )
                except Exception:
                    self.logger.debug("ack message failed for chat_id={}", chat_id)

            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media_paths if media_paths else None,
                metadata={
                    "message_id": data.id,
                    "attachments": att_meta,
                },
            )
        except Exception:
            self.logger.exception("Error handling inbound message id={}", getattr(data, "id", "?"))

    async def _handle_attachments(
        self,
        attachments: list[BaseMessage._Attachments],
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """Extract, download (chunked), and format attachments for agent consumption."""
        media_paths: list[str] = []
        recv_lines: list[str] = []
        att_meta: list[dict[str, Any]] = []

        if not attachments:
            return media_paths, recv_lines, att_meta

        for att in attachments:
            url = getattr(att, "url", None) or ""
            filename = getattr(att, "filename", None) or ""
            ctype = getattr(att, "content_type", None) or ""

            self.logger.info("Downloading file: {}", filename or url)
            local_path = await self._download_to_media_dir_chunked(url, filename_hint=filename)

            att_meta.append(
                {
                    "url": url,
                    "filename": filename,
                    "content_type": ctype,
                    "saved_path": local_path,
                }
            )

            if local_path:
                media_paths.append(local_path)
                shown_name = filename or os.path.basename(local_path)
                recv_lines.append(f"- {shown_name}\n  saved: {local_path}")
            else:
                shown_name = filename or url
                recv_lines.append(f"- {shown_name}\n  saved: [download failed]")

        return media_paths, recv_lines, att_meta

    async def _download_to_media_dir_chunked(
        self,
        url: str,
        filename_hint: str = "",
    ) -> str | None:
        """Download an inbound attachment using streaming chunk write.

        Uses chunked streaming to avoid loading large files into memory.
        Enforces a max download size and writes to a .part temp file
        that is atomically renamed on success.
        """
        # Handle protocol-relative URLs (e.g. "//multimedia.nt.qq.com/...")
        if url.startswith("//"):
            url = f"https:{url}"

        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        safe = _sanitize_filename(filename_hint)
        ts = int(time.time() * 1000)
        tmp_path: Path | None = None

        try:
            async with self._http.get(
                url,
                timeout=aiohttp.ClientTimeout(total=120),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    self.logger.warning("download failed: status={} url={}", resp.status, url)
                    return None

                ctype = (resp.headers.get("Content-Type") or "").lower()

                # Infer extension: url -> filename_hint -> content-type -> fallback
                ext = Path(urlparse(url).path).suffix
                if not ext:
                    ext = Path(filename_hint).suffix
                if not ext:
                    if "png" in ctype:
                        ext = ".png"
                    elif "jpeg" in ctype or "jpg" in ctype:
                        ext = ".jpg"
                    elif "gif" in ctype:
                        ext = ".gif"
                    elif "webp" in ctype:
                        ext = ".webp"
                    elif "pdf" in ctype:
                        ext = ".pdf"
                    else:
                        ext = ".bin"

                if safe:
                    if not Path(safe).suffix:
                        safe = safe + ext
                    filename = safe
                else:
                    filename = f"qq_file_{ts}{ext}"

                target = self._media_root / filename
                if target.exists():
                    target = self._media_root / f"{target.stem}_{ts}{target.suffix}"

                tmp_path = target.with_suffix(target.suffix + ".part")

                # Stream write
                downloaded = 0
                chunk_size = max(1024, _QQ_DOWNLOAD_CHUNK_SIZE)
                max_bytes = max(1024 * 1024, _QQ_DOWNLOAD_MAX_BYTES)

                def _open_tmp():
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    return open(tmp_path, "wb")  # noqa: SIM115

                f = await asyncio.to_thread(_open_tmp)
                try:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            self.logger.warning(
                                "download exceeded max_bytes={} url={} -> abort",
                                max_bytes,
                                url,
                            )
                            return None
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

                # Atomic rename
                await asyncio.to_thread(os.replace, tmp_path, target)
                tmp_path = None  # mark as moved
                self.logger.info("file saved: {}", str(target))
                return str(target)

        except Exception:
            self.logger.exception("download error")
            return None
        finally:
            # Cleanup partial file
            if tmp_path is not None:
                with suppress(Exception):
                    tmp_path.unlink(missing_ok=True)
