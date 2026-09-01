"""Shared path helpers for workspace-scoped tools."""

from pathlib import Path

from miniunicorn.config.paths import get_media_dir
from miniunicorn.security.workspace_policy import (
    WORKSPACE_BOUNDARY_NOTE,
    WorkspaceBoundaryError,
    is_path_within,
    resolve_allowed_path,
)


def is_under(path: Path, directory: Path) -> bool:
    """Return True when path resolves under directory."""
    return is_path_within(path, directory)


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path against workspace and enforce allowed directory containment."""
    extra_roots = [get_media_dir(), *(extra_allowed_dirs or [])] if allowed_dir else None
    return resolve_allowed_path(
        path,
        workspace=workspace,
        allowed_root=allowed_dir,
        extra_allowed_roots=extra_roots,
    )


def verify_workspace_path(
    path: Path | str,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> None:
    """Post-write containment re-check (TOCTOU hardening).

    预检查与实际写入之间存在窗口: 本地并发进程可把文件或其父目录换成
    指向边界外的符号链接。写入完成后用 strict 解析重新校验 containment,
    命中即抛错, 把越界写入暴露出来而不是静默通过。
    """
    if allowed_dir is None:
        return
    roots = [allowed_dir, get_media_dir(), *(extra_allowed_dirs or [])]
    if not any(is_path_within(path, root) for root in roots):
        raise WorkspaceBoundaryError(
            f"Path {path} resolved outside allowed directory {allowed_dir} after write"
            + WORKSPACE_BOUNDARY_NOTE
        )
