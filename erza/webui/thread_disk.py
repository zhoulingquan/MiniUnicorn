"""Legacy WebUI JSON snapshot path helpers (JSON file); transcripts use transcript."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from erza.config.paths import get_webui_dir
from erza.session.manager import SessionManager
from erza.webui.transcript import delete_webui_transcript


def webui_thread_file_path(session_key: str) -> Path:
    stem = SessionManager.safe_key(session_key)
    return get_webui_dir() / f"{stem}.json"


def delete_webui_thread(session_key: str) -> bool:
    """Remove legacy WebUI JSON snapshot and append-only transcript for *session_key*.

    同时清理 V2 与 legacy 命名路径(与 SessionManager.delete_session / transcript
    对齐),避免遗留 legacy 文件导致删除后刷新又出现。
    """
    removed = False
    # V2 与 legacy 两套 .json 快照路径
    for path in (
        webui_thread_file_path(session_key),
        get_webui_dir() / f"{SessionManager.safe_key_legacy(session_key)}.json",
    ):
        if path.is_file():
            try:
                path.unlink()
                removed = True
            except OSError as e:
                logger.warning("Failed to delete webui thread file {}: {}", path, e)
    if delete_webui_transcript(session_key):
        removed = True
    return removed
