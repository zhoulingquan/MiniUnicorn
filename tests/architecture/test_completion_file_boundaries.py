"""Completion-stage file boundary gates.

Asserts that files refactored during the channel-boundary stage (Stage B3,
Task 15) stay within their physical line budget after extraction.  These
gates prevent regressions where logic creeps back into the façade module
and re-inflates it past the agreed ceiling.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHANNEL = _REPO_ROOT / "miniunicorn" / "channels" / "websocket" / "channel.py"
_FEISHU_CHANNEL = _REPO_ROOT / "miniunicorn" / "channels" / "feishu" / "channel.py"
_WEIXIN_CHANNEL = _REPO_ROOT / "miniunicorn" / "channels" / "weixin" / "channel.py"

# Task 15 ceiling: channel.py must stay below 1450 physical lines after the
# outbound emission extraction.  If this test fails, outbound logic has
# leaked back into the channel façade — move it to ``outbound.py``.
_CHANNEL_MAX_LINES = 1450

# Task 17 ceiling: feishu/channel.py must stay below 1750 physical lines
# after the media-operations extraction.  If this test fails, media logic
# has leaked back into the channel façade — move it to ``media.py``.
_FEISHU_CHANNEL_MAX_LINES = 1750

# Task 18 ceiling: weixin/channel.py must stay below 1150 physical lines
# after the crypto/API/media extraction.  If this test fails, logic has
# leaked back into the channel façade — move it to the extracted service.
_WEIXIN_CHANNEL_MAX_LINES = 1150


def _count_lines(path: Path) -> int:
    """Return the number of physical lines in *path* (including blanks)."""
    return len(path.read_text(encoding="utf-8").splitlines())


def test_channel_py_under_line_budget() -> None:
    """``channel.py`` must stay below the post-extraction line ceiling."""
    actual = _count_lines(_CHANNEL)
    assert actual < _CHANNEL_MAX_LINES, (
        f"channel.py grew to {actual} lines (ceiling {_CHANNEL_MAX_LINES}); "
        "move outbound logic back into outbound.py"
    )


def test_feishu_channel_py_under_line_budget() -> None:
    """Feishu ``channel.py`` must stay below the post-extraction line ceiling."""
    actual = _count_lines(_FEISHU_CHANNEL)
    assert actual < _FEISHU_CHANNEL_MAX_LINES, (
        f"feishu/channel.py grew to {actual} lines "
        f"(ceiling {_FEISHU_CHANNEL_MAX_LINES}); "
        "move media logic back into media.py"
    )


def test_weixin_channel_py_under_line_budget() -> None:
    """Weixin ``channel.py`` must stay below the post-extraction line ceiling."""
    actual = _count_lines(_WEIXIN_CHANNEL)
    assert actual < _WEIXIN_CHANNEL_MAX_LINES, (
        f"weixin/channel.py grew to {actual} lines "
        f"(ceiling {_WEIXIN_CHANNEL_MAX_LINES}); "
        "move crypto/API/media logic back into the extracted services"
    )
