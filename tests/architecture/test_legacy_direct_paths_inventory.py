"""WP0 — Inventory of direct execution paths the durable runtime replaces.

Design §30 WP0 task 5: "inventory every direct ``process_direct``,
``tool.execute``, Channel send, maintenance ``create_task``, and final
outbound publication."

Each inventory item is expressed as an ``xfail`` test that asserts the
*future* state (the path is gone or routed through a runtime port). Until
the relevant WP lands, the assertion fails — making the migration debt
visible in the test suite. When the WP completes, the xfail flips to a
passing test that prevents regression.

WP mapping (design §30):
- WP3: durable task path — replaces ``process_direct``, turn ``create_task``,
  ``runtime_checkpoint`` / ``pending_user_turn`` authority
- WP4: Tool Gateway — replaces direct ``tool.execute()`` in the runner
- WP5: Outbox — replaces direct ``channel.send`` and ``bus.publish_outbound``
  for final replies
- WP7: maintenance — replaces fire-and-forget ``create_task`` for required
  background work
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT = _REPO_ROOT / "miniunicorn" / "agent"
_RUNNER = _AGENT / "runner.py"
_DISPATCHER = _AGENT / "turn_dispatcher.py"
_PERSISTENCE = _AGENT / "turn_persistence.py"
_TOOLS_REGISTRY = _AGENT / "tools" / "registry.py"
_MESSAGE_TOOL = _AGENT / "tools" / "message.py"
_CHANNELS_MANAGER = _REPO_ROOT / "miniunicorn" / "channels" / "manager.py"
_DREAM_TRIGGER = _AGENT / "dream_trigger.py"
_CRON_SERVICE = _REPO_ROOT / "miniunicorn" / "cron" / "service.py"
_TASK_SUPERVISOR = _REPO_ROOT / "miniunicorn" / "utils" / "task_supervisor.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _count_pattern(source: str, pattern: str) -> int:
    """Count non-comment occurrences of ``pattern`` in source."""
    return sum(
        1
        for line in source.splitlines()
        if pattern in line and not line.lstrip().startswith("#")
    )


def _has_method_call(source: str, method_name: str) -> bool:
    """True if ``method_name`` appears as a call in source (rough check)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == method_name:
            return True
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == method_name:
            return True
    return False


# ---------------------------------------------------------------------------
# WP3: durable task path — process_direct must be routed through TaskService
# ---------------------------------------------------------------------------


class TestProcessDirectInventory:
    """``process_direct`` bypasses durable acceptance (design §28.4, §29.1)."""

    @pytest.mark.xfail(
        reason="WP3: route process_direct through TaskService.submit() per §28.4",
        strict=True,
        raises=AssertionError,
    )
    def test_dispatcher_process_direct_is_removed_or_routed(self) -> None:
        source = _read(_DISPATCHER)
        assert "def process_direct" not in source, (
            "TurnDispatcher.process_direct still exists; WP3 must replace it "
            "with TaskService.submit() + await."
        )

    @pytest.mark.xfail(
        reason="WP3: replace asyncio.create_task turn dispatch with durable submit",
        strict=True,
        raises=AssertionError,
    )
    def test_dispatcher_does_not_create_task_for_turns(self) -> None:
        source = _read(_DISPATCHER)
        assert "asyncio.create_task(self._host._dispatch" not in source, (
            "TurnDispatcher still spawns fire-and-forget dispatch tasks; WP3 "
            "must submit through the durable Scheduler instead."
        )

    @pytest.mark.xfail(
        reason="WP3: remove pending_queues as a process-local task authority",
        strict=True,
        raises=AssertionError,
    )
    def test_dispatcher_does_not_own_pending_queues_registry(self) -> None:
        source = _read(_DISPATCHER)
        assert "self.pending_queues" not in source, (
            "TurnDispatcher still owns pending_queues as a process-local "
            "authority; WP3 must replace it with Runtime Store."
        )


# ---------------------------------------------------------------------------
# WP3: legacy checkpoint / pending_user_turn metadata must not be an authority
# ---------------------------------------------------------------------------


class TestLegacyCheckpointInventory:
    """``runtime_checkpoint`` / ``pending_user_turn`` are recovery authorities
    today; durable tasks must not dual-write them (design §6.22, §29.4)."""

    @pytest.mark.xfail(
        reason="WP3: runtime tasks must not write runtime_checkpoint metadata",
        strict=True,
        raises=AssertionError,
    )
    def test_turn_persistence_does_not_write_runtime_checkpoint(self) -> None:
        source = _read(_PERSISTENCE)
        assert "set_runtime_checkpoint" not in source or "def set_runtime_checkpoint" not in source, (
            "TurnPersistence still defines set_runtime_checkpoint; WP3 must "
            "route through TurnJournalPort.save_checkpoint() instead."
        )

    @pytest.mark.xfail(
        reason="WP3: runtime tasks must not write pending_user_turn metadata",
        strict=True,
        raises=AssertionError,
    )
    def test_turn_persistence_does_not_mark_pending_user_turn(self) -> None:
        source = _read(_PERSISTENCE)
        assert "mark_pending_user_turn" not in source, (
            "TurnPersistence still defines mark_pending_user_turn; WP3 must "
            "use the durable task state machine (WAITING_USER) instead."
        )


# ---------------------------------------------------------------------------
# WP4: Tool Gateway — runner must not call tool.execute() directly
# ---------------------------------------------------------------------------


class TestDirectToolExecutionInventory:
    """The runner invokes tools directly today (design §29.6, §20).

    WP4 replaces both forms with ``ToolExecutionPort.execute()`` routed
    through ``ToolGateway``.
    """

    @pytest.mark.xfail(
        reason="WP4: route tool execution through ToolExecutionPort / ToolGateway",
        strict=True,
        raises=AssertionError,
    )
    def test_runner_does_not_call_tool_execute_directly(self) -> None:
        source = _read(_RUNNER)
        assert "await tool.execute(" not in source, (
            "AgentRunner still calls tool.execute() directly; WP4 must route "
            "through ToolExecutionPort.execute()."
        )

    @pytest.mark.xfail(
        reason="WP4: route tool registry execution through ToolGateway",
        strict=True,
        raises=AssertionError,
    )
    def test_registry_execute_is_not_called_from_runner(self) -> None:
        source = _read(_RUNNER)
        assert "spec.tools.execute(" not in source, (
            "AgentRunner still calls spec.tools.execute(); WP4 must route "
            "through ToolExecutionPort.execute() and the ToolGateway."
        )


# ---------------------------------------------------------------------------
# WP5: Outbox — final replies must not be published directly to the bus
# ---------------------------------------------------------------------------


class TestDirectOutboundPublicationInventory:
    """Final replies go through ``bus.publish_outbound`` today (design §29.1).

    WP5 routes them through the durable Outbox so delivery is reliable.
    """

    @pytest.mark.xfail(
        reason="WP5: route final reply through Outbox, not bus.publish_outbound",
        strict=True,
        raises=AssertionError,
    )
    def test_dispatcher_does_not_publish_final_reply_directly(self) -> None:
        source = _read(_DISPATCHER)
        # The final-response publish happens in the dispatch completion path.
        assert "publish_outbound" not in source or _count_pattern(source, "publish_outbound") == 0, (
            "TurnDispatcher still publishes final replies via bus.publish_outbound; "
            "WP5 must enqueue through Outbox atomically with task completion."
        )

    @pytest.mark.xfail(
        reason="WP5: ChannelManager must expose send_with_receipt to Outbox Sender",
        strict=True,
        raises=AssertionError,
    )
    def test_channel_manager_does_not_send_directly_without_receipt(self) -> None:
        source = _read(_CHANNELS_MANAGER)
        assert "await channel.send(" not in source, (
            "ChannelManager still calls channel.send() directly; WP5 must "
            "route through send_with_receipt() and let Outbox own delivery state."
        )

    @pytest.mark.xfail(
        reason="WP5: MessageTool must enqueue through Outbox, not bus.publish_outbound",
        strict=True,
        raises=AssertionError,
    )
    def test_message_tool_does_not_bind_bus_publish_directly(self) -> None:
        source = _read(_MESSAGE_TOOL)
        assert "bus.publish_outbound" not in source, (
            "MessageTool still binds bus.publish_outbound as its send_callback; "
            "WP5 must enqueue through Outbox and return outbox_id as the receipt."
        )


# ---------------------------------------------------------------------------
# WP7: maintenance — required background work must be durable
# ---------------------------------------------------------------------------


class TestMaintenanceCreateTaskInventory:
    """Dream, consolidation, cron, and cleanup use ``asyncio.create_task``
    today (design §29.16, §22.3). WP7 enqueues them as durable internal tasks."""

    @pytest.mark.xfail(
        reason="WP7: Dream trigger must enqueue a durable DREAM task",
        strict=True,
        raises=AssertionError,
    )
    def test_dream_trigger_does_not_own_required_work_via_create_task(self) -> None:
        source = _read(_DREAM_TRIGGER)
        assert "asyncio.create_task(self._safe_run())" not in source, (
            "DreamIdleTrigger still owns required work via asyncio.create_task; "
            "WP7 must submit a durable DREAM task through TaskService.submit_internal()."
        )

    @pytest.mark.xfail(
        reason="WP7: cron timer must enqueue a durable task, not create_task",
        strict=True,
        raises=AssertionError,
    )
    def test_cron_service_does_not_own_required_work_via_create_task(self) -> None:
        source = _read(_CRON_SERVICE)
        assert "asyncio.create_task(self._on_timer())" not in source, (
            "CronService still owns required timer work via asyncio.create_task; "
            "WP7 must submit durable USER_TURN or internal tasks instead."
        )


# ---------------------------------------------------------------------------
# Inventory totals — documented so migration progress is measurable
# ---------------------------------------------------------------------------


def test_inventory_count_of_process_direct_call_sites() -> None:
    """Current count of ``process_direct`` references in production source.

    This is a passing characterization test — it documents today's debt.
    When WP3 lands, the count drops to zero.
    """
    source = _read(_DISPATCHER)
    count = source.count("process_direct")
    assert count > 0, "process_direct references have been removed — WP3 progress"


def test_inventory_count_of_publish_outbound_in_dispatcher() -> None:
    """Current count of ``publish_outbound`` calls in the dispatcher.

    Passing characterization; WP5 drives this to zero.
    """
    source = _read(_DISPATCHER)
    count = _count_pattern(source, "publish_outbound")
    assert count > 0, "publish_outbound calls removed from dispatcher — WP5 progress"
