"""参考图路径安全校验。

参考图必须落在 workspace 或 media 目录内, 拒绝工作区外的路径 (防 `../` 越界)。
同时通过 magic bytes 二次校验文件确实是受支持的图片格式 (防 MIME 伪造)。
"""

from __future__ import annotations

from pathlib import Path

from miniunicorn.config.paths import get_media_dir
from miniunicorn.utils.helpers import detect_image_mime


class ReferenceImageError(ValueError):
    """参考图路径或内容校验失败。"""


_ALLOWED_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def resolve_allowed_image_path(
    path_str: str,
    *,
    workspace: str | Path,
    media_root: Path | None = None,
) -> Path:
    """校验参考图路径必须在 workspace 或 media 目录之内; 返回绝对路径。

    Args:
        path_str: LLM 传入的参考图路径 (可能是 artifact path 或用户上传图路径)
        workspace: 当前 agent 工作区路径
        media_root: media 根目录; None 时取 get_media_dir()

    Raises:
        ReferenceImageError: 路径越界、文件不存在、扩展名不支持、或 magic bytes 不匹配
    """
    if not path_str or not isinstance(path_str, str):
        raise ReferenceImageError("reference image path is empty")

    if "\x00" in path_str:
        raise ReferenceImageError("reference image path contains null byte")

    raw = Path(path_str).expanduser()
    if not raw.is_absolute():
        # 相对路径: 相对 workspace 解析 (避免相对 CWD 误读)
        raw = (Path(workspace).expanduser() / raw).resolve(strict=False)
    else:
        raw = raw.resolve(strict=False)

    workspace_resolved = Path(workspace).expanduser().resolve(strict=False)
    media_resolved = (media_root or get_media_dir()).resolve(strict=False)

    # 路径必须在 workspace 或 media 目录之内
    in_workspace = _is_within(raw, workspace_resolved)
    in_media = _is_within(raw, media_resolved)
    if not (in_workspace or in_media):
        raise ReferenceImageError(
            f"reference image path must be inside workspace ({workspace_resolved}) "
            f"or media directory ({media_resolved}); got {raw}"
        )

    if not raw.exists():
        raise ReferenceImageError(f"reference image not found: {raw}")

    if not raw.is_file():
        raise ReferenceImageError(f"reference image path is not a regular file: {raw}")

    ext = raw.suffix.lower()
    if ext not in _ALLOWED_IMAGE_EXTENSIONS:
        raise ReferenceImageError(
            f"unsupported reference image extension '{ext}'; allowed: {sorted(_ALLOWED_IMAGE_EXTENSIONS)}"
        )

    # magic bytes 二次校验 (防扩展名伪造)
    try:
        with raw.open("rb") as f:
            head = f.read(16)
    except OSError as exc:
        raise ReferenceImageError(f"cannot read reference image for MIME check: {exc}") from exc

    detected = detect_image_mime(head)
    if detected is None:
        raise ReferenceImageError(
            f"reference image content is not a recognized image format (magic bytes mismatch): {raw}"
        )

    return raw


def _is_within(path: Path, root: Path) -> bool:
    """判断 path 是否在 root 目录之内 (允许 root 本身)。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
