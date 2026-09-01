"""Dynamic effective-workspace memory routing tests (blocker A).

Runtime must resolve one governed ``MemoryStore`` per effective workspace path,
reuse it across turns, and route context building, writes, consolidation, and
Dream processing to that store instead of the default workspace's store.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent._state_machine import TurnContext, TurnState
from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.memory_lifecycle import IngestContext
from miniunicorn.agent.memory_models import (
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    MemoryScope,
    ScopeKind,
)
from miniunicorn.bus.events import InboundMessage
from miniunicorn.bus.queue import MessageBus
from miniunicorn.config.schema import StructuredMemoryConfig

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal(statement: str, *, subject: str):
    return {
        "proposal_index": 0,
        "kind": "decision",
        "scope_hint": "project",
        "subject": subject,
        "slot": "memory.retrieval.strategy",
        "statement": statement,
        "detail": "",
        "tags": ["architecture.memory"],
        "aliases": [],
        "confidence": 1.0,
        "importance": 5,
        "evidence_refs": ["history:1"],
        "speech_act": "confirmed_decision",
        "expires_at": None,
    }


def _seed_active_fact(store, statement: str, *, subject: str) -> None:
    """Ingest and promote an ACTIVE project-scope record via the lifecycle."""
    from miniunicorn.agent.memory_extraction import parse_extraction_batch

    evidence_catalog = {
        "history:1": EvidenceRef(
            kind=EvidenceKind.HISTORY,
            ref="history:1",
            excerpt=statement,
            observed_at=datetime(2026, 8, 11, 8, 30, tzinfo=UTC),
        )
    }
    extracted = parse_extraction_batch(
        json.dumps({"schema_version": 1, "proposals": [_proposal(statement, subject=subject)]}),
        evidence_catalog,
        store.structured_repository.tag_catalog,
    )
    ctx = IngestContext(
        actor=ActorKind.DREAM,
        reason="test seed",
        source_batch=f"seed:{statement}",
        scope=MemoryScope(kind=ScopeKind.PROJECT, key=store.project_scope_key),
        evidence_catalog=evidence_catalog,
        now=datetime.now(UTC),
    )
    result = store.structured_lifecycle.ingest(extracted.proposals[0], ctx)
    store.structured_lifecycle.promote(
        result.candidate_id,
        actor=ActorKind.SYSTEM,
        reason="test seed promote",
    )


def _write_policy(workspace: Path, body: str) -> None:
    policy = workspace / "memory" / "shared" / "POLICY.md"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(body, encoding="utf-8")


def _write_reflection(store, lesson: str, reflection_id: str) -> None:
    rf = store.memory_dir / "reflections.jsonl"
    rf.parent.mkdir(parents=True, exist_ok=True)
    with open(rf, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": "2026-08-11 08:30",
                    "trigger": "tool_error",
                    "iteration": 3,
                    "context": "boom",
                    "reflection": lesson,
                    "lesson": lesson,
                    "reflection_id": reflection_id,
                    "session_key": "websocket:chat-b",
                }
            )
            + "\n"
        )


def _make_loop(bus, provider, workspace, session_ttl_minutes: int = 0):
    from miniunicorn.agent.loop import AgentLoop

    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="test-model",
        session_ttl_minutes=session_ttl_minutes,
    )


def _webui_provider():
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (1_000, "test")
    return provider


def _make_turn_context(loop, session, msg):
    return TurnContext(
        msg=msg,
        session=session,
        session_key=session.key,
        state=TurnState.RESTORE,
        turn_id=f"{session.key}:{time.time_ns()}",
    )


def _b_session(loop, workspace: Path):
    session = loop.sessions.get_or_create("websocket:chat-b")
    session.metadata["workspace_scope"] = {"project_path": str(workspace), "access_mode": "full"}
    return session


# ---------------------------------------------------------------------------
# Context building routes policy / facts / history / notes / audit to B
# ---------------------------------------------------------------------------


def test_context_building_for_workspace_b_uses_b_memory_only(tmp_path: Path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    builder = ContextBuilder(a)
    store_b = builder.memory_for(b)

    _write_policy(a, "POLICY A ONLY\n")
    _write_policy(b, "POLICY B ONLY\n")
    _seed_active_fact(builder.memory, "FACT A ONLY", subject="shared-subject")
    _seed_active_fact(store_b, "FACT B ONLY", subject="shared-subject")
    builder.memory.append_history("HISTORY A ONLY")
    store_b.append_history("HISTORY B ONLY")
    builder.memory.append_notes("NOTES A ONLY")
    store_b.append_notes("NOTES B ONLY")

    prompt_b = builder.build_system_prompt(workspace=b, recall_query="shared-subject")

    assert "FACT B ONLY" in prompt_b
    assert "HISTORY B ONLY" in prompt_b
    assert "POLICY B ONLY" in prompt_b
    assert "NOTES B ONLY" in prompt_b
    assert "FACT A ONLY" not in prompt_b
    assert "HISTORY A ONLY" not in prompt_b
    assert "POLICY A ONLY" not in prompt_b
    assert "NOTES A ONLY" not in prompt_b

    prompt_a = builder.build_system_prompt(recall_query="shared-subject")

    assert "FACT A ONLY" in prompt_a
    assert "HISTORY A ONLY" in prompt_a
    assert "POLICY A ONLY" in prompt_a
    assert "FACT B ONLY" not in prompt_a
    assert "HISTORY B ONLY" not in prompt_a
    assert "POLICY B ONLY" not in prompt_a


def test_recall_audit_written_to_effective_workspace(tmp_path: Path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    builder = ContextBuilder(
        a,
        structured_memory_config=StructuredMemoryConfig(recall_audit_enabled=True),
    )

    builder.build_system_prompt(workspace=b, recall_query="shared-subject")

    b_audit = b / "memory" / "structured" / "recall-audit.jsonl"
    a_audit = a / "memory" / "structured" / "recall-audit.jsonl"
    assert b_audit.exists()
    assert b_audit.read_text(encoding="utf-8").strip()
    assert not a_audit.exists()


# ---------------------------------------------------------------------------
# One store per resolved workspace, reused across prompts/turns
# ---------------------------------------------------------------------------


def test_repeated_resolution_returns_same_store_instance(tmp_path: Path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    builder = ContextBuilder(a)

    first = builder.memory_for(b)
    second = builder.memory_for(b)

    assert first is second
    assert builder.memory_for(a) is builder.memory
    assert builder.memory is not first


def test_concurrent_resolution_is_deterministic(tmp_path: Path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    builder = ContextBuilder(a)

    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(builder.memory_for(b))
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len({id(result) for result in results}) == 1


# ---------------------------------------------------------------------------
# Dream routing: B reflections/history consumed by B's store; A untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_reflection_and_history_consumed_by_b_dream(tmp_path: Path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    loop = _make_loop(MessageBus(), _webui_provider(), a)

    store_b = loop.memory_for(b)
    store_b.append_history("B history fact.", session_key="websocket:chat-b")
    _write_reflection(store_b, "B lesson.", "rfl_00000000000000000000000000000001")

    loop.provider.chat_with_retry = AsyncMock(
        return_value=MagicMock(content='{"schema_version":1,"proposals":[]}')
    )

    dream_b = loop._dream_for(b)
    assert dream_b.store is store_b
    assert await dream_b.run() is True

    assert store_b.get_last_dream_cursor() == 1
    refl_file = b / "memory" / "reflections.jsonl"
    assert not refl_file.exists() or refl_file.read_text(encoding="utf-8").strip() == ""
    assert store_b.structured_repository.current_records() == ()

    store_a = loop.context.memory
    assert store_a.get_last_dream_cursor() == 0
    assert store_a.get_last_reflections_cursor() == 0
    assert not (a / "memory" / "reflections.jsonl").exists()
    assert store_a.structured_repository.current_records() == ()


@pytest.mark.asyncio
async def test_run_all_dreams_covers_non_default_workspaces(tmp_path: Path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    loop = _make_loop(MessageBus(), _webui_provider(), a)

    store_b = loop.memory_for(b)
    store_b.append_history("B-only history for dream.", session_key="websocket:chat-b")
    _write_reflection(store_b, "B-only lesson.", "rfl_11111111111111111111111111111111")
    loop.provider.chat_with_retry = AsyncMock(
        return_value=MagicMock(content='{"schema_version":1,"proposals":[]}')
    )

    assert await loop.run_all_dreams() is True

    assert store_b.get_last_dream_cursor() == 1
    assert loop.context.memory.get_last_dream_cursor() == 0
    assert loop.context.memory.get_last_reflections_cursor() == 0
    assert loop.context.memory.structured_repository.current_records() == ()


# ---------------------------------------------------------------------------
# Write routing through the real AgentLoop path (consolidation + file cap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_routes_consolidation_and_file_cap_to_workspace_b(
    tmp_path: Path,
) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    loop = _make_loop(MessageBus(), _webui_provider(), a)
    store_b = loop.memory_for(b)
    b_root = loop._resolved_root(b)

    fake_b_consolidator = SimpleNamespace(maybe_consolidate_by_tokens=AsyncMock(return_value=None))
    loop._workspace_consolidators[b_root] = fake_b_consolidator
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )

    session = _b_session(loop, b)
    session.add_message("user", "hello from B")

    captured: dict[str, object] = {}

    def capture_file_cap(on_archive=None, limit=2000):
        captured["on_archive"] = on_archive

    session.enforce_file_cap = capture_file_cap  # type: ignore[method-assign]

    msg = InboundMessage(channel="websocket", chat_id="chat-b", sender_id="u1", content="hi")
    ctx = _make_turn_context(loop, session, msg)

    await loop._state_build(ctx)
    await loop._state_save(ctx)
    await asyncio_sleep_zero()

    fake_b_consolidator.maybe_consolidate_by_tokens.assert_awaited()
    loop.consolidator.maybe_consolidate_by_tokens.assert_not_awaited()

    on_archive = captured.get("on_archive")
    assert on_archive is not None
    on_archive([{"role": "user", "content": "B TURN PAYLOAD", "session_key": "websocket:chat-b"}])

    assert any("B TURN PAYLOAD" in entry["content"] for entry in store_b._read_entries()), (
        "file-cap archive must land in B's history"
    )
    assert not any(
        "B TURN PAYLOAD" in entry["content"] for entry in loop.context.memory._read_entries()
    ), "file-cap archive must not leak into A's history"


async def asyncio_sleep_zero() -> None:
    """Yield to the event loop so scheduled background tasks finish."""
    import asyncio

    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# AutoCompact routes an expired non-default-workspace session to B's
# consolidator instead of the default loop.consolidator (A's store).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compact_routes_expired_workspace_b_session_to_b_consolidator(
    tmp_path: Path,
) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    loop = _make_loop(MessageBus(), _webui_provider(), a, session_ttl_minutes=15)
    b_root = loop._resolved_root(b)

    session = _b_session(loop, b)
    session.add_message("user", "hello from B")
    session.updated_at = datetime.now() - timedelta(minutes=30)
    loop.sessions.save(session)

    fake_b_consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value="B summary."))
    loop._workspace_consolidators[b_root] = fake_b_consolidator
    loop.consolidator.compact_idle_session = AsyncMock(  # type: ignore[method-assign]
        return_value="A summary."
    )

    loop.auto_compact.check_expired(loop._schedule_background)
    await asyncio_sleep_zero()

    fake_b_consolidator.compact_idle_session.assert_awaited_once_with(
        session.key,
        loop.auto_compact._RECENT_SUFFIX_MESSAGES,
    )
    loop.consolidator.compact_idle_session.assert_not_awaited()


# ---------------------------------------------------------------------------
# Consolidator token probe must be built against the consolidator's own
# workspace store, not the default workspace's store.
# ---------------------------------------------------------------------------


def test_consolidator_token_probe_uses_workspace_b_prompt(tmp_path: Path) -> None:
    from miniunicorn.agent.memory import Consolidator
    from miniunicorn.session.manager import Session

    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    builder = ContextBuilder(a)
    store_b = builder.memory_for(b)

    _write_policy(a, "POLICY A ONLY\n")
    _write_policy(b, "POLICY B ONLY\n")

    provider = MagicMock()
    provider.estimate_prompt_tokens.return_value = (100, "test")
    consolidator = Consolidator(
        store=store_b,
        provider=provider,
        model="test-model",
        sessions=MagicMock(),
        context_window_tokens=1000,
        build_messages=builder.build_messages,
        get_tool_definitions=MagicMock(return_value=[]),
        max_completion_tokens=100,
    )

    session = Session(key="websocket:chat-b")
    session.add_message("user", "hello from B")

    consolidator.estimate_session_prompt_tokens(session)

    probe_messages = provider.estimate_prompt_tokens.call_args.args[0]
    system_prompt = probe_messages[0]["content"]
    assert "POLICY B ONLY" in system_prompt
    assert "POLICY A ONLY" not in system_prompt


# ---------------------------------------------------------------------------
# Concurrency: WorkspaceMemoryRegistry.known_stores() must snapshot under its
# lock so it synchronizes with concurrent lazy store creation (memory_for).
# ---------------------------------------------------------------------------


def test_known_stores_synchronizes_on_registry_lock(tmp_path: Path) -> None:
    a = tmp_path / "A"
    a.mkdir()
    builder = ContextBuilder(a)
    registry = builder.memory_registry
    b = tmp_path / "B"
    b.mkdir()
    store_b = registry.memory_for(b)
    c = tmp_path / "C"
    c.mkdir()
    store_c = registry.memory_for(c)

    done = threading.Event()
    snapshot: dict[str, list[object]] = {}

    def reader() -> None:
        snapshot["stores"] = registry.known_stores()
        done.set()

    with registry._lock:
        thread = threading.Thread(target=reader)
        thread.start()
        # While the lock is held, known_stores() must block: it must copy the
        # store dict under the same lock that guards lazy store creation.
        assert not done.is_set(), "known_stores() must wait for the registry lock"
        time.sleep(0.05)
    thread.join()

    assert done.is_set()
    assert store_b in snapshot["stores"]
    assert store_c in snapshot["stores"]


# ---------------------------------------------------------------------------
# Concurrency: AgentLoop._sync_runtime_helpers() must snapshot helper dicts
# under the workspace-helper lock and run provider updates outside the lock.
# ---------------------------------------------------------------------------


def test_sync_runtime_helpers_synchronizes_on_helper_lock(tmp_path: Path) -> None:
    a = tmp_path / "A"
    a.mkdir()
    loop = _make_loop(MessageBus(), _webui_provider(), a)

    done = threading.Event()

    def switcher() -> None:
        loop._sync_runtime_helpers(MagicMock(), "test-model", 1000)
        done.set()

    with loop._workspace_helpers_lock:
        thread = threading.Thread(target=switcher)
        thread.start()
        # The snapshot copy must happen under the same lock that guards helper
        # creation, so a concurrent provider switch cannot iterate the dicts
        # while _consolidator_for/_dream_for mutate them.
        assert not done.is_set(), "_sync_runtime_helpers() must wait for the helper lock"
        time.sleep(0.05)
    thread.join()

    assert done.is_set()


def test_sync_runtime_helpers_does_not_hold_lock_during_updates(tmp_path: Path) -> None:
    a = tmp_path / "A"
    a.mkdir()
    loop = _make_loop(MessageBus(), _webui_provider(), a)

    lock_state: dict[str, bool] = {}

    def set_provider(*args, **kwargs) -> None:
        # A non-reentrant lock held by the same thread cannot be re-acquired;
        # acquiring it proves _sync_runtime_helpers released the lock first.
        acquired = loop._workspace_helpers_lock.acquire(blocking=False)
        if acquired:
            loop._workspace_helpers_lock.release()
        lock_state["lock_free"] = acquired

    fake = SimpleNamespace(set_provider=set_provider)
    loop._workspace_consolidators["root-b"] = fake
    loop._workspace_dreams["root-b"] = fake

    loop._sync_runtime_helpers(MagicMock(), "test-model", 1000)

    assert lock_state.get("lock_free") is True
