import json

import pytest

from miniunicorn.agent.explicit_memory import ExplicitMemoryJournal
from miniunicorn.agent.memory_sources import MemorySourceCatalog


def test_catalog_indexes_only_effective_explicit_revision(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("浅色", "用户喜欢浅色主题", None)
    journal.append_update(first.memory_id, "深色", "用户喜欢深色主题", None)
    records = [r for r in MemorySourceCatalog(tmp_path).scan().records if r.source_type == "explicit"]
    assert len(records) == 1
    assert records[0].source_id == f"explicit:{first.memory_id}"
    assert records[0].source_revision == "2"
    assert records[0].text == "用户喜欢深色主题"
    assert records[0].importance == 1.0
    assert records[0].source_file == "memory/explicit.jsonl"
    assert records[0].metadata["memory_id"] == first.memory_id
    assert records[0].metadata["revision"] == 2


def test_update_keeps_history_but_only_latest_is_effective(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("我喜欢浅色主题", "用户喜欢浅色主题", None)
    second = journal.append_update(first.memory_id, "我喜欢深色主题", "用户喜欢深色主题", None)
    assert second.revision == 2
    assert second.supersedes_revision == 1
    assert [row.revision for row in journal.history(first.memory_id)] == [1, 2]
    assert ExplicitMemoryJournal(tmp_path).effective()[0].normalized_fact == "用户喜欢深色主题"


def test_invalid_trailing_line_does_not_hide_valid_revisions(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    saved = journal.append_new("叫我小王", "称呼用户为小王", None)
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert journal.effective()[0].memory_id == saved.memory_id
    assert journal.errors()[0].line == 2


def test_restore_appends_a_new_revision_instead_of_rewriting_history(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("浅色", "用户喜欢浅色主题", None)
    journal.append_update(first.memory_id, "深色", "用户喜欢深色主题", None)
    restored = journal.restore(first.memory_id, revision=1)
    assert restored.revision == 3
    assert restored.normalized_fact == "用户喜欢浅色主题"
    assert [row.revision for row in journal.history(first.memory_id)] == [1, 2, 3]


def test_append_update_unknown_memory_id_raises_key_error(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    with pytest.raises(KeyError):
        journal.append_update("no-such-id", "任何文本", "任何事实", None)


def test_restore_unknown_revision_raises_key_error(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    saved = journal.append_new("文本", "事实", None)
    with pytest.raises(KeyError):
        journal.restore(saved.memory_id, revision=99)


def test_scope_is_optional_and_preserved_across_reopen(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    saved = journal.append_new("今天提到量子计算", "用户对量子计算感兴趣", "personal")
    assert saved.scope == "personal"
    reopened = ExplicitMemoryJournal(tmp_path)
    assert reopened.effective()[0].scope == "personal"


def test_blank_text_or_fact_is_rejected(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    with pytest.raises(ValueError):
        journal.append_new("", "事实", None)
    with pytest.raises(ValueError):
        journal.append_new("文本", "   ", None)


def test_bad_json_line_is_reported_and_does_not_hide_other_memory_ids(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("甲的记忆", "甲的事实", None)
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
        handle.write("not-json\n")
    second = journal.append_new("乙的记忆", "乙的事实", None)
    ids = {row.memory_id for row in journal.effective()}
    assert ids == {first.memory_id, second.memory_id}
    assert {err.line for err in journal.errors()} == {2, 3}
    assert all(err.code == "invalid_json" for err in journal.errors())


def test_non_monotonic_revision_line_is_skipped(tmp_path):
    journal = ExplicitMemoryJournal(tmp_path)
    first = journal.append_new("文字", "事实", None)
    with journal.path.open("a", encoding="utf-8") as handle:
        bogus = {
            "memory_id": first.memory_id,
            "revision": 5,
            "raw_text": "文字",
            "normalized_fact": "跳过",
            "scope": None,
            "created_at": "2026-01-01T00:00:00Z",
            "supersedes_revision": 4,
        }
        handle.write(json.dumps(bogus, ensure_ascii=False) + "\n")
    reopened = ExplicitMemoryJournal(tmp_path)
    assert reopened.effective()[0].normalized_fact == "事实"
    assert reopened.errors()[0].code == "invalid_revision_sequence"
