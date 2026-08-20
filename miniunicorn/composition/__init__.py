"""Static composition root for MiniUnicorn.

The composition root is the only place that knows how every module is wired
together.  Production code outside ``miniunicorn/composition/`` must not
construct the gateway / headless application objects directly; they go
through ``build_gateway_application`` / ``build_agent_application``.

Public surface:
- ``GatewayApplication`` / ``build_gateway_application`` — full gateway
  assembly (bus, sessions, cron, agent, channels, system jobs).
- ``build_agent_application`` — headless AgentLoop assembly shared by the
  ``cli agent``, ``cli serve`` and SDK ``Miniunicorn.from_config`` entries.
"""

from miniunicorn.composition.agent_app import build_agent_application
from miniunicorn.composition.gateway import GatewayApplication, build_gateway_application

__all__ = [
    "build_agent_application",
    "build_gateway_application",
    "GatewayApplication",
]
