"""Regression tests for atomic config save.

Covers the design spec §4.4 requirements:
- 配置写入故障和并发保存不破坏原文件
- 临时文件清理
- 并发写串行化（FileLock）
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from erza.config.loader import save_config
from erza.config.schema import Config


def _write_initial(path: Path) -> dict:
    """Seed *path* with a known-good config and return its JSON dict."""
    cfg = Config()
    save_config(cfg, path)
    return json.loads(path.read_text(encoding="utf-8"))


def test_save_config_is_atomic_on_success(tmp_path: Path) -> None:
    """A successful save replaces the file atomically — no partial writes."""
    path = tmp_path / "config.json"
    _write_initial(path)

    cfg = Config()
    # Mutate something observable so we can detect the swap.
    cfg.agents.defaults.max_tokens = 9999
    save_config(cfg, path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["agents"]["defaults"]["maxTokens"] == 9999

    # No leftover temp files in the directory (only config.json + lock).
    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name != "config.json" and p.name != "config.json.lock"
    ]
    assert leftovers == [], f"Unexpected leftover temp files: {leftovers}"


def test_save_config_preserves_original_on_write_failure(tmp_path: Path) -> None:
    """If the write raises mid-flight, the original config must be intact."""
    path = tmp_path / "config.json"
    original = _write_initial(path)

    cfg = Config()
    cfg.agents.defaults.max_tokens = 4242

    # Simulate a write failure by making os.replace raise. The original
    # file MUST be untouched and the temp file MUST be cleaned up.
    def boom(src, dst):  # noqa: ANN001
        raise OSError("simulated replace failure")

    with patch("erza.config.loader.os.replace", side_effect=boom):
        with pytest.raises(OSError):
            save_config(cfg, path)

    # Original config untouched.
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == original, "Original config was corrupted by a failed save"

    # No leftover temp files.
    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name != "config.json" and p.name != "config.json.lock"
    ]
    assert leftovers == [], f"Temp file not cleaned up after failure: {leftovers}"


def test_save_config_preserves_original_on_fsync_failure(tmp_path: Path) -> None:
    """A fsync failure after writing the temp file must not corrupt the original."""
    path = tmp_path / "config.json"
    original = _write_initial(path)

    cfg = Config()
    cfg.agents.defaults.max_tokens = 7777

    # We patch os.fsync to raise; save_config should catch it,
    # clean up the temp, and re-raise without touching the original.
    def failing_fsync(_fd):
        raise OSError("simulated fsync failure")

    with patch("erza.config.loader.os.fsync", side_effect=failing_fsync):
        with pytest.raises(OSError):
            save_config(cfg, path)

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == original, "Original config corrupted by fsync failure"

    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name != "config.json" and p.name != "config.json.lock"
    ]
    assert leftovers == [], f"Temp file not cleaned up after fsync failure: {leftovers}"


def test_concurrent_save_config_does_not_corrupt(tmp_path: Path) -> None:
    """Multiple threads writing concurrently must serialize via FileLock.

    Each thread writes a distinct maxTokens value; after all threads finish
    the file must be valid JSON and contain one of the written values
    (no torn writes, no corruption).
    """
    path = tmp_path / "config.json"
    _write_initial(path)

    values = [100, 200, 300, 400, 500]
    errors: list[BaseException] = []

    def writer(value: int) -> None:
        try:
            cfg = Config()
            cfg.agents.defaults.max_tokens = value
            save_config(cfg, path)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(v,)) for v in values]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent writers raised: {errors}"

    # File must be valid JSON (no torn writes) and contain one of the values.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["maxTokens"] in values

    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name != "config.json" and p.name != "config.json.lock"
    ]
    assert leftovers == [], f"Leftover temp files after concurrent writes: {leftovers}"


def test_save_config_creates_parent_directory(tmp_path: Path) -> None:
    """save_config must create missing parent directories (atomic write path)."""
    path = tmp_path / "nested" / "deeper" / "config.json"
    cfg = Config()
    cfg.agents.defaults.max_tokens = 31337
    save_config(cfg, path)

    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["maxTokens"] == 31337
