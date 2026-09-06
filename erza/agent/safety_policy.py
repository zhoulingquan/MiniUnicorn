"""SafetyPolicy: tool risk classification orthogonal to planning complexity.

Provides deterministic risk classification for tools:
- LOW: read-only / retrieval tools
- MEDIUM: external side effects (messages, web posts)
- HIGH: local write / execute / delete tools

Pure functional policy; can be overridden by spec (like PlanningPolicy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from erza.security.risk import RiskLevel

if TYPE_CHECKING:
    from erza.tools.base import Tool


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    risk_level: RiskLevel
    requires_checkpoint: bool
    reason: str


class SafetyPolicy:
    """Classifies tool risk; orthogonal to planning complexity."""

    def evaluate(self, tool_name: str, tool: Tool | None) -> SafetyVerdict:
        # 1. Explicit risk_level on tool takes priority
        if tool is not None:
            explicit = getattr(tool, "risk_level", None)
            if explicit is not None:
                return SafetyVerdict(
                    risk_level=explicit,
                    requires_checkpoint=explicit == RiskLevel.HIGH,
                    reason=f"explicit risk_level={explicit.value}",
                )

        # 2. Infer from read_only property
        if tool is not None and getattr(tool, "read_only", False):
            return SafetyVerdict(
                risk_level=RiskLevel.LOW,
                requires_checkpoint=False,
                reason="read_only tool inferred as LOW",
            )

        # 3. Unclassified non-read_only defaults to MEDIUM (err on side of caution)
        return SafetyVerdict(
            risk_level=RiskLevel.MEDIUM,
            requires_checkpoint=False,
            reason="unclassified non-read_only tool defaults to MEDIUM",
        )
