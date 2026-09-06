"""Unit tests for the CallLedger call accounting module."""

from __future__ import annotations

import asyncio

from erza.ledger import (
    CallLedger,
    CallPurpose,
    CallRecord,
    allow_call_ledger_child_tasks,
    bind_call_ledger,
    call_purpose,
    current_call_ledger,
    reset_call_ledger,
)


class TestCallPurpose:
    """Test the CallPurpose enum."""

    def test_all_purposes(self) -> None:
        assert CallPurpose.EXECUTOR.value == "executor"
        assert CallPurpose.PLANNER.value == "planner"
        assert CallPurpose.REPLAN.value == "replan"
        assert CallPurpose.REFLECTION.value == "reflection"
        assert CallPurpose.COMPACT.value == "compact"
        assert CallPurpose.FINALIZATION.value == "finalization"
        assert CallPurpose.MEMORY.value == "memory"
        assert CallPurpose.TOOL.value == "tool"
        assert CallPurpose.UNCLASSIFIED.value == "unclassified"

    def test_from_str(self) -> None:
        assert CallPurpose("executor") == CallPurpose.EXECUTOR
        assert CallPurpose("unclassified") == CallPurpose.UNCLASSIFIED


class TestCallRecord:
    """Test the CallRecord dataclass."""

    def test_basic_creation(self) -> None:
        record = CallRecord(
            purpose=CallPurpose.EXECUTOR,
            model="gpt-4o",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            finish_reason="stop",
        )
        assert record.purpose == CallPurpose.EXECUTOR
        assert record.model == "gpt-4o"
        assert record.usage == {"prompt_tokens": 10, "completion_tokens": 20}
        assert record.finish_reason == "stop"
        assert record.order_index == 0

    def test_order_index(self) -> None:
        record = CallRecord(
            purpose=CallPurpose.EXECUTOR,
            model="gpt-4o",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            finish_reason="stop",
            order_index=3,
        )
        assert record.order_index == 3


class TestCallLedgerInitial:
    """Test CallLedger initialization and default state."""

    def test_fresh_ledger_is_empty(self) -> None:
        ledger = CallLedger()
        assert ledger.total_usage == {}
        assert ledger.last_call_usage == {}
        assert ledger.purpose_usage == {}

    def test_record_basic(self) -> None:
        ledger = CallLedger()
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            finish_reason="stop",
            purpose=CallPurpose.EXECUTOR,
        )
        assert ledger.total_usage == {"prompt_tokens": 10, "completion_tokens": 20}
        assert ledger.last_call_usage == {"prompt_tokens": 10, "completion_tokens": 20}
        assert ledger.purpose_usage == {"executor": {"prompt_tokens": 10, "completion_tokens": 20}}

    def test_record_multiple_different_purposes(self) -> None:
        ledger = CallLedger()
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            finish_reason="stop",
            purpose=CallPurpose.EXECUTOR,
        )
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 5, "completion_tokens": 15},
            finish_reason="stop",
            purpose=CallPurpose.PLANNER,
        )
        assert ledger.total_usage == {"prompt_tokens": 15, "completion_tokens": 35}
        assert ledger.last_call_usage == {"prompt_tokens": 5, "completion_tokens": 15}
        assert ledger.purpose_usage == {
            "executor": {"prompt_tokens": 10, "completion_tokens": 20},
            "planner": {"prompt_tokens": 5, "completion_tokens": 15},
        }

    def test_record_same_purpose_tracks_order(self) -> None:
        ledger = CallLedger()
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            finish_reason="stop",
            purpose=CallPurpose.EXECUTOR,
        )
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 5, "completion_tokens": 10},
            finish_reason="stop",
            purpose=CallPurpose.EXECUTOR,
        )
        assert ledger.purpose_usage == {"executor": {"prompt_tokens": 15, "completion_tokens": 30}}


class TestCallLedgerMalformedUsage:
    """Test malformed usage handling."""

    def test_malformed_usage_ignored_field_by_field(self) -> None:
        ledger = CallLedger()
        # usage with non-numeric values should be ignored
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": "bad", "completion_tokens": 20},
            finish_reason="stop",
        )
        # Only completion_tokens should be counted since prompt_tokens is not int
        assert ledger.last_call_usage == {"completion_tokens": 20}

    def test_missing_usage_keys(self) -> None:
        ledger = CallLedger()
        ledger.record(
            model="gpt-4o",
            usage={},
            finish_reason="stop",
        )
        assert ledger.last_call_usage == {}

    def test_none_usage(self) -> None:
        ledger = CallLedger()
        ledger.record(
            model="gpt-4o",
            usage=None,
            finish_reason="stop",
        )
        assert ledger.last_call_usage == {}


class TestCallLedgerBudgetIntegration:
    """Test budget integration with TurnBudget (legacy API)."""

    def test_budget_check_with_none_budget(self) -> None:
        ledger = CallLedger()
        # Should not raise when budget is None
        reason = ledger.check_budget(None)
        assert reason is None

    def test_budget_check_with_budget(self) -> None:
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_cost_usd=1.0)
        # Record a call with cost
        ledger.record(
            model="gpt-4o",
            usage={"cost_usd": 0.5},
            finish_reason="stop",
        )
        # Budget should not be exceeded yet (check_budget accumulates and checks)
        reason = ledger.check_budget(budget)
        assert reason is None
        assert budget.used_cost == 0.5

    def test_budget_exceeded_after_multiple_calls(self) -> None:
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_cost_usd=0.2)
        # Record multiple calls that exceed budget
        ledger.record(model="gpt-4o", usage={"cost_usd": 0.1}, finish_reason="stop")
        reason = ledger.check_budget(budget)
        assert reason is None  # 0.1 < 0.2
        assert budget.used_cost == 0.1
        ledger.record(model="gpt-4o", usage={"cost_usd": 0.1}, finish_reason="stop")
        reason = ledger.check_budget(budget)
        # TurnBudget uses > comparison, so 0.2 > 0.2 is False
        # Need to exceed the limit
        assert reason is None
        assert budget.used_cost == 0.2
        ledger.record(model="gpt-4o", usage={"cost_usd": 0.01}, finish_reason="stop")
        reason = ledger.check_budget(budget)
        assert reason == "cost_exceeded ($0.2100 > $0.2000)"


class TestCallLedgerTurnBudgetIntegration:
    """Test CallLedger integration with TurnBudget (new design)."""

    def test_record_accumulates_to_budget_once(self) -> None:
        """Each record() should call budget.accumulate() exactly once with actual model."""
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_cost_usd=1.0, max_input_tokens=1000, max_output_tokens=500)

        ledger.record(
            model="gpt-4o-mini",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.01},
            finish_reason="stop",
        )
        # Budget only accumulates when check_budget is called
        reason = ledger.check_budget(budget)

        # Budget should have accumulated the usage
        assert budget.used_input == 100
        assert budget.used_output == 50
        assert budget.used_cost == 0.01
        assert budget.exceeded_reason is None
        assert reason is None

    def test_multiple_records_accumulate_correctly(self) -> None:
        """Multiple record() calls should accumulate correctly in budget."""
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_cost_usd=1.0, max_input_tokens=1000, max_output_tokens=500)

        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            finish_reason="stop",
        )
        ledger.record(
            model="gpt-4o-mini",
            usage={"prompt_tokens": 200, "completion_tokens": 100},
            finish_reason="stop",
        )
        # Trigger accumulation
        reason = ledger.check_budget(budget)

        assert budget.used_input == 300
        assert budget.used_output == 150
        assert budget.exceeded_reason is None
        assert reason is None

    def test_check_budget_idempotent(self) -> None:
        """Repeated check_budget() calls should be idempotent and not re-accumulate."""
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_cost_usd=1.0, max_input_tokens=1000, max_output_tokens=500)

        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            finish_reason="stop",
        )

        # First check
        reason1 = ledger.check_budget(budget)
        assert reason1 is None

        # Second check - should not change budget state
        reason2 = ledger.check_budget(budget)
        assert reason2 is None

        # Budget totals should be unchanged
        assert budget.used_input == 100
        assert budget.used_output == 50
        assert budget.used_cost == 0.0

    def test_input_token_limit_exceeded(self) -> None:
        """Input token limit should be enforced via budget.check()."""
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_input_tokens=150, max_output_tokens=500, max_cost_usd=None)

        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            finish_reason="stop",
        )
        reason = ledger.check_budget(budget)
        assert reason is None  # 100 < 150

        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 60, "completion_tokens": 30},
            finish_reason="stop",
        )
        reason = ledger.check_budget(budget)
        assert reason == "input_tokens_exceeded (160 > 150)"

    def test_output_token_limit_exceeded(self) -> None:
        """Output token limit should be enforced via budget.check()."""
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_input_tokens=1000, max_output_tokens=100, max_cost_usd=None)

        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 50, "completion_tokens": 40},
            finish_reason="stop",
        )
        reason = ledger.check_budget(budget)
        assert reason is None  # 40 < 100

        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 50, "completion_tokens": 70},
            finish_reason="stop",
        )
        reason = ledger.check_budget(budget)
        assert reason == "output_tokens_exceeded (110 > 100)"

    def test_cost_limit_exceeded_uses_gt_not_ge(self) -> None:
        """Cost limit should use > comparison (matching TurnBudget.check), not >=."""
        from erza.ledger.turn_budget import TurnBudget

        ledger = CallLedger()
        budget = TurnBudget(max_cost_usd=0.10, max_input_tokens=None, max_output_tokens=None)

        # Exactly at limit should NOT exceed (TurnBudget uses >)
        ledger.record(model="gpt-4o", usage={"cost_usd": 0.10}, finish_reason="stop")
        reason = ledger.check_budget(budget)
        assert reason is None  # 0.10 > 0.10 is False

        # Slightly over should exceed
        ledger.record(model="gpt-4o", usage={"cost_usd": 0.01}, finish_reason="stop")
        reason = ledger.check_budget(budget)
        assert reason == "cost_exceeded ($0.1100 > $0.1000)"

    def test_actual_model_pricing_used(self) -> None:
        """Budget should use actual model from record() for pricing, not hardcoded gpt-4o."""
        from erza.ledger.turn_budget import TurnBudget

        # gpt-4o-mini pricing: $0.15/1M input, $0.60/1M output
        pricing = {"gpt-4o-mini": (0.15 / 1000, 0.60 / 1000)}

        ledger = CallLedger()
        budget = TurnBudget(
            max_cost_usd=0.01, max_input_tokens=None, max_output_tokens=None, pricing=pricing
        )

        # 1000 input tokens * $0.15/1M = $0.00015, 1000 output * $0.60/1M = $0.0006
        # Total = $0.00075
        ledger.record(
            model="gpt-4o-mini",
            usage={"prompt_tokens": 1000, "completion_tokens": 1000},
            finish_reason="stop",
        )

        reason = ledger.check_budget(budget)
        assert reason is None  # $0.00075 < $0.01
        assert abs(budget.used_cost - 0.00075) < 0.00001

    def test_explicit_cost_usd_preferred_over_pricing(self) -> None:
        """Explicit cost_usd in usage should be preferred over calculated pricing."""
        from erza.ledger.turn_budget import TurnBudget

        pricing = {"gpt-4o": (10.0 / 1000, 30.0 / 1000)}  # High pricing

        ledger = CallLedger()
        budget = TurnBudget(
            max_cost_usd=0.5, max_input_tokens=None, max_output_tokens=None, pricing=pricing
        )

        # Explicit cost_usd=0.1 should be used, not calculated from pricing
        ledger.record(
            model="gpt-4o",
            usage={"prompt_tokens": 1000, "completion_tokens": 1000, "cost_usd": 0.1},
            finish_reason="stop",
        )

        reason = ledger.check_budget(budget)
        assert reason is None
        assert budget.used_cost == 0.1  # Not calculated from pricing

    def test_budget_can_be_attached_at_construction(self) -> None:
        """CallLedger may accept/attach a TurnBudget at construction."""
        from erza.ledger.turn_budget import TurnBudget

        budget = TurnBudget(max_cost_usd=1.0)
        ledger = CallLedger(budget=budget)

        assert ledger._budget is budget

    def test_check_budget_uses_attached_budget(self) -> None:
        """check_budget() without argument should use attached budget."""
        from erza.ledger.turn_budget import TurnBudget

        budget = TurnBudget(max_cost_usd=1.0)
        ledger = CallLedger(budget=budget)

        ledger.record(model="gpt-4o", usage={"cost_usd": 0.5}, finish_reason="stop")
        reason = ledger.check_budget()
        assert reason is None
        assert budget.used_cost == 0.5

    def test_check_budget_argument_overrides_attached(self) -> None:
        """check_budget(budget) argument should override attached budget for checking."""
        from erza.ledger.turn_budget import TurnBudget

        attached = TurnBudget(max_cost_usd=1.0)
        override = TurnBudget(max_cost_usd=0.1)
        ledger = CallLedger(budget=attached)

        ledger.record(model="gpt-4o", usage={"cost_usd": 0.5}, finish_reason="stop")
        # check_budget(override) accumulates records to override and checks override's limits
        reason = ledger.check_budget(override)
        assert reason is not None  # Should use override budget's stricter limit
        assert override.used_cost == 0.5
        # Attached budget also has the data (accumulated at record time)
        assert attached.used_cost == 0.5
        # But the check result comes from override budget
        assert reason == "cost_exceeded ($0.5000 > $0.1000)"


class TestCallLedgerBinding:
    """Test context-local binding of CallLedger."""

    def test_bind_and_reset(self) -> None:
        ledger = CallLedger()
        with bind_call_ledger(ledger):
            ledger.record(
                model="gpt-4o",
                usage={"prompt_tokens": 10, "completion_tokens": 20},
                finish_reason="stop",
                purpose=CallPurpose.EXECUTOR,
            )
            assert current_call_ledger() is ledger
        # After exit, should be reset
        assert current_call_ledger() is None

    def test_nested_binding_restores_outer(self) -> None:
        outer = CallLedger()
        with bind_call_ledger(outer):
            inner = CallLedger()
            with bind_call_ledger(inner):
                inner.record(
                    model="gpt-4o",
                    usage={"prompt_tokens": 5, "completion_tokens": 10},
                    finish_reason="stop",
                    purpose=CallPurpose.PLANNER,
                )
            # After inner exit, outer is restored
            assert current_call_ledger() is outer
            assert outer.last_call_usage == {}
            assert inner.last_call_usage == {
                "prompt_tokens": 5,
                "completion_tokens": 10,
            }
        assert current_call_ledger() is None

    def test_nested_purpose_restores_outer(self) -> None:
        ledger = CallLedger()
        with bind_call_ledger(ledger), call_purpose(CallPurpose.PLANNER):
            ledger.record(model="test", usage={"prompt_tokens": 1})
            with call_purpose(CallPurpose.EXECUTOR):
                ledger.record(model="test", usage={"prompt_tokens": 2})
            ledger.record(model="test", usage={"prompt_tokens": 3})

        assert [record.purpose for record in ledger.records] == [
            CallPurpose.PLANNER,
            CallPurpose.EXECUTOR,
            CallPurpose.PLANNER,
        ]

    def test_reset_in_finally(self) -> None:
        ledger = CallLedger()
        try:
            ledger.record(
                model="gpt-4o",
                usage={"prompt_tokens": 10, "completion_tokens": 20},
                finish_reason="stop",
            )
        finally:
            reset_call_ledger()
        # After finally, the module-level var is reset
        assert current_call_ledger() is None


class TestCallLedgerConcurrentIsolation:
    """Test that concurrent tasks don't share ledger state."""

    def test_concurrent_isolation(self) -> None:
        import asyncio

        async def test():
            from erza.ledger import (
                bind_call_ledger,
                current_call_ledger,
            )

            ledger_a = CallLedger()
            ledger_b = CallLedger()

            async with bind_call_ledger(ledger_a):
                ledger_a.record(
                    model="gpt-4o",
                    usage={"prompt_tokens": 10, "completion_tokens": 20},
                    finish_reason="stop",
                    purpose=CallPurpose.EXECUTOR,
                )
                assert current_call_ledger() is ledger_a

            async with bind_call_ledger(ledger_b):
                ledger_b.record(
                    model="gpt-4o",
                    usage={"prompt_tokens": 5, "completion_tokens": 15},
                    finish_reason="stop",
                    purpose=CallPurpose.PLANNER,
                )
                assert current_call_ledger() is ledger_b

            # After both contexts exit
            assert current_call_ledger() is None

        asyncio.run(test())

    def test_child_task_cannot_mutate_parent_ledger(self) -> None:
        async def child(ledger: CallLedger) -> None:
            assert current_call_ledger() is None
            ledger.record(
                model="test-model",
                usage={"prompt_tokens": 20},
                finish_reason="stop",
            )

        async def test() -> None:
            ledger = CallLedger()
            async with bind_call_ledger(ledger):
                ledger.record(
                    model="test-model",
                    usage={"prompt_tokens": 10},
                    finish_reason="stop",
                )
                await asyncio.create_task(child(ledger))

            assert ledger.total_usage == {"prompt_tokens": 10}

        asyncio.run(test())

    def test_explicitly_allowed_child_task_can_record_while_turn_is_active(self) -> None:
        async def child() -> None:
            ledger = current_call_ledger()
            assert ledger is not None
            ledger.record(model="test", usage={"prompt_tokens": 7})

        async def test() -> None:
            ledger = CallLedger()
            async with bind_call_ledger(ledger):
                with allow_call_ledger_child_tasks():
                    await asyncio.create_task(child())

            assert ledger.total_usage == {"prompt_tokens": 7}

        asyncio.run(test())

    def test_allowed_child_cannot_record_after_turn_binding_closes(self) -> None:
        async def test() -> None:
            ledger = CallLedger()
            release = asyncio.Event()

            async def child() -> None:
                await release.wait()
                ledger.record(model="test", usage={"prompt_tokens": 7})

            async with bind_call_ledger(ledger):
                with allow_call_ledger_child_tasks():
                    task = asyncio.create_task(child())

            release.set()
            await task
            assert ledger.total_usage == {}

        asyncio.run(test())


def test_required_cost_tracking_fails_closed_without_cost_or_pricing() -> None:
    from erza.ledger.turn_budget import TurnBudget

    budget = TurnBudget(
        max_input_tokens=None,
        max_output_tokens=None,
        max_cost_usd=0.25,
        require_cost_tracking=True,
    )
    ledger = CallLedger(budget=budget)

    ledger.record(
        model="unknown-model",
        usage={"prompt_tokens": 100, "completion_tokens": 20},
        finish_reason="stop",
    )

    assert ledger.check_budget() == "cost_tracking_unavailable (model=unknown-model)"


def test_explicit_zero_cost_counts_as_available_tracking() -> None:
    from erza.ledger.turn_budget import TurnBudget

    budget = TurnBudget(
        max_input_tokens=None,
        max_output_tokens=None,
        max_cost_usd=0.0,
        require_cost_tracking=True,
    )
    ledger = CallLedger(budget=budget)

    ledger.record(
        model="local-model",
        usage={"prompt_tokens": 10, "cost_usd": 0.0},
        finish_reason="stop",
    )

    assert ledger.check_budget() is None
