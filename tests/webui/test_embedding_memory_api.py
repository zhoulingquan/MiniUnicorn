"""Tests for the WebUI embedding-memory status section."""

from __future__ import annotations

from pathlib import Path

import pytest

from miniunicorn.config.loader import save_config
from miniunicorn.config.schema import Config
from miniunicorn.webui.settings_api import settings_payload


def _install_config(tmp_path, monkeypatch, *, vector_recall: bool = True) -> Path:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    config.agents.defaults.vector_recall = vector_recall
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    return config_path


def test_settings_payload_includes_embedding_memory_status(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    payload = settings_payload()
    status = payload["embeddingMemory"]
    assert set(status) == {"model", "index", "sources", "recall"}
    assert status["index"]["state"] == "missing"
    assert status["sources"]["discovered"] == 0
    assert status["recall"]["configured"] is True
    assert status["recall"]["active"] is False
    assert status["recall"]["fallback_reason"] == "index_missing"


def test_embedding_memory_status_respects_vector_recall_switch(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch, vector_recall=False)
    payload = settings_payload()
    recall = payload["embeddingMemory"]["recall"]
    assert recall["configured"] is False
    assert recall["active"] is False
    assert recall["fallback_reason"] == "disabled"


def test_embedding_memory_status_has_no_side_effects(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    settings_payload()
    assert not (tmp_path / "ws" / "memory" / "memory.db").exists()


def test_embedding_memory_status_derives_source_counts(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    (ws / "USER.md").write_text("# Always\n叫我小王", encoding="utf-8")
    (ws / "memory").mkdir(exist_ok=True)
    (ws / "memory" / "MEMORY.md").write_text("# Facts\n项目用 Python", encoding="utf-8")
    payload = settings_payload()
    sources = payload["embeddingMemory"]["sources"]
    assert sources["discovered"] == 2
    assert sources["indexed"] == 0
    assert sources["pending"] == 2


def test_embedding_memory_status_reports_ready_index(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    _install_config(tmp_path, monkeypatch)
    from miniunicorn.agent.vector_index import VectorIndexManager

    db = tmp_path / "ws" / "memory" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    index = VectorIndexManager(db)
    index.close()
    payload = settings_payload()
    status = payload["embeddingMemory"]["index"]
    assert status["state"] == "ready"
    assert Path(status["path"]) == db
