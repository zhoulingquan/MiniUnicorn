"""Managed lifecycle of the pinned local embedding model.

The manager owns download, verification, and status of the model assets,
never the inference policy. Every runtime file is recorded in a local
manifest with its SHA-256 so tampered or half-downloaded models are refused
before any inference runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from miniunicorn.config.paths import get_embedding_model_dir
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION
from miniunicorn.embedding.types import ModelStatus

#: FastEmbed's supported-model artifact source for ``BAAI/bge-small-zh-v1.5``.
#: The upstream BAAI repository ships PyTorch weights only; the ONNX runtime
#: file actually loaded by FastEmbed (``model_optimized.onnx``) lives in this
#: mirror. Integrity is anchored by the manifest hashes rather than a git
#: revision (FastEmbed itself does not pin one), while the manifest retains
#: the upstream model revision as identity metadata.
MODEL_DOWNLOAD_REPO = "Qdrant/bge-small-zh-v1.5"

_EXCLUDED_TOP_LEVEL = (".partial", ".quarantine", ".cache")
_MANIFEST_KEYS = ("model_id", "revision", "dimension", "files", "verified_at")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class EmbeddingModelManager:
    """Download, verify, and report on the pinned local embedding model."""

    def __init__(
        self,
        model_dir: Path | None = None,
        *,
        snapshot_download: object | None = None,
    ) -> None:
        self.model_dir = (model_dir or get_embedding_model_dir()).resolve(strict=False)
        self.manifest_path = self.model_dir / "manifest.json"
        self.state_path = self.model_dir / ".state.json"
        self._snapshot_download = snapshot_download
        self._operation_lock = asyncio.Lock()

    # ------------------------------------------------------------------ status

    def status(self) -> ModelStatus:
        """Read-only status from state file, manifest, and file sizes."""
        state = self._read_state()
        manifest = self._read_manifest()
        if state is None:
            return ModelStatus(state="not_downloaded", message="模型尚未下载")
        return ModelStatus(
            state=state.get("state", "failed"),
            model_id=manifest.get("model_id") if manifest else None,
            revision=manifest.get("revision") if manifest else None,
            dimension=manifest.get("dimension") if manifest else None,
            cache_path=str(self.model_dir),
            bytes=int(state.get("bytes", 0)),
            last_self_test=state.get("last_self_test"),
            last_error_code=state.get("last_error_code"),
            message=state.get("message", ""),
        )

    # ------------------------------------------------------------------- setup

    async def setup(self, force: bool = False) -> ModelStatus:
        """Download, hash-verify, and self-test the pinned model."""
        async with self._operation_lock:
            if not force and (await self.verify(run_self_test=True)).state == "ready":
                return self.status()
            self.model_dir.mkdir(parents=True, exist_ok=True)
            self._write_state("downloading", None, "正在下载模型")
            try:
                await asyncio.to_thread(self._download_sync)
                self._write_manifest_atomic(self._build_manifest())
                return await self.verify(run_self_test=True)
            except asyncio.CancelledError:
                return self._write_state(
                    "failed", "cancelled", "模型下载已取消，可重新执行 setup"
                )
            except Exception as exc:
                self._quarantine_unverified_files()
                return self._write_state("failed", "download_failed", str(exc))

    def _download_sync(self) -> None:
        downloader = self._snapshot_download
        if downloader is None:
            from huggingface_hub import snapshot_download

            downloader = snapshot_download
        downloader(repo_id=MODEL_DOWNLOAD_REPO, local_dir=str(self.model_dir))

    # ------------------------------------------------------------------ verify

    async def verify(self, run_self_test: bool = True) -> ModelStatus:
        """Verify pinned identity, file hashes, then run a real CPU self-test."""
        manifest = self._read_manifest()
        if manifest is None:
            return self._write_state("not_downloaded", None, "模型尚未下载")
        if not self._identity_matches(manifest):
            return self._write_state(
                "corrupt",
                "model_mismatch",
                "模型身份与固定契约不一致（model_id/revision/dimension）",
            )
        mismatch = self._hash_mismatch(manifest["files"])
        if mismatch is not None:
            return self._write_state(
                "corrupt", "hash_mismatch", f"模型文件校验失败: {mismatch}"
            )
        if not run_self_test:
            return self._write_state(
                "ready", None, "", last_self_test=manifest.get("verified_at")
            )
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return self._write_state(
                "failed",
                "dependency_missing",
                "缺少 fastembed，请安装 vector extra",
            )
        try:
            dimension, _norm = await asyncio.to_thread(
                self._self_test_sync, self.model_dir
            )
        except Exception as exc:
            return self._write_state("failed", "model_load_failed", str(exc))
        if dimension != MODEL_DIMENSION:
            return self._write_state(
                "failed",
                "dimension_mismatch",
                f"自检维度 {dimension} != {MODEL_DIMENSION}",
            )
        return self._write_state("ready", None, "", last_self_test=_utc_now())

    def _self_test_sync(self, path: Path) -> tuple[int, float]:
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=MODEL_ID,
            specific_model_path=str(path),
            providers=["CPUExecutionProvider"],
        )
        vector = [float(value) for value in next(model.embed(["我喜欢安静的工作环境"]))]
        if len(vector) != MODEL_DIMENSION or not all(math.isfinite(value) for value in vector):
            raise ValueError("self-test returned invalid vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if not 0.999 <= norm <= 1.001:
            raise ValueError(f"self-test vector norm is {norm}")
        return len(vector), norm

    def validated_model_path(self) -> Path | None:
        """Return the model dir only when state is ready and hashes still match.

        Recomputes every runtime file hash synchronously, so files tampered
        after verification are refused and the status flips to corrupt.
        """
        state = self._read_state()
        manifest = self._read_manifest()
        if state is None or manifest is None or state.get("state") != "ready":
            return None
        mismatch = self._hash_mismatch(manifest["files"])
        if mismatch is not None:
            self._write_state(
                "corrupt", "hash_mismatch", f"模型文件校验失败: {mismatch}"
            )
            return None
        return self.model_dir

    # ---------------------------------------------------------------- manifest

    def _build_manifest(self) -> dict[str, object]:
        files: dict[str, str] = {}
        has_onnx = False
        has_tokenizer_config = False
        for path in sorted(self.model_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.model_dir).as_posix()
            if rel in ("manifest.json", ".state.json"):
                continue
            if rel.startswith(_EXCLUDED_TOP_LEVEL):
                continue
            files[rel] = _sha256_file(path)
            if rel.endswith(".onnx"):
                has_onnx = True
            if rel in ("tokenizer.json", "tokenizer_config.json", "config.json"):
                has_tokenizer_config = True
        if not has_onnx or not has_tokenizer_config:
            raise ValueError("下载内容缺少 ONNX 或 tokenizer/config 文件")
        return {
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "dimension": MODEL_DIMENSION,
            "files": files,
            "verified_at": _utc_now(),
        }

    def _write_manifest_atomic(self, manifest: dict[str, object]) -> None:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.manifest_path, manifest)

    def _read_manifest(self) -> dict | None:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        if not all(key in payload for key in _MANIFEST_KEYS):
            return None
        return payload

    def _identity_matches(self, manifest: dict) -> bool:
        return (
            manifest.get("model_id") == MODEL_ID
            and manifest.get("revision") == MODEL_REVISION
            and manifest.get("dimension") == MODEL_DIMENSION
        )

    def _hash_mismatch(self, expected: dict[str, str]) -> str | None:
        current = {
            path.relative_to(self.model_dir).as_posix(): _sha256_file(path)
            for path in self.model_dir.rglob("*")
            if path.is_file()
            and path.relative_to(self.model_dir).as_posix()
            not in ("manifest.json", ".state.json")
            and not path.relative_to(self.model_dir).as_posix().startswith(
                _EXCLUDED_TOP_LEVEL
            )
        }
        if set(current) != set(expected):
            return f"文件集合不一致: expected={sorted(expected)} got={sorted(current)}"
        for rel, digest in expected.items():
            if current.get(rel) != digest:
                return f"{rel}: expected={digest} got={current.get(rel)}"
        return None

    # ------------------------------------------------------------------- state

    def _write_state(
        self,
        state: str,
        last_error_code: str | None,
        message: str,
        *,
        last_self_test: str | None = None,
    ) -> ModelStatus:
        manifest = self._read_manifest()
        total_bytes = 0
        if manifest:
            for path in self.model_dir.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.model_dir).as_posix()
                if rel in manifest.get("files", {}):
                    total_bytes += path.stat().st_size
        self.model_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            self.state_path,
            {
                "state": state,
                "last_error_code": last_error_code,
                "message": message,
                "last_self_test": last_self_test,
                "bytes": total_bytes,
            },
        )
        return self.status()

    def _read_state(self) -> dict | None:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None

    def _quarantine_unverified_files(self) -> None:
        quarantine = self.model_dir / ".quarantine" / _file_stamp()
        quarantine.mkdir(parents=True, exist_ok=True)
        for path in self.model_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.model_dir).as_posix()
            if rel in ("manifest.json", ".state.json") or rel.startswith(
                _EXCLUDED_TOP_LEVEL
            ):
                continue
            target = quarantine / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(target))
            except OSError:
                logger.exception("failed to quarantine model file {}", path)
        logger.warning("unverified model files quarantined under {}", quarantine)
