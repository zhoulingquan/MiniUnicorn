"""Regression tests for ChannelManager.stop_all shutdown behavior.

Covers the design spec section 4.4 requirement: "Gateway cleanup 使用分层
try/finally,确保无论 channel stop 是否失败,session flush 都会执行。"

These tests verify that:
1. A CancelledError raised by one channel.stop() does not skip remaining
   channels (CancelledError is caught per-channel and re-raised after all
   channels have been stopped).
2. A regular Exception in one channel.stop() does not skip remaining
   channels.
"""

from __future__ import annotations

import asyncio

import pytest

from erza.bus.queue import MessageBus
from erza.channels.base import BaseChannel
from erza.channels.manager import ChannelManager
from erza.config.schema import Config


class _RecordingChannel(BaseChannel):
    """A channel that records whether stop() was called and can raise on stop."""

    name = "recording"
    display_name = "Recording"

    def __init__(self, config, bus, *, stop_exc: BaseException | None = None):
        super().__init__(config, bus)
        self.stop_called = False
        self._stop_exc = stop_exc

    async def start(self) -> None:  # pragma: no cover - not used in these tests
        pass

    async def stop(self) -> None:
        self.stop_called = True
        if self._stop_exc is not None:
            raise self._stop_exc

    async def send(self, message) -> None:  # pragma: no cover - abstract impl
        pass


def _make_manager(*channels: _RecordingChannel) -> ChannelManager:
    cfg = Config()
    bus = MessageBus()
    manager = ChannelManager(cfg, bus)
    # Replace the channels dict directly for testing.
    for i, ch in enumerate(channels):
        manager.channels[ch.name if ch.name != "recording" else f"recording_{i}"] = ch
    return manager


@pytest.mark.asyncio
async def test_stop_all_continues_when_channel_raises_cancelled():
    """A CancelledError from one channel.stop() must not skip remaining
    channels. The CancelledError is re-raised AFTER all channels stopped."""
    ch_a = _RecordingChannel(Config(), MessageBus(), stop_exc=asyncio.CancelledError())
    ch_b = _RecordingChannel(Config(), MessageBus())

    manager = _make_manager(ch_a, ch_b)
    # stop_all re-raises the CancelledError after all channels are stopped.
    with pytest.raises(asyncio.CancelledError):
        await manager.stop_all()

    assert ch_a.stop_called, "channel A stop was not called"
    assert ch_b.stop_called, "channel B stop was skipped after A raised CancelledError"


@pytest.mark.asyncio
async def test_stop_all_continues_when_channel_raises_exception():
    """A regular Exception from one channel.stop() must not skip remaining
    channels."""
    ch_a = _RecordingChannel(Config(), MessageBus(), stop_exc=RuntimeError("boom"))
    ch_b = _RecordingChannel(Config(), MessageBus())

    manager = _make_manager(ch_a, ch_b)
    # Should not raise — the exception is logged and swallowed.
    await manager.stop_all()

    assert ch_a.stop_called, "channel A stop was not called"
    assert ch_b.stop_called, "channel B stop was skipped after A raised"


@pytest.mark.asyncio
async def test_stop_all_reraises_cancelled_after_all_channels_stopped():
    """When a channel raises CancelledError, stop_all should re-raise it
    AFTER all channels have been stopped, preserving cancellation semantics
    without losing cleanup of remaining channels."""
    ch_a = _RecordingChannel(Config(), MessageBus(), stop_exc=asyncio.CancelledError())
    ch_b = _RecordingChannel(Config(), MessageBus())
    ch_c = _RecordingChannel(Config(), MessageBus())

    manager = _make_manager(ch_a, ch_b, ch_c)

    with pytest.raises(asyncio.CancelledError):
        await manager.stop_all()

    # All three channels must have been stopped, even though A raised.
    assert ch_a.stop_called
    assert ch_b.stop_called
    assert ch_c.stop_called


@pytest.mark.asyncio
async def test_stop_all_first_cancelled_is_reraised():
    """If multiple channels raise CancelledError, the first one is re-raised."""
    ch_a = _RecordingChannel(Config(), MessageBus(), stop_exc=asyncio.CancelledError())
    ch_b = _RecordingChannel(Config(), MessageBus(), stop_exc=asyncio.CancelledError())

    manager = _make_manager(ch_a, ch_b)

    with pytest.raises(asyncio.CancelledError):
        await manager.stop_all()

    assert ch_a.stop_called
    assert ch_b.stop_called
