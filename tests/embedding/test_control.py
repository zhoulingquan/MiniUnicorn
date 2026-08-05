"""Tests for the shared single-process EmbeddingControl assembly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from miniunicorn.agent.memory_recall import RecallOutcome
from miniunicorn.agent.memory_sources import SourceScan
from miniunicorn.embedding.control import EmbeddingControl


def write_workspace(tmp_path, user: str = "", memory: str = "") -> None:
    if user:
        (tmp_path / "USER.md").write_text(user, encoding="utf-8")
    if memory:
        (tmp_path / "memory").mkdir(exist_ok=True)
        (tmp_path / "memory" / "MEMORY.md").write_text(memory, encoding="utf-8")


def _clear_instances() -> None:
    EmbeddingControl._instances.clear()


def test_for_workspace_is_singleton_per_key(tmp_path):
    try:
        first = EmbeddingControl.for_workspace(tmp_path)
        second = EmbeddingControl.for_workspace(tmp_path)
        disabled = EmbeddingControl.for_workspace(tmp_path, configured=False)
        assert first is second
        assert disabled is not first
        assert EmbeddingControl.for_workspace(tmp_path, configured=False) is disabled
    finally:
        _clear_instances()


@pytest.mark.asyncio
async def test_disabled_control_is_lazy(tmp_path):
    _clear_instances()
    try:
        control = EmbeddingControl.for_workspace(tmp_path, configured=False)
        outcome = await control.recall_for_turn("问题")
        assert outcome.fallback_reason == "disabled"
        assert not (tmp_path / "memory" / "memory.db").exists()
        control.request_reconcile()
    finally:
        _clear_instances()


@pytest.mark.asyncio
async def test_recall_without_db_reports_index_missing_and_schedules_rebuild(tmp_path):
    _clear_instances()
    try:
        write_workspace(tmp_path, user="# Always\n核心")
        control = EmbeddingControl.for_workspace(tmp_path, configured=True)
        started: list[bool] = []
        control.start_guarded_rebuild = MagicMock(
            side_effect=lambda: started.append(True)
        )
        outcome = await control.recall_for_turn("问题")
        assert outcome.fallback_reason == "index_missing"
        assert outcome.records == ()
        assert started == [True]
    finally:
        _clear_instances()


@pytest.mark.asyncio
async def test_recall_ready_index_uses_recall_service_with_core_texts(tmp_path):
    _clear_instances()
    try:
        write_workspace(tmp_path, user="# Always\n核心", memory="# Always\n项目记忆")
        control = EmbeddingControl.for_workspace(tmp_path, configured=True)
        ready = AsyncMock(return_value=RecallOutcome((), None, 1.0))
        fake_index = MagicMock()
        fake_index.is_search_ready.return_value = True
        fake_index.reconcile = AsyncMock(return_value=MagicMock())
        control._index = fake_index
        control._recall_service = MagicMock(recall=ready)
        outcome = await control.recall_for_turn("问题")
        assert outcome.fallback_reason is None
        ready.assert_awaited_once()
        positional, kwargs = ready.call_args
        assert positional == ("问题",)
        assert kwargs["core_texts"] == ["核心", "项目记忆"]
        fake_index.reconcile.assert_awaited_once()
    finally:
        _clear_instances()


@pytest.mark.asyncio
async def test_reconcile_guarded_uses_catalog_scan(tmp_path):
    _clear_instances()
    try:
        write_workspace(tmp_path, user="# Always\n核心")
        control = EmbeddingControl.for_workspace(tmp_path, configured=True)
        fake_index = MagicMock()
        fake_index.reconcile = AsyncMock(return_value=MagicMock())
        control._index = fake_index
        control._provider = MagicMock()
        report = await control.reconcile_guarded()
        assert report is not None
        fake_index.reconcile.assert_awaited_once()
        assert isinstance(fake_index.reconcile.call_args.args[0], SourceScan)
    finally:
        _clear_instances()


def test_status_snapshot_reports_disabled_without_db(tmp_path):
    _clear_instances()
    try:
        control = EmbeddingControl.for_workspace(tmp_path, configured=False)
        status = control.status(configured=False)
        assert status.recall.fallback_reason == "disabled"
        assert not (tmp_path / "memory" / "memory.db").exists()
    finally:
        _clear_instances()