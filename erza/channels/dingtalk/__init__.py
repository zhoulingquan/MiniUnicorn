"""DingTalk channel package.

Backward-compat: re-exports everything from ``.channel`` so legacy
``from erza.channels.dingtalk import DingTalkChannel`` keeps working.
"""

from .channel import *  # noqa: F401,F403  — public symbols

__all__ = [
    "DingTalkChannel",
    "DingTalkConfig",
    "ErzaDingTalkHandler",
    "channel",
]
