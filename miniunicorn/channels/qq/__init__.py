"""QQ channel package.

Backward-compat: re-exports everything from ``.channel`` so legacy
``from miniunicorn.channels.qq import QQChannel`` keeps working.
"""

from .channel import *  # noqa: F401,F403  — public symbols
from .channel import (  # noqa: F401  — private names used by tests
    _guess_send_file_type,
    _is_image_name,
    _sanitize_filename,
)

__all__ = [
    "QQChannel",
    "QQConfig",
    "channel",
]
