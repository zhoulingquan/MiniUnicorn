"""Tests for WebUI on-disk cleanup (legacy JSON + transcript JSONL)."""

from __future__ import annotations

from miniunicorn.session.manager import SessionManager
from miniunicorn.webui.thread_disk import delete_webui_thread, webui_thread_file_path
from miniunicorn.webui.transcript import (
    append_transcript_object,
    delete_webui_transcript,
    webui_transcript_path,
)


def test_delete_webui_thread_removes_legacy_json_and_transcript(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:k1"
    json_path = webui_thread_file_path(key)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text('{"x":1}', encoding="utf-8")
    append_transcript_object(key, {"event": "user", "chat_id": "k1", "text": "hi"})
    assert webui_transcript_path(key).is_file()
    assert delete_webui_thread(key) is True
    assert not json_path.is_file()
    assert not webui_transcript_path(key).is_file()
    assert delete_webui_thread(key) is False


def test_delete_webui_transcript_removes_legacy_named_file(tmp_path, monkeypatch) -> None:
    """删除 transcript 时同时清理 legacy 命名文件。

    回归测试:旧版 ``delete_webui_transcript`` 只删 V2 路径(``<prefix>--<sha256>.jsonl``),
    导致从未被加载迁移的 legacy 文件(``<safe_key_legacy>.jsonl``)残留,WebUI 侧边栏
    删除后刷新又出现。此处与 SessionManager.delete_session 对齐。
    """
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:legacy-unloaded"

    # 手动写一个 legacy 命名的 transcript 文件,模拟从未被 V2 路径加载过的会话
    legacy_path = webui_transcript_path(key).parent / f"{SessionManager.safe_key_legacy(key)}.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"event":"user","chat_id":"legacy","text":"hi"}\n', encoding="utf-8")

    v2_path = webui_transcript_path(key)
    assert not v2_path.exists()
    assert legacy_path.exists()

    # 删除:应返回 True(删掉了 legacy 文件)
    assert delete_webui_transcript(key) is True

    # legacy 与 V2 都不存在
    assert not legacy_path.exists()
    assert not v2_path.exists()

    # 再次删除返回 False
    assert delete_webui_transcript(key) is False


def test_delete_webui_thread_removes_legacy_named_json(tmp_path, monkeypatch) -> None:
    """删除 thread JSON 快照时同时清理 legacy 命名文件。"""
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:legacy-json"

    # 手动写 legacy 命名的 .json 快照
    legacy_json = webui_thread_file_path(key).parent / f"{SessionManager.safe_key_legacy(key)}.json"
    legacy_json.parent.mkdir(parents=True, exist_ok=True)
    legacy_json.write_text('{"x":1}', encoding="utf-8")

    assert not webui_thread_file_path(key).exists()
    assert legacy_json.exists()

    assert delete_webui_thread(key) is True
    assert not legacy_json.exists()
    assert not webui_thread_file_path(key).exists()
