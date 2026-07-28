"""Skill 增删改查 handler。"""

from __future__ import annotations

import re
from urllib.parse import unquote

from websockets.http11 import Response

from miniunicorn.security.workspace_policy import is_path_within

from .._http_router import RouteContext, router
from .._http_routes import (
    SKILL_ZIP_MAX_HEADERS,
    SKILL_ZIP_MAX_TOTAL_BYTES,
    ChunkedHeaderLimitError,
    _collect_chunked_header,
    _http_error,
    _http_json_response,
    _query_first,
    collect_chunked_header_limited,
)
from ._common import require_auth


@router.route("/api/skills", methods={"GET"})
def list_skills(ctx: RouteContext) -> Response:
    """Return available skills (builtin + workspace) as JSON.

    Includes disabled skills with a ``disabled`` flag so the UI can render
    the toggle state.
    """
    from miniunicorn.agent.skills import SkillsLoader

    try:
        loader = SkillsLoader(ctx.deps.workspace_path)
        # Refresh the disabled set from live config, then enumerate all
        # skills (without the disabled filter) so the UI can show them.
        loader._refresh_disabled_from_config()
        disabled_set = set(loader.disabled_skills)
        all_entries = loader._skill_entries_from_dir(loader.workspace_skills, "workspace")
        workspace_names = {e["name"] for e in all_entries}
        if loader.builtin_skills and loader.builtin_skills.exists():
            all_entries.extend(
                loader._skill_entries_from_dir(
                    loader.builtin_skills, "builtin", skip_names=workspace_names
                )
            )
        result = []
        for entry in all_entries:
            meta = loader.get_skill_metadata(entry["name"]) or {}
            description = meta.get("description", entry["name"])
            skill_meta = loader._get_skill_meta(entry["name"])
            available = loader._check_requirements(skill_meta)
            always = bool(
                loader._parse_miniunicorn_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
            # 暴露声明的依赖(requires)与缺失项(missing),供前端卡片展示。
            requires_meta = skill_meta.get("requires", {}) or {}
            requires = {
                "bins": list(requires_meta.get("bins", []) or []),
                "env": list(requires_meta.get("env", []) or []),
            }
            missing = loader._get_missing_requirements(skill_meta) if not available else ""
            result.append(
                {
                    "name": entry["name"],
                    "description": description,
                    "source": entry["source"],
                    "available": available,
                    "disabled": entry["name"] in disabled_set,
                    "always": always,
                    "builtin_only": loader.is_builtin_skill(entry["name"]),
                    "path": entry["path"],
                    "requires": requires,
                    "missing": missing,
                }
            )
        return _http_json_response({"skills": result})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/skills/delete", methods={"GET", "POST"})
def delete_skill(ctx: RouteContext) -> Response:
    """Delete a workspace skill by name."""
    import shutil

    names = ctx.query.get("name", [])
    name = names[0] if names else None
    if not name:
        return _http_error(400, "missing 'name' parameter")

    # Strict whitelist: only allow filesystem-safe identifiers. This
    # replaces the old blacklist (``replace("/", "").replace("..", "")``)
    # which could be bypassed with sequences like ``....//``.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return _http_error(400, "invalid skill name")

    skills_dir = (ctx.deps.workspace_path / "skills").resolve()
    skill_dir = (skills_dir / name).resolve()
    # Secondary check: ensure the resolved path is still inside skills_dir
    # (guards against symlink tricks and filesystem edge cases).
    if not is_path_within(skill_dir, skills_dir):
        return _http_error(400, "invalid skill name")

    if not skill_dir.exists():
        return _http_error(404, f"skill '{name}' not found in workspace")

    try:
        shutil.rmtree(skill_dir)
        return _http_json_response({"deleted": True, "name": name})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/skills/toggle", methods={"GET", "POST"})
@require_auth
def toggle_skill(ctx: RouteContext) -> Response:
    """Enable or disable a skill at runtime (hot reload, no restart).

    Updates ``config.agents.defaults.disabled_skills`` and saves. The
    change is picked up on the next agent turn via
    ``SkillsLoader._refresh_disabled_from_config``.
    """
    from miniunicorn.agent.skills import is_valid_skill_name
    from miniunicorn.config.loader import load_config, save_config

    name = _query_first(ctx.query, "name")
    if not name or not is_valid_skill_name(name):
        return _http_error(400, "invalid 'name' parameter")
    disabled_value = _query_first(ctx.query, "disabled")
    if disabled_value is None:
        return _http_error(400, "missing 'disabled' parameter")
    disable = disabled_value.lower() in ("1", "true", "yes", "on")

    try:
        config = load_config()
        current = list(getattr(config.agents.defaults, "disabled_skills", []) or [])
        if disable:
            if name not in current:
                current.append(name)
        else:
            current = [n for n in current if n != name]
        config.agents.defaults.disabled_skills = current
        save_config(config)
        return _http_json_response({"name": name, "disabled": disable, "disabled_skills": current})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/skills/read", methods={"GET"})
def read_skill(ctx: RouteContext) -> Response:
    """Return a skill's SKILL.md content and bundled file list."""
    from miniunicorn.agent.skills import SkillsLoader

    name = _query_first(ctx.query, "name")
    if not name:
        return _http_error(400, "missing 'name' parameter")
    try:
        loader = SkillsLoader(ctx.deps.workspace_path)
        content = loader.load_skill(name)
        if content is None:
            return _http_error(404, f"skill '{name}' not found")
        files = loader.list_skill_files(name)
        meta = loader.get_skill_metadata(name) or {}
        ws_exists = (loader.workspace_skills / name / "SKILL.md").exists()
        return _http_json_response(
            {
                "name": name,
                "content": content,
                "files": files,
                "source": "workspace" if ws_exists else "builtin",
                "metadata": meta,
                "builtin_only": loader.is_builtin_skill(name),
            }
        )
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/skills/file", methods={"GET"})
def read_skill_file(ctx: RouteContext) -> Response:
    """Read a single bundled file from a skill (traversal-safe)."""
    from miniunicorn.agent.skills import SkillsLoader

    name = _query_first(ctx.query, "name")
    rel = _query_first(ctx.query, "path")
    if not name or not rel:
        return _http_error(400, "missing 'name' or 'path' parameter")
    try:
        loader = SkillsLoader(ctx.deps.workspace_path)
        content = loader.read_skill_file(name, rel)
        if content is None:
            return _http_error(404, "file not found")
        return _http_json_response({"name": name, "path": rel, "content": content})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/skills/save", methods={"GET", "POST"})
@require_auth
def save_skill(ctx: RouteContext) -> Response:
    """Create or update a workspace skill's SKILL.md.

    Accepts the new content via repeated ``x-miniunicorn-Skill-Content``
    headers (URL-encoded chunks, concatenated in order) to stay within the
    HTTP line limit for large skills. Falls back to the ``content`` query
    parameter for small edits.
    """

    from miniunicorn.agent.skills import SkillsLoader, is_valid_skill_name

    name = _query_first(ctx.query, "name")
    if not name or not is_valid_skill_name(name):
        return _http_error(400, "invalid 'name' parameter")

    # Prefer chunked content headers; fall back to query param.
    header_b64 = _collect_chunked_header(ctx.request.headers, "x-miniunicorn-Skill-Content")
    if header_b64:
        content = unquote(header_b64)
    else:
        content_values = ctx.query.get("content", [])
        content = unquote(content_values[0]) if content_values else ""
    if not content.strip():
        return _http_error(400, "content must not be empty")

    try:
        loader = SkillsLoader(ctx.deps.workspace_path)
        path = loader.save_skill_content(name, content)
        return _http_json_response({"saved": True, "name": name, "path": str(path)})
    except ValueError as exc:
        return _http_error(400, str(exc))
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/skills/upload", methods={"GET", "POST"})
@require_auth
def upload_skill(ctx: RouteContext) -> Response:
    """Upload and extract a ZIP skill package into the workspace.

    The websockets HTTP layer does not read request bodies, so the ZIP is
    transported as base64 chunks in repeated ``x-miniunicorn-Skill-Zip``
    headers (each header stays under the 8KB line limit). The chunks are
    concatenated in order before decoding.

    大型 ZIP 会触发大量分块 header,可能超过协议 header 数量上限。前后端使用
    同一最大 header 数(``SKILL_ZIP_MAX_HEADERS``)和总字节上限
    (``SKILL_ZIP_MAX_TOTAL_BYTES``);超限返回 413,前端在发送前即报错。
    """
    import base64

    from miniunicorn.agent.skills import SkillsLoader

    preferred = _query_first(ctx.query, "name")
    preferred = unquote(preferred) if preferred else None

    # Collect base64 chunks from repeated headers (order preserved), enforcing
    # a shared header-count and total-byte limit so a huge ZIP cannot exhaust
    # the HTTP header budget.
    try:
        b64_data = collect_chunked_header_limited(
            ctx.request.headers,
            "x-miniunicorn-Skill-Zip",
            max_count=SKILL_ZIP_MAX_HEADERS,
            max_total_bytes=SKILL_ZIP_MAX_TOTAL_BYTES,
        )
    except ChunkedHeaderLimitError as exc:
        return _http_error(413, str(exc))
    if not b64_data:
        return _http_error(400, "missing ZIP data (send via x-miniunicorn-Skill-Zip headers)")

    try:
        data = base64.b64decode(b64_data)
    except Exception as exc:
        return _http_error(400, f"invalid base64 data: {exc}")

    if not data:
        return _http_error(400, "empty zip data")

    try:
        loader = SkillsLoader(ctx.deps.workspace_path)
        skill_name = loader.extract_zip_skill(data, preferred_name=preferred)
        return _http_json_response({"uploaded": True, "name": skill_name})
    except ValueError as exc:
        return _http_error(400, str(exc))
    except Exception as exc:
        return _http_error(500, str(exc))
