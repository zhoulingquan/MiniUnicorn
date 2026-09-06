from __future__ import annotations

from pathlib import Path

import pytest

from erza.security.workspace_policy import (
    WorkspaceBoundaryError,
    is_path_within,
    resolve_allowed_path,
)


def test_resolve_allowed_path_accepts_workspace_relative_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("print('ok')", encoding="utf-8")

    resolved = resolve_allowed_path("src/main.py", workspace=workspace, allowed_root=workspace)

    assert resolved == target.resolve()


def test_resolve_allowed_path_blocks_parent_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceBoundaryError, match="outside allowed directory"):
        resolve_allowed_path("../secret.txt", workspace=workspace, allowed_root=workspace)


def test_resolve_allowed_path_blocks_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert not is_path_within(link, workspace)
    with pytest.raises(WorkspaceBoundaryError):
        resolve_allowed_path("linked-secret.txt", workspace=workspace, allowed_root=workspace)


def test_resolve_allowed_path_allows_extra_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    image = media / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    resolved = resolve_allowed_path(
        image,
        workspace=workspace,
        allowed_root=workspace,
        extra_allowed_roots=[media],
    )

    assert resolved == image.resolve()


def test_is_path_within_allows_nonexistent_file_in_workspace(tmp_path: Path) -> None:
    """创建新文件场景:目标不存在但父目录在 workspace 内,应允许通过。

    回归测试:``is_path_within`` 之前用 ``resolve(strict=True)`` 对不存在路径
    一律返回 False,导致 restricted 模式无法在 workspace 内创建新文件。
    修复后回退到 strict=False 并验证父目录在 root 内。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    # 新文件路径(workspace 内,但文件本身不存在)
    new_file = workspace / "src" / "new_module.py"

    assert is_path_within(new_file, workspace)


def test_is_path_within_rejects_nonexistent_file_outside_workspace(tmp_path: Path) -> None:
    """创建新文件场景:目标不存在且父目录在 workspace 外,应拒绝。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # workspace 外的新文件
    new_file_outside = outside / "evil.py"

    assert not is_path_within(new_file_outside, workspace)


def test_is_path_within_rejects_nonexistent_via_symlink_parent(tmp_path: Path) -> None:
    """创建新文件场景:通过不存在的符号链接父目录逃逸应被拒绝。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # workspace 内指向 outside 的符号链接目录(不存在时创建)
    link_dir = workspace / "escape"
    try:
        link_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    # 通过符号链接目录引用一个不存在的文件
    new_file_via_link = link_dir / "evil.py"

    assert not is_path_within(new_file_via_link, workspace)


def test_is_path_within_env_expansion_detects_bypass(tmp_path: Path) -> None:
    """env 展开检测:``$HOME`` 指向 workspace 外时应被拒绝。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "home"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    env = {"HOME": str(outside)}
    # $HOME/secret.txt 展开后在 workspace 外
    assert not is_path_within("$HOME/secret.txt", workspace, env=env)
    # %HOME% 形式同样应被检测(POSIX/Windows 语法都展开)
    assert not is_path_within("%HOME%/secret.txt", workspace, env=env)
