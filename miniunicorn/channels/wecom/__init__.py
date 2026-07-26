"""WeCom (Enterprise WeChat) channel package.

Backward-compat: re-exports everything from ``.channel`` so legacy
``from miniunicorn.channels.wecom import WecomChannel`` keeps working.
"""
from .channel import *  # noqa: F401,F403  — public symbols
from .channel import (  # noqa: F401  — private names used by tests
    _guess_wecom_media_type,
    _sanitize_filename,
)

__all__ = [
    "WecomChannel",
    "WecomConfig",
    "channel",
]
