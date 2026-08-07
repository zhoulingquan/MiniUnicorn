"""Tests for the deterministic core-test marker policy.

The fast CI gate selects tests by repository-relative path prefix, so the
classification is stable across machines and independent of collection
order. These tests pin the policy so an accidental reclassification is
caught before it reaches CI.
"""

import pytest
from conftest import is_core_test_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/agent/test_runner_core.py", True),
        ("tests/agent/test_turn_concurrency.py", True),
        ("tests/session/test_goal_state.py", True),
        ("tests/providers/test_openai_responses.py", True),
        ("tests/config/test_api_security.py", True),
        ("tests/channels/test_feishu_streaming.py", False),
        ("tests/test_document_parsing.py", False),
    ],
)
def test_core_path_policy(path, expected):
    assert is_core_test_path(path) is expected


def test_core_path_policy_normalizes_backslashes():
    """Windows paths use backslashes; the policy must normalize them."""
    assert is_core_test_path("tests\\agent\\test_runner_core.py") is True
    assert is_core_test_path("tests\\channels\\test_feishu_streaming.py") is False
