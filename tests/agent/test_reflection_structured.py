"""Structured-mode Reflection tests: application-assigned stable IDs, legacy fallback.

Normative source: docs/superpowers/specs/2026-08-12-c2-plan-b-hardening-design.md section 4.2
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.memory import MemoryStore, reflection_evidence_id
from miniunicorn.agent.reflection import Reflection, new_reflection_id
from miniunicorn.config.schema import StructuredMemoryConfig

_REFLECTION_ID_RE = re.compile(r"^rfl_[0-9a-f]{32}$")
_LEGACY_REFLECTION_ID_RE = re.compile(r"^rfl_legacy_[0-9a-f]{24}$")


def structured_response_lessons(*lessons: str) -> str:
    return json.dumps({"lesson": "Verify exact evidence IDs." if not lessons else lessons[0]})


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def store(workspace):
    return MemoryStore(
        workspace,
        structured_config=StructuredMemoryConfig(
            mode="governed",
            auto_promote_verified=True,
        ),
    )


def make_provider(*responses: str):
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[MagicMock(content=response) for response in responses]
    )
    return provider


def read_jsonl(path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.mark.asyncio
async def test_structured_reflection_persists_program_generated_id(workspace):
    provider = make_provider('{"lesson":"Verify exact evidence IDs."}')
    reflection = Reflection(
        provider=provider, model="m", workspace=workspace, structured_mode=True
    )

    returned = await reflection.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="boom",
        messages=[{"role": "user", "content": "x"}],
        session_key="s1",
    )

    assert returned == "Verify exact evidence IDs."
    entries = read_jsonl(workspace / "memory" / "reflections.jsonl")
    assert len(entries) == 1
    persisted_id = entries[0]["reflection_id"]
    assert _REFLECTION_ID_RE.match(persisted_id), persisted_id
    assert entries[0]["lesson"] == "Verify exact evidence IDs."
    system_prompt = provider.chat_with_retry.await_args_list[0].kwargs["messages"][0][
        "content"
    ]
    assert "reflection_id" not in system_prompt
    assert "line" not in system_prompt.lower()


@pytest.mark.asyncio
async def test_reflection_ids_stable_across_prune_and_append(workspace, store):
    first_provider = make_provider('{"lesson":"Lesson one."}')
    first = Reflection(
        provider=first_provider, model="m", workspace=workspace, structured_mode=True
    )
    first_result = await first.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="a",
        messages=[{"role": "user", "content": "x"}],
    )
    entries = read_jsonl(workspace / "memory" / "reflections.jsonl")
    first_id = entries[0]["reflection_id"]
    assert _REFLECTION_ID_RE.match(first_id)

    store.set_last_reflections_cursor(1)
    pruned = store.prune_reflections_after_cursor()
    assert pruned == 1
    assert not (workspace / "memory" / "reflections.jsonl").exists() or not (
        workspace / "memory" / "reflections.jsonl"
    ).read_text(encoding="utf-8").strip()

    second_provider = make_provider('{"lesson":"Lesson two."}')
    second = Reflection(
        provider=second_provider, model="m", workspace=workspace, structured_mode=True
    )
    await second.reflect(
        trigger="periodic",
        iteration=2,
        context_summary="b",
        messages=[{"role": "user", "content": "y"}],
    )
    entries = read_jsonl(workspace / "memory" / "reflections.jsonl")
    assert len(entries) == 1
    second_id = entries[0]["reflection_id"]
    assert _REFLECTION_ID_RE.match(second_id)
    assert second_id != first_id
    assert first_result is not None


def test_legacy_reflection_evidence_id_is_deterministic(workspace, store):
    legacy = {
        "timestamp": "2026-08-11 08:30",
        "trigger": "tool_error",
        "iteration": 3,
        "context": "boom",
        "reflection": "Always re-read files before patching.",
        "reflection_id": "R1",
        "session_key": "s1",
    }

    first = reflection_evidence_id(legacy)
    second = reflection_evidence_id(dict(legacy))

    assert first == second
    assert _LEGACY_REFLECTION_ID_RE.match(first), first

    with open(workspace / "memory" / "reflections.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(legacy, ensure_ascii=False) + "\n")
    reloaded = store.read_unprocessed_reflections(since_cursor=0)
    assert len(reloaded) == 1
    assert reflection_evidence_id(reloaded[0]) == first

    changed = dict(legacy)
    changed["reflection"] = "A different lesson entirely."
    assert reflection_evidence_id(changed) != first


def test_valid_reflection_id_is_used_directly():
    entry = {
        "reflection_id": "rfl_0123456789abcdef0123456789abcdef",
        "lesson": "Keep it exact.",
        "reflection": "Keep it exact.",
        "timestamp": "2026-08-11 08:30",
    }
    assert (
        reflection_evidence_id(entry)
        == "rfl_0123456789abcdef0123456789abcdef"
    )


def test_new_reflection_id_format():
    first = new_reflection_id()
    second = new_reflection_id()
    assert _REFLECTION_ID_RE.match(first), first
    assert _REFLECTION_ID_RE.match(second), second
    assert first != second