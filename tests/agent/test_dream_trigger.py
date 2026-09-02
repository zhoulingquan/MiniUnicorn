"""Dream idle trigger + startup backlog counting (Requirement D).

Both the idle trigger and the gateway startup check must count pending
history AND reflection rows; a reflection-only backlog is enough to trigger.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.dream_trigger import DreamIdleTrigger
from miniunicorn.config.schema import StructuredMemoryConfig
from miniunicorn.memory import Dream, MemoryStore, count_pending_dream_entries


@pytest.fixture
def store(tmp_path):
    return MemoryStore(
        tmp_path,
        structured_config=StructuredMemoryConfig(
            auto_promote_verified=True,
        ),
    )


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock(
        return_value=MagicMock(content='{"schema_version":1,"proposals":[]}')
    )
    return p


def write_reflections(store, count: int) -> None:
    rf = store.memory_dir / "reflections.jsonl"
    rf.parent.mkdir(parents=True, exist_ok=True)
    with open(rf, "w", encoding="utf-8") as f:
        for i in range(1, count + 1):
            f.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-11 08:30",
                        "trigger": "tool_error",
                        "iteration": i,
                        "context": "boom",
                        "reflection": f"Lesson {i}.",
                        "lesson": f"Lesson {i}.",
                        "reflection_id": f"rfl_{i:032x}",
                        "session_key": "test",
                    }
                )
                + "\n"
            )


def make_trigger(store, mock_provider, *, min_entries: int) -> DreamIdleTrigger:
    dream = Dream(store=store, provider=mock_provider, model="test-model", max_batch_size=5)
    trigger = DreamIdleTrigger(
        dream,
        enabled=True,
        min_idle_seconds=0,
        min_entries=min_entries,
        min_interval_s=0,
    )
    trigger._last_user_activity_ts = 0.0
    trigger._last_trigger_ts = 0.0
    return trigger


def test_count_pending_dream_entries_counts_both_sources(store):
    store.append_history("H1.", session_key="web:chat-7", user_key="user:alice")
    store.append_history("H2.", session_key="web:chat-8", user_key="user:bob")
    write_reflections(store, 3)

    assert count_pending_dream_entries(store) == 5

    store.set_last_dream_cursor(1)
    assert count_pending_dream_entries(store) == 4

    store.set_last_reflections_cursor(2)
    assert count_pending_dream_entries(store) == 2


@pytest.mark.asyncio
async def test_idle_trigger_fires_on_reflection_only_backlog(store, mock_provider):
    write_reflections(store, 2)
    trigger = make_trigger(store, mock_provider, min_entries=2)

    await trigger.maybe_trigger()

    assert trigger._dream_task is not None
    await trigger._dream_task
    assert trigger.is_running is False
    rf = store.memory_dir / "reflections.jsonl"
    assert not rf.exists() or not rf.read_text(encoding="utf-8").strip()


@pytest.mark.asyncio
async def test_idle_trigger_counts_both_sources_toward_threshold(store, mock_provider):
    store.append_history("H1.")
    write_reflections(store, 1)
    trigger = make_trigger(store, mock_provider, min_entries=2)

    await trigger.maybe_trigger()

    assert trigger._dream_task is not None
    await trigger._dream_task


@pytest.mark.asyncio
async def test_idle_trigger_does_not_fire_below_combined_threshold(store, mock_provider):
    store.append_history("H1.")
    write_reflections(store, 1)
    trigger = make_trigger(store, mock_provider, min_entries=3)

    await trigger.maybe_trigger()

    assert trigger._dream_task is None


def test_gateway_startup_backlog_counts_both_sources(store):
    from miniunicorn.cli._gateway_runner import _dream_backlog_total

    store.append_history("H1.")
    write_reflections(store, 2)

    assert _dream_backlog_total([store]) == 3

    store.set_last_reflections_cursor(1)
    assert _dream_backlog_total([store]) == 2
