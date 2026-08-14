"""Structured Reflection tests: strict JSON and stable evidence IDs.

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
        provider=provider, model="m", workspace=workspace
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
        provider=first_provider, model="m", workspace=workspace
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
        provider=second_provider, model="m", workspace=workspace
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


def test_invalid_reflection_evidence_id_is_rejected():
    invalid = {
        "timestamp": "2026-08-11 08:30",
        "trigger": "tool_error",
        "iteration": 3,
        "context": "boom",
        "reflection": "Always re-read files before patching.",
        "reflection_id": "R1",
        "session_key": "s1",
    }

    assert reflection_evidence_id(invalid) is None


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


# ---------------------------------------------------------------------------
# C2 plan B review closeout: cursor-safe rotation + strict structured parsing
# ---------------------------------------------------------------------------


def _write_entries(path, count: int, *, start_iteration: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(count):
            iteration = start_iteration + i
            entry = {
                "timestamp": f"2026-08-13 09:{iteration % 60:02d}",
                "trigger": "periodic",
                "iteration": iteration,
                "context": f"old {iteration}",
                "reflection": f"Old {iteration}",
                "lesson": f"Old {iteration}",
                "reflection_id": f"rfl_{iteration:032x}",
                "session_key": None,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_rotation_with_full_consumed_cursor_keeps_new_reflection(workspace, store):
    path = workspace / "memory" / "reflections.jsonl"
    _write_entries(path, 500)
    store.set_last_reflections_cursor(500)

    provider = make_provider('{"lesson":"New lesson."}')
    reflection = Reflection(
        provider=provider, model="m", workspace=workspace
    )
    await reflection.reflect(
        trigger="periodic",
        iteration=500,
        context_summary="new",
        messages=[{"role": "user", "content": "x"}],
    )

    persisted = read_jsonl(path)[-1]
    persisted_id = persisted["reflection_id"]
    assert _REFLECTION_ID_RE.match(persisted_id)

    entries = store.read_unprocessed_reflections(since_cursor=0)
    assert len(entries) == 1
    assert entries[0]["lesson"] == "New lesson."
    assert entries[0]["reflection_id"] == persisted_id
    assert store.get_last_reflections_cursor() == 0


@pytest.mark.asyncio
async def test_rotation_never_drops_unconsumed_entries(workspace, store):
    path = workspace / "memory" / "reflections.jsonl"
    _write_entries(path, 501)

    provider = make_provider('{"lesson":"Fresh lesson."}')
    reflection = Reflection(
        provider=provider, model="m", workspace=workspace
    )
    await reflection.reflect(
        trigger="periodic",
        iteration=501,
        context_summary="fresh",
        messages=[{"role": "user", "content": "y"}],
    )

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 502
    assert lines[0]["lesson"] == "Old 0"
    assert lines[-1]["lesson"] == "Fresh lesson."
    assert store.get_last_reflections_cursor() == 0


@pytest.mark.asyncio
async def test_rotation_prunes_consumed_prefix_and_resets_cursor(workspace, store):
    path = workspace / "memory" / "reflections.jsonl"
    _write_entries(path, 501)
    store.set_last_reflections_cursor(200)

    provider = make_provider('{"lesson":"Prefix lesson."}')
    reflection = Reflection(
        provider=provider, model="m", workspace=workspace
    )
    await reflection.reflect(
        trigger="periodic",
        iteration=501,
        context_summary="prefix",
        messages=[{"role": "user", "content": "z"}],
    )

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 302
    assert lines[0]["iteration"] == 200
    assert lines[-1]["lesson"] == "Prefix lesson."
    assert lines[-1]["reflection_id"].startswith("rfl_")
    assert store.get_last_reflections_cursor() == 0


def test_rotation_failure_preserves_canonical_file_and_cursor(workspace, store, monkeypatch):
    import miniunicorn.agent.reflection as reflection_module

    path = workspace / "memory" / "reflections.jsonl"
    _write_entries(path, 501)
    store.set_last_reflections_cursor(200)
    original = path.read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(reflection_module.os, "replace", _boom)
    reflection = Reflection(
        provider=make_provider('{"lesson":"Ignored."}'),
        model="m",
        workspace=workspace,
    )
    reflection._maybe_rotate()

    assert path.read_text(encoding="utf-8") == original
    assert store.get_last_reflections_cursor() == 200


def test_rotation_cursor_reset_failure_does_not_renumber_file(workspace, store, monkeypatch):
    """A cursor write failure must happen before the canonical file is renumbered.

    Otherwise the old physical cursor can skip the newly renumbered unconsumed
    prefix forever. Leaving the original file and cursor untouched is safe.
    """
    import miniunicorn.agent.reflection as reflection_module

    path = workspace / "memory" / "reflections.jsonl"
    cursor_path = workspace / "memory" / ".reflections_cursor"
    _write_entries(path, 501)
    store.set_last_reflections_cursor(200)
    original = path.read_text(encoding="utf-8")
    real_rewrite = reflection_module._atomic_rewrite_lines

    def fail_cursor_reset(target, lines):
        if target == cursor_path:
            return False
        return real_rewrite(target, lines)

    monkeypatch.setattr(reflection_module, "_atomic_rewrite_lines", fail_cursor_reset)
    reflection = Reflection(
        provider=make_provider('{"lesson":"Ignored."}'),
        model="m",
        workspace=workspace,
    )

    reflection._maybe_rotate()

    assert path.read_text(encoding="utf-8") == original
    assert store.get_last_reflections_cursor() == 200


def test_rotation_cursor_beyond_file_resets_without_dropping_entries(workspace, store):
    path = workspace / "memory" / "reflections.jsonl"
    _write_entries(path, 501)
    store.set_last_reflections_cursor(700)
    reflection = Reflection(
        provider=make_provider('{"lesson":"Ignored."}'),
        model="m",
        workspace=workspace,
    )

    reflection._maybe_rotate()

    assert len(read_jsonl(path)) == 501
    assert store.get_last_reflections_cursor() == 0
    assert len(store.read_unprocessed_reflections(since_cursor=0)) == 501


def test_read_stale_cursor_falls_back_to_all_physical_lines(workspace, store):
    path = workspace / "memory" / "reflections.jsonl"
    _write_entries(path, 2)

    entries = store.read_unprocessed_reflections(since_cursor=700)

    assert [entry["iteration"] for entry in entries] == [0, 1]


def test_store_prune_file_rewrite_failure_resets_cursor_before_renumbering(
    workspace, store, monkeypatch
):
    import miniunicorn.agent.memory as memory_module

    path = workspace / "memory" / "reflections.jsonl"
    cursor_path = workspace / "memory" / ".reflections_cursor"
    _write_entries(path, 3)
    store.set_last_reflections_cursor(2)
    original = path.read_text(encoding="utf-8")
    real_rewrite = memory_module._atomic_rewrite_lines

    def fail_file_rewrite(target, lines):
        if target == path:
            return False
        return real_rewrite(target, lines)

    monkeypatch.setattr(memory_module, "_atomic_rewrite_lines", fail_file_rewrite)

    assert store.prune_reflections_after_cursor() == 0
    assert path.read_text(encoding="utf-8") == original
    assert cursor_path.read_text(encoding="utf-8").strip() == "0"
    assert len(store.read_unprocessed_reflections(since_cursor=0)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "just some text",
        "[1, 2]",
        "null",
        "{}",
        '{"lesson": "x", "extra": 1}',
        '{"missing": "x"}',
        '{"lesson": ""}',
        '{"lesson": "   "}',
        '{"lesson": 42}',
        '{"lesson": null}',
        "",
    ],
)
async def test_structured_reflect_rejects_invalid_payload(workspace, payload):
    provider = make_provider(payload)
    reflection = Reflection(
        provider=provider, model="m", workspace=workspace
    )

    result = await reflection.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="boom",
        messages=[{"role": "user", "content": "x"}],
    )

    assert result is None
    assert not (workspace / "memory" / "reflections.jsonl").exists()


@pytest.mark.parametrize(
    "payload, expected",
    [
        ('{"lesson": "A lesson."}', "A lesson."),
        ('{"lesson": "  padded  lesson  "}', "padded  lesson"),
    ],
)
def test_structured_parser_accepts_exact_lesson_object(payload, expected):
    reflection = Reflection(
        provider=MagicMock(), model="m", workspace=None
    )
    assert reflection._parse_structured_response(payload) == expected


# ---------------------------------------------------------------------------
# Governed-memory identity: every reflection row carries stable session/user
# identity so Dream can partition evidence by the exact identity tuple.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_reflection_persists_session_and_user_key(workspace):
    provider = make_provider('{"lesson":"Identity is stable."}')
    reflection = Reflection(provider=provider, model="m", workspace=workspace)

    returned = await reflection.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="boom",
        messages=[{"role": "user", "content": "x"}],
        session_key="web:chat-7#sub:task-1",
        user_key="user:alice",
    )

    assert returned == "Identity is stable."
    entry = read_jsonl(workspace / "memory" / "reflections.jsonl")[0]
    assert entry["session_key"] == "web:chat-7#sub:task-1"
    assert entry["user_key"] == "user:alice"


@pytest.mark.asyncio
async def test_structured_reflection_omits_user_key_when_absent(workspace):
    provider = make_provider('{"lesson":"No user identity."}')
    reflection = Reflection(provider=provider, model="m", workspace=workspace)

    await reflection.reflect(
        trigger="tool_error",
        iteration=1,
        context_summary="boom",
        messages=[{"role": "user", "content": "x"}],
        session_key="web:chat-7",
    )

    entry = read_jsonl(workspace / "memory" / "reflections.jsonl")[0]
    assert entry["session_key"] == "web:chat-7"
    assert "user_key" not in entry
