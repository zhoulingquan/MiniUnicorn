"""Hosts for the durable runtime (design §9).

- :class:`LightweightHost` — single-process mode for development and testing.
- :class:`SupervisedHost` (WP6) — multi-process mode with Worker isolation,
  spawn supervision, restart backoff, and OS-level child containment.
"""

from __future__ import annotations

from miniunicorn.runtime.hosts.lightweight import LightweightHost
from miniunicorn.runtime.hosts.supervised import RealtimeEventBridge, SupervisedHost

__all__ = ["LightweightHost", "SupervisedHost", "RealtimeEventBridge"]
