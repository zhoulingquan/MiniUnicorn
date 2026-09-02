"""Per-turn call ledger for accounting LLM provider calls."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from miniunicorn.ledger.turn_budget import TurnBudget


class CallPurpose(str, Enum):
    """Purpose of an LLM call, used for ledger accounting."""

    EXECUTOR = "executor"
    PLANNER = "planner"
    REPLAN = "replan"
    REFLECTION = "reflection"
    COMPACT = "compact"
    FINALIZATION = "finalization"
    MEMORY = "memory"
    TOOL = "tool"
    VERIFIER = "verifier"
    UNCLASSIFIED = "unclassified"

    def __str__(self) -> str:
        return self.value


@dataclass
class CallRecord:
    """A single LLM call record logged in the ledger."""

    purpose: CallPurpose
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = "stop"
    order_index: int = 0


def _normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Normalize usage dict: keep int and float-valued fields."""
    if usage is None:
        return {}
    result: dict[str, int | float] = {}
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            result[key] = value
    return result


_CURRENT: ContextVar[CallLedger | None] = ContextVar("call_ledger", default=None)
_ALLOWED_CHILD_LEDGER: ContextVar[CallLedger | None] = ContextVar(
    "allowed_child_call_ledger",
    default=None,
)
_PURPOSE: ContextVar[CallPurpose] = ContextVar(
    "call_purpose",
    default=CallPurpose.UNCLASSIFIED,
)


class CallLedger:
    """Context-local ledger accounting per-turn LLM call usage and budget."""

    def __init__(self, budget: TurnBudget | None = None) -> None:
        self.total_usage: dict[str, int | float] = {}
        self.last_call_usage: dict[str, int | float] = {}
        self.purpose_usage: dict[str, dict[str, int | float]] = {}
        self.records: list[CallRecord] = []
        self._cost_usd: float = 0.0
        self.budget_exceeded_reason: str | None = None
        self._budget: TurnBudget | None = budget
        self._budget_indices: dict[int, int] = {}
        self._owner_task: asyncio.Task[Any] | None = None
        self._binding_depth = 0
        self._ever_bound = False

    @property
    def budget(self) -> TurnBudget | None:
        """Return the budget attached at construction, if any."""
        return self._budget

    @property
    def current_purpose(self) -> CallPurpose:
        return _PURPOSE.get()

    def record(
        self,
        *,
        model: str,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str = "stop",
        purpose: CallPurpose | str | None = None,
    ) -> None:
        if not self._is_accessible():
            return
        normalized = _normalize_usage(usage)

        self.last_call_usage = dict(normalized)

        for key, value in normalized.items():
            self.total_usage[key] = self.total_usage.get(key, 0) + value

        if isinstance(normalized.get("cost_usd"), (int, float)):
            self._cost_usd += float(normalized["cost_usd"])

        purpose_enum = (
            CallPurpose(purpose) if isinstance(purpose, str) else (purpose or self.current_purpose)
        )
        pname = str(purpose_enum)
        if pname not in self.purpose_usage:
            self.purpose_usage[pname] = {}
        for key, value in normalized.items():
            self.purpose_usage[pname][key] = self.purpose_usage[pname].get(key, 0) + value

        self.records.append(
            CallRecord(
                purpose=purpose_enum,
                model=model,
                usage=dict(normalized),
                finish_reason=finish_reason,
                order_index=len(self.records),
            )
        )

        if self._budget is not None:
            self._budget.accumulate(normalized, model)
            self.budget_exceeded_reason = self._budget.check()
            self._budget_indices[id(self._budget)] = len(self.records)
        else:
            self.budget_exceeded_reason = None

    @staticmethod
    def _current_task() -> asyncio.Task[Any] | None:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _activate(self) -> None:
        task = self._current_task()
        if self._binding_depth and self._owner_task is not task:
            raise RuntimeError("CallLedger cannot be bound by multiple tasks")
        self._owner_task = task
        self._binding_depth += 1
        self._ever_bound = True

    def _deactivate(self) -> None:
        self._binding_depth = max(0, self._binding_depth - 1)
        if self._binding_depth == 0:
            self._owner_task = None

    def _is_accessible(self) -> bool:
        if not self._ever_bound:
            return True
        return self._binding_depth > 0 and (
            self._owner_task is self._current_task() or _ALLOWED_CHILD_LEDGER.get() is self
        )

    def check_budget(self, budget: TurnBudget | None = None) -> str | None:
        target_budget = budget if budget is not None else self._budget
        if target_budget is None:
            return None
        if self.budget_exceeded_reason is not None and target_budget is self._budget:
            return self.budget_exceeded_reason

        budget_id = id(target_budget)
        start_idx = self._budget_indices.get(budget_id, 0)
        for record in self.records[start_idx:]:
            target_budget.accumulate(record.usage, record.model)
        self._budget_indices[budget_id] = len(self.records)

        self.budget_exceeded_reason = target_budget.check()
        return self.budget_exceeded_reason


def current_call_ledger() -> CallLedger | None:
    """Return the CallLedger bound to the current context, if any."""
    ledger = _CURRENT.get()
    return ledger if ledger is not None and ledger._is_accessible() else None


@contextmanager
def allow_call_ledger_child_tasks():
    """Allow explicitly spawned child tasks to write the active ledger.

    The permission is inherited through ContextVar copying and remains valid
    only while the owning turn's ledger binding is active.
    """
    ledger = current_call_ledger()
    if ledger is None:
        yield
        return
    token = _ALLOWED_CHILD_LEDGER.set(ledger)
    try:
        yield
    finally:
        _ALLOWED_CHILD_LEDGER.reset(token)


class _LedgerContext:
    """Context manager for binding a CallLedger (supports both sync and async)."""

    def __init__(self, ledger: CallLedger) -> None:
        self._ledger = ledger
        self._token: Any = None

    def __enter__(self) -> CallLedger:
        self._ledger._activate()
        self._token = _CURRENT.set(self._ledger)
        return self._ledger

    def __exit__(self, *args: Any) -> None:
        try:
            _CURRENT.reset(self._token)
        finally:
            self._ledger._deactivate()

    async def __aenter__(self) -> CallLedger:
        self._ledger._activate()
        self._token = _CURRENT.set(self._ledger)
        return self._ledger

    async def __aexit__(self, *args: Any) -> None:
        try:
            _CURRENT.reset(self._token)
        finally:
            self._ledger._deactivate()


def bind_call_ledger(ledger: CallLedger) -> _LedgerContext:
    """Bind *ledger* for the current context.

    Returns a context manager that works with both `with` and `async with`.
    """
    return _LedgerContext(ledger)


def reset_call_ledger() -> None:
    """Reset the current CallLedger context to None."""
    _CURRENT.set(None)
    _PURPOSE.set(CallPurpose.UNCLASSIFIED)


class _DualPurposeContext:
    """Context manager that works with both `with` and `async with`."""

    def __init__(self, purpose: CallPurpose | str | None = None) -> None:
        self._purpose = (
            CallPurpose(purpose)
            if isinstance(purpose, str)
            else (purpose or CallPurpose.UNCLASSIFIED)
        )

    def __enter__(self) -> None:
        self._token = _PURPOSE.set(self._purpose)

    def __exit__(self, *args: Any) -> None:
        _PURPOSE.reset(self._token)

    async def __aenter__(self) -> None:
        self._token = _PURPOSE.set(self._purpose)

    async def __aexit__(self, *args: Any) -> None:
        _PURPOSE.reset(self._token)


def call_purpose(purpose: CallPurpose | str | None = None) -> _DualPurposeContext:
    """Return a context manager that sets the call purpose in the current ledger.

    Works with both `with` and `async with`.

    Usage::

        with call_purpose(CallPurpose.PLANNER):
            response = await provider.chat_with_retry(...)

        async with call_purpose(CallPurpose.PLANNER):
            response = await provider.chat_with_retry(...)
    """
    return _DualPurposeContext(purpose)
