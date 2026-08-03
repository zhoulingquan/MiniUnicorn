"""Release soak summary contract tests (Task 26 Step 1).

Validates the pure :func:`assert_release_soak_summary` validator
imported from :mod:`scripts.runtime_soak`. The validator is the
authoritative contract for the 30-minute supervised soak report: it
must accept a valid summary and reject every invalid key with a
diagnostic that names that key.

The validator is pure (no I/O, no sockets, no runtime) so it can be
unit-tested deterministically.
"""

from __future__ import annotations

from typing import Any

import pytest


def _valid_summary() -> dict[str, Any]:
    """Return a summary that satisfies every release assertion."""
    return {
        "worker_ids": ["worker-0", "worker-1", "worker-2"],
        "missing_terminal": 0,
        "missing_final_replies": 0,
        "duplicate_effects": 0,
        "same_session_overlaps": 0,
        "same_session_order_violations": 0,
        "stale_mutations": 0,
        "unresolved_sqlite_busy": 0,
        "children_alive_after_shutdown": 0,
    }


def _import_validator():
    """Import the validator lazily so the red phase fails cleanly."""
    from scripts.runtime_soak import assert_release_soak_summary

    return assert_release_soak_summary


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_summary_passes() -> None:
    """A summary with three distinct workers and all-zero guards passes."""
    assert_release_soak_summary = _import_validator()
    # Must not raise.
    assert_release_soak_summary(_valid_summary())


# ---------------------------------------------------------------------------
# worker_ids contract
# ---------------------------------------------------------------------------


def test_worker_ids_must_be_three_distinct() -> None:
    """Fewer than three distinct worker ids is a release-blocking fault."""
    assert_release_soak_summary = _import_validator()
    summary = _valid_summary()
    summary["worker_ids"] = ["worker-0", "worker-1"]
    with pytest.raises(AssertionError) as exc_info:
        assert_release_soak_summary(summary)
    assert "worker_ids" in str(exc_info.value)


def test_worker_ids_must_be_distinct() -> None:
    """Three entries with a duplicate collapses to two distinct workers."""
    assert_release_soak_summary = _import_validator()
    summary = _valid_summary()
    summary["worker_ids"] = ["worker-0", "worker-1", "worker-0"]
    with pytest.raises(AssertionError) as exc_info:
        assert_release_soak_summary(summary)
    assert "worker_ids" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Parametrized zero-guard contract: each invalid key names itself
# ---------------------------------------------------------------------------

_ZERO_GUARD_KEYS = (
    "missing_terminal",
    "missing_final_replies",
    "duplicate_effects",
    "same_session_overlaps",
    "same_session_order_violations",
    "stale_mutations",
    "unresolved_sqlite_busy",
    "children_alive_after_shutdown",
)


@pytest.mark.parametrize("key", _ZERO_GUARD_KEYS)
def test_nonzero_guard_names_the_key(key: str) -> None:
    """Every non-zero guard must fail and name the offending key."""
    assert_release_soak_summary = _import_validator()
    summary = _valid_summary()
    summary[key] = 1
    with pytest.raises(AssertionError) as exc_info:
        assert_release_soak_summary(summary)
    assert key in str(exc_info.value), f"expected diagnostic to name {key!r}, got: {exc_info.value}"


@pytest.mark.parametrize("key", _ZERO_GUARD_KEYS)
def test_missing_key_raises_keyerror_or_assertion(key: str) -> None:
    """A missing required key must not silently pass."""
    assert_release_soak_summary = _import_validator()
    summary = _valid_summary()
    del summary[key]
    with pytest.raises((AssertionError, KeyError)):
        assert_release_soak_summary(summary)
