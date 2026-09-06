"""Message bus module for decoupled channel-agent communication."""

from erza.bus.events import InboundMessage, OutboundMessage
from erza.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
