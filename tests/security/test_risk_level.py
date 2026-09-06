"""Regression tests for the RiskLevel leaf module (W5-0 pre-batch)."""

from __future__ import annotations

from pathlib import Path

from erza.agent.safety_policy import RiskLevel as RiskLevelFromSafetyPolicy
from erza.security.risk import RiskLevel as RiskLevelFromSecurity

_RISK_PY = Path(__file__).resolve().parents[2] / "erza" / "security" / "risk.py"
_PKG_NAME = "erza"


def test_risklevel_identity_compatibility() -> None:
    assert RiskLevelFromSafetyPolicy is RiskLevelFromSecurity
    assert [member.value for member in RiskLevelFromSecurity] == ["low", "medium", "high"]


def test_risk_module_is_leaf() -> None:
    source = _RISK_PY.read_text(encoding="utf-8")
    assert ("from " + _PKG_NAME) not in source
    assert ("import " + _PKG_NAME) not in source
