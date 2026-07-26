"""Chat channels module with plugin architecture."""

from miniunicorn.channels.base import BaseChannel
from miniunicorn.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
