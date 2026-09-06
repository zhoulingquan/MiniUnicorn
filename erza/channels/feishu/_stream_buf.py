"""Per-chat streaming accumulator for the Feishu CardKit streaming API.

Extracted from ``channel.py`` so the small dataclass has its own module.
``FeishuChannel`` keeps one ``_FeishuStreamBuf`` per active stream key and
mutates ``text`` / ``sequence`` as CardKit streaming updates are pushed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _FeishuStreamBuf:
    """Per-chat streaming accumulator using CardKit streaming API."""

    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit: float = 0.0
    # 记录 buf 最近一次被 touch 的时间(单调时钟),供 TTL 清理使用。
    # 与 last_edit 区分:last_edit 仅在卡片成功创建/更新时刷新,
    # last_update 在 buf 创建/追加 delta 时即刷新,避免卡片创建失败导致 buf 永驻。
    last_update: float = 0.0
