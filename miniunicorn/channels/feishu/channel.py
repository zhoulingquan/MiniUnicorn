"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import threading
import time
import types
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field
from rich.markup import escape

from miniunicorn.bus.events import OutboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.channels.base import BaseChannel
from miniunicorn.channels.feishu._content_extract import (
    _extract_interactive_content,
    _extract_post_content,
    _extract_share_card_content,
)
from miniunicorn.channels.feishu._feishu_instances import DEFAULT_INSTANCE_ID
from miniunicorn.channels.feishu._feishu_ws import get_feishu_ws_runner
from miniunicorn.channels.feishu._registration import (
    _LOGIN_CONSOLE,
    _feishu_app_identity_key,
    qr_register,
    save_registration_result,
    sync_saved_feishu_identity_boundary,
)
from miniunicorn.channels.feishu._stream_buf import _FeishuStreamBuf
from miniunicorn.command.router import normalize_command_text
from miniunicorn.config.paths import get_media_dir
from miniunicorn.config.schema import Base
from miniunicorn.utils.helpers import safe_filename
from miniunicorn.utils.logging_bridge import redirect_lib_logging

if TYPE_CHECKING:
    from lark_oapi.api.im.v1.model import MentionEvent, P2ImMessageReceiveV1

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
SDK_AVAILABLE = FEISHU_AVAILABLE

_LARK_PKG_RESOURCES_SHIM: types.ModuleType | None = None


def _ensure_pkg_resources_declare_namespace() -> None:
    """Guarantee ``pkg_resources.declare_namespace`` while lark-oapi loads.

    setuptools>=82 removed ``declare_namespace`` from ``pkg_resources``, but
    lark-oapi's vendored protobuf package init still calls it; without a
    shim, ``import lark_oapi`` crashes on such environments. The no-op shim
    is installed once (idempotent) and kept in ``sys.modules`` because
    lark-oapi may defer its own ``pkg_resources`` imports to runtime.
    """
    global _LARK_PKG_RESOURCES_SHIM
    if _LARK_PKG_RESOURCES_SHIM is not None:
        return
    try:
        import pkg_resources as pkg_resources_mod
    except ImportError:
        shim = types.ModuleType("pkg_resources")
        shim.declare_namespace = lambda _name: None  # type: ignore[attr-defined]
        sys.modules.setdefault("pkg_resources", shim)
        _LARK_PKG_RESOURCES_SHIM = shim
        return
    if not hasattr(pkg_resources_mod, "declare_namespace"):
        setattr(pkg_resources_mod, "declare_namespace", lambda _name: None)
    _LARK_PKG_RESOURCES_SHIM = pkg_resources_mod


def _identity_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_lark_runtime() -> tuple[Any, str, str]:
    """Import the heavy Feishu SDK lazily.

    lark_oapi imports a large generated API surface at module import time, so
    keep it out of channel discovery and constructor paths.

    .. warning::

        已知 anti-pattern（待后续重构）：``lark_oapi.ws.client`` 首次 import 时
        会在「当前线程」创建一个 asyncio event loop 并存到模块级 ``loop`` 属性。
        本函数通常通过 ``asyncio.to_thread`` 在线程池的 worker 线程中执行，因此
        这个临时 loop 会绑定到可复用的 worker 线程上，可能影响同进程其他频道
        后续在该线程上调用 ``asyncio.get_event_loop()`` 的行为。

        为降低污染，首次导入完成时做最小化清理：
        1. 关闭 lark 创建的临时 loop（仅在它未运行、未关闭时）；
        2. 将 ``lark_ws_client.loop`` 重置为 None，避免后续 ws runner 误用；
        3. 当当前线程的 loop 非 running 时，调用 ``asyncio.set_event_loop(None)``
           解除绑定。

        清理仅在与 lark 导入同一次执行时进行（``ws_client_already_imported``
        为 False），且仅在 worker 线程中进行（不在主线程），避免影响主线程的
        running loop 或重复覆盖其他模块已设置的 loop。后续若 lark SDK 修复了
        线程问题，应直接删除这段清理逻辑。
    """
    import sys

    _ensure_pkg_resources_declare_namespace()
    ws_client_already_imported = "lark_oapi.ws.client" in sys.modules
    import lark_oapi as lark
    import lark_oapi.ws.client as lark_ws_client
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

    if not ws_client_already_imported and threading.current_thread() is not threading.main_thread():
        # 关闭 lark 在导入时为当前 worker 线程创建的临时 loop。
        import_loop = getattr(lark_ws_client, "loop", None)
        if import_loop is not None and not import_loop.is_running() and not import_loop.is_closed():
            import_loop.close()
        lark_ws_client.loop = None

        # 解除当前 worker 线程的 event loop 绑定，避免线程被线程池复用后
        # 影响其他频道。仅当 loop 非 running 时才清除（防御性兜底，避免误清
        # 掉其他模块在同一线程上设置的 running loop）。
        try:
            current_loop = asyncio.get_event_loop_policy().get_event_loop()
        except Exception:
            current_loop = None
        if current_loop is None or not current_loop.is_running():
            with suppress(Exception):
                asyncio.set_event_loop(None)

    return lark, FEISHU_DOMAIN, LARK_DOMAIN


def fetch_feishu_app_identity(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
) -> dict[str, str]:
    """Fetch the user-facing Feishu/Lark app identity for display.

    This is best-effort metadata for WebUI presentation.  Callers should treat
    an empty result as a normal fallback path.
    """
    if not FEISHU_AVAILABLE or not app_id or not app_secret:
        return {}

    try:
        lark, feishu_domain, lark_domain = _load_lark_runtime()
        from lark_oapi.api.application.v6.model.get_application_request import (
            GetApplicationRequest,
        )

        sdk_domain = lark_domain if domain == "lark" else feishu_domain
        client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(sdk_domain)
            .timeout(5)
            .build()
        )
        request = GetApplicationRequest.builder().app_id(app_id).lang("zh_cn").build()
        response = client.application.v6.application.get(request)
        if hasattr(response, "success") and not response.success():
            return {}

        app = getattr(getattr(response, "data", None), "app", None)
        if app is None:
            return {}

        identity: dict[str, str] = {}
        display_name = str(getattr(app, "app_name", "") or "").strip()
        avatar_url = str(getattr(app, "avatar_url", "") or "").strip()
        if display_name:
            identity["displayName"] = display_name
        if avatar_url:
            identity["avatarUrl"] = avatar_url
        if identity:
            identity["identityFetchedAt"] = _identity_timestamp()
        return identity
    except Exception:
        return {}


# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    instance_id: str = DEFAULT_INSTANCE_ID
    name: str = "miniunicorn"
    identity_key: str = ""
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    react_emoji: str = "THUMBSUP"
    done_emoji: str | None = None  # Emoji to show when task is completed (e.g., "DONE", "OK")
    tool_hint_prefix: str = "\U0001f527"  # Prefix for inline tool hints (default: 🔧)
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False  # If True, bot replies quote the user's original message
    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"  # Set to "lark" for international Lark
    topic_isolation: bool = (
        True  # If True, each topic in group chat gets its own session (isolation)
    )


_STREAM_ELEMENT_ID = "streaming_md"
_NEW_SESSION_DIVIDER_CONTENT = json.dumps(
    {
        "type": "divider",
        "params": {"divider_text": {"text": "New session started."}},
    }
)


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"
    display_name = "Feishu"

    _STREAM_EDIT_INTERVAL = 0.5  # throttle between CardKit streaming updates
    # 流缓冲 TTL:超过该时长(秒)未更新的 buf 视为陈旧并被清理。
    # 30 分钟足以覆盖正常流式回复间隔,异常中断的流不会长期占用内存。
    _STREAM_BUF_TTL = 1800
    # 清理任务周期:每 5 分钟扫描一次,在及时回收与 CPU 开销之间取折中。
    _STREAM_BUF_CLEANUP_INTERVAL = 300

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return FeishuConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_runner = get_feishu_ws_runner()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}
        self._bot_open_id: str | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id
        # 流缓冲 TTL 清理任务:周期性删除超过 _STREAM_BUF_TTL 未更新的 buf,
        # 防止异常中断的流(无 stream_end)导致 _stream_bufs 内存泄漏。
        self._stream_buf_cleanup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # QR login — writes credentials directly to config.json
    # ------------------------------------------------------------------

    async def login(self, force: bool = False) -> bool:
        """Perform QR code scan-to-create login for Feishu/Lark.

        Uses the Feishu device-code registration flow to create a new bot
        application automatically.  Opens a URL for the user to authorize
        with the Feishu or Lark mobile app.

        On success, writes ``appId``, ``appSecret``, and ``domain`` to
        ``channels.feishu`` in ``config.json`` and sets ``enabled: true``.

        Args:
            force: If True, clear existing credentials and force re-authentication.

        Returns True on success.
        """
        if force:
            self.config.app_id = ""
            self.config.app_secret = ""

        if self.config.app_id and self.config.app_secret:
            _LOGIN_CONSOLE.print("[green]Feishu/Lark is already authenticated.[/green]")
            _LOGIN_CONSOLE.print("Use --force to re-authenticate with a new bot.\n")
            return True

        _LOGIN_CONSOLE.print(
            "Authorize with the mobile app. miniunicorn will save the new bot credentials.\n"
        )

        result = qr_register(initial_domain=self.config.domain or "feishu")
        if not result:
            _LOGIN_CONSOLE.print(
                "[yellow]Login was not completed.[/yellow] "
                "Run 'miniunicorn channels login feishu --force' to retry."
            )
            return False

        self.config.app_id = result["app_id"]
        self.config.app_secret = result["app_secret"]
        self.config.domain = result.get("domain", "feishu")

        save_registration_result(
            result,
            instance_id=self.config.instance_id,
            name=self.config.name,
        )

        _LOGIN_CONSOLE.print("\n[green]Feishu/Lark login complete.[/green]")
        _LOGIN_CONSOLE.print(f"App ID: {escape(result['app_id'])}")
        _LOGIN_CONSOLE.print(f"Domain: {escape(self.config.domain)}")
        return True

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            self.logger.error("SDK not installed. Run: pip install -e \".[feishu]\"")
            return

        if not self.config.app_id or not self.config.app_secret:
            self.logger.error(
                "app_id and app_secret not configured. "
                "Run 'miniunicorn channels login feishu' to set up via QR code."
            )
            return

        if sync_saved_feishu_identity_boundary(
            instance_id=self.config.instance_id,
            app_id=self.config.app_id,
            domain=self.config.domain,
        ):
            self.config.identity_key = _feishu_app_identity_key(
                self.config.app_id, self.config.domain
            )
            self.config.allow_from = []
            self.logger.info(
                "Feishu app identity changed for {}; cleared paired users for this assistant",
                self.name,
            )

        lark, feishu_domain, lark_domain = await asyncio.to_thread(_load_lark_runtime)

        redirect_lib_logging("Lark")

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        domain = lark_domain if self.config.domain == "lark" else feishu_domain
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        # Silence "processor not found" errors when bots are added/removed from groups.
        # These events carry no actionable data for the agent.
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_added_v1",
            lambda _: None,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_deleted_v1",
            lambda _: None,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            domain=domain,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        await self._ws_runner.start_client(self.name, self._ws_client)

        # Fetch bot's own open_id for accurate @mention matching
        self._bot_open_id = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_bot_open_id
        )
        if self._bot_open_id:
            self.logger.info("bot open_id: {}", self._bot_open_id)
        else:
            self.logger.warning("Could not fetch bot open_id; @mention matching may be inaccurate")

        self.logger.info("bot started with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # 启动流缓冲 TTL 清理后台任务
        self._stream_buf_cleanup_task = asyncio.create_task(self._stream_buf_cleanup_loop())

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        Stop the Feishu bot.

        Notice: lark.ws.Client does not expose stop method， simply exiting the program will close the client.

        Reference: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        # 取消流缓冲清理任务,等待其退出
        if self._stream_buf_cleanup_task is not None:
            self._stream_buf_cleanup_task.cancel()
            with suppress(Exception):
                await self._stream_buf_cleanup_task
            self._stream_buf_cleanup_task = None
        await self._ws_runner.stop_client(self.name)
        self.logger.info("bot stopped")

    def _fetch_bot_open_id(self) -> str | None:
        """Fetch the bot's own open_id via GET /open-apis/bot/v3/info."""
        try:
            import lark_oapi as lark

            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                import json

                data = json.loads(response.raw.content)
                bot = (data.get("data") or data).get("bot") or data.get("bot") or {}
                return bot.get("open_id")
            self.logger.warning(
                "Failed to get bot info: code={}, msg={}", response.code, response.msg
            )
            return None
        except Exception as e:
            self.logger.warning("Error fetching bot info: {}", e)
            return None

    @staticmethod
    def _resolve_mentions(text: str, mentions: list[MentionEvent] | None) -> str:
        """Replace @_user_n placeholders with actual user info from mentions.

        Args:
            text: The message text containing @_user_n placeholders
            mentions: List of mention objects from Feishu message

        Returns:
            Text with placeholders replaced by @姓名 (open_id)
        """
        if not mentions or not text:
            return text

        for mention in mentions:
            key = mention.key or None
            if not key:
                continue
            # Feishu placeholders are numbered keys like @_user_1. Keep
            # punctuation-adjacent mentions valid without matching @_user_10.
            pattern = rf"{re.escape(key)}(?![A-Za-z0-9_])"
            if not re.search(pattern, text):
                continue

            user_id_obj = mention.id or None
            if not user_id_obj:
                continue

            open_id = user_id_obj.open_id
            user_id = user_id_obj.user_id
            name = mention.name or key

            # Format: @姓名 (open_id, user_id: xxx)
            if open_id and user_id:
                replacement = f"@{name} ({open_id}, user id: {user_id})"
            elif open_id:
                replacement = f"@{name} ({open_id})"
            else:
                replacement = f"@{name}"

            text = re.sub(pattern, replacement, text)

        return text

    def _is_bot_mention_event(self, mention: Any) -> bool:
        mid = getattr(mention, "id", None)
        if not mid:
            return False

        mention_open_id = getattr(mid, "open_id", None) or ""
        bot_open_id = getattr(self, "_bot_open_id", None) or ""
        if bot_open_id:
            return mention_open_id == bot_open_id

        # Fallback heuristic when bot open_id is unavailable.
        return not getattr(mid, "user_id", None) and mention_open_id.startswith("ou_")

    def _strip_leading_bot_mention(self, text: str, mentions: list[MentionEvent] | None) -> str:
        """Remove a required leading bot mention before slash command routing."""
        if not mentions or not text:
            return text

        candidate = text.lstrip()
        for mention in mentions:
            key = getattr(mention, "key", None) or ""
            if not key or not re.match(rf"{re.escape(key)}(?![A-Za-z0-9_])", candidate):
                continue
            if not self._is_bot_mention_event(mention):
                continue

            stripped = candidate[len(key) :].strip()
            return stripped or text

        return text

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Check if the bot is @mentioned in the message."""
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        for mention in getattr(message, "mentions", None) or []:
            if self._is_bot_mention_event(mention):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """Allow group messages when policy is open or bot is @mentioned."""
        if self.config.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Sync helper for adding reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                self.logger.warning(
                    "Failed to add reaction: code={}, msg={}", response.code, response.msg
                )
                return None
            else:
                self.logger.debug("Added {} reaction to message {}", emoji_type, message_id)
                return response.data.reaction_id if response.data else None
        except Exception as e:
            self.logger.warning("Error adding reaction: {}", e)
            return None

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        """Add a reaction emoji to a message.

        Returns the reaction_id on success, None on failure.
        When called via a tracked background task, the returned reaction_id
        is stored in ``_reaction_ids`` for later cleanup by ``send_delta``.

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """Sync helper for removing reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )

            response = self._client.im.v1.message_reaction.delete(request)
            if response.success():
                self.logger.debug("Removed reaction {} from message {}", reaction_id, message_id)
            else:
                self.logger.debug(
                    "Failed to remove reaction: code={}, msg={}", response.code, response.msg
                )
        except Exception as e:
            self.logger.debug("Error removing reaction: {}", e)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """
        Remove a reaction emoji from a message (non-blocking).

        Used to clear the "processing" indicator after bot replies.
        """
        if not self._client or not reaction_id:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_reaction_sync, message_id, reaction_id)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Callback: remove from tracking set and log unhandled exceptions."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.logger.warning("Background task failed: {}", exc)

    async def _cleanup_stale_stream_bufs(self) -> None:
        """周期性清理超过 ``_STREAM_BUF_TTL`` 未更新的流缓冲。

        正常流式回复会在 ``stream_end`` 时主动 ``pop`` 缓冲;但若上游异常
        中断(进程崩溃/网络断开)导致 ``stream_end`` 未送达,buf 会残留在
        ``_stream_bufs`` 中。此处用 TTL 兜底回收,避免内存无限增长。
        """
        cutoff = time.monotonic() - self._STREAM_BUF_TTL
        stale = [k for k, v in self._stream_bufs.items() if v.last_update < cutoff]
        for k in stale:
            self._stream_bufs.pop(k, None)
        if stale:
            self.logger.warning(
                "清理了 {} 个陈旧的流缓冲(超过 {} 秒未更新)",
                len(stale),
                self._STREAM_BUF_TTL,
            )

    async def _stream_buf_cleanup_loop(self) -> None:
        """流缓冲 TTL 清理后台循环,周期由 ``_STREAM_BUF_CLEANUP_INTERVAL`` 控制。"""
        while self._running:
            try:
                await asyncio.sleep(self._STREAM_BUF_CLEANUP_INTERVAL)
                await self._cleanup_stale_stream_bufs()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("流缓冲清理任务异常: {}", exc)

    def _on_reaction_added(self, message_id: str, task: asyncio.Task) -> None:
        """Callback: store reaction_id after background add-reaction completes."""
        if task.cancelled():
            return
        # Failures already logged by _on_background_task_done.
        with suppress(Exception):
            reaction_id = task.result()
            if reaction_id:
                self._reaction_ids[message_id] = reaction_id
        # Trim cache to prevent unbounded growth
        if len(self._reaction_ids) > 500:
            self._reaction_ids.pop(next(iter(self._reaction_ids)))

    @staticmethod
    def _stream_key(chat_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Scope streaming buffers to the inbound message when available."""
        meta = metadata or {}
        return meta.get("message_id") or chat_id

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    # Markdown formatting patterns that should be stripped from plain-text
    # surfaces like table cells and heading text.
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """Strip markdown formatting markers from text for plain display.

        Feishu table cells do not support markdown rendering, so we remove
        the formatting markers to keep the text readable.
        """
        # Remove bold markers
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        # Remove italic markers
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        # Remove strikethrough markers
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None

        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(h) for h in split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in split(_line)] for _line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(
                self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict], max_tables: int = 1
    ) -> list[list[dict]]:
        """Split card elements into groups with at most *max_tables* table elements each.

        Feishu cards have a hard limit of one table per card (API error 11310).
        When the rendered content contains multiple markdown tables each table is
        placed in a separate card message so every table reaches the user.
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            display_text = f"**{text}**" if text else ""
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": display_text,
                    },
                }
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    # ── Smart format detection ──────────────────────────────────────────
    # Patterns that indicate "complex" markdown needing card rendering
    _COMPLEX_MD_RE = re.compile(
        r"```"  # fenced code block
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown table (header + separator)
        r"|^#{1,6}\s+",  # headings
        re.MULTILINE,
    )

    # Simple markdown patterns (bold, italic, strikethrough)
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"  # **bold**
        r"|__.+?__"  # __bold__
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *italic* (single *)
        r"|~~.+?~~",  # ~~strikethrough~~
        re.DOTALL,
    )

    # Markdown link: [text](url)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    # Unordered list items
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    # Ordered list items
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # Max length for plain text format
    _TEXT_MAX_LEN = 200

    # Max length for post (rich text) format; beyond this, use card
    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """Determine the optimal Feishu message format for *content*.

        Returns one of:
        - ``"text"``        – plain text, short and no markdown
        - ``"post"``        – rich text (links only, moderate length)
        - ``"interactive"`` – card with full markdown rendering
        """
        stripped = content.strip()

        # Complex markdown (code blocks, tables, headings) → always card
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        # Long content → card (better readability with card layout)
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        # Has bold/italic/strikethrough → card (post format can't render these)
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        # Has list items → card (post format can't render list bullets well)
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        # Has links → post format (supports <a> tags)
        if cls._MD_LINK_RE.search(stripped):
            return "post"

        # Short plain text → text format
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        # Medium plain text without any formatting → post format
        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """Convert markdown content to Feishu post message JSON.

        Handles links ``[text](url)`` as ``a`` tags; everything else as ``text`` tags.
        Each line becomes a paragraph (row) in the post body.
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                # Text before this link
                before = line[last_end : m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append(
                    {
                        "tag": "a",
                        "text": m.group(1),
                        "href": m.group(2),
                    }
                )
                last_end = m.end()

            # Remaining text after last link
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            # Empty line → empty paragraph for spacing
            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}
    _FILE_TYPE_MAP = {
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()
                    )
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    self.logger.debug(
                        "Uploaded image {}: {}", os.path.basename(file_path), image_key
                    )
                    return image_key
                else:
                    self.logger.error(
                        "Failed to upload image: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading image {}", file_path)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    self.logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    self.logger.error(
                        "Failed to upload file: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading file {}", file_path)
            return None

    def _download_image_sync(
        self, message_id: str, image_key: str
    ) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download image: code={}, msg={}", response.code, response.msg
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading image {}", image_key)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu resource download API only accepts 'image' or 'file' as type.
        # Both 'audio' and 'media' (video) messages use type='file' for download.
        if resource_type in ("audio", "media"):
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download {}: code={}, msg={}",
                    resource_type,
                    response.code,
                    response.msg,
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    @staticmethod
    def _safe_media_filename(filename: str | None, fallback: str) -> str:
        """Return a local-only filename for downloaded Feishu media."""
        candidate = filename or fallback
        # Feishu/Lark filenames come from message metadata. Treat both POSIX
        # and Windows separators as path boundaries before applying the shared
        # filename sanitizer so downloads cannot escape the channel media dir.
        candidate = os.path.basename(candidate.replace("\\", "/"))
        candidate = safe_filename(candidate)
        if candidate in ("", ".", ".."):
            return safe_filename(fallback) or uuid.uuid4().hex
        return candidate

    async def _download_and_save_media(
        self, msg_type: str, content_json: dict, message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk.

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")

        data, filename = None, None
        fallback_filename = uuid.uuid4().hex

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                fallback_filename = f"{image_key[:16]}.jpg"
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = fallback_filename

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if not file_key:
                self.logger.warning("{} message missing file_key: {}", msg_type, content_json)
                return None, f"[{msg_type}: missing file_key]"
            if not message_id:
                self.logger.warning("{} message missing message_id", msg_type)
                return None, f"[{msg_type}: missing message_id]"

            fallback_filename = file_key[:16]
            data, filename = await loop.run_in_executor(
                None, self._download_file_sync, message_id, file_key, msg_type
            )

            if not data:
                self.logger.warning("{} download failed: file_key={}", msg_type, file_key)
                return None, f"[{msg_type}: download failed]"

            if not filename:
                filename = fallback_filename

            # Feishu voice messages are opus in OGG container.
            # Use .ogg extension for better Whisper compatibility.
            if msg_type == "audio":
                if not any(filename.endswith(ext) for ext in (".opus", ".ogg", ".oga")):
                    filename = f"{filename}.ogg"

        if data and filename:
            filename = self._safe_media_filename(filename, fallback_filename)
            file_path = media_dir / filename
            file_path.write_bytes(data)
            path_str = str(file_path)
            self.logger.debug("Downloaded {} to {}", msg_type, path_str)
            return path_str, f"[{msg_type}: {path_str}]"

        return None, f"[{msg_type}: download failed]"

    _REPLY_CONTEXT_MAX_LEN = 200

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """Fetch the text content of a Feishu message by ID (synchronous).

        Returns a "[Reply to: ...]" context string, or None on failure.
        """
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                self.logger.debug(
                    "could not fetch parent message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            self.logger.debug("error fetching parent message {}: {}", message_id, e)
            return None

    def _reply_message_sync(
        self, parent_message_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False
    ) -> bool:
        """Reply to an existing Feishu message using the Reply API (synchronous).

        Args:
            reply_in_thread: If True, reply as a thread/topic message
                in the Feishu client.
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            body_builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if reply_in_thread:
                body_builder = body_builder.reply_in_thread(True)
            request = (
                ReplyMessageRequest.builder()
                .message_id(parent_message_id)
                .request_body(body_builder.build())
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                self.logger.error(
                    "Failed to reply to message {}: code={}, msg={}, log_id={}",
                    parent_message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                if msg_type == "interactive":
                    return self._reply_interactive_fallback_sync(
                        parent_message_id,
                        content,
                        reply_in_thread=reply_in_thread,
                    )
                return False
            self.logger.debug("reply sent to message {}", parent_message_id)
            return True
        except Exception:
            self.logger.exception("Error replying to message {}", parent_message_id)
            if msg_type == "interactive":
                return self._reply_interactive_fallback_sync(
                    parent_message_id,
                    content,
                    reply_in_thread=reply_in_thread,
                )
            return False

    @staticmethod
    def _interactive_content_to_text(content: str) -> str | None:
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        parts = [part.strip() for part in _extract_interactive_content(payload) if part.strip()]
        text = "\n".join(parts).strip()
        return text or None

    @staticmethod
    def _fallback_text_chunks(text: str, limit: int = 3500) -> list[str]:
        text = text.strip()
        if not text:
            return []
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    def _reply_interactive_fallback_sync(
        self,
        parent_message_id: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> bool:
        text = self._interactive_content_to_text(content)
        if not text:
            return False
        sent = False
        for chunk in self._fallback_text_chunks(text):
            body = json.dumps({"text": chunk}, ensure_ascii=False)
            sent = (
                self._reply_message_sync(
                    parent_message_id,
                    "text",
                    body,
                    reply_in_thread=reply_in_thread,
                )
                or sent
            )
        if sent:
            self.logger.warning("Sent Feishu interactive reply as text fallback")
        return sent

    def _send_interactive_fallback_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        content: str,
    ) -> str | None:
        text = self._interactive_content_to_text(content)
        if not text:
            return None
        last_message_id: str | None = None
        for chunk in self._fallback_text_chunks(text):
            body = json.dumps({"text": chunk}, ensure_ascii=False)
            message_id = self._send_message_sync(receive_id_type, receive_id, "text", body)
            if message_id:
                last_message_id = message_id
        if last_message_id:
            self.logger.warning("Sent Feishu interactive message as text fallback")
        return last_message_id

    def _should_use_reply_in_thread(self, metadata: dict[str, Any]) -> bool:
        """Return whether a group reply should create a Feishu thread/topic."""
        return metadata.get("chat_type", "group") == "group" and self.config.reply_to_message

    def _thread_reply_target(self, metadata: dict[str, Any]) -> str | None:
        """Return the message_id that should receive a Reply API response."""
        if metadata.get("chat_type", "group") != "group":
            return None
        message_id = metadata.get("message_id")
        if not message_id:
            return None
        if metadata.get("thread_id") or self.config.reply_to_message:
            return message_id
        return None

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        """Send a single message and return the message_id on success."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                self.logger.error(
                    "Failed to send {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                if msg_type == "interactive":
                    return self._send_interactive_fallback_sync(
                        receive_id_type,
                        receive_id,
                        content,
                    )
                return None
            msg_id = getattr(response.data, "message_id", None)
            self.logger.debug("{} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception:
            self.logger.exception("Error sending {} message", msg_type)
            return None

    def _create_streaming_card_sync(
        self,
        receive_id_type: str,
        chat_id: str,
        reply_message_id: str | None = None,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Create a CardKit streaming card, send it to chat, return card_id.

        When *reply_message_id* is provided the card is delivered via the
        reply API. *reply_in_thread* controls whether Feishu creates a
        thread/topic for that reply. Otherwise the plain create-message API is
        used.
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
            "body": {
                "elements": [{"tag": "markdown", "content": "", "element_id": _STREAM_ELEMENT_ID}]
            },
        }
        try:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                self.logger.warning(
                    "Failed to create streaming card: code={}, msg={}", response.code, response.msg
                )
                return None
            card_id = getattr(response.data, "card_id", None)
            if card_id:
                card_content = json.dumps(
                    {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
                )
                if reply_message_id:
                    sent = self._reply_message_sync(
                        reply_message_id,
                        "interactive",
                        card_content,
                        reply_in_thread=reply_in_thread,
                    )
                else:
                    sent = (
                        self._send_message_sync(
                            receive_id_type,
                            chat_id,
                            "interactive",
                            card_content,
                        )
                        is not None
                    )
                if sent:
                    return card_id
                self.logger.warning(
                    "Created streaming card {} but failed to send it to {}", card_id, chat_id
                )
            return None
        except Exception as e:
            self.logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(self, card_id: str, content: str, sequence: int) -> bool:
        """Stream-update the markdown element on a CardKit card (typewriter effect)."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(_STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                self.logger.warning(
                    "Failed to stream-update card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _set_streaming_mode_sync(self, card_id: str, enabled: bool, sequence: int) -> bool:
        """Set CardKit streaming_mode using a strictly increasing sequence."""
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings_payload = json.dumps({"config": {"streaming_mode": enabled}}, ensure_ascii=False)
        try:
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                self.logger.warning(
                    "Failed to set streaming={} on card {}: code={}, msg={}",
                    enabled,
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error setting streaming={} on card {}: {}", enabled, card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """Turn off CardKit streaming_mode so the chat list preview exits the streaming placeholder.

        Per Feishu docs, streaming cards keep a generating-style summary in the session list until
        streaming_mode is set to false via card settings (after final content update).
        Sequence must strictly exceed the previous card OpenAPI operation on this entity.
        """
        return self._set_streaming_mode_sync(card_id, False, sequence)

    def _stream_update_text_with_reopen_sync(
        self,
        card_id: str,
        content: str,
        sequence: int,
    ) -> tuple[bool, int]:
        if self._stream_update_text_sync(card_id, content, sequence):
            return True, sequence
        sequence += 1
        if not self._set_streaming_mode_sync(card_id, True, sequence):
            return False, sequence
        sequence += 1
        return self._stream_update_text_sync(card_id, content, sequence), sequence

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
        """Progressive streaming via CardKit: create card on first delta, stream-update on subsequent.

        Supported metadata keys:
            message_id:  Original message id (used with stream end for reaction cleanup).
            chat_type:   "group" or "p2p" — controls reply-in-thread for streaming cards.
        """
        if not self._client:
            return
        meta = metadata or {}
        stream_key = self._stream_key(chat_id, meta)
        loop = asyncio.get_running_loop()
        rid_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        # --- stream end: final update or fallback ---
        if stream_end:
            message_id = meta.get("message_id")
            # Only finalize the OnIt -> DONE reaction transition on the truly
            # final stream end. resuming=True means the agent will keep
            # working (more tool-call rounds), so leave the reaction state
            # in place — otherwise the OnIt indicator disappears prematurely
            # and the DONE reaction fires after every tool call.
            if message_id and not resuming:
                reaction_id = self._reaction_ids.pop(message_id, None)
                if reaction_id:
                    await self._remove_reaction(message_id, reaction_id)
                # Add completion emoji if configured
                if self.config.done_emoji:
                    await self._add_reaction(message_id, self.config.done_emoji)

            buf = self._stream_bufs.pop(stream_key, None)
            if not buf or not buf.text:
                return
            # Try to finalize via streaming card; if that fails (e.g.
            # streaming mode was closed by Feishu due to timeout), fall
            # back to sending a regular interactive card.
            if buf.card_id:
                buf.sequence += 1
                ok, buf.sequence = await loop.run_in_executor(
                    None,
                    self._stream_update_text_with_reopen_sync,
                    buf.card_id,
                    buf.text,
                    buf.sequence,
                )
                if ok:
                    buf.sequence += 1
                    closed = await loop.run_in_executor(
                        None,
                        self._close_streaming_mode_sync,
                        buf.card_id,
                        buf.sequence,
                    )
                    if not closed:
                        buf.sequence += 1
                        await loop.run_in_executor(
                            None,
                            self._close_streaming_mode_sync,
                            buf.card_id,
                            buf.sequence,
                        )
                    return
                buf.sequence += 1
                await loop.run_in_executor(
                    None,
                    self._close_streaming_mode_sync,
                    buf.card_id,
                    buf.sequence,
                )
                self.logger.warning(
                    "Streaming card {} final update failed, falling back to regular card",
                    buf.card_id,
                )
            for chunk in self._split_elements_by_table_limit(self._build_card_elements(buf.text)):
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": chunk},
                    ensure_ascii=False,
                )
                # Fallback replies stay in existing topics, but only create a
                # new topic when reply-to-message is enabled.
                fallback_msg_id = self._thread_reply_target(meta)
                if fallback_msg_id:
                    await loop.run_in_executor(
                        None,
                        lambda: self._reply_message_sync(
                            fallback_msg_id,
                            "interactive",
                            card,
                            reply_in_thread=self._should_use_reply_in_thread(meta),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, rid_type, chat_id, "interactive", card
                    )
            return

        # --- accumulate delta ---
        buf = self._stream_bufs.get(stream_key)
        if buf is None:
            buf = _FeishuStreamBuf()
            self._stream_bufs[stream_key] = buf
        buf.text += delta
        # 每次追加 delta 都刷新 last_update,供 TTL 清理判断 buf 是否陈旧。
        buf.last_update = time.monotonic()
        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.card_id is None:
            # Use the Reply API for existing topics, and only create new topics
            # when reply-to-message is enabled.
            use_reply_in_thread = self._should_use_reply_in_thread(meta)
            reply_msg_id = self._thread_reply_target(meta)
            card_id = await loop.run_in_executor(
                None,
                lambda: self._create_streaming_card_sync(
                    rid_type,
                    chat_id,
                    reply_msg_id,
                    reply_in_thread=use_reply_in_thread,
                ),
            )
            if card_id:
                ok, sequence = await loop.run_in_executor(
                    None, self._stream_update_text_with_reopen_sync, card_id, buf.text, 1
                )
                if ok:
                    buf.card_id = card_id
                    buf.sequence = sequence
                    buf.last_edit = now
                else:
                    await loop.run_in_executor(
                        None, self._close_streaming_mode_sync, card_id, sequence + 1
                    )
        elif (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            ok, buf.sequence = await loop.run_in_executor(
                None,
                self._stream_update_text_with_reopen_sync,
                buf.card_id,
                buf.text,
                buf.sequence + 1,
            )
            if ok:
                buf.last_edit = now
            else:
                buf.sequence += 1
                await loop.run_in_executor(
                    None,
                    self._close_streaming_mode_sync,
                    buf.card_id,
                    buf.sequence,
                )
                buf.card_id = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present."""
        if not self._client:
            self.logger.warning("client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            # Handle tool hint messages.  When a streaming card is active for
            # this chat, inline the hint into the card instead of sending a
            # separate message so the user experience stays cohesive.
            progress_event = msg.metadata.get("_progress")

            if progress_event and msg.metadata.get("_tool_hint"):
                hint = (msg.content or "").strip()
                if not hint:
                    return
                buf = self._stream_bufs.get(self._stream_key(msg.chat_id, msg.metadata))
                if buf and buf.card_id:
                    # Delegate to send_delta so tool hints get the same
                    # throttling (and card creation) as regular text deltas.
                    await self.send_delta(
                        msg.chat_id,
                        "\n\n" + self._format_tool_hint_delta(hint) + "\n\n",
                        metadata=msg.metadata,
                    )
                    return
                # No active streaming card — send as a regular interactive card
                # with the same 🔧 prefix style. Existing topics stay threaded;
                # new topics are created only when reply-to-message is enabled.
                card = json.dumps(
                    {
                        "config": {"wide_screen_mode": True},
                        "elements": [
                            {"tag": "markdown", "content": self._format_tool_hint_delta(hint)},
                        ],
                    },
                    ensure_ascii=False,
                )
                _th_msg_id = self._thread_reply_target(msg.metadata)
                if _th_msg_id:
                    await loop.run_in_executor(
                        None,
                        lambda: self._reply_message_sync(
                            _th_msg_id,
                            "interactive",
                            card,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None,
                        self._send_message_sync,
                        receive_id_type,
                        msg.chat_id,
                        "interactive",
                        card,
                    )
                return

            if (
                msg.content.strip() == "New session started."
                and msg.metadata.get("chat_type") == "p2p"
                and not msg.media
                and not msg.buttons
            ):
                return

            # Determine whether the first message should quote the user's message.
            # Only the very first send (media or text) in this call uses reply; subsequent
            # chunks/media fall back to plain create to avoid redundant quote bubbles.
            # Always target message_id — the Feishu Reply API keeps replies in the
            # same topic automatically when the target message is inside a topic.
            reply_message_id: str | None = None
            _msg_id = msg.metadata.get("message_id")
            has_thread_id = msg.metadata.get("thread_id")
            if self.config.reply_to_message and not progress_event:
                reply_message_id = _msg_id
            # For topic group messages, always reply to keep context in thread
            elif has_thread_id:
                reply_message_id = _msg_id

            first_send = True  # tracks whether the reply has already been used

            def _do_send(m_type: str, content: str) -> None:
                """Send via reply (first message) or create (subsequent).

                Group chats only set reply_in_thread=True when
                reply_to_message is enabled; otherwise a Reply API call for an
                existing topic must not create a new topic.
                """
                nonlocal first_send
                if reply_message_id:
                    # If we're in a topic, always use reply to stay in the topic
                    if has_thread_id:
                        ok = self._reply_message_sync(
                            reply_message_id,
                            m_type,
                            content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    elif first_send:
                        # If we're not in a topic but replying to message, only first uses reply
                        first_send = False
                        ok = self._reply_message_sync(
                            reply_message_id,
                            m_type,
                            content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    # Fall back to regular send if reply fails
                message_id = self._send_message_sync(
                    receive_id_type,
                    msg.chat_id,
                    m_type,
                    content,
                )
                if not message_id:
                    raise RuntimeError(f"Feishu {m_type} message was not delivered")

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    self.logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        # Feishu's OpenAPI names video messages "media".
                        # Use "audio" for audio, "media" for video, "file" for documents.
                        # Feishu requires these specific msg_types for inline playback.
                        if ext in self._AUDIO_EXTS:
                            media_type = "audio"
                        elif ext in self._VIDEO_EXTS:
                            media_type = "media"
                        else:
                            media_type = "file"
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)

                if fmt == "text":
                    # Short plain text – send as simple text message
                    text_body = json.dumps({"text": msg.content.strip()}, ensure_ascii=False)
                    await loop.run_in_executor(None, _do_send, "text", text_body)

                elif fmt == "post":
                    # Medium content with links – send as rich-text post
                    post_body = self._markdown_to_post(msg.content)
                    await loop.run_in_executor(None, _do_send, "post", post_body)

                else:
                    # Complex / long content – send as interactive card
                    elements = self._build_card_elements(msg.content)
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )

        except Exception:
            self.logger.exception("Error sending message")
            raise

    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if not self._running:
            return
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming message from Feishu."""
        if not self._running:
            return
        try:
            event = data.event
            message = event.message
            sender = event.sender

            self.logger.debug("raw message: {}", message.content)
            self.logger.debug("mentions: {}", getattr(message, "mentions", None))

            message_id = message.message_id

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                self.logger.debug("skipping group message (not mentioned)")
                return

            # Deduplication check
            if not self._dedup_message(message_id):
                return

            # Early permission check — avoid side effects for unauthorized users.
            # Group chats are silently ignored; DMs get a pairing code.
            if not self.is_allowed(sender_id):
                if chat_type == "p2p":
                    # content="" because the pairing reply is generated by
                    # BaseChannel._handle_message, not from the original message.
                    await self._handle_message(
                        sender_id=sender_id,
                        chat_id=sender_id,
                        content="",
                        is_dm=True,
                    )
                return

            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(self._add_reaction(message_id, self.config.react_emoji))
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    mentions = getattr(message, "mentions", None)
                    text = self._strip_leading_bot_mention(text, mentions)
                    text = self._resolve_mentions(text, mentions)
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            # Extract reply context (parent/root message IDs)
            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None

            # Prepend quoted message text when the user replied to another message
            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            if chat_type == "p2p" and normalize_command_text(content).lower() == "/new":
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self._send_message_sync,
                    "open_id",
                    sender_id,
                    "system",
                    _NEW_SESSION_DIVIDER_CONTENT,
                )

            # Build session key for conversation isolation.
            # If topic_isolation is True: each topic gets its own session via root_id/message_id.
            # If topic_isolation is False: all messages in group share the same session.
            # Private chat: no override — same behavior as Telegram/Slack.
            if chat_type == "group":
                if self.config.topic_isolation:
                    session_key = f"{self.name}:{chat_id}:{root_id or message_id}"
                else:
                    session_key = f"{self.name}:{chat_id}"
            else:
                session_key = None

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                },
                session_key=session_key,
                is_dm=chat_type == "p2p",
            )

        except Exception:
            self.logger.exception("Error processing message")

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore p2p-enter events when a user opens a bot chat."""
        self.logger.debug("Bot entered p2p chat (user opened chat window)")
        pass

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """Split tool hints across lines on top-level call separators only."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    def _format_tool_hint_delta(self, tool_hint: str) -> str:
        """Format a tool hint string with the 🔧 prefix for each line."""
        lines = self.__class__._format_tool_hint_lines(tool_hint).split("\n")
        return "\n".join(f"{self.config.tool_hint_prefix} {ln}" for ln in lines if ln.strip())
