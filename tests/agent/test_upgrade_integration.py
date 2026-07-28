"""End-to-end integration tests for the upgraded miniunicorn full chain.

Covers the upgraded components and their wiring:
  * Planner (planner.py) -> Plan/PlanStep, create_plan
  * execute_plan tool (tools/execute_plan.py) -> batch spawn subagents
  * delegate tool (tools/delegate.py) -> name-based dispatch via SubagentRegistry
  * SubagentRegistry (subagent_registry.py) -> scans agents/*.md
  * spawn_and_wait (subagent.py) -> await subagent + override passthrough
  * VectorMemoryStore (vector_memory.py) -> sqlite-vec retrieval (NoOp fallback)
  * recall tool (tools/recall.py) -> active memory recall
  * TurnBudget (turn_budget.py) -> cross-turn token accounting
  * Reflection (reflection.py) -> reflection persistence
  * ContextGovernor (context_governor.py) -> pluggable context governance

All tests run fully mocked: no real LLM, no real network.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.context_governor import ContextGovernor
from miniunicorn.agent.planner import Plan, Planner, PlanStep, StepStatus
from miniunicorn.agent.reflection import Reflection
from miniunicorn.agent.subagent_registry import SubagentRegistry
from miniunicorn.agent.tools.context import RequestContext
from miniunicorn.agent.tools.delegate import DelegateTool
from miniunicorn.agent.tools.execute_plan import ExecutePlanTool
from miniunicorn.agent.turn_budget import TurnBudget
from miniunicorn.agent.vector_memory import NoOpVectorStore, VectorMemoryStore
from miniunicorn.bus.queue import MessageBus
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.providers.base import LLMProvider, LLMResponse

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


# ---------------------------------------------------------------------------
# Helpers / factory
# ---------------------------------------------------------------------------


def _make_subagent_manager(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Build a real SubagentManager with a mock provider (no real LLM)."""
    from miniunicorn.agent.subagent import SubagentManager

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        model="test-model",
    )
    return mgr


def _make_mock_manager() -> MagicMock:
    """A lightweight MagicMock stand-in for SubagentManager.

    Used by tests that only care about spawn_and_wait argument capture and
    the concurrency bookkeeping (get_running_count / max_concurrent_subagents).
    """
    mgr = MagicMock()
    mgr.get_running_count.return_value = 0
    mgr.max_concurrent_subagents = 8
    mgr.max_subagent_recursion_depth = 1
    mgr.spawn_and_wait = AsyncMock(return_value=("ok", "result"))
    return mgr


# ---------------------------------------------------------------------------
# 1. Planner -> execute_plan chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_to_execute_plan_chain(tmp_path):
    """Planner produces a Plan; execute_plan consumes it and spawns per step."""
    # --- Planner side: mock provider returns a plan JSON ---
    plan_json = json.dumps(
        {
            "goal": "refactor module X",
            "steps": [
                {"id": 1, "action": "read main.py", "tool_hint": "read_file"},
                {"id": 2, "action": "write tests", "tool_hint": "write_file"},
            ],
        }
    )
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=plan_json,
            tool_calls=[],
            usage={},
        )
    )

    planner = Planner(provider=provider, model="test-model")
    plan = await planner.create_plan(
        task="refactor module X", tools_summary="read_file, write_file"
    )

    assert plan.goal == "refactor module X"
    assert len(plan.steps) == 2
    assert plan.steps[0].action == "read main.py"
    assert plan.steps[1].action == "write tests"
    assert all(s.status == StepStatus.PENDING for s in plan.steps)

    # --- execute_plan side: mock manager records spawn calls ---
    mock_manager = _make_mock_manager()
    spawn_calls = []
    original_spawn = mock_manager.spawn_and_wait

    async def _capture_spawn(**kwargs):
        spawn_calls.append(kwargs)
        return await original_spawn(**kwargs)

    mock_manager.spawn_and_wait = _capture_spawn

    tool = ExecutePlanTool(manager=mock_manager)
    tool.set_context(RequestContext(channel="cli", chat_id="c1", session_key="cli:c1"))

    plan_str = json.dumps(plan.to_dict())
    result = await tool.execute(plan=plan_str, execution="parallel")

    # Both steps spawned exactly once
    assert len(spawn_calls) == 2
    actions = [c["task"] for c in spawn_calls]
    assert "read main.py" in actions
    assert "write tests" in actions
    # Result summary includes goal and ok count
    assert "refactor module X" in result
    assert "OK: 2" in result


# ---------------------------------------------------------------------------
# 2. delegate tool + SubagentRegistry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_with_registry(tmp_path):
    """delegate tool resolves a subagent from the registry and forwards overrides."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "code-reviewer.md").write_text(
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code for bugs and style.\n"
        "model: gpt-4o\n"
        "tools: read_file, search\n"
        "---\n"
        "You are a meticulous code reviewer.\n",
        encoding="utf-8",
    )

    registry = SubagentRegistry(workspace=tmp_path)
    loaded = registry.load()
    assert loaded == 1
    assert registry.get("code-reviewer") is not None

    mock_manager = _make_mock_manager()
    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ("ok", "LGTM")

    mock_manager.spawn_and_wait = _capture

    tool = DelegateTool(manager=mock_manager, registry=registry)
    tool.set_context(RequestContext(channel="cli", chat_id="c1", session_key="cli:c1"))

    result = await tool.execute(subagent="code-reviewer", task="review x")

    assert "LGTM" in result
    assert "code-reviewer" in result
    # Overrides forwarded from the .md definition
    assert captured["system_prompt_override"] == "You are a meticulous code reviewer."
    assert captured["model_override"] == "gpt-4o"
    assert captured["tools_whitelist"] == ["read_file", "search"]
    assert captured["task"] == "review x"
    assert captured["label"] == "code-reviewer"


# ---------------------------------------------------------------------------
# 3. spawn_and_wait override passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_spawn_and_wait_overrides(tmp_path):
    """spawn_and_wait forwards overrides to _run_subagent_direct / AgentRunSpec."""
    from miniunicorn.agent.subagent import SubagentManager

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        model="default-model",
    )

    captured_specs: list = []

    async def fake_run(spec):
        captured_specs.append(spec)
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    status, content = await mgr.spawn_and_wait(
        task="do thing",
        label="ovr",
        system_prompt_override="OVERRIDE PROMPT",
        model_override="override-model",
        tools_whitelist=["read_file"],
    )

    assert status == "ok"
    assert content == "done"
    assert len(captured_specs) == 1
    spec = captured_specs[0]
    # model_override reaches AgentRunSpec.model
    assert spec.model == "override-model"
    # system_prompt_override replaces the system message
    assert spec.initial_messages[0]["role"] == "system"
    assert spec.initial_messages[0]["content"] == "OVERRIDE PROMPT"
    # tools_whitelist restricts the registry
    tool_names = (
        list(spec.tools._tools.keys())
        if hasattr(spec.tools, "_tools")
        else [t.name for t in spec.tools.list_all()]
    )
    assert "read_file" in tool_names
    assert "write_file" not in tool_names


def test_spawn_and_wait_signature_has_overrides():
    """Contract check: spawn_and_wait exposes the three override parameters."""
    from miniunicorn.agent.subagent import SubagentManager

    sig = inspect.signature(SubagentManager.spawn_and_wait)
    params = sig.parameters
    assert "system_prompt_override" in params
    assert "model_override" in params
    assert "tools_whitelist" in params


# ---------------------------------------------------------------------------
# 4. VectorMemoryStore roundtrip (NoOp fallback)
# ---------------------------------------------------------------------------


def test_vector_memory_noop_store_contract():
    """NoOpVectorStore reports disabled and returns empty results."""
    store = NoOpVectorStore()
    assert store.enabled is False
    assert store.index("text", [0.1] * 8) is None
    assert store.search([0.1] * 8, k=5) == []
    assert store.count() == 0
    store.close()  # must not raise


def test_vector_memory_store_disabled_when_no_sqlite_vec(tmp_path, monkeypatch):
    """VectorMemoryStore degrades to disabled when sqlite-vec is unavailable."""
    from miniunicorn.agent import vector_memory as vm

    def _fail_load(_conn):
        return False

    monkeypatch.setattr(vm, "_try_load_sqlite_vec", _fail_load)

    store = VectorMemoryStore(tmp_path / "vec.db", embedding_dim=4)
    assert store.enabled is False
    # Disabled store behaves like NoOp
    assert store.index("hi", [0.1, 0.2, 0.3, 0.4]) is None
    assert store.search([0.1, 0.2, 0.3, 0.4], k=3) == []
    assert store.count() == 0
    store.close()


def test_create_vector_store_falls_back_to_noop(tmp_path, monkeypatch):
    """create_vector_store returns NoOpVectorStore when sqlite-vec is missing."""
    from miniunicorn.agent import vector_memory as vm

    monkeypatch.setattr(vm, "_try_load_sqlite_vec", lambda _conn: False)
    store = vm.create_vector_store(tmp_path / "vec.db", embedding_dim=4)
    assert isinstance(store, NoOpVectorStore)
    assert store.enabled is False


def test_agent_loop_uses_memory_db_for_vector_recall(tmp_path, monkeypatch):
    """Vector recall stores its internal index at memory/memory.db.

    The local embedding provider (FastEmbed/BGE, dim=512) is wired in
    alongside the chat provider; the chat provider is never used for
    embeddings.
    """
    from miniunicorn.agent import vector_memory as vm
    from miniunicorn.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    vector_store = MagicMock()
    create_vector_store = MagicMock(return_value=vector_store)
    monkeypatch.setattr(vm, "create_vector_store", create_vector_store)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
        vector_recall=True,
    )

    create_vector_store.assert_called_once()
    call_args = create_vector_store.call_args
    assert call_args.args[0] == tmp_path / "memory" / "memory.db"
    assert call_args.kwargs["embedding_dim"] == 512
    assert call_args.kwargs["model_id"] == "BAAI/bge-small-zh-v1.5"
    # The loop owns a dedicated local embedding provider, separate from chat.
    assert loop._embedding_provider is not None
    assert loop._embedding_provider is not provider


# ---------------------------------------------------------------------------
# 5. TurnBudget tracking
# ---------------------------------------------------------------------------


def test_turn_budget_accumulate_and_check_under_limit():
    budget = TurnBudget(max_input_tokens=1000, max_output_tokens=500, max_cost_usd=None)
    budget.accumulate({"prompt_tokens": 200, "completion_tokens": 50}, "m")
    budget.accumulate({"prompt_tokens": 300, "completion_tokens": 100}, "m")
    assert budget.used_input == 500
    assert budget.used_output == 150
    assert budget.check() is None  # still under cap
    assert budget.exceeded_reason is None


def test_turn_budget_check_returns_stop_reason_when_input_exceeded():
    budget = TurnBudget(max_input_tokens=1000)
    budget.accumulate({"prompt_tokens": 1500, "completion_tokens": 0}, "m")
    reason = budget.check()
    assert reason is not None
    assert "input_tokens_exceeded" in reason
    assert budget.exceeded_reason == reason


def test_turn_budget_check_idempotent():
    """Once exceeded, check() keeps returning the same reason."""
    budget = TurnBudget(max_output_tokens=100)
    budget.accumulate({"prompt_tokens": 0, "completion_tokens": 200}, "m")
    first = budget.check()
    second = budget.check()
    assert first == second
    assert "output_tokens_exceeded" in first


def test_turn_budget_cost_tracking_via_pricing():
    pricing = {"gpt-4o": (0.01, 0.03)}  # per 1k tokens
    budget = TurnBudget(
        max_input_tokens=None,
        max_output_tokens=None,
        max_cost_usd=0.001,
        pricing=pricing,
    )
    # 50 input * 0.01/1k = 0.0005 ; 10 output * 0.03/1k = 0.0003 -> 0.0008 total
    budget.accumulate({"prompt_tokens": 50, "completion_tokens": 10}, "gpt-4o")
    assert budget.used_cost == pytest.approx(0.0008, rel=1e-6)
    assert budget.check() is None


def test_turn_budget_summary_includes_exceeded_reason():
    budget = TurnBudget(max_input_tokens=10)
    budget.accumulate({"prompt_tokens": 100}, "m")
    budget.check()
    summary = budget.summary()
    assert "BUDGET_EXCEEDED" in summary
    assert "in=100" in summary


# ---------------------------------------------------------------------------
# 6. Reflection persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_persistence(tmp_path, monkeypatch):
    """Reflection writes JSONL entries and reads them back."""
    # Control timestamps so the two entries land in distinct minutes — the
    # JSONL filter uses strict `>` on the "YYYY-MM-DD HH:MM" string, so two
    # entries written within the same minute would otherwise be indistinguishable.
    from miniunicorn.agent import reflection as reflection_mod

    fake_clock = {"tick": 0}

    class _FakeDateTime:
        @classmethod
        def now(cls):
            # First reflection -> 2024-01-01 00:00, second -> 00:01
            minute = fake_clock["tick"]
            fake_clock["tick"] += 1
            return SimpleNamespace(
                strftime=lambda fmt: f"2024-01-01 00:{minute:02d}",
            )

    monkeypatch.setattr(reflection_mod, "datetime", _FakeDateTime)

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content="Lesson A: always validate inputs.", tool_calls=[], usage={}),
            LLMResponse(content="Lesson B: cache misses cost tokens.", tool_calls=[], usage={}),
        ]
    )

    reflection = Reflection(provider=provider, model="m", workspace=tmp_path)

    msg = [{"role": "user", "content": "do something"}]
    r1 = await reflection.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="tool failed",
        messages=msg,
        session_key="s1",
    )
    r2 = await reflection.reflect(
        trigger="periodic",
        iteration=5,
        context_summary="periodic check",
        messages=msg,
        session_key="s1",
    )

    assert r1 is not None and "validate inputs" in r1
    assert r2 is not None and "cache misses" in r2

    # File exists with 2 lines
    reflections_file = tmp_path / "memory" / "reflections.jsonl"
    assert reflections_file.exists()

    entries = reflection.read_unprocessed()
    assert len(entries) == 2
    assert entries[0]["trigger"] == "tool_error"
    assert entries[1]["trigger"] == "periodic"
    assert entries[0]["reflection"] == "Lesson A: always validate inputs."
    assert entries[0]["timestamp"] == "2024-01-01 00:00"
    assert entries[1]["timestamp"] == "2024-01-01 00:01"

    # since_timestamp filtering: only entries strictly newer than the cutoff
    only_newer = reflection.read_unprocessed(since_timestamp=entries[0]["timestamp"])
    assert len(only_newer) == 1
    assert only_newer[0]["reflection"] == "Lesson B: cache misses cost tokens."

    # A cutoff in the far future returns nothing
    assert reflection.read_unprocessed(since_timestamp="2099-12-31 23:59") == []


@pytest.mark.asyncio
async def test_reflection_no_workspace_skips(tmp_path):
    """Reflection with workspace=None must not crash and returns None."""
    provider = MagicMock(spec=LLMProvider)
    reflection = Reflection(provider=provider, model="m", workspace=None)
    result = await reflection.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="x",
        messages=[],
        session_key=None,
    )
    assert result is None
    provider.chat_with_retry.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. ContextGovernor default strategies
# ---------------------------------------------------------------------------


def test_context_governor_default_strategies():
    """ContextGovernor loads the 5 built-in strategies by default."""
    gov = ContextGovernor()
    names = [s.name for s in gov._strategies]
    # 5 unique builtins (plugins may add more, so >= 5)
    assert len(names) >= 5
    assert "drop_orphan_tool_results" in names
    assert "backfill_missing_tool_results" in names
    assert "microcompact" in names
    assert "apply_tool_result_budget" in names
    assert "snip_history" in names


def test_context_governor_get_by_name():
    gov = ContextGovernor()
    assert gov.get("microcompact") is not None
    assert gov.get("nonexistent_strategy") is None


def test_context_governor_builtin_pipeline_order():
    """The BUILTIN_PIPELINE tuple documents the legacy ordering."""
    pipeline = ContextGovernor.BUILTIN_PIPELINE
    assert pipeline[0] == "drop_orphan_tool_results"
    assert "microcompact" in pipeline
    assert "snip_history" in pipeline
    # Cleanup pass repeats drop_orphan + backfill at the end
    assert pipeline[-2:] == ("drop_orphan_tool_results", "backfill_missing_tool_results")


# ---------------------------------------------------------------------------
# 8. SubagentRegistry empty when no agents/ dir
# ---------------------------------------------------------------------------


def test_registry_empty_when_no_agents_dir(tmp_path):
    """No agents/ directory => registry empty, prompt section empty string."""
    registry = SubagentRegistry(workspace=tmp_path)
    count = registry.load()
    assert count == 0
    assert registry.list_all() == []
    assert registry.get("anything") is None
    assert registry.build_prompt_section() == ""


def test_registry_loads_multiple_agents(tmp_path):
    """Sanity: registry loads multiple .md files and lists them in prompts."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "a.md").write_text(
        "---\nname: a\ndescription: agent a\n---\nbody a\n",
        encoding="utf-8",
    )
    (agents_dir / "b.md").write_text(
        "---\nname: b\ndescription: agent b\nmodel: gpt-4o\n---\nbody b\n",
        encoding="utf-8",
    )

    registry = SubagentRegistry(workspace=tmp_path)
    assert registry.load() == 2
    names = {d.name for d in registry.list_all()}
    assert names == {"a", "b"}

    section = registry.build_prompt_section()
    assert "Available Subagents" in section
    assert "delegate(" in section
    assert "- a: agent a" in section
    assert "- b: agent b" in section


# ---------------------------------------------------------------------------
# 9. execute_plan serial mode chains results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_plan_serial_chains_results(tmp_path):
    """serial mode feeds each step's result into the next step's task text."""
    mock_manager = _make_mock_manager()
    spawn_calls: list[dict] = []

    async def _serial_spawn(**kwargs):
        spawn_calls.append(kwargs)
        # First step returns "result_A" so we can verify it reaches step 2.
        if len(spawn_calls) == 1:
            return ("ok", "result_A")
        return ("ok", "result_B")

    mock_manager.spawn_and_wait = _serial_spawn

    tool = ExecutePlanTool(manager=mock_manager)
    tool.set_context(RequestContext(channel="cli", chat_id="c1", session_key="cli:c1"))

    plan_str = json.dumps(
        {
            "goal": "chain test",
            "steps": [
                {"id": 1, "action": "produce A"},
                {"id": 2, "action": "consume A"},
            ],
        }
    )
    result = await tool.execute(plan=plan_str, execution="serial")

    assert len(spawn_calls) == 2
    # Step 1 task is just the action
    assert spawn_calls[0]["task"] == "produce A"
    # Step 2 task includes the previous step's result as context
    assert "consume A" in spawn_calls[1]["task"]
    assert "result_A" in spawn_calls[1]["task"]
    assert "[Previous step result for context]" in spawn_calls[1]["task"]
    # Summary reflects both ok
    assert "OK: 2" in result


# ---------------------------------------------------------------------------
# 10. Full chain mock (lightweight end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_chain_mock(tmp_path):
    """Lightweight end-to-end: Planner -> execute_plan -> spawn_and_wait -> summary.

    Everything mocked: no real LLM, no real subagent execution.
    """
    # --- Stage 1: Planner produces a plan from a mocked LLM response ---
    plan_json = json.dumps(
        {
            "goal": "ship feature",
            "steps": [
                {"id": 1, "action": "design API"},
                {"id": 2, "action": "implement endpoint"},
                {"id": 3, "action": "write tests"},
            ],
        }
    )
    planner_provider = MagicMock(spec=LLMProvider)
    planner_provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=plan_json,
            tool_calls=[],
            usage={},
        )
    )
    planner = Planner(provider=planner_provider, model="test-model")
    plan = await planner.create_plan(task="ship feature", tools_summary="read_file, write_file")
    assert plan.all_done is False
    assert len(plan.steps) == 3

    # --- Stage 2: execute_plan consumes the plan via mock subagent manager ---
    mock_manager = MagicMock()
    mock_manager.get_running_count.return_value = 0
    mock_manager.max_concurrent_subagents = 8
    mock_manager.max_subagent_recursion_depth = 1

    # Per-step results to assert chaining in serial mode would also work,
    # but here we use parallel to keep the test focused on aggregation.
    call_results = [
        ("ok", "designed API v1"),
        ("ok", "implemented /api/v1/feature"),
        ("error", "tests failed: missing import"),
    ]
    call_idx = {"i": 0}

    async def _spawn(**kwargs):
        i = call_idx["i"]
        call_idx["i"] += 1
        return call_results[i]

    mock_manager.spawn_and_wait = _spawn

    tool = ExecutePlanTool(manager=mock_manager)
    tool.set_context(RequestContext(channel="cli", chat_id="c1", session_key="cli:c1"))

    result = await tool.execute(
        plan=json.dumps(plan.to_dict()),
        execution="parallel",
    )

    # --- Stage 3: assert the aggregated summary ---
    assert "ship feature" in result
    assert "Mode: parallel" in result
    assert "Steps: 3" in result
    assert "OK: 2" in result  # two ok, one error
    assert "FAIL" in result
    assert "designed API v1" in result
    assert "implemented /api/v1/feature" in result
    assert "tests failed" in result

    # Plan-level invariants after the chain
    assert isinstance(plan, Plan)
    assert all(isinstance(s, PlanStep) for s in plan.steps)
    assert plan.can_replan is True
