"""WeChat (公众号) channel package.

Backward-compat: re-exports everything from ``.channel`` so legacy
``from miniunicorn.channels.weixin import WeixinChannel`` keeps working.
"""

from .channel import *  # noqa: F401,F403  — public symbols
from .channel import (  # noqa: F401  — private names used by tests
    _decrypt_aes_ecb,
    _encrypt_aes_ecb,
)

__all__ = [
    "WeixinChannel",
    "WeixinConfig",
    "channel",
]
