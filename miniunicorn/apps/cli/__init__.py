"""CLI app adapter for the unified Apps domain.

Re-exports are lazy (PEP 562) so importing ``miniunicorn.apps.cli`` (e.g.
for the lightweight ``utils`` helpers on the gateway main flow) does not
load the ``service`` module until first real use of the feature.
"""

from typing import Any

__all__ = [
    "CliAppError",
    "CliAppManager",
    "CliAppsRuntimeConfig",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from miniunicorn.apps.cli import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)
