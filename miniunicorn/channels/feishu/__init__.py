"""Feishu/Lark channel package.

Backward-compat: re-exports everything from ``.channel`` so legacy
``from miniunicorn.channels.feishu import FeishuChannel`` keeps working.
"""

from .channel import *  # noqa: F401,F403  — public symbols
from .channel import (  # noqa: F401  — private names used by tests/CLI
    _extract_post_content,
    _FeishuStreamBuf,
)

__all__ = [
    "FeishuChannel",
    "FeishuConfig",
    "channel",
]
