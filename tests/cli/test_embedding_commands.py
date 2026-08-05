"""Tests for the ``miniunicorn embedding`` CLI command group.

The command group consumes ``EmbeddingControl``; every test injects a
``FakeControl`` so no model download, sqlite-vec load, or real embedding ever
runs. The shared status contract (``model``/``index``/``sources``/``recall``)
is asserted exactly once here so the WebUI tests can reuse the same shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from miniunicorn.cli.commands import app
from miniunicorn.embedding.types import ModelStatus


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def status_payload() -> dict:
    """A minimal payload that matches ``EmbeddingStatus.to_dict()``."""
    return {
        "model": {
            "state": "ready",
            "model_id": "BAAI/bge-small-zh-v1.5",
            "revision": "7999e1d3359715c523056ef9478215996d62a620",
            "dimension": 512,
            "cache_path": "/cache/bge-small-zh-v1.5",
            "bytes": 0,
            "last_self_test": "2026-08-04 12:00",
            "last_error_code": None,
            "message": "",
        },
        "index": {
            "state": "missing",
            "path": None,
            "bytes": 0,
            "last_rebuild": None,
            "last_error_code": None,
            "message": "",
        },
        "sources": {
            "discovered": 0,
            "indexed": 0,
            "pending": 0,
            "stale": 0,
            "invalid": 0,
            "inactive": 0,
            "errors": (),
        },
        "recall": {
            "configured": True,
            "active": False,
            "fallback_reason": "index_missing",
            "last_self_test": None,
            "last_latency_ms": None,
        },
    }


class _FakeModelManager:
    """Mimics ``EmbeddingModelManager`` for setup/verify/status calls."""

    def __init__(self, status: ModelStatus) -> None:
        self._status = status

    def status(self) -> ModelStatus:
        return self._status

    async def setup(self, force: bool = False) -> ModelStatus:  # noqa: ARG002
        return self._status

    async def verify(self, run_self_test: bool = True) -> ModelStatus:  # noqa: ARG002
        return self._status


class _StatusView:
    """Lightweight stand-in for ``EmbeddingStatus`` exposing ``to_dict``/``model``."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)

    @property
    def model(self) -> ModelStatus:
        # The CLI only reads ``model.state`` after an operation; reuse the
        # ModelStatus carried by the fake model manager for consistency.
        return self._model

    @model.setter
    def model(self, value: ModelStatus) -> None:
        self._model = value


class FakeControl:
    """Stand-in for ``EmbeddingControl`` used by the CLI command group."""

    def __init__(self, payload: dict, *, model_state: str = "ready") -> None:
        self._payload = payload
        self._model_status = ModelStatus(state=model_state)  # type: ignore[arg-type]
        self.model_manager = _FakeModelManager(self._model_status)
        # A nonexistent path so verify/rebuild skip real index work.
        self.db_path = Path("/nonexistent-embedding-index")
        self.catalog = None
        self.provider = None
        self._status_view = _StatusView(payload)
        self._status_view.model = self._model_status

    def status(self, *, configured: bool = True) -> _StatusView:  # noqa: ARG002
        return self._status_view


@pytest.fixture
def failing_control(monkeypatch) -> FakeControl:
    control = FakeControl(status_payload(), model_state="failed")
    monkeypatch.setattr(
        "miniunicorn.cli.embedding_commands.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: control,  # noqa: ARG005
    )
    return control


def test_embedding_help_lists_all_operations(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["embedding", "--help"])

    assert result.exit_code == 0
    for command in ("setup", "status", "verify", "rebuild"):
        assert command in result.stdout


def test_embedding_status_json_uses_shared_contract(
    cli_runner: CliRunner, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "miniunicorn.cli.embedding_commands.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: FakeControl(status_payload()),  # noqa: ARG005
    )
    result = cli_runner.invoke(
        app, ["embedding", "status", "--workspace", str(tmp_path), "--json"]
    )

    assert result.exit_code == 0
    assert set(json.loads(result.stdout)) >= {"model", "index", "sources", "recall"}


def test_embedding_verify_failure_is_nonzero_but_does_not_delete_files(
    cli_runner: CliRunner, failing_control: FakeControl, tmp_path: Path
) -> None:
    source = tmp_path / "memory" / "MEMORY.md"
    source.parent.mkdir()
    source.write_text("保留我", encoding="utf-8")

    result = cli_runner.invoke(app, ["embedding", "verify", "--workspace", str(tmp_path)])

    assert result.exit_code == 1
    assert source.read_text(encoding="utf-8") == "保留我"
