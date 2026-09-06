"""Headless agent application assembly.

Static composition root for the non-gateway entry points (``cli agent``,
``cli serve`` and the ``Erza`` SDK facade).  Centralises the
bus / cron-service default creation that used to be duplicated across
``cli/commands.py`` (``agent``, ``serve``) and ``erza/erza.py``
(``Erza.from_config``).

Like the gateway composition root, ``AgentLoop`` is resolved at call time
through ``erza.cli.commands.AgentLoop`` so that test patches on the
``commands`` module namespace (``patch("erza.cli.commands.AgentLoop",
...)``) keep taking effect without touching the tests.
"""

from __future__ import annotations

from typing import Any


def build_agent_application(config, bus=None, cron_service=None, **overrides: Any):
    """Build a headless AgentLoop application from a resolved ``Config``.

    Creates the MessageBus and workspace-scoped CronService when they are not
    supplied, then delegates to ``AgentLoop.from_config`` (parameter semantics
    unchanged).  Extra keyword arguments are forwarded to ``from_config``
    verbatim (e.g. ``session_manager=...`` for the API server entry point).

    The CronService here is only constructed and injected; its lifecycle is the
    caller's responsibility. Only the gateway composition root starts it (and
    stops it); the ``cli agent`` / ``cli serve`` / SDK paths do not.
    """
    # Late imports: bus/cron are patched by tests on their own modules and
    # ``commands`` must be resolved at call time to honour test patches on
    # the ``commands.AgentLoop`` name (same late-binding pattern as the
    # gateway runner).  These imports also avoid import cycles at module load.
    from erza.bus.queue import MessageBus
    from erza.cli import commands
    from erza.cron.service import CronService

    if bus is None:
        bus = MessageBus()
    if cron_service is None:
        cron_service = CronService(config.workspace_path / "cron" / "jobs.json")

    agent_cls = commands.AgentLoop
    return agent_cls.from_config(config, bus, cron_service=cron_service, **overrides)
