"""跨频道共享的媒体文件处理工具。

历史上 qq / wecom / dingtalk 三个频道各自维护了近乎逐字符相同的
``_SAFE_NAME_RE`` 与 ``_sanitize_filename``，以及略有差异的
``_IMAGE_EXTS`` / ``_VIDEO_EXTS`` / ``_AUDIO_EXTS`` 集合。本模块把这些
公共逻辑集中到一处，避免后续修 bug 时漏改某个频道。

注意：
- 集合采用「各频道取最大并集」的策略，因此比任何单一频道原本的集合都更宽松。
  各频道若需要更严格的判定（例如 QQ 把所有非 image 都归为 file）仍可在
  本地函数里自行处理，只是不再重复定义正则与基础集合。
- ``classify_media_type`` 返回标准的 ``"audio"``；wecom/dingtalk 的上传 API
  使用 ``"voice"``，请各频道在调用处做 ``audio -> voice`` 的映射，不要在本
  模块里改返回值。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

# 替换不安全字符为 "_"，保留中文与常见安全标点。
# 与历史实现逐字符相同：qq/channel.py 与 wecom/channel.py 此前各自维护一份。
_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)

# 各频道历史集合的并集（取最大集）：
#   - 图片：QQ 的集合最完整（含 .tif/.tiff/.ico/.svg）
#   - 视频：dingtalk 的集合最完整（含 .mkv/.webm）
#   - 音频：dingtalk 的集合最完整（含 .m4a/.aac）
_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
}
_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
_AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}

MediaType = Literal["image", "video", "audio", "file"]


def sanitize_filename(name: str) -> str:
    """清理文件名，避免路径穿越与系统不友好字符。

    与历史 ``_sanitize_filename`` 行为完全一致：
    1. 去除首尾空白；
    2. 取 ``Path(name).name`` 去掉任何目录部分；
    3. 将不在白名单内的字符替换为 ``_``；
    4. 再次去掉首尾的 ``.``/``_``/空格。
    """
    name = (name or "").strip()
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


def classify_media_type(filename: str) -> MediaType:
    """根据扩展名把文件归入 image/video/audio/file 四类之一。

    大小写不敏感。无扩展名或未识别扩展名一律返回 ``"file"``。
    """
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "file"
