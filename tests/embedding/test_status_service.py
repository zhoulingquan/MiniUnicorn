"""Tests for the shared embedding status service."""

from __future__ import annotations

from types import SimpleNamespace

from miniunicorn.agent.memory_sources import MemorySourceCatalog
from miniunicorn.embedding.model_manager import EmbeddingModelManager
from miniunicorn.embedding.status_service import EmbeddingStatusService


def write_workspace(tmp_path, user: str, memory: str) -> None:
    (tmp_path / "USER.md").write_text(user, encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "MEMORY.md").write_text(memory, encoding="utf-8")


def make_status_service(tmp_path, index_state: str = "missing") -> EmbeddingStatusService:
    model_manager = EmbeddingModelManager(tmp_path / "models" / "embedding")
    catalog = MemorySourceCatalog(tmp_path)
    return EmbeddingStatusService(
        tmp_path, model_manager=model_manager, catalog=catalog, index_state=index_state
    )


def test_status_service_derives_source_counts_without_creating_index(tmp_path):
    write_workspace(tmp_path, user="# Always\n叫我小王", memory="# Facts\n项目用 Python")
    status = make_status_service(tmp_path, index_state="missing").snapshot(configured=True)
    assert status.sources.discovered == 2
    assert status.sources.indexed == 0
    assert status.sources.pending == 2
    assert status.index.state == "missing"
    assert status.recall.fallback_reason == "index_missing"
    assert not (tmp_path / "memory" / "memory.db").exists()


def test_status_service_disabled_when_not_configured(tmp_path):
    write_workspace(tmp_path, user="# Always\n内容", memory="# Always\n内容")
    status = make_status_service(tmp_path).snapshot(configured=False)
    assert status.recall.configured is False
    assert status.recall.active is False
    assert status.recall.fallback_reason == "disabled"


def test_status_service_reports_model_not_downloaded(tmp_path):
    write_workspace(tmp_path, user="# Always\n内容", memory="# Always\n内容")
    status = make_status_service(tmp_path, index_state="ready").snapshot(configured=True)
    assert status.model.state == "not_downloaded"
    assert status.recall.active is False
    assert status.recall.fallback_reason == "model_not_ready"


def test_status_service_ready_when_index_and_model_ready(tmp_path):
    pytest = __import__("pytest")
    pytest.importorskip("sqlite_vec")
    write_workspace(tmp_path, user="# Always\n内容", memory="# Always\n内容")
    from miniunicorn.agent.vector_index import VectorIndexManager

    index = VectorIndexManager(tmp_path / "memory" / "memory.db")
    model_manager = EmbeddingModelManager(tmp_path / "models" / "embedding")
    catalog = MemorySourceCatalog(tmp_path)
    service = EmbeddingStatusService(
        tmp_path,
        index_manager=index,
        model_manager=model_manager,
        catalog=catalog,
    )
    status = service.snapshot(configured=True)
    assert status.index.state == "ready"
    assert status.recall.active is False  # model not downloaded yet
    assert status.recall.fallback_reason == "model_not_ready"


def make_config(workspace: object, *, vector_recall: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_path=workspace,
        agents=SimpleNamespace(defaults=SimpleNamespace(vector_recall=vector_recall)),
    )


def test_from_config_derives_workspace_and_sources(tmp_path):
    write_workspace(tmp_path, user="# Always\n叫我小王", memory="# Facts\n项目用 Python")
    config = make_config(tmp_path)
    status = EmbeddingStatusService.from_config(config).snapshot(configured=True)
    assert status.sources.discovered == 2
    assert status.sources.pending == 2
    assert status.index.state == "missing"
    assert status.recall.fallback_reason == "index_missing"
    assert not (tmp_path / "memory" / "memory.db").exists()


def test_from_config_probes_existing_index(tmp_path):
    pytest = __import__("pytest")
    pytest.importorskip("sqlite_vec")
    write_workspace(tmp_path, user="# Always\n内容", memory="# Always\n内容")
    from miniunicorn.agent.vector_index import VectorIndexManager

    db = tmp_path / "memory" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    index = VectorIndexManager(db)
    index.close()
    config = make_config(tmp_path)
    status = EmbeddingStatusService.from_config(config).snapshot(configured=True)
    assert status.index.state == "ready"
    assert status.index.path == str(db)