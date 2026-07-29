"""Root-level pytest configuration.

Defines the deterministic core-test marker policy: tests whose
repository-relative path matches one of ``CORE_PREFIXES`` are automatically
marked with ``pytest.mark.core`` (unless explicitly marked ``slow``), so
the fast CI gate (``pytest -m core``) selects a stable, machine-independent
subset without requiring per-test decorators.
"""

from __future__ import annotations

import pytest

# Repository-relative path prefixes that identify "core" tests — the fast
# correctness gate covering orchestration, sessions, providers, config, and
# security. Channel adapters, integration suites, and top-level misc tests
# are intentionally excluded so the gate stays fast.
CORE_PREFIXES = (
    "tests/agent/test_runner_",
    "tests/agent/test_loop_runner_integration.py",
    "tests/agent/test_loop_progress.py",
    "tests/agent/test_loop_save_turn.py",
    "tests/agent/test_turn_",
    "tests/session/",
    "tests/providers/",
    "tests/config/",
    "tests/security/",
    "tests/runtime/",
)


def is_core_test_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a core test by repository-relative prefix.

    Backslashes (Windows) are normalized to forward slashes before matching.
    """

    normalized = path.replace("\\", "/")
    return any(normalized.startswith(prefix) for prefix in CORE_PREFIXES)


def pytest_collection_modifyitems(config, items):
    """Auto-mark core tests with ``pytest.mark.core``.

    Tests explicitly marked ``slow`` are skipped so they are excluded from
    the fast gate even when their path would otherwise qualify.
    """

    for item in items:
        repo_relative = item.path.relative_to(config.rootpath).as_posix()
        if is_core_test_path(repo_relative) and "slow" not in item.keywords:
            item.add_marker(pytest.mark.core)
