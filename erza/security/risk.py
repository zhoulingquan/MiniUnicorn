"""Risk vocabulary shared by agent core and the tools library.

Standalone leaf on purpose: importing it must not trigger
``erza.agent`` package init (import-order safety for the tools
library, see docs/architecture/w5-tools-extraction-plan/).
"""

from __future__ import annotations

from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
