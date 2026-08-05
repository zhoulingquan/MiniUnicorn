"""Tests for the CLI `status` command's embedding-memory block."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from miniunicorn.cli.commands import app
from miniunicorn.config.loader import save_config
from miniunicorn.config.schema import Config


def _install_config(tmp_path, monkeypatch, *, vector_recall: bool = True) -> Path:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    config.agents.defaults.vector_recall = vector_recall
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    return config_path


def test_status_command_shows_embedding_memory_block(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Embedding Memory" in result.output
    assert "enabled" in result.output
    assert "index=missing" in result.output
    assert "sources=0/0" in result.output


def test_status_command_shows_embedding_disabled(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch, vector_recall=False)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Embedding Memory" in result.output
    assert "disabled" in result.output