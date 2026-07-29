"""Hosts for the durable runtime (design §9).

- :class:`LightweightHost` — single-process mode for development and testing.
- SupervisedHost (WP7) — multi-process mode with Worker isolation.
"""

from __future__ import annotations

from miniunicorn.runtime.hosts.lightweight import LightweightHost

__all__ = ["LightweightHost"]
