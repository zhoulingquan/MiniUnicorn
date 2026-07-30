"""Compatibility re-export of the root-owned Runtime configuration.

The canonical definition lives in :mod:`miniunicorn.config.runtime`.
This module re-exports the public names so existing imports under
``miniunicorn.runtime.config`` continue to work during the cutover.
"""

from miniunicorn.config.runtime import (
    RuntimeConfig,
    RuntimeMode,
    parse_runtime_config,
    resolve_runtime_mode,
    resolve_runtime_paths,
)

__all__ = [
    "RuntimeConfig",
    "RuntimeMode",
    "parse_runtime_config",
    "resolve_runtime_mode",
    "resolve_runtime_paths",
]
