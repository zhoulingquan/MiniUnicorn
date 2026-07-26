"""AGENTS.md/SOUL.md 读写 + Dream 记忆文件只读 handler。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from websockets.http11 import Response

from .._http_routes import (
    _collect_chunked_header,
    _http_error,
    _http_json_response,
    _human_readable_size,
    _query_first,
)
from .._http_router import RouteContext, router
from ._common import require_auth

# Allowed bootstrap files (workspace-root markdown loaded into the system
# prompt by ContextBuilder). Kept to a fixed allowlist to avoid arbitrary
# file reads/writes through this endpoint.
BOOTSTRAP_FILE_ALLOWLIST: tuple[str, ...] = ("AGENTS.md", "SOUL.md")

# Dream 维护的记忆/人格文件白名单。这些文件由 Dream(记忆整合)流程
# 读取或写入,通过此端点向 WebUI 暴露只读视图。所有路径相对于工作区根目录。
# SOUL.md 既是 bootstrap 人格文件,也是 Dream 演化的产物(Phase 1 读、
# Phase 2 可通过 EditFileTool 修改),故在此暴露只读视图;编辑入口仍在
# Persona section。AGENTS.md 完全由用户手动维护,Dream 不动,故不列入。
DREAM_FILE_ALLOWLIST: tuple[str, ...] = (
    "SOUL.md",
    "USER.md",
    "memory/MEMORY.md",
    "memory/history.jsonl",
    "memory/episodic.jsonl",
    "memory/procedural.jsonl",
    "memory/reflections.jsonl",
    "memory/shared/MEMORY_SHARED.md",
    "memory/shared/procedural_shared.jsonl",
)


@router.route("/api/bootstrap-file")
@require_auth
def read_bootstrap_file(ctx: RouteContext) -> Response:
    """Read a workspace bootstrap markdown file (AGENTS.md / SOUL.md)."""
    name = _query_first(ctx.query, "name")
    if name not in BOOTSTRAP_FILE_ALLOWLIST:
        return _http_error(400, "invalid or missing 'name' parameter")
    try:
        path = (ctx.deps.workspace_path / name).resolve()
        try:
            path.relative_to(ctx.deps.workspace_path.resolve())
        except ValueError:
            return _http_error(400, "path escapes workspace")
        if not path.exists():
            return _http_json_response({"name": name, "content": "", "exists": False})
        content = path.read_text(encoding="utf-8")
        return _http_json_response({"name": name, "content": content, "exists": True})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/bootstrap-file/save")
@require_auth
def save_bootstrap_file(ctx: RouteContext) -> Response:
    """Create or update a workspace bootstrap markdown file.

    Accepts content via repeated ``x-miniunicorn-Bootstrap-Content`` headers
    (URL-encoded chunks concatenated in order) to stay within the HTTP line
    limit for large files.
    """

    name = _query_first(ctx.query, "name")
    if name not in BOOTSTRAP_FILE_ALLOWLIST:
        return _http_error(400, "invalid or missing 'name' parameter")
    header_b64 = _collect_chunked_header(ctx.request.headers, "x-miniunicorn-Bootstrap-Content")
    if header_b64:
        content = unquote(header_b64)
    else:
        content_values = ctx.query.get("content", [])
        content = unquote(content_values[0]) if content_values else ""
    if not content.strip():
        return _http_error(400, "content must not be empty")
    try:
        path = (ctx.deps.workspace_path / name).resolve()
        try:
            path.relative_to(ctx.deps.workspace_path.resolve())
        except ValueError:
            return _http_error(400, "path escapes workspace")
        path.write_text(content, encoding="utf-8")
        # Invalidate ContextBuilder's bootstrap cache so the next turn sees
        # the new content without waiting for mtime to change (mtime
        # resolution is sufficient on most filesystems, but explicit
        # invalidation guarantees immediacy for same-mtime writes).
        ctx.deps.invalidate_bootstrap_cache(name)
        return _http_json_response({"saved": True, "name": name, "path": str(path)})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/dream/files")
@require_auth
def list_dream_files(ctx: RouteContext) -> Response:
    """列出 Dream 生成的记忆文件及其元信息(大小、修改时间、是否存在)。"""
    try:
        ws_root = ctx.deps.workspace_path.resolve()
        files = []
        for rel in DREAM_FILE_ALLOWLIST:
            path = (ws_root / rel).resolve()
            try:
                path.relative_to(ws_root)
            except ValueError:
                continue
            exists = path.exists()
            entry = {
                "name": rel,
                "exists": exists,
                "size": 0,
                "size_human": "",
                "modified_at": None,
                "modified_at_human": "",
            }
            if exists:
                stat = path.stat()
                entry["size"] = stat.st_size
                entry["size_human"] = _human_readable_size(stat.st_size)
                mtime = stat.st_mtime
                entry["modified_at"] = mtime
                entry["modified_at_human"] = datetime.fromtimestamp(
                    mtime, tz=timezone.utc
                ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            files.append(entry)
        return _http_json_response({"files": files})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/dream/file")
@require_auth
def read_dream_file(ctx: RouteContext) -> Response:
    """读取 Dream 生成的记忆文件内容(只读)。"""
    name = _query_first(ctx.query, "name")
    if not name or name not in DREAM_FILE_ALLOWLIST:
        return _http_error(400, "invalid or missing 'name' parameter")
    try:
        ws_root = ctx.deps.workspace_path.resolve()
        path = (ws_root / name).resolve()
        try:
            path.relative_to(ws_root)
        except ValueError:
            return _http_error(400, "path escapes workspace")
        if not path.exists():
            return _http_json_response({"name": name, "content": "", "exists": False})
        content = path.read_text(encoding="utf-8")
        return _http_json_response({"name": name, "content": content, "exists": True})
    except Exception as exc:
        return _http_error(500, str(exc))
