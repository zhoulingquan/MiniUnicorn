"""Tests for the prompt memory policy (bounded soul, always core, fallback)."""

from __future__ import annotations

from miniunicorn.agent.memory_prompt import (
    CORE_TOKEN_BUDGET,
    FILE_FALLBACK_TOKEN_BUDGET,
    SOUL_TOKEN_BUDGET,
    MemoryPromptPayload,
    MemoryPromptPolicy,
    extract_always_core,
)
from miniunicorn.agent.memory_recall import RecallOutcome


def write_workspace(tmp_path, user: str, memory: str) -> None:
    (tmp_path / "USER.md").write_text(user, encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "MEMORY.md").write_text(memory, encoding="utf-8")


def test_prompt_uses_always_core_not_full_user_and_memory(tmp_path):
    write_workspace(tmp_path, user="# Always\n叫我小王\n# Archive\n" + "旧资料" * 5000,
                    memory="# Always\n项目用 Python\n# Details\n" + "细节" * 5000)
    payload = MemoryPromptPolicy(tmp_path).build(RecallOutcome((), None, 1.0))
    assert "叫我小王" in payload.text and "项目用 Python" in payload.text
    assert "旧资料" * 100 not in payload.text
    assert payload.token_count <= 5200


def test_fallback_is_bounded_and_explains_reason(tmp_path):
    write_workspace(tmp_path, user="U" * 50000, memory="M" * 50000)
    payload = MemoryPromptPolicy(tmp_path).build(RecallOutcome((), "model_not_ready", 0.0))
    assert payload.mode == "file_fallback"
    assert payload.token_count <= 5200
    assert "model_not_ready" in payload.diagnostic


def test_disabled_recall_produces_empty_disabled_payload(tmp_path):
    write_workspace(tmp_path, user="内容", memory="内容")
    payload = MemoryPromptPolicy(tmp_path).build(RecallOutcome((), "disabled", 0.0))
    assert payload.mode == "disabled"
    assert payload.text == ""
    assert payload.diagnostic == "disabled"


def test_vector_mode_includes_core_and_records_with_identity(tmp_path):
    write_workspace(tmp_path, user="# Always\n核心", memory="# Always\n记忆")
    from miniunicorn.agent.memory_recall import RecallRecord

    record = RecallRecord(
        source_id="user:preferences:1",
        source_type="user",
        source_file="USER.md",
        source_revision="1",
        text="用户喜欢深色主题",
        content_hash="h",
        similarity=0.91,
        score=0.85,
        token_count=10,
        synchronized=True,
    )
    payload = MemoryPromptPolicy(tmp_path).build(RecallOutcome((record,), None, 2.0))
    assert payload.mode == "vector"
    assert "核心" in payload.text
    assert "用户喜欢深色主题" in payload.text
    assert "user:preferences:1" in payload.text
    assert payload.token_count <= 5200


def test_extract_always_core_prefers_always_section():
    markdown = "# Always\n核心内容\n# Archive\n" + "旧" * 5000
    core = extract_always_core(markdown, CORE_TOKEN_BUDGET)
    assert "核心内容" in core
    assert "旧" * 100 not in core


def test_extract_always_core_falls_back_to_leading_content():
    markdown = "# 普通\n" + "内容" * 5000
    core = extract_always_core(markdown, 100)
    assert len(core) < len(markdown)


def test_bounded_soul_truncates_oversized_soul(tmp_path):
    (tmp_path / "SOUL.md").write_text("S" * 100000, encoding="utf-8")
    soul = MemoryPromptPolicy(tmp_path).bounded_soul()
    assert len(soul) < 100000
    assert soul.startswith("SSS")


def test_bounded_soul_missing_file_returns_empty(tmp_path):
    assert MemoryPromptPolicy(tmp_path).bounded_soul() == ""


def test_replace_section_replaces_marked_block_in_system_message():
    start = "<!-- miniunicorn-memory:start -->"
    end = "<!-- miniunicorn-memory:end -->"
    messages = [
        {"role": "system", "content": f"identity\n{start}\n旧内容\n{end}\nskills"},
        {"role": "user", "content": "hi"},
    ]
    payload = MemoryPromptPayload(
        text=f"{start}\n新内容\n{end}", mode="vector", token_count=3, diagnostic=""
    )
    MemoryPromptPolicy.replace_section(messages, payload)
    assert "新内容" in messages[0]["content"]
    assert "旧内容" not in messages[0]["content"]
    assert messages[0]["content"].endswith("\nskills")


def test_replace_section_empty_payload_removes_block():
    start = "<!-- miniunicorn-memory:start -->"
    end = "<!-- miniunicorn-memory:end -->"
    messages = [
        {"role": "system", "content": f"identity\n{start}\n旧内容\n{end}"},
    ]
    MemoryPromptPolicy.replace_section(
        messages, MemoryPromptPayload(text="", mode="disabled", token_count=0, diagnostic="")
    )
    assert start not in messages[0]["content"]
    assert "旧内容" not in messages[0]["content"]


def test_budgets_are_fixed():
    assert SOUL_TOKEN_BUDGET == 4000
    assert CORE_TOKEN_BUDGET == 1200
    assert FILE_FALLBACK_TOKEN_BUDGET == 2400


def test_core_texts_returns_always_core_texts(tmp_path):
    write_workspace(
        tmp_path,
        user="# Always\n叫我小王\n# Archive\n旧内容",
        memory="# Always\n项目用 Python\n# Details\n细节",
    )
    policy = MemoryPromptPolicy(tmp_path)
    assert policy.core_texts() == ["叫我小王", "项目用 Python"]


def test_core_texts_empty_without_files(tmp_path):
    assert MemoryPromptPolicy(tmp_path).core_texts() == []
