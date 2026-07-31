"""WP0 — Inventory of direct execution paths the durable runtime replaces.

Design §30 WP0 task 5: "inventory every direct ``process_direct``,
``tool.execute``, Channel send, maintenance ``create_task``, and final
outbound publication."

Each inventory item is expressed as a hard assertion that the legacy
path is gone or routed through a runtime port. These tests prevent
regression of the hard cutover (Task 10).
"""

from __future__ import annotations

import ast
from pathlib import Path

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
# Task 6 Step 8: production Agent modules that must not instantiate
# DirectToolExecutionPort or call tool.execute() outside approved helpers.
_SUBAGENT = _AGENT / "subagent.py"
_AGENT_RUN_ADAPTER = _AGENT / "agent_run_adapter.py"
_COMMAND_BUILTIN = _REPO_ROOT / "miniunicorn" / "command" / "builtin.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _count_pattern(source: str, pattern: str) -> int:
    """Count non-comment occurrences of ``pattern`` in source."""
    return sum(
        1
        for line in source.splitlines()
        if pattern in line and not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# WP3: durable task path — process_direct must be routed through TaskService
# ---------------------------------------------------------------------------


class TestProcessDirectInventory:
    """``process_direct`` bypasses durable acceptance (design §28.4, §29.1)."""

    def test_dispatcher_process_direct_is_removed_or_routed(self) -> None:
        source = _read(_DISPATCHER)
        assert "def process_direct" not in source, (
            "TurnDispatcher.process_direct still exists; WP3 must replace it "
            "with TaskService.submit() + await."
        )

    def test_dispatcher_does_not_create_task_for_turns(self) -> None:
        source = _read(_DISPATCHER)
        assert "asyncio.create_task(self._host._dispatch" not in source, (
            "TurnDispatcher still spawns fire-and-forget dispatch tasks; WP3 "
            "must submit through the durable Scheduler instead."
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

    def test_turn_persistence_does_not_write_runtime_checkpoint(self) -> None:
        source = _read(_PERSISTENCE)
        assert "set_runtime_checkpoint" not in source or "def set_runtime_checkpoint" not in source, (
            "TurnPersistence still defines set_runtime_checkpoint; WP3 must "
            "route through TurnJournalPort.save_checkpoint() instead."
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
    through ``ToolGateway``. Task 6 Step 8 adds architecture gates so
    ``DirectToolExecutionPort(`` cannot reappear in production Agent or
    subagent modules outside the approved ``_resolve_tool_execution_port``
    fallback helper.
    """

    def test_runner_does_not_call_tool_execute_directly(self) -> None:
        source = _read(_RUNNER)
        assert "await tool.execute(" not in source, (
            "AgentRunner still calls tool.execute() directly; WP4 must route "
            "through ToolExecutionPort.execute()."
        )

    def test_registry_execute_is_not_called_from_runner(self) -> None:
        source = _read(_RUNNER)
        assert "spec.tools.execute(" not in source, (
            "AgentRunner still calls spec.tools.execute(); WP4 must route "
            "through ToolExecutionPort.execute() and the ToolGateway."
        )

    @staticmethod
    def _direct_port_call_sites(source: str) -> set[str]:
        """Return enclosing function names where ``DirectToolExecutionPort(...)`` is called."""
        tree = ast.parse(source)
        sites: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "DirectToolExecutionPort"
                ):
                    sites.add(node.name)
        return sites

    def test_subagent_direct_port_only_in_approved_helper(self) -> None:
        """SubagentManager must derive from the root ToolGateway (Task 6 Step 6).

        ``DirectToolExecutionPort(...)`` may appear only inside
        ``_resolve_tool_execution_port`` — the explicit legacy/test
        fallback. Any other call site bypasses the durable safety layer.
        """
        source = _read(_SUBAGENT)
        sites = self._direct_port_call_sites(source)
        assert sites <= {"_resolve_tool_execution_port"}, (
            "SubagentManager instantiates DirectToolExecutionPort outside "
            "_resolve_tool_execution_port; Task 6 Step 6 requires derived "
            f"ports from the root ToolGateway. Found in: {sites}"
        )

    def test_adapter_direct_port_only_in_approved_helper(self) -> None:
        """AgentRunAdapter must raise DURABLE_TOOL_PORT_MISSING in durable mode (Task 6 Step 7).

        ``DirectToolExecutionPort(...)`` may appear only inside
        ``_resolve_tool_execution_port`` — the explicit legacy/test
        fallback for non-durable turns.
        """
        source = _read(_AGENT_RUN_ADAPTER)
        sites = self._direct_port_call_sites(source)
        assert sites <= {"_resolve_tool_execution_port"}, (
            "AgentRunAdapter instantiates DirectToolExecutionPort outside "
            "_resolve_tool_execution_port; Task 6 Step 7 requires "
            "DURABLE_TOOL_PORT_MISSING for durable turns. Found in: "
            f"{sites}"
        )

    @staticmethod
    def _tool_execute_call_sites_outside_direct_port(source: str) -> set[str]:
        """Return enclosing function names where ``tool.execute(`` is called
        outside the approved ``DirectToolExecutionPort`` class.
        """
        tree = ast.parse(source)
        # Find the line range of the DirectToolExecutionPort class so we
        # can skip ``tool.execute(`` calls inside it (the approved helper).
        direct_port_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DirectToolExecutionPort"
            ):
                direct_port_ranges.append((node.lineno, node.end_lineno or node.lineno))
        sites: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if not (isinstance(child, ast.Await) and isinstance(child.value, ast.Call)):
                    continue
                call = child.value
                if not (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "tool"
                ):
                    continue
                # Skip if the call is inside the DirectToolExecutionPort class.
                if any(
                    start <= child.lineno <= end
                    for start, end in direct_port_ranges
                ):
                    continue
                sites.add(node.name)
        return sites

    def test_subagent_does_not_call_tool_execute_directly(self) -> None:
        """SubagentManager must route tool calls through the ToolExecutionPort."""
        source = _read(_SUBAGENT)
        sites = self._tool_execute_call_sites_outside_direct_port(source)
        assert not sites, (
            "SubagentManager still calls tool.execute() directly outside "
            "DirectToolExecutionPort; Task 6 must route through "
            f"ToolExecutionPort.execute(). Found in: {sites}"
        )

    def test_adapter_does_not_call_tool_execute_directly(self) -> None:
        """AgentRunAdapter must route tool calls through the ToolExecutionPort.

        ``tool.execute()`` is allowed only inside the
        :class:`DirectToolExecutionPort` fake/test helper class.
        """
        source = _read(_AGENT_RUN_ADAPTER)
        sites = self._tool_execute_call_sites_outside_direct_port(source)
        assert not sites, (
            "AgentRunAdapter still calls tool.execute() directly outside "
            "DirectToolExecutionPort; Task 6 must route through "
            f"ToolExecutionPort.execute(). Found in: {sites}"
        )


# ---------------------------------------------------------------------------
# WP5: Outbox — final replies must not be published directly to the bus
# ---------------------------------------------------------------------------


class TestDirectOutboundPublicationInventory:
    """Final replies go through ``bus.publish_outbound`` today (design §29.1).

    WP5 routes them through the durable Outbox so delivery is reliable.
    """

    def test_dispatcher_does_not_publish_final_reply_directly(self) -> None:
        source = _read(_DISPATCHER)
        # The final-response publish happens in the dispatch completion path.
        assert "publish_outbound" not in source or _count_pattern(source, "publish_outbound") == 0, (
            "TurnDispatcher still publishes final replies via bus.publish_outbound; "
            "WP5 must enqueue through Outbox atomically with task completion."
        )

    def test_channel_manager_does_not_send_directly_without_receipt(self) -> None:
        """``channel.send(...)`` is allowed only inside ``send_with_receipt``.

        Uses ``ast`` to find all ``Call`` nodes where ``func.attr == "send"``
        and verifies the enclosing function name is ``send_with_receipt``.
        """
        source = _read(_CHANNELS_MANAGER)
        tree = ast.parse(source)
        send_callers: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "send"
                ):
                    send_callers.add(node.name)
        assert send_callers <= {"send_with_receipt"}, (
            "ChannelManager still calls channel.send() outside "
            "send_with_receipt; WP5 must route through send_with_receipt() "
            f"and let Outbox own delivery state. Found in: {send_callers}"
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

    def test_dream_trigger_does_not_own_required_work_via_create_task(self) -> None:
        source = _read(_DREAM_TRIGGER)
        assert "asyncio.create_task(self._safe_run())" not in source, (
            "DreamIdleTrigger still owns required work via asyncio.create_task; "
            "WP7 must submit a durable DREAM task through TaskService.submit_internal()."
        )

    def test_cron_service_does_not_own_required_work_via_create_task(self) -> None:
        source = _read(_CRON_SERVICE)
        assert "asyncio.create_task(self._on_timer())" not in source, (
            "CronService still owns required timer work via asyncio.create_task; "
            "WP7 must submit durable USER_TURN or internal tasks instead."
        )

    def test_gateway_does_not_call_dream_run_directly(self) -> None:
        """Gateway production handlers must not call ``agent.dream.run()``.

        Task 9 Step 6 removed the legacy ``_handle_dream_job`` /
        ``on_cron_job`` direct-dispatch helpers. Dream and other
        maintenance work must be enqueued as durable internal tasks and
        dispatched through :class:`MaintenanceExecutor` inside the Worker
        (design §22.3). A direct ``agent.dream.run()`` call in the
        Gateway bypasses durability and recovery.
        """
        gateway = _REPO_ROOT / "miniunicorn" / "cli" / "_gateway_runner.py"
        source = _read(gateway)
        assert "agent.dream.run()" not in source, (
            "_gateway_runner.py still calls agent.dream.run() directly; "
            "Task 9 must enqueue a durable DREAM task and dispatch through "
            "MaintenanceExecutor."
        )
        assert "def on_cron_job" not in source, (
            "_gateway_runner.py still defines on_cron_job; Task 9 Step 6 "
            "removed direct Cron-job dispatch in favor of durable enqueue."
        )
        assert "def _handle_dream_job" not in source, (
            "_gateway_runner.py still defines _handle_dream_job; Task 9 "
            "Step 6 removed direct Dream execution."
        )


# ---------------------------------------------------------------------------
# Task 13: command handlers must not bypass durable maintenance dispatch
# ---------------------------------------------------------------------------


class TestCommandDirectMaintenanceInventory:
    """Command handlers (e.g. ``/dream``) must submit durable maintenance
    tasks through TaskService, not call ``loop.dream.run()`` in an untracked
    coroutine or publish results directly to the MessageBus (Task 13 Step 2).
    """

    def test_command_builtin_does_not_call_dream_run_directly(self) -> None:
        source = _read(_COMMAND_BUILTIN)
        assert "loop.dream.run()" not in source, (
            "command/builtin.py still calls loop.dream.run() directly; "
            "Task 13 must submit a durable DREAM task through the "
            "maintenance_enqueue callback."
        )

    def test_command_builtin_does_not_create_untracked_dream_task(self) -> None:
        """The ``/dream`` command must not launch ``asyncio.create_task``
        for Dream execution. The only ``asyncio.create_task`` remaining
        in builtin.py is the ``/restart`` one-shot (line-level exception).
        """
        source = _read(_COMMAND_BUILTIN)
        dream_section = source
        # Find the cmd_dream function body and verify no create_task in it.
        if "async def cmd_dream" in dream_section:
            start = dream_section.index("async def cmd_dream")
            # Find the next top-level def/class after cmd_dream
            rest = dream_section[start + len("async def cmd_dream"):]
            next_def = rest.find("\ndef ")
            if next_def == -1:
                next_def = rest.find("\nasync def ")
            if next_def != -1:
                dream_section = dream_section[start : start + len("async def cmd_dream") + next_def]
        assert "asyncio.create_task" not in dream_section, (
            "cmd_dream still uses asyncio.create_task for Dream; "
            "Task 13 must submit through the durable maintenance_enqueue callback."
        )

    def test_command_builtin_does_not_publish_dream_result_to_bus(self) -> None:
        """The ``/dream`` command must not publish the Dream result
        directly to the MessageBus. The durable Outbox owns final delivery.
        """
        source = _read(_COMMAND_BUILTIN)
        # Find the cmd_dream function body
        start = source.index("async def cmd_dream")
        rest = source[start + len("async def cmd_dream"):]
        next_def = rest.find("\ndef ")
        if next_def == -1:
            next_def = rest.find("\nasync def ")
        if next_def != -1:
            dream_body = source[start : start + len("async def cmd_dream") + next_def]
        else:
            dream_body = source[start:]
        assert "bus.publish_outbound" not in dream_body, (
            "cmd_dream still publishes Dream results to bus.publish_outbound; "
            "Task 13 must let the durable Outbox deliver the final result."
        )


# ---------------------------------------------------------------------------
# Inventory totals — documented so migration progress is measurable
# ---------------------------------------------------------------------------


def test_inventory_count_of_process_direct_call_sites() -> None:
    """``process_direct`` references in production source must be zero."""
    source = _read(_DISPATCHER)
    assert source.count("process_direct") == 0


def test_inventory_count_of_publish_outbound_in_dispatcher() -> None:
    """``publish_outbound`` calls in the dispatcher must be zero."""
    source = _read(_DISPATCHER)
    assert _count_pattern(source, "publish_outbound") == 0
