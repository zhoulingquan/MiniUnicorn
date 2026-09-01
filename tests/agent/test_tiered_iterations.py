"""T1: Tiered max tool iterations per planning mode.

FAST tier defaults to 50 iterations, MANAGED tier defaults to 200.
Explicit max_iterations always wins. Tier overrides work via config fields.
"""

from __future__ import annotations

from miniunicorn.agent.loop import AgentLoop
from miniunicorn.config.schema import Config


class FakeProvider:
    def get_default_model(self) -> str:
        return "test-model"

    class Generation:
        max_tokens = 8192

    generation = Generation()

    async def chat_with_retry(self, **kwargs):
        from miniunicorn.providers.base import LLMResponse

        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
        )

    async def chat_stream_with_retry(self, **kwargs):
        from miniunicorn.providers.base import LLMResponse

        return LLMResponse(
            content="",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=[],
            finish_reason="stop",
        )


def _config(defaults: dict) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": defaults},
            "providers": {"custom": {"api_key": "sk-test", "api_base": "http://test"}},
            "tools": {},
        }
    )


def _loop(defaults: dict) -> AgentLoop:
    return AgentLoop.from_config(_config(defaults), provider=FakeProvider())


def test_fast_default_50() -> None:
    """usePlanner=False → loop.max_iterations == 50"""
    loop = _loop({"usePlanner": False})
    assert loop.max_iterations == 50


def test_managed_default_200() -> None:
    """usePlanner=True → loop.max_iterations == 200"""
    loop = _loop({"usePlanner": True})
    assert loop.max_iterations == 200


def test_explicit_max_iterations_wins() -> None:
    """显式 maxToolIterations=120 → 120（两种模式都验证）"""
    loop_fast = _loop({"usePlanner": False, "maxToolIterations": 120})
    assert loop_fast.max_iterations == 120

    loop_managed = _loop({"usePlanner": True, "maxToolIterations": 120})
    assert loop_managed.max_iterations == 120


def test_fast_tier_override() -> None:
    """fastMaxToolIterations=30 → 30"""
    loop = _loop({"usePlanner": False, "fastMaxToolIterations": 30})
    assert loop.max_iterations == 30


def test_managed_tier_override() -> None:
    """managedMaxToolIterations=300 → 300"""
    loop = _loop({"usePlanner": True, "managedMaxToolIterations": 300})
    assert loop.max_iterations == 300


def test_config_propagation() -> None:
    """AgentLoop.from_config 全链路（config→builder→loop）字段到达"""
    config = _config(
        {
            "usePlanner": False,
            "fastMaxToolIterations": 25,
            "managedMaxToolIterations": 150,
        }
    )
    loop = AgentLoop.from_config(config, provider=FakeProvider())
    assert loop.max_iterations == 25

    config2 = _config(
        {
            "usePlanner": True,
            "fastMaxToolIterations": 25,
            "managedMaxToolIterations": 150,
        }
    )
    loop2 = AgentLoop.from_config(config2, provider=FakeProvider())
    assert loop2.max_iterations == 150
