"""Static composition root for Erza.

The composition root is the only place that knows how every module is wired
together.  Production code outside ``erza/composition/`` must not
construct the gateway / headless application objects directly; they go
through ``build_gateway_application`` / ``build_agent_application``.

Public surface:
- ``GatewayApplication`` / ``build_gateway_application`` — full gateway
  assembly (bus, sessions, cron, agent, channels, system jobs).
- ``build_agent_application`` — headless AgentLoop assembly shared by the
  ``cli agent``, ``cli serve`` and SDK ``Erza.from_config`` entries.
"""

from erza.composition.agent_app import build_agent_application
from erza.composition.gateway import GatewayApplication, build_gateway_application

__all__ = [
    "build_agent_application",
    "build_gateway_application",
    "GatewayApplication",
]
