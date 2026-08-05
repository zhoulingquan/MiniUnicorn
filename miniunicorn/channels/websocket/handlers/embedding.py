"""Embedding memory HTTP handler: status, operations, and search.

Routes:
- GET  /api/embedding/status   — shared status snapshot (200)
- POST /api/embedding/setup    — start background model setup (202)
- POST /api/embedding/verify   — start background model+index verify (202)
- POST /api/embedding/rebuild  — start background index rebuild (202)
- GET  /api/embedding/search   — bounded recall search (200)

The websockets HTTP layer only supports GET, so mutating endpoints also
accept GET (query params carry no body). All endpoints require auth.
"""

from __future__ import annotations

from websockets.http11 import Response

from miniunicorn.config.loader import load_config
from miniunicorn.webui.embedding_api import EmbeddingApiError, EmbeddingApiService

from .._http_router import RouteContext, router
from .._http_routes import _http_json_response
from ._common import require_auth


def _make_service(ctx: RouteContext) -> EmbeddingApiService:
    """Build the service from the live config and request workspace."""
    config = load_config()
    configured = bool(config.agents.defaults.vector_recall)
    return EmbeddingApiService(ctx.deps.workspace_path, configured=configured)


@router.route("/api/embedding/status", methods={"GET"})
@require_auth
def embedding_status(ctx: RouteContext) -> Response:
    service = _make_service(ctx)
    return _http_json_response(service.status())


@router.route("/api/embedding/setup", methods={"GET", "POST"})
@require_auth
def embedding_setup(ctx: RouteContext) -> Response:
    service = _make_service(ctx)
    try:
        return _http_json_response(service.start("setup"), status=202)
    except EmbeddingApiError as exc:
        return _http_json_response({"error": exc.code, "message": exc.message}, status=exc.status)


@router.route("/api/embedding/verify", methods={"GET", "POST"})
@require_auth
def embedding_verify(ctx: RouteContext) -> Response:
    service = _make_service(ctx)
    try:
        return _http_json_response(service.start("verify"), status=202)
    except EmbeddingApiError as exc:
        return _http_json_response({"error": exc.code, "message": exc.message}, status=exc.status)


@router.route("/api/embedding/rebuild", methods={"GET", "POST"})
@require_auth
def embedding_rebuild(ctx: RouteContext) -> Response:
    service = _make_service(ctx)
    try:
        return _http_json_response(service.start("rebuild"), status=202)
    except EmbeddingApiError as exc:
        return _http_json_response({"error": exc.code, "message": exc.message}, status=exc.status)


@router.route("/api/embedding/search", methods={"GET"})
@require_auth
async def embedding_search(ctx: RouteContext) -> Response:
    service = _make_service(ctx)
    query_values = ctx.query.get("q", [])
    query = query_values[0] if query_values else ""
    try:
        return _http_json_response(await service.search(query))
    except EmbeddingApiError as exc:
        return _http_json_response({"error": exc.code, "message": exc.message}, status=exc.status)
