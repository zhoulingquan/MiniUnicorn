"""Channel configuration REST helpers for the WebUI HTTP surface.

读取/更新 ``config.channels`` 中的 per-channel 配置。可用 channel 列表
通过 ``channels.registry.discover_all()`` 自动发现，无需手动维护。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import typing
from typing import Any, get_args, get_origin

from miniunicorn.config.loader import load_config, save_config
from miniunicorn.config.schema import Base, ChannelsConfig

from ._query import _query_first, _query_first_alias
from ._runtime import QueryParams


class WebUIChannelsError(ValueError):
    """User-facing channel validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ChannelsConfig 中显式声明的内置频道字段（QwenPaw-style）。
# 其余频道（插件）通过 pydantic extra="allow" 存放。
_BUILTIN_CHANNEL_FIELDS = frozenset(
    {
        "feishu",
        "dingtalk",
        "qq",
        "wecom",
        "weixin",
        "websocket",
    }
)


def _get_channel_section(cfg: ChannelsConfig, name: str) -> dict[str, Any] | None:
    """读取某个 channel 的配置 dict（先看显式字段，再看 extras）。"""
    if name in _BUILTIN_CHANNEL_FIELDS:
        section = getattr(cfg, name, None)
    else:
        extras = getattr(cfg, "__pydantic_extra__", None) or {}
        section = extras.get(name)
    if section is None:
        return None
    if isinstance(section, dict):
        return section
    # pydantic 对象 → dump
    model_dump = getattr(section, "model_dump", None)
    if callable(model_dump):
        return model_dump(by_alias=True)
    return None


def _set_channel_section(cfg: ChannelsConfig, name: str, value: dict[str, Any]) -> None:
    """写入 channel 配置（自动路由到显式字段或 extras）。"""
    if name in _BUILTIN_CHANNEL_FIELDS:
        setattr(cfg, name, value)
    else:
        extras = getattr(cfg, "__pydantic_extra__", None)
        if extras is None:
            raise WebUIChannelsError("channels config does not accept extra fields")
        extras[name] = value


def _remove_channel_section(cfg: ChannelsConfig, name: str) -> bool:
    """移除 channel 配置；返回是否曾存在。"""
    if name in _BUILTIN_CHANNEL_FIELDS:
        current = getattr(cfg, name, None)
        if current is None:
            return False
        setattr(cfg, name, None)
        return True
    extras = getattr(cfg, "__pydantic_extra__", None) or {}
    if name not in extras:
        return False
    extras.pop(name, None)
    return True


def _channel_is_configured(cfg: ChannelsConfig, name: str) -> bool:
    """判断 channel 是否已在 config 中显式配置（启用）。"""
    if name in _BUILTIN_CHANNEL_FIELDS:
        return getattr(cfg, name, None) is not None
    extras = getattr(cfg, "__pydantic_extra__", None) or {}
    return name in extras


def _channel_has_credentials(
    config_dict: dict[str, Any] | None, schema: list[dict[str, Any]]
) -> bool:
    """判断 channel 是否已填写至少一个凭证字段。

    凭证字段 = 字符串类型(text/password)且默认值为空的字段（排除 enabled、
    base_url 等有非空默认值的字段）。只要任一凭证字段非空即视为已配置凭证。
    """
    if not config_dict or not isinstance(config_dict, dict):
        return False
    for field in schema:
        if field.get("ui_type") not in ("text", "password"):
            continue
        default = field.get("default")
        # 跳过有非空默认值的字段（如 base_url、instance_id、ack_message）
        if isinstance(default, str) and default:
            continue
        name = field.get("name", "")
        alias = field.get("alias", name)
        value = config_dict.get(alias) or config_dict.get(name)
        if isinstance(value, str) and value:
            return True
    return False


# 字段名包含这些关键字时，前端渲染为 password 输入框
_SECRET_NAME_HINTS = ("secret", "token", "password", "apikey", "api_key")


def _is_secret_field(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _SECRET_NAME_HINTS)


def _detect_field_type(name: str, annotation: Any) -> dict[str, Any]:
    """根据 pydantic 字段类型注解推断前端 UI 控件类型与选项。

    返回 dict 含 ``ui_type`` (text/password/number/boolean/list/select) 与可选的 ``options``。
    """
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional[X] / Union[X, None] → 取非 None 的部分
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _detect_field_type(name, non_none[0])

    # Literal["a", "b"] → select
    if origin is typing.Literal:
        opts = [str(a) for a in args]
        return {"ui_type": "select", "options": opts}

    # list[str] → list
    if origin in (list, tuple):
        return {"ui_type": "list"}

    # bool 必须在 int 之前判断（bool 是 int 的子类）
    if annotation is bool:
        return {"ui_type": "boolean"}

    if annotation in (int, float):
        return {"ui_type": "number"}

    if annotation is str:
        if _is_secret_field(name):
            return {"ui_type": "password"}
        return {"ui_type": "text"}

    # 兜底
    return {"ui_type": "text"}


def _extract_config_schema(cls: type) -> list[dict[str, Any]]:
    """从 Channel 模块中找到 Config 类（继承 Base，类名以 Config 结尾）并提取字段元数据。

    参考 QwenPaw：每个 Channel 都有对应的 pydantic Config 类定义字段结构，
    这里通过反射自动提取字段类型/默认值/必填/描述等元数据，供前端动态渲染表单。
    """
    module = inspect.getmodule(cls)
    if module is None:
        return []

    # 在 channel 模块中查找继承 Base 且类名以 Config 结尾的类
    config_cls: type[Base] | None = None
    for attr_name in dir(module):
        obj = getattr(module, attr_name, None)
        if (
            isinstance(obj, type)
            and issubclass(obj, Base)
            and obj is not Base
            and obj.__name__.endswith("Config")
        ):
            # 跳过子配置类（如 MochatMentionConfig、MochatGroupRule，名字不以 Config 结尾）
            config_cls = obj
            break

    if config_cls is None:
        return []

    schema: list[dict[str, Any]] = []
    for name, field in config_cls.model_fields.items():
        type_info = _detect_field_type(name, field.annotation)
        # 默认值处理：field.default 可能是 PydanticUndefined sentinel
        # （当字段只有 default_factory 时），此时调用 factory 取值
        from pydantic_core import PydanticUndefined

        default: Any = field.default
        if default is PydanticUndefined and field.default_factory is not None:
            try:
                default = field.default_factory()
            except Exception:
                default = None
        if default is PydanticUndefined:
            default = None
        # 序列化为 JSON 兼容类型
        if isinstance(default, (str, int, float, bool)) or default is None:
            serializable_default: Any = default
        elif isinstance(default, list):
            serializable_default = list(default)
        elif isinstance(default, dict):
            serializable_default = dict(default)
        else:
            serializable_default = str(default)

        schema.append(
            {
                "name": name,
                "alias": field.alias or name,
                "label": name.replace("_", " ").title(),
                "ui_type": type_info["ui_type"],
                "options": type_info.get("options"),
                "required": field.is_required(),
                "default": serializable_default,
                "description": field.description or "",
                "secret": type_info["ui_type"] == "password",
            }
        )

    return schema


def _channel_class_meta(cls: type) -> dict[str, Any]:
    """从 BaseChannel 子类提取展示用元数据（含 default_config 模板和 config_schema）。"""
    doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
    # 调用 channel 类的 default_config() classmethod 获取默认配置模板
    # 参考 nanobot 模式：每个 Channel 类都通过 default_config() 返回完整字段
    default_cfg: dict[str, Any] | None = None
    try:
        default_config_fn = getattr(cls, "default_config", None)
        if callable(default_config_fn):
            default_cfg = default_config_fn()
    except Exception:
        default_cfg = None
    # 自动提取 Config 类字段元数据，供前端动态渲染表单
    # 参考 QwenPaw Console：每个 channel 卡片展开后显示鉴权字段表单
    config_schema = _extract_config_schema(cls)
    return {
        "name": cls.name,
        "display_name": getattr(cls, "display_name", cls.name),
        "description": doc,
        "default_config": default_cfg,
        "config_schema": config_schema,
    }


def list_channels() -> dict[str, Any]:
    """返回所有可用 channel 及其当前配置。

    通过 ``discover_all()`` 枚举内置 + 插件 channel，再从
    ``config.channels`` 读取每个 channel 的当前配置 dict。
    未在 config 中显式配置的 channel 返回 ``config: null``。

    QwenPaw-style：返回项含 ``is_builtin`` 字段供前端区分内置频道与
    插件频道，用于两段式布局（已启用/可用频道）和筛选 tabs。

    ``qr_login_supported`` 标记该频道是否支持 WebUI 扫码登录（参考
    QwenPaw QrcodeAuthBlock）。判定方式：在
    ``miniunicorn.webui.qrcode_auth_handler.QRCODE_AUTH_HANDLERS`` 注册表
    中存在对应 handler 的频道即支持。所有 handler 都是无状态纯函数，
    不依赖 channel 实例。
    """
    from miniunicorn.channels.registry import discover_all
    from miniunicorn.webui.qrcode_auth_handler import QRCODE_AUTH_HANDLERS

    try:
        available = discover_all()
    except Exception:
        available = {}

    config = load_config()
    channels_cfg: ChannelsConfig = config.channels

    # 支持扫码登录的频道集合 = 已注册 handler 的频道（feishu/weixin/wecom/dingtalk/qq）
    _QR_LOGIN_SUPPORTED = frozenset(QRCODE_AUTH_HANDLERS.keys())  # noqa: N806

    items: list[dict[str, Any]] = []
    for name in sorted(available.keys()):
        # websocket 频道是 WebUI 服务器进程本身（承担静态资源 + WS + REST API），
        # 关闭它会让整个 WebUI 挂掉；在频道页面中隐藏，避免误操作。
        if name == "websocket":
            continue
        cls = available[name]
        meta = _channel_class_meta(cls)
        config_dict = _get_channel_section(channels_cfg, name)
        # 内置频道 = ChannelsConfig 中显式声明的字段；其余为插件频道
        is_builtin = name in _BUILTIN_CHANNEL_FIELDS

        items.append(
            {
                "name": name,
                "display_name": meta["display_name"],
                "description": meta["description"],
                "default_config": meta["default_config"],
                "config_schema": meta["config_schema"],
                "configured": config_dict is not None,
                "enabled": _channel_is_configured(channels_cfg, name),
                "has_credentials": _channel_has_credentials(config_dict, meta["config_schema"]),
                "config": config_dict,
                "is_builtin": is_builtin,
                "qr_login_supported": name in _QR_LOGIN_SUPPORTED,
            }
        )

    # 顶层 ChannelsConfig 公共字段
    top_level = {
        "send_progress": channels_cfg.send_progress,
        "send_tool_hints": channels_cfg.send_tool_hints,
        "show_reasoning": channels_cfg.show_reasoning,
        "extract_document_text": channels_cfg.extract_document_text,
        "send_max_retries": channels_cfg.send_max_retries,
        "transcription_provider": channels_cfg.transcription_provider,
        "transcription_language": channels_cfg.transcription_language,
    }

    return {
        "channels": items,
        "defaults": top_level,
        "requires_restart": False,
    }


def update_channel_config(query: QueryParams) -> dict[str, Any]:
    """更新单个 channel 的配置（JSON 字符串形式提交）。

    参数：
      - ``name``: channel 名称（必填）
      - ``config``: JSON 字符串，解析后存入 ``config.channels.<name>``
      - ``enabled``: 可选，"true"/"false" 控制是否启用
    """
    name = (_query_first(query, "name") or "").strip().lower()
    if not name:
        raise WebUIChannelsError("name is required")
    if name in {
        "send_progress",
        "send_tool_hints",
        "show_reasoning",
        "extract_document_text",
        "send_max_retries",
        "transcription_provider",
        "transcription_language",
    }:
        raise WebUIChannelsError(f"'{name}' is a reserved top-level field")

    config_json = _query_first_alias(query, "config", "channelConfig")

    config = load_config()
    channels_cfg: ChannelsConfig = config.channels

    if config_json is not None:
        import json

        try:
            parsed = json.loads(config_json)
        except json.JSONDecodeError as e:
            raise WebUIChannelsError(f"invalid config JSON: {e}") from None
        if not isinstance(parsed, dict):
            raise WebUIChannelsError("config must be a JSON object")
        _set_channel_section(channels_cfg, name, parsed)
    elif not _channel_is_configured(channels_cfg, name):
        # 未配置该 channel 且未显式提交 config 时，使用 channel 类的 default_config()
        # 作为兜底，使 toggle 可以直接启用未配置的 channel。
        from miniunicorn.channels.registry import discover_all

        try:
            available = discover_all()
        except Exception:
            available = {}
        cls = available.get(name)
        if cls is None:
            raise WebUIChannelsError(f"channel '{name}' is not registered")
        default_fn = getattr(cls, "default_config", None)
        if not callable(default_fn):
            raise WebUIChannelsError(f"channel '{name}' has no existing config to update")
        try:
            defaults = default_fn() or {}
        except Exception as e:
            raise WebUIChannelsError(
                f"channel '{name}' has no existing config and default_config failed: {e}"
            ) from e
        if not isinstance(defaults, dict):
            raise WebUIChannelsError(f"channel '{name}' default_config returned non-dict value")
        _set_channel_section(channels_cfg, name, defaults)

    enabled_raw = _query_first(query, "enabled")
    if enabled_raw is not None:
        # enabled=false 时移除该 channel 配置
        if enabled_raw.strip().lower() in {"false", "0", "no", "off"}:
            _remove_channel_section(channels_cfg, name)
        # enabled=true 时，section 中应已存在 config（由上方 config_json 或 default_config 写入）
        elif not _channel_is_configured(channels_cfg, name):
            # 兜底：使用空 dict，确保后续 save 不丢失 enabled 标志
            _set_channel_section(channels_cfg, name, {})

    try:
        save_config(config)
    except Exception as e:
        raise WebUIChannelsError(f"failed to save config: {e}") from e

    return {
        "ok": True,
        "name": name,
        "enabled": _channel_is_configured(channels_cfg, name),
        "config": _get_channel_section(channels_cfg, name),
    }


def delete_channel_config(query: QueryParams) -> dict[str, Any]:
    """移除某个 channel 的配置。

    参数：
      - ``name``: channel 名称（必填）
    """
    name = (_query_first(query, "name") or "").strip().lower()
    if not name:
        raise WebUIChannelsError("name is required")

    config = load_config()
    channels_cfg: ChannelsConfig = config.channels

    if not _remove_channel_section(channels_cfg, name):
        raise WebUIChannelsError(f"channel '{name}' is not configured", status=404)

    try:
        save_config(config)
    except Exception as e:
        raise WebUIChannelsError(f"failed to save config: {e}") from e

    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# QR 扫码登录（参考 QwenPaw QrcodeAuthBlock，统一走 QRCODE_AUTH_HANDLERS）
# ---------------------------------------------------------------------------


def _run_async(coro):
    """在同步 WebUI API 中驱动 async handler 协程到完成。

    ``channels_api`` 暴露的是同步函数（被 websocket channel 的同步路由
    handler 调用），而 QwenPaw 风格的 handler 都是 ``async`` 的。

    gateway 主线程通常已经有一个运行中的 asyncio loop（websocket channel
    的 dispatcher），直接调 ``loop.run_until_complete`` 会抛
    ``RuntimeError: This event loop is already running``。因此提交到独立的
    长期工作线程的持久 loop 上执行，彻底避开主 loop 冲突。

    历史实现每次调用都 ``threading.Thread + new_event_loop + close`` — 现在
    改为模块级单例 worker 线程持有持久 loop，避免每调一次都付 loop 创建 /
    销毁的 ~5ms 开销，对于 QR 登录的轮询场景（每秒一次）尤其明显。
    """
    return _ASYNC_WORKER.submit(coro)


class _AsyncWorker:
    """长期工作线程 + 持久 asyncio loop 单例。

    通过 ``asyncio.run_coroutine_threadsafe`` 提交协程到独立 loop，
    主线程同步等待结果。loop 在进程生命周期内复用，daemon=True 保证
    进程退出时自动清理。
    """

    def __init__(self) -> None:
        self._loop: Any = None  # asyncio.AbstractEventLoop
        self._thread: Any = None  # threading.Thread
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def _ensure_started(self) -> Any:
        """懒启动 worker 线程（幂等、线程安全）。"""
        if self._loop is not None and self._loop.is_running():
            return self._loop
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="channels-async-worker",
                daemon=True,
            )
            self._thread.start()
            # 等待 loop 真正运行起来再返回，避免 submit 时 loop 还没就绪
            if not self._ready.wait(timeout=5.0):
                raise RuntimeError("channels async worker thread failed to start")
            if self._loop is None:
                raise RuntimeError("channels async worker loop not initialized")
            return self._loop

    def _run_loop(self) -> None:
        """Worker 线程入口：创建并运行持久 loop。"""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()
                self._loop = None

    def submit(self, coro: Any) -> Any:
        """提交协程到持久 loop 并阻塞等待结果。"""
        loop = self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()  # 阻塞至 coro 完成，异常透传


# 模块级单例：进程生命周期内复用同一 worker 线程与 loop。
_ASYNC_WORKER = _AsyncWorker()


def _persist_qr_credentials(name: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """把 handler 返回的 credentials 持久化到 config.channels.<name>。

    保留每个频道已有的 config 字段（如 base_url/domain/allow_from），
    仅用 credentials 中的字段覆盖同名键，并标记 ``enabled=True``。

    对 feishu 频道，仍调用 ``save_registration_result`` 以走完整的实例
    合并逻辑（多实例支持 + identityKey 维护），不直接覆盖 section。
    """
    if name == "feishu":
        from miniunicorn.channels.feishu.channel import (
            DEFAULT_INSTANCE_ID,
            save_registration_result,
        )

        save_registration_result(
            {
                "app_id": credentials.get("app_id", ""),
                "app_secret": credentials.get("app_secret", ""),
                "domain": credentials.get("domain", "feishu"),
            },
            instance_id=DEFAULT_INSTANCE_ID,
        )
    else:
        config = load_config()
        channels_cfg: ChannelsConfig = config.channels
        current = _get_channel_section(channels_cfg, name) or {}
        if not isinstance(current, dict):
            current = {}
        # 合并：保留已有字段，用 credentials 覆盖同名键
        merged = {**current, **credentials, "enabled": True}
        _set_channel_section(channels_cfg, name, merged)
        try:
            save_config(config)
        except Exception as e:
            raise WebUIChannelsError(f"failed to save credentials: {e}") from e

    # 重新读取已保存的 config 返回给前端
    cfg = load_config()
    return _get_channel_section(cfg.channels, name) or {}


def begin_channel_qr_login(query: QueryParams) -> dict[str, Any]:
    """启动某个 channel 的扫码登录流程。

    参数：
      - ``name``: channel 名称（必填，必须在 ``QRCODE_AUTH_HANDLERS`` 中注册）
      - ``domain``: 可选，feishu/lark，仅 feishu handler 使用
      - ``base_url``: 可选，weixin 自定义 iLink 服务地址

    返回：
      - ``qrcode_image``: base64 PNG 二维码图片（前端直接 ``<img src="data:image/png;base64,...">``）
      - ``scan_url``: 原始扫码 URL（备用，前端一般用 ``qrcode_image``）
      - ``poll_token``: 轮询扫码状态所需的 token
      - ``interval``: 轮询间隔（秒）
      - ``expires_in``: 二维码有效期（秒）
    """
    import time

    name = (_query_first(query, "name") or "").strip().lower()
    if not name:
        raise WebUIChannelsError("name is required")

    from miniunicorn.webui.qrcode_auth_handler import (
        generate_qrcode_image,
        get_qr_handler,
    )

    handler = get_qr_handler(name)
    if handler is None:
        raise WebUIChannelsError(
            f"channel '{name}' does not support QR login via WebUI",
            status=400,
        )

    try:
        result = _run_async(handler.fetch_qrcode(query))
    except WebUIChannelsError:
        raise
    except Exception as e:
        raise WebUIChannelsError(f"failed to start QR login: {e}") from e

    try:
        qrcode_image = generate_qrcode_image(result.scan_url)
    except Exception as e:
        raise WebUIChannelsError(f"failed to generate QR image: {e}") from e

    return {
        "qrcode_image": qrcode_image,
        "scan_url": result.scan_url,
        "poll_token": result.poll_token,
        "interval": result.interval,
        "expires_in": result.expires_in,
        "started_at": time.time(),
    }


def poll_channel_qr_status(query: QueryParams) -> dict[str, Any]:
    """轮询扫码登录状态。

    参数：
      - ``name``: channel 名称（必填）
      - ``poll_token``: 由 ``begin_channel_qr_login`` 返回的 token
      - ``domain``: 可选，feishu/lark

    返回：
      - ``status``: ``pending`` / ``succeeded`` / ``failed`` / ``expired``
      - ``error``: 失败原因（status=failed 或 expired 时）
      - ``config``: 成功时返回已写入 config 的 channel 配置 dict（status=succeeded 时）

    成功时自动持久化凭证到 ``config.json``，并标记频道为已配置（enabled=True）。
    二维码过期（status=expired）时前端应重新调用 ``begin_channel_qr_login``。
    """
    name = (_query_first(query, "name") or "").strip().lower()
    if not name:
        raise WebUIChannelsError("name is required")

    from miniunicorn.webui.qrcode_auth_handler import get_qr_handler

    handler = get_qr_handler(name)
    if handler is None:
        raise WebUIChannelsError(
            f"channel '{name}' does not support QR login via WebUI",
            status=400,
        )

    poll_token = (_query_first(query, "poll_token") or "").strip()
    # 兼容老前端用 device_code 参数名调用
    if not poll_token:
        poll_token = (_query_first(query, "device_code") or "").strip()
    if not poll_token:
        raise WebUIChannelsError("poll_token is required")

    try:
        res = _run_async(handler.poll_status(poll_token, query))
    except WebUIChannelsError:
        raise
    except Exception as e:
        raise WebUIChannelsError(f"failed to poll QR status: {e}") from e

    status_raw = res.status
    if status_raw == "success":
        try:
            saved = _persist_qr_credentials(name, res.credentials)
        except WebUIChannelsError:
            raise
        except Exception as e:
            raise WebUIChannelsError(f"login succeeded but failed to save credentials: {e}") from e
        return {
            "status": "succeeded",
            "config": saved,
        }

    if status_raw == "expired":
        return {
            "status": "expired",
            "error": res.credentials.get("fail_reason", "qr_code_expired"),
        }

    if status_raw == "fail":
        return {
            "status": "failed",
            "error": res.credentials.get("fail_reason", "authorization_failed"),
        }

    return {"status": "pending"}
