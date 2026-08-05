import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION
from miniunicorn.embedding.model_manager import (
    MODEL_DOWNLOAD_REPO,
    EmbeddingModelManager,
)


@pytest.fixture
def ready_model_dir(tmp_path: Path) -> Path:
    manager = EmbeddingModelManager(tmp_path)
    (tmp_path / "model_optimized.onnx").write_bytes(b"onnx")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    manifest = manager._build_manifest()
    manager._write_manifest_atomic(manifest)
    manager._write_state("ready", None, "", last_self_test="2026-08-04 10:00")
    return tmp_path


@pytest.mark.asyncio
async def test_setup_pins_artifact_source_hashes_files_and_runs_self_test(tmp_path, monkeypatch):
    calls = {}

    def fake_snapshot_download(**kwargs):
        calls.update(kwargs)
        model = Path(kwargs["local_dir"])
        model.mkdir(parents=True, exist_ok=True)
        (model / "model_optimized.onnx").write_bytes(b"onnx")
        (model / "tokenizer.json").write_text("{}", encoding="utf-8")
        (model / "config.json").write_text("{}", encoding="utf-8")
        return str(model)

    manager = EmbeddingModelManager(tmp_path, snapshot_download=fake_snapshot_download)
    monkeypatch.setattr(manager, "_self_test_sync", lambda path: (512, 1.0))
    status = await manager.setup()
    assert status.state == "ready", status.message
    assert calls["repo_id"] == MODEL_DOWNLOAD_REPO
    assert "revision" not in calls
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_id"] == MODEL_ID
    assert manifest["revision"] == MODEL_REVISION
    assert manifest["dimension"] == MODEL_DIMENSION
    assert manifest["files"]["model_optimized.onnx"] == hashlib.sha256(b"onnx").hexdigest()


@pytest.mark.asyncio
async def test_setup_skips_download_when_already_ready(ready_model_dir, monkeypatch):
    manager = EmbeddingModelManager(ready_model_dir, snapshot_download=lambda **_: None)
    monkeypatch.setattr(manager, "_self_test_sync", lambda path: (512, 1.0))
    status = await manager.setup()
    assert status.state == "ready"


@pytest.mark.asyncio
async def test_verify_rejects_changed_runtime_file(ready_model_dir):
    (ready_model_dir / "model_optimized.onnx").write_bytes(b"tampered")
    status = await EmbeddingModelManager(ready_model_dir).verify(run_self_test=False)
    assert status.state == "corrupt"
    assert status.last_error_code == "hash_mismatch"


@pytest.mark.asyncio
async def test_verify_rejects_foreign_model_identity(ready_model_dir):
    manifest_path = ready_model_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_id"] = "other/model"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    status = await EmbeddingModelManager(ready_model_dir).verify(run_self_test=False)
    assert status.state == "corrupt"
    assert status.last_error_code == "model_mismatch"


@pytest.mark.asyncio
async def test_verify_missing_manifest_means_not_downloaded(tmp_path):
    status = await EmbeddingModelManager(tmp_path).verify(run_self_test=False)
    assert status.state == "not_downloaded"


@pytest.mark.asyncio
async def test_validated_model_path_returns_none_and_flags_corrupt_after_tamper(
    ready_model_dir, monkeypatch
):
    manager = EmbeddingModelManager(ready_model_dir)
    assert manager.validated_model_path() == ready_model_dir
    (ready_model_dir / "model_optimized.onnx").write_bytes(b"tampered")
    assert manager.validated_model_path() is None
    assert manager.status().state == "corrupt"


@pytest.mark.asyncio
async def test_dependency_missing_is_typed_failure(ready_model_dir, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "fastembed", None)
    manager = EmbeddingModelManager(ready_model_dir)
    status = await manager.verify(run_self_test=True)
    assert status.state == "failed"
    assert status.last_error_code == "dependency_missing"


@pytest.mark.asyncio
async def test_cancelled_setup_is_recorded_without_raising(ready_model_dir, monkeypatch):
    manager = EmbeddingModelManager(ready_model_dir, snapshot_download=lambda **_: None)

    def blocking_self_test(path):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "_self_test_sync", blocking_self_test)
    manager._download_sync = lambda: None
    status = await manager.setup(force=True)
    assert status.state == "failed"
    assert status.last_error_code == "cancelled"
