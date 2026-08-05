"""Tests for the WebUI embedding memory REST API service.

These tests inject a ``FakeControl`` so no model download, sqlite-vec load,
or real embedding ever runs. The shared status contract and operation
mutual-exclusion are asserted here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.embedding.control import OperationState
from miniunicorn.embedding.types import (
    EmbeddingStatus,
    IndexStatus,
    ModelStatus,
    RecallStatus,
    SourceStatus,
)
from miniunicorn.webui.embedding_api import EmbeddingApiError, EmbeddingApiService


def _status_payload() -> EmbeddingStatus:
    return EmbeddingStatus(
        model=ModelStatus(state="not_downloaded", message="模型尚未下载"),
        index=IndexStatus(state="missing"),
        sources=SourceStatus(discovered=0, indexed=0, pending=0, stale=0, invalid=0, inactive=0),
        recall=RecallStatus(
            configured=True, active=False, fallback_reason="index_missing"
        ),
    )


class FakeControl:
    """Stand-in for EmbeddingControl used by the API service tests."""

    def __init__(self, payload: EmbeddingStatus, *, configured: bool = True) -> None:
        self._payload = payload
        self.configured = configured
        self._operation: OperationState | None = None
        self.recall_service = MagicMock()
        self.recall_service.recall = AsyncMock()

    @property
    def operation_running(self) -> bool:
        return self._operation is not None and self._operation.state == "running"

    def start_operation(self, kind: str) -> OperationState:
        if self.operation_running:
            raise RuntimeError("operation_already_running")
        if not self.configured:
            raise RuntimeError("embedding_disabled")
        self._operation = OperationState(id="op-1", kind=kind, state="running")  # type: ignore[arg-type]
        return self._operation

    def status(self, *, configured: bool) -> EmbeddingStatus:
        return self._payload


@pytest.fixture
def fake_control(monkeypatch, tmp_path: Path) -> FakeControl:
    control = FakeControl(_status_payload())
    monkeypatch.setattr(
        "miniunicorn.webui.embedding_api.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: control,
    )
    return control


@pytest.fixture
def running_control(monkeypatch, tmp_path: Path) -> FakeControl:
    control = FakeControl(_status_payload())
    control._operation = OperationState(id="op-running", kind="rebuild", state="running")  # type: ignore[arg-type]
    monkeypatch.setattr(
        "miniunicorn.webui.embedding_api.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: control,
    )
    return control


def test_embedding_status_matches_shared_contract(fake_control: FakeControl):
    service = EmbeddingApiService(Path("/ws"), configured=True)
    result = service.status()
    assert result == fake_control.status(configured=True).to_dict()
    assert set(result) >= {"model", "index", "sources", "recall"}


def test_second_long_operation_returns_409(running_control: FakeControl):
    service = EmbeddingApiService(Path("/ws"), configured=True)
    with pytest.raises(EmbeddingApiError) as exc_info:
        service.start("rebuild")
    assert exc_info.value.status == 409
    assert exc_info.value.code == "operation_already_running"


def test_start_setup_returns_accepted_with_operation(fake_control: FakeControl):
    service = EmbeddingApiService(Path("/ws"), configured=True)
    result = service.start("setup")
    assert result["accepted"] is True
    assert result["operation"]["kind"] == "setup"
    assert result["operation"]["state"] == "running"


def test_disabled_configured_returns_409(monkeypatch, tmp_path: Path):
    control = FakeControl(_status_payload(), configured=False)
    monkeypatch.setattr(
        "miniunicorn.webui.embedding_api.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: control,
    )
    service = EmbeddingApiService(Path("/ws"), configured=False)
    with pytest.raises(EmbeddingApiError) as exc_info:
        service.start("setup")
    assert exc_info.value.status == 409
    assert exc_info.value.code == "embedding_disabled"


def test_search_returns_workspace_relative_source_and_no_vector(
    monkeypatch, tmp_path: Path
):
    from miniunicorn.agent.memory_recall import RecallOutcome, RecallRecord

    control = FakeControl(_status_payload())
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "USER.md").write_text("x", encoding="utf-8")
    control.recall_service.recall = AsyncMock(
        return_value=RecallOutcome(
            records=(
                RecallRecord(
                    source_id="user:preferences:1",
                    source_type="user",
                    source_file=str(ws / "USER.md"),
                    source_revision="1",
                    text="早餐喝豆浆",
                    content_hash="abc",
                    similarity=0.9,
                    score=0.95,
                    token_count=10,
                    synchronized=True,
                ),
            ),
            fallback_reason=None,
            latency_ms=1.5,
        )
    )
    monkeypatch.setattr(
        "miniunicorn.webui.embedding_api.EmbeddingControl.for_workspace",
        lambda workspace, configured=True: control,
    )
    service = EmbeddingApiService(ws, configured=True)
    result = asyncio.run(service.search("早餐"))

    row = result["results"][0]
    assert row["source_file"] == "USER.md"
    assert "embedding" not in row and "vector" not in row
    assert not Path(row["source_file"]).is_absolute()


def test_search_rejects_empty_and_oversized_query(fake_control: FakeControl):
    service = EmbeddingApiService(Path("/ws"), configured=True)
    with pytest.raises(EmbeddingApiError) as exc_info:
        asyncio.run(service.search(""))
    assert exc_info.value.status == 400
    assert exc_info.value.code == "invalid_query"

    with pytest.raises(EmbeddingApiError) as exc_info:
        asyncio.run(service.search("x" * 501))
    assert exc_info.value.status == 400
