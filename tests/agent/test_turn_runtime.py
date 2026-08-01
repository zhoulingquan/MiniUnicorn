import pytest

from miniunicorn.agent.turn_runtime import (
    TurnRuntime,
    bind_turn_runtime,
    current_turn_runtime,
    require_turn_runtime,
    reset_turn_runtime,
)


def test_turn_runtime_is_absent_by_default():
    assert current_turn_runtime() is None
    with pytest.raises(RuntimeError, match="No turn runtime is bound"):
        require_turn_runtime()


def test_turn_runtime_binding_is_nested_and_resettable():
    outer = TurnRuntime(turn_id="outer", session_key="ws:a")
    inner = TurnRuntime(turn_id="inner", session_key="ws:b")
    outer_token = bind_turn_runtime(outer)
    try:
        assert require_turn_runtime() is outer
        inner_token = bind_turn_runtime(inner)
        try:
            assert require_turn_runtime() is inner
        finally:
            reset_turn_runtime(inner_token)
        assert require_turn_runtime() is outer
    finally:
        reset_turn_runtime(outer_token)
    assert current_turn_runtime() is None
