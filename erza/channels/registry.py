"""Auto-discovery for built-in channel packages and external plugins.

QwenPaw-style: each built-in channel lives in its own subpackage
``erza/channels/<name>/`` containing ``channel.py`` plus optional
helpers. The package ``__init__.py`` re-exports the public channel class.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from erza.channels.base import BaseChannel

_INTERNAL = frozenset({"base", "manager", "registry"})

# IM 频道适配器已拆为可选 extras（PyPI extras 名与频道名一致）。本体只内置
# websocket(WebUI)；适配器模块仍留在 erza/channels/ 下，仅在用户配置
# 启用对应频道时才通过 importlib 动态导入（见 discover_enabled）。
_CHANNEL_EXTRAS: dict[str, str] = {
    "feishu": "feishu",
    "weixin": "weixin",
    "dingtalk": "dingtalk",
    "qq": "qq",
    "wecom": "wecom",
}

_DISTRIBUTION_NAME = "erza-ai"

# 各适配器「缺了就无法工作」的第三方模块（均已声明在对应 extras 中）。
# 仅列硬依赖：可优雅降级的可选依赖（如 weixin 的 cryptography/qrcode、
# feishu 登录二维码打印）不在此列，避免误伤降级路径。适配器内部对 SDK
# 的 try/except 守卫保持原样；本预检只负责在启动路径给出安装指引。
_CHANNEL_REQUIRED_MODULES: dict[str, tuple[str, ...]] = {
    "feishu": ("lark_oapi",),
    "weixin": (),
    "dingtalk": ("dingtalk_stream",),
    "qq": ("aiohttp", "botpy"),
    "wecom": ("wecom_aibot_sdk",),
}


class ChannelDependencyError(RuntimeError):
    """An enabled channel's optional dependencies are not installed.

    Raised only for channels the user explicitly enabled in config; the
    message always contains the matching ``pip install`` command.
    """


def install_hint(module_name: str) -> str:
    """Return the pip command that installs the optional deps for *module_name*."""
    extra = _CHANNEL_EXTRAS.get(module_name, module_name)
    return f"pip install {_DISTRIBUTION_NAME}[{extra}]"


def _missing_required_modules(module_name: str) -> list[str]:
    """Return required third-party modules of *module_name* that are absent.

    Uses ``importlib.util.find_spec`` (no import side effects) so a missing
    SDK is detected without executing the adapter module.
    """
    required = _CHANNEL_REQUIRED_MODULES.get(module_name)
    if not required:
        return []
    return [mod for mod in required if importlib.util.find_spec(mod) is None]


def discover_channel_names() -> list[str]:
    """Return all built-in channel package names by scanning the parent package.

    Only subpackages (directories with ``__init__.py``) are returned — flat
    ``.py`` modules are ignored. This matches the QwenPaw-style layout where
    each channel has its own folder.
    """
    import erza.channels as pkg

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if name not in _INTERNAL and ispkg
    ]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Import the ``<module_name>`` channel package and return its BaseChannel subclass.

    Looks first in ``erza.channels.<module_name>.channel`` (the
    QwenPaw-style submodule), then falls back to scanning the package itself
    for a ``BaseChannel`` subclass (covers packages that re-export the class
    via ``__init__.py``).
    """
    from erza.channels.base import BaseChannel as _Base

    candidates = (
        f"erza.channels.{module_name}.channel",
        f"erza.channels.{module_name}",
    )
    last_err: Exception | None = None
    for qualname in candidates:
        try:
            mod = importlib.import_module(qualname)
        except ImportError as e:
            last_err = e
            continue
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
                return obj
    raise ImportError(
        f"No BaseChannel subclass in erza.channels.{module_name} "
        f"(tried: {', '.join(candidates)})"
    ) from last_err


def discover_plugins(enabled_names: set[str] | None = None) -> dict[str, type[BaseChannel]]:
    """Discover external channel plugins registered via entry_points."""
    from importlib.metadata import entry_points

    plugins: dict[str, type[BaseChannel]] = {}
    for ep in entry_points(group="erza.channels"):
        if enabled_names is not None and ep.name not in enabled_names:
            continue
        try:
            cls = ep.load()
            plugins[ep.name] = cls
        except Exception as e:
            logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)
    return plugins


def discover_enabled(
    enabled_names: set[str],
    *,
    _names: list[str] | None = None,
    _include_all_external: bool = False,
    strict: bool = True,
) -> dict[str, type[BaseChannel]]:
    """Return channels whose module names are in *enabled_names*.

    Uses cheap ``pkgutil.iter_modules`` to list names, then imports only
    those that match — skipping the heavy third-party SDK imports of
    unneeded channels. This keeps the slim core install free of the five
    optional IM adapters' dependencies (they ship as extras).

    Args:
        enabled_names: Channel names enabled in user config.
        strict: When True (default), a built-in channel that is enabled but
            whose dependencies are absent (detected via ``find_spec``
            preflight or import failure) raises
            :class:`ChannelDependencyError` carrying a
            ``pip install erza-ai[<extra>]`` hint. When False, the
            channel is skipped with a debug log (used by ``discover_all``
            for enumeration surfaces like the CLI and WebUI listings).
    """
    names = _names if _names is not None else discover_channel_names()
    result: dict[str, type[BaseChannel]] = {}
    for modname in names:
        if modname not in enabled_names:
            continue
        # Preflight: detect absent extras via find_spec (no side effects), then
        # fall back to the actual import for anything the preflight can't see.
        missing = _missing_required_modules(modname)
        failure: Exception | None
        if missing:
            failure = ImportError(f"missing required modules: {', '.join(missing)}")
        else:
            try:
                result[modname] = load_channel_class(modname)
                continue
            except ImportError as e:
                failure = e
        if strict:
            raise ChannelDependencyError(
                f"Channel '{modname}' is enabled in config but its "
                f"dependencies are missing ({failure}). Install them with: "
                f"{install_hint(modname)}"
            ) from failure
        logger.debug("Skipping built-in channel '{}': {}", modname, failure)

    external = discover_plugins(None if _include_all_external else enabled_names)
    shadowed = set(external) & set(result)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)
    if _include_all_external:
        result.update({k: v for k, v in external.items() if k not in shadowed})
    else:
        result.update(
            {k: v for k, v in external.items() if k not in shadowed and k in enabled_names}
        )

    return result


def discover_all() -> dict[str, type[BaseChannel]]:
    """Return all channels: built-in (pkgutil) merged with external (entry_points).

    Built-in channels take priority — an external plugin cannot shadow a built-in name.
    Enumeration-only (CLI/WebUI listings), so missing optional dependencies skip the
    channel instead of raising (``strict=False``).
    """
    names = discover_channel_names()
    return discover_enabled(set(names), _names=names, _include_all_external=True, strict=False)
