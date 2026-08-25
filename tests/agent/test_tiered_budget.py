"""P2-T3: Tiered turn budgets.

FAST ReAct turns get lower default ceilings (80k input / $2) while MANAGED
plan-and-execute turns keep P0 headroom (200k / $5). Explicit legacy fields
(``maxInputTokensPerTurn`` / ``maxCostPerTurnUsd``) always take priority over
the tiered defaults.
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


def test_fast_defaults_80k_and_2usd() -> None:
    loop = _loop({"usePlanner": False})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 80_000
    assert budget.max_cost_usd == 2.0


def test_managed_defaults_200k_and_5usd() -> None:
    loop = _loop({"usePlanner": True})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 200_000
    assert budget.max_cost_usd == 5.0


def test_explicit_override_takes_priority() -> None:
    loop = _loop(
        {
            "usePlanner": False,
            "maxInputTokensPerTurn": 1234,
            "maxCostPerTurnUsd": 0.25,
        }
    )
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 1234
    assert budget.max_cost_usd == 0.25
    assert budget.require_cost_tracking is True


def test_explicit_input_with_tiered_cost_default() -> None:
    """Only maxInputTokensPerTurn set: it wins; cost falls back to the tier."""
    loop = _loop({"usePlanner": False, "maxInputTokensPerTurn": 5000})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 5000
    assert budget.max_cost_usd == 2.0
    # Tiered cost caps stay advisory: no hard failure when cost is untracked.
    assert budget.require_cost_tracking is False


def test_fast_tier_fields_overridable() -> None:
    loop = _loop(
        {
            "usePlanner": False,
            "fastMaxInputTokensPerTurn": 50_000,
            "fastMaxCostPerTurnUsd": 1.0,
        }
    )
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 50_000
    assert budget.max_cost_usd == 1.0


def test_managed_tier_fields_overridable() -> None:
    loop = _loop(
        {
            "usePlanner": True,
            "managedMaxInputTokensPerTurn": 300_000,
            "managedMaxCostPerTurnUsd": 8.0,
        }
    )
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 300_000
    assert budget.max_cost_usd == 8.0


def test_explicit_beats_tiered_fields() -> None:
    """The legacy explicit field outranks the tiered field."""
    loop = _loop(
        {
            "usePlanner": False,
            "maxInputTokensPerTurn": 999_999,
            "fastMaxInputTokensPerTurn": 50_000,
        }
    )
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_input_tokens == 999_999


def test_zero_cost_tier_respected() -> None:
    """An explicit 0.0 tiered cost cap must not fall back to the default."""
    loop = _loop({"usePlanner": False, "fastMaxCostPerTurnUsd": 0.0})
    budget = loop._build_turn_budget()
    assert budget is not None
    assert budget.max_cost_usd == 0.0
