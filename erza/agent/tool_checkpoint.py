"""Per-tool checkpoint record for observability (P1-T5).

A ``ToolCheckpoint`` is emitted through ``spec.checkpoint_callback`` after
each tool completes — success, error, or skipped.  It captures the tool's
intent (call arguments), a truncated result summary, execution status,
wall-clock duration, and the associated plan step id when running in
MANAGED mode.

Design ref: docs/superpowers/specs/2026-08-24-lean-react-kernel-p1-p2-design.md
§ P1 Architecture §5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCheckpoint:
    """Structured per-tool execution record."""

    tool_call_id: str
    tool_name: str
    intent: dict[str, Any]
    result_summary: str
    status: str  # "ok" | "error" | "skipped"
    duration_ms: float
    step_id: int | None = None
    risk_level: str | None = None  # P3-T3: SafetyPolicy classification

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "intent": dict(self.intent),
            "result_summary": self.result_summary,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "step_id": self.step_id,
            "risk_level": self.risk_level,
        }
