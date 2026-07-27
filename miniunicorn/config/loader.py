"""Configuration loading utilities."""

import copy
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pydantic
from filelock import FileLock
from loguru import logger
from pydantic import BaseModel

from miniunicorn.config.schema import Config, _resolve_tool_config_refs

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_schema_refs_ready = False
_legacy_migration_done = False

_LEGACY_DATA_DIR_NAME = ".miniUnicorn"
_DATA_DIR_NAME = ".miniunicorn"


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def migrate_legacy_data_dir() -> None:
    """Migrate data directory from legacy camelCase path to lowercase.

    On case-sensitive filesystems (Linux), ``~/.miniUnicorn/`` and
    ``~/.miniunicorn/`` are different directories. This moves data from the
    old path to the new one when only the old path exists.

    On case-insensitive filesystems (macOS default), both paths resolve to the
    same directory, so no migration is needed.
    """
    global _legacy_migration_done
    if _legacy_migration_done:
        return

    old_dir = Path.home() / _LEGACY_DATA_DIR_NAME
    new_dir = Path.home() / _DATA_DIR_NAME

    if old_dir.exists():
        if not new_dir.exists():
            try:
                old_dir.rename(new_dir)
                logger.info("Migrated data directory: {} -> {}", old_dir, new_dir)
            except OSError as exc:
                logger.warning(
                    "Failed to migrate data directory from {} to {}: {}",
                    old_dir,
                    new_dir,
                    exc,
                )
        elif old_dir.resolve() != new_dir.resolve():
            logger.warning(
                "Both {} and {} exist; using {}. Please manually migrate "
                "data from the old directory if needed.",
                old_dir,
                new_dir,
                new_dir,
            )

    _legacy_migration_done = True


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    migrate_legacy_data_dir()
    return Path.home() / ".miniunicorn" / "config.json"


def load_config(config_path: Path | None = None, *, apply_ssrf: bool = True) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.
        apply_ssrf: 是否把 config.tools.ssrf_whitelist 应用到网络安全模块。
            测试场景下可传 False 以避免副作用。生产网关启动时由调用方决定。

    Returns:
        Loaded configuration object.
    """
    global _schema_refs_ready
    if not _schema_refs_ready:
        _resolve_tool_config_refs()
        _schema_refs_ready = True

    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            # config_version 是迁移元数据，不属于 Config schema（extra="forbid"），
            # 在验证前取出供未来迁移逻辑使用，避免触发 ValidationError。
            data.pop("config_version", None)
            config = Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning("Failed to load config from {}: {}", path, e)
            logger.warning("Using default configuration.")

    if apply_ssrf:
        _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from miniunicorn.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def _fsync_dir(dir_path: Path) -> bool:
    """fsync the parent directory so the atomic rename is durable.

    Returns ``True`` if the directory was synced, ``False`` if the platform
    does not support directory fsync (notably Windows). On unsupported
    platforms we log a debug message but never raise — the atomic
    ``os.replace`` has already succeeded and must not be rolled back.
    """
    if sys.platform == "win32":
        # Windows has no portable directory fsync; os.replace is still atomic.
        logger.debug(
            "Skipping directory fsync on Windows for {} (not supported)",
            dir_path,
        )
        return False
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError as exc:
        logger.debug("Could not open directory for fsync {}: {}", dir_path, exc)
        return False
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("Could not fsync directory {}: {}", dir_path, exc)
        return False
    finally:
        os.close(fd)
    return True


@contextmanager
def _locked_config_write(path: Path):
    """Acquire a cross-process FileLock for *path* for the duration of the block.

    The lock file lives next to the config file (``<path>.lock``) and is
    shared between this writer and any concurrent process writing the same
    config. We deliberately keep the lock held only across the write+replace
    window so concurrent readers (which never take the lock) are not blocked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock")
    with lock:
        yield


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Save configuration to file atomically.

    Writes the config to a temporary file in the same directory, fsyncs it,
    then atomically replaces the destination via ``os.replace``. A
    cross-process ``FileLock`` prevents two concurrent writers from
    interleaving. On failure the temporary file is removed and the original
    file is left untouched.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()

    data = config.model_dump(mode="json", by_alias=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    with _locked_config_write(path):
        # Named temp file in the SAME directory so os.replace stays atomic
        # on the same filesystem. We manage the lifecycle manually so we
        # can fsync the file before the rename.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            # tmp_path is now gone after successful replace; mark it so the
            # finally block does not try to remove it again.
            tmp_path = None
            _fsync_dir(path.parent)
        except Exception:
            # On any failure, remove the temp file and leave the original
            # config untouched. Re-raise so callers see the error and do
            # not silently fall back to a corrupt state.
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.warning("Could not remove temporary config file {}", tmp_path)
            raise


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` (e.g.
    ``DreamConfig.cron``) survive; returns the same instance when no
    references are present. Raises ``ValueError`` if a referenced
    variable is not set.
    """
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        resolved = {k: _resolve_in_place(v) for k, v in obj.items()}
        return resolved if any(resolved[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        resolved = [_resolve_in_place(v) for v in obj]
        return resolved if any(nv is not ov for nv, ov in zip(resolved, obj)) else obj
    return obj


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    default = match.group(2)
    value = os.environ.get(name)
    if value is None:
        # 支持 ${VAR:-default} 语法：环境变量未设置时回退到默认值
        if default is not None:
            return default
        raise ValueError(f"Environment variable '{name}' referenced in config is not set")
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current.

    返回深拷贝后的新 dict，避免修改调用方传入的原始数据。
    同时写入 config_version（若不存在）以便未来迁移逻辑判断版本。
    """
    result = copy.deepcopy(data)
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = result.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.setdefault("my", {})
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    # 设置版本号（若不存在），便于未来迁移逻辑按版本分支处理
    if "config_version" not in result:
        result["config_version"] = 1
    return result
