"""Workspace path boundary helpers.

These helpers are application-level guards.  They make path decisions
consistent across tools, but they are not a replacement for an OS sandbox.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping

WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)


class WorkspaceBoundaryError(PermissionError):
    """Raised when a requested path escapes an allowed workspace boundary."""


# Env-var expansion patterns: Windows ``%VAR%`` and POSIX ``$VAR`` / ``${VAR}``.
# Used by ``expand_env_vars`` to expand path expressions using the SAME env dict
# the subprocess receives (not ``os.environ``), so restricted mode can detect
# paths that escape the workspace via ``%USERPROFILE%`` / ``$HOME`` etc.
_WIN_VAR_RE = re.compile(r"%(\w+)%")
_POSIX_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def expand_env_vars(value: str, env: "Mapping[str, str] | None" = None) -> str:
    """Expand ``%VAR%`` / ``$VAR`` / ``${VAR}`` using *env* (defaults to ``os.environ``).

    Unlike ``os.path.expandvars``, this function:
    - Accepts a caller-supplied env dict (so restricted mode can use the
      controlled subprocess env, not the parent's ``os.environ``).
    - Expands BOTH Windows ``%VAR%`` and POSIX ``$VAR``/``${VAR}`` syntax on
      ALL platforms, so a POSIX-style ``$HOME`` in a path works on Windows too.
    - Leaves unknown vars unexpanded (does not replace ``$UNKNOWN`` with ``""``)
      so the caller can detect and reject unresolved references.
    """
    source = env if env is not None else os.environ

    def _win_repl(m: re.Match[str]) -> str:
        return source.get(m.group(1), m.group(0))

    def _posix_repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return source.get(name, m.group(0))

    result = _WIN_VAR_RE.sub(_win_repl, value)
    result = _POSIX_VAR_RE.sub(_posix_repl, result)
    return result


def resolve_path(
    path: str | Path,
    workspace: str | Path | None = None,
    *,
    strict: bool = False,
    env: "Mapping[str, str] | None" = None,
) -> Path:
    """Resolve *path*, interpreting relative paths against *workspace* when set.

    If *env* is provided, ``%VAR%`` / ``$VAR`` / ``${VAR}`` in *path* are
    expanded using *env* (the controlled subprocess env) before resolution,
    so restricted mode can detect paths that escape the workspace via env vars.
    """
    raw = str(path)
    if env is not None:
        raw = expand_env_vars(raw, env)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and workspace is not None:
        ws = str(workspace)
        if env is not None:
            ws = expand_env_vars(ws, env)
        candidate = Path(ws).expanduser() / candidate
    return candidate.resolve(strict=strict)


def is_path_within(
    path: str | Path,
    root: str | Path,
    *,
    env: "Mapping[str, str] | None" = None,
) -> bool:
    """Return True when *path* resolves to *root* or a descendant of *root*.

    使用 ``resolve(strict=True)`` 确保符号链接被完全解析到真实目标,
    防止通过符号链接逃逸工作区边界。

    当路径不存在(如即将创建的新文件)时,回退到 ``strict=False`` 解析并
    验证已解析的父目录在 *root* 内。这样既兼容"创建新文件"场景,又防止
    通过不存在的符号链接逃逸(父目录的符号链接仍会被 strict=False 解析)。

    If *env* is provided, env-var references (``%VAR%`` / ``$VAR``) in *path*
    and *root* are expanded using *env* before resolution, matching the
    subprocess's controlled environment.
    """
    try:
        path_raw = str(path)
        root_raw = str(root)
        if env is not None:
            path_raw = expand_env_vars(path_raw, env)
            root_raw = expand_env_vars(root_raw, env)
        resolved_root = Path(root_raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    try:
        resolved_path = Path(path_raw).expanduser().resolve(strict=True)
        resolved_path.relative_to(resolved_root)
        return True
    except OSError:
        # 路径不存在(可能是即将创建的新文件)。回退到 strict=False,
        # 但验证已解析的父目录在 root 内,防止通过不存在的符号链接逃逸。
        try:
            resolved_path = Path(path_raw).expanduser().resolve(strict=False)
            resolved_path.parent.relative_to(resolved_root)
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
    except (RuntimeError, TypeError, ValueError):
        return False


def is_path_allowed(path: str | Path, roots: Iterable[str | Path]) -> bool:
    """Return True when *path* is inside any allowed root."""
    return any(is_path_within(path, root) for root in roots)


def require_path_within(
    path: str | Path,
    root: str | Path,
    *,
    message: str | None = None,
) -> Path:
    """Resolve *path* and require it to be inside *root*.

    使用 ``resolve(strict=True)`` 防止符号链接逃逸。当路径不存在
    (如即将创建的新文件)时,回退到 ``strict=False`` 解析并验证父目录
    在 *root* 内,兼容"创建新文件"场景同时防止通过不存在的符号链接逃逸。
    """
    root_resolved = Path(root).expanduser().resolve(strict=False)
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError:
        # 路径不存在(可能是即将创建的新文件)。回退到 strict=False,
        # 但验证父目录在 root 内,防止通过不存在的符号链接逃逸。
        resolved = Path(path).expanduser().resolve(strict=False)
        try:
            resolved.parent.relative_to(root_resolved)
        except ValueError:
            raise WorkspaceBoundaryError(
                message
                or f"Path {path} is outside allowed directory {Path(root).expanduser()}"
                + WORKSPACE_BOUNDARY_NOTE
            ) from None
        return resolved
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise WorkspaceBoundaryError(
            message
            or f"Path {path} is outside allowed directory {Path(root).expanduser()}"
            + WORKSPACE_BOUNDARY_NOTE
        ) from None
    return resolved


def resolve_allowed_path(
    path: str | Path,
    *,
    workspace: str | Path | None = None,
    allowed_root: str | Path | None = None,
    extra_allowed_roots: Iterable[str | Path] | None = None,
    strict: bool = False,
) -> Path:
    """Resolve a path and enforce containment in allowed roots when configured."""
    resolved = resolve_path(path, workspace, strict=False)
    if allowed_root is None:
        return resolve_path(path, workspace, strict=strict) if strict else resolved

    roots = [allowed_root, *(extra_allowed_roots or [])]
    if not is_path_allowed(resolved, roots):
        raise WorkspaceBoundaryError(
            f"Path {path} is outside allowed directory {Path(allowed_root).expanduser()}"
            + WORKSPACE_BOUNDARY_NOTE
        )
    if strict:
        return resolve_path(path, workspace, strict=True)
    return resolved
