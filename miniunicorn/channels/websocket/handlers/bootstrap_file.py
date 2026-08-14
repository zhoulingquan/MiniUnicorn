"""AGENTS.md/SOUL.md read-write and memory diagnostics read-only handler."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import unquote

from websockets.http11 import Response

from .._http_router import RouteContext, router
from .._http_routes import (
    _collect_chunked_header,
    _http_error,
    _http_json_response,
    _human_readable_size,
    _query_first,
)
from ._common import require_auth

# Allowed bootstrap files (workspace-root markdown loaded into the system
# prompt by ContextBuilder). Kept to a fixed allowlist to avoid arbitrary
# file reads/writes through this endpoint.
BOOTSTRAP_FILE_ALLOWLIST: tuple[str, ...] = ("AGENTS.md", "SOUL.md")

# Governed memory diagnostics exposed read-only in the WebUI.
DREAM_FILE_ALLOWLIST: tuple[str, ...] = (
    "SOUL.md",
    "memory/history.jsonl",
    "memory/reflections.jsonl",
    "memory/shared/POLICY.md",
    "memory/structured/journal.jsonl",
    "memory/structured/tags.json",
    "memory/structured/recall-audit.jsonl",
)


@router.route("/api/bootstrap-file", methods={"GET"})
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


@router.route("/api/bootstrap-file/save", methods={"GET", "POST"})
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


@router.route("/api/dream/files", methods={"GET"})
@require_auth
def list_dream_files(ctx: RouteContext) -> Response:
    """List memory files and their metadata for read-only diagnostics."""
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
                entry["modified_at_human"] = (
                    datetime.fromtimestamp(mtime, tz=timezone.utc)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S")
                )
            files.append(entry)
        return _http_json_response({"files": files})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/dream/file", methods={"GET"})
@require_auth
def read_dream_file(ctx: RouteContext) -> Response:
    """Read an allowlisted memory diagnostic file."""
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
