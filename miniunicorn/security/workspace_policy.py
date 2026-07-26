"""Workspace path boundary helpers.

These helpers are application-level guards.  They make path decisions
consistent across tools, but they are not a replacement for an OS sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)


class WorkspaceBoundaryError(PermissionError):
    """Raised when a requested path escapes an allowed workspace boundary."""


def resolve_path(path: str | Path, workspace: str | Path | None = None, *, strict: bool = False) -> Path:
    """Resolve *path*, interpreting relative paths against *workspace* when set."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and workspace is not None:
        candidate = Path(workspace).expanduser() / candidate
    return candidate.resolve(strict=strict)


def is_path_within(path: str | Path, root: str | Path) -> bool:
    """Return True when *path* resolves to *root* or a descendant of *root*.

    使用 ``resolve(strict=True)`` 确保符号链接被完全解析到真实目标,
    防止通过符号链接逃逸工作区边界。路径或 root 不存在时返回 False
    (安全默认:无法确认在边界内即视为边界外)。
    """
    try:
        resolved_path = Path(path).expanduser().resolve(strict=True)
        resolved_root = Path(root).expanduser().resolve(strict=True)
        resolved_path.relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
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
