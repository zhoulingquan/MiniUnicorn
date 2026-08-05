from pathlib import Path

from miniunicorn.agent.memory_sources import MemorySourceCatalog


def test_catalog_emits_stable_markdown_and_jsonl_ids(tmp_path):
    (tmp_path / "USER.md").write_text(
        "# Always\n叫我小王\n# Preferences\n喜欢深色主题", encoding="utf-8"
    )
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "history.jsonl").write_text(
        '{"cursor":7,"timestamp":"2026-08-04 10:00","content":"完成了项目初始化"}\n',
        encoding="utf-8",
    )
    scan = MemorySourceCatalog(tmp_path).scan()
    ids = {record.source_id for record in scan.records}
    assert "user:preferences:1" in ids
    assert "history:7" in ids
    assert not any(record.source_type == "soul" for record in scan.records)


def test_invalid_jsonl_line_is_reported_without_blocking_valid_lines(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "procedural.jsonl").write_text(
        '{bad json}\n{"cursor":2,"content":"提交前运行测试"}\n', encoding="utf-8"
    )
    scan = MemorySourceCatalog(tmp_path).scan()
    assert [record.source_id for record in scan.records] == ["procedural:2"]
    assert scan.errors[0].line == 1


def test_missing_files_are_silently_skipped(tmp_path):
    scan = MemorySourceCatalog(tmp_path).scan()
    assert scan.records == ()
    assert scan.errors == ()


def test_long_markdown_section_is_split_into_bounded_chunks(tmp_path):
    body = "喜欢深色主题" * 600  # 3600 chars
    (tmp_path / "USER.md").write_text(f"# Preferences\n{body}", encoding="utf-8")
    scan = MemorySourceCatalog(tmp_path).scan()
    records = [r for r in scan.records if r.source_id.startswith("user:preferences:")]
    assert len(records) >= 2
    assert [r.source_id.split(":")[-1] for r in records] == [
        str(i) for i in range(1, len(records) + 1)
    ]
    for record in records:
        assert len(record.text) <= 2400 + 64  # plus heading prefix


def test_heading_path_slug_normalizes_case_and_punctuation(tmp_path):
    (tmp_path / "USER.md").write_text(
        "# My Preferences!\n喜欢浅色主题\n## Deep Details!\n细节内容", encoding="utf-8"
    )
    ids = {record.source_id for record in MemorySourceCatalog(tmp_path).scan().records}
    assert "user:my-preferences:1" in ids
    assert "user:deep-details:1" in ids


def test_episodic_uses_event_id_or_legacy_identity(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "episodic.jsonl").write_text(
        '{"event_id":"e1","content":"创建了项目"}\n'
        '{"summary":"修复了登录问题"}\n',
        encoding="utf-8",
    )
    records = MemorySourceCatalog(tmp_path).scan().records
    episodic = {record.source_id: record for record in records if record.source_type == "episodic"}
    assert "episodic:e1" in episodic
    legacy = [sid for sid in episodic if sid.startswith("episodic:legacy:")]
    assert len(legacy) == 1
    assert legacy[0].startswith("episodic:legacy:2:")
    assert episodic[legacy[0]].text == "修复了登录问题"


def test_revision_and_text_field_fallbacks(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "history.jsonl").write_text(
        '{"cursor":3,"summary":"只有摘要"}\n', encoding="utf-8"
    )
    record = MemorySourceCatalog(tmp_path).scan().records[0]
    assert record.text == "只有摘要"
    assert record.source_revision.startswith("line:1:")


def test_invalid_cursor_is_reported_as_error(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "history.jsonl").write_text(
        '{"cursor":"abc","content":"x"}\n{"cursor":-1,"content":"y"}\n',
        encoding="utf-8",
    )
    scan = MemorySourceCatalog(tmp_path).scan()
    assert scan.records == ()
    assert {error.line for error in scan.errors} == {1, 2}


def test_blank_and_whitespace_only_content_is_not_indexed(tmp_path):
    (tmp_path / "USER.md").write_text(
        "# Always\n   \n# Preferences\n\n", encoding="utf-8"
    )
    scan = MemorySourceCatalog(tmp_path).scan()
    assert scan.records == ()


def test_sources_use_workspace_relative_posix_paths(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "procedural.jsonl").write_text(
        '{"cursor":1,"content":"提交前运行测试"}\n', encoding="utf-8"
    )
    (tmp_path / "USER.md").write_text("# Preferences\n喜欢深色主题", encoding="utf-8")
    files = {record.source_file for record in MemorySourceCatalog(tmp_path).scan().records}
    assert files == {"USER.md", "memory/procedural.jsonl"}
    for source_file in files:
        assert not Path(source_file).is_absolute()
