"""子代理增删改查 + LLM 生成 handler。"""

from __future__ import annotations

from urllib.parse import unquote

from websockets.http11 import Response

from .._http_router import RouteContext, router
from .._http_routes import (
    _collect_chunked_header,
    _http_error,
    _http_json_response,
    _query_first,
)
from ._common import require_auth


@router.route("/api/agents", methods={"GET"})
def list(ctx: RouteContext) -> Response:
    """Return all registered subagent definitions as JSON."""
    from miniunicorn.agent.routes_agents import router

    try:
        return _http_json_response(router.list_agents(ctx.deps.workspace_path))
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/agents/read", methods={"GET"})
def read(ctx: RouteContext) -> Response:
    """Return a single subagent definition (parsed fields + raw .md)."""
    from miniunicorn.agent.routes_agents import router

    name = _query_first(ctx.query, "name")
    if not name:
        return _http_error(400, "missing 'name' parameter")
    try:
        data = router.read_agent(ctx.deps.workspace_path, name)
        if data is None:
            return _http_error(404, f"agent '{name}' not found")
        return _http_json_response(data)
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/agents/save", methods={"GET", "POST"})
@require_auth
def save(ctx: RouteContext) -> Response:
    """Create or update an agent's ``.md`` file.

    Accepts content via repeated ``x-miniunicorn-Agent-Content`` headers
    (URL-encoded chunks) like the skills save endpoint.
    """

    from miniunicorn.agent.routes_agents import router

    name = _query_first(ctx.query, "name")
    if not name:
        return _http_error(400, "missing 'name' parameter")

    header_data = _collect_chunked_header(ctx.request.headers, "x-miniunicorn-Agent-Content")
    if header_data:
        content = unquote(header_data)
    else:
        content_values = ctx.query.get("content", [])
        content = unquote(content_values[0]) if content_values else ""

    try:
        path = router.save_agent(ctx.deps.workspace_path, name, content)
        return _http_json_response({"saved": True, "name": name, "path": path})
    except ValueError as exc:
        return _http_error(400, str(exc))
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/agents/delete", methods={"GET", "POST"})
@require_auth
def delete(ctx: RouteContext) -> Response:
    """Delete an agent's ``.md`` file by name."""
    from miniunicorn.agent.routes_agents import router

    name = _query_first(ctx.query, "name")
    if not name:
        return _http_error(400, "missing 'name' parameter")
    try:
        deleted = router.delete_agent(ctx.deps.workspace_path, name)
        if not deleted:
            return _http_error(404, f"agent '{name}' not found")
        return _http_json_response({"deleted": True, "name": name})
    except Exception as exc:
        return _http_error(500, str(exc))


@router.route("/api/agents/generate", methods={"GET", "POST"})
@require_auth
async def generate_agent(ctx: RouteContext) -> Response:
    """Generate a subagent ``.md`` definition via the LLM.

    Accepts the user's natural-language description via the
    ``x-miniunicorn-Agent-Description`` chunked header (URL-encoded)
    or a ``description`` query parameter. Returns the generated
    content as JSON without persisting it; the client is expected to
    review the preview and then POST to ``/api/agents/save``.
    """

    from miniunicorn.agent.routes_agents import router

    # Resolve the LLM provider lazily so the channel can be constructed
    # before the agent loop is fully wired (e.g. in tests).
    provider = None
    if ctx.deps.provider_loader is not None:
        try:
            provider = ctx.deps.provider_loader()
        except Exception as exc:
            ctx.deps.logger.warning("provider_loader failed: {}", exc)
            provider = None
    if provider is None:
        return _http_error(503, "LLM provider unavailable")

    # Resolve model name (may be None — provider default applies).
    model_name: str | None = None
    if ctx.deps.runtime_model_name is not None:
        try:
            raw_model = ctx.deps.runtime_model_name()
        except Exception:
            raw_model = None
        if isinstance(raw_model, str) and raw_model.strip():
            model_name = raw_model.strip()

    header_data = _collect_chunked_header(ctx.request.headers, "x-miniunicorn-Agent-Description")
    if header_data:
        description = unquote(header_data)
    else:
        desc_values = ctx.query.get("description", [])
        description = unquote(desc_values[0]) if desc_values else ""

    if not description.strip():
        return _http_error(400, "missing 'description' parameter")

    try:
        result = await router.generate_agent(
            ctx.deps.workspace_path, provider, model_name, description
        )
        return _http_json_response(result)
    except ValueError as exc:
        return _http_error(400, str(exc))
    except Exception as exc:
        return _http_error(500, str(exc))
