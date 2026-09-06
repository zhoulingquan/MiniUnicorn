"""定时任务管理 handler。"""

from __future__ import annotations

from websockets.http11 import Response

from .._http_router import RouteContext, router
from .._http_routes import _http_error, _http_json_response
from ._common import require_auth, service_unavailable


@router.route("/api/cron/jobs", methods={"GET"})
@require_auth
def list(ctx: RouteContext) -> Response:
    """List all cron jobs (including system jobs and disabled ones)."""
    from erza.webui.cron_api import list_cron_jobs

    if ctx.deps.cron_service is None:
        return service_unavailable("cron service is not available")
    try:
        payload = list_cron_jobs(ctx.deps.cron_service, include_disabled=True)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/cron/jobs/create", methods={"GET", "POST"})
@require_auth
def create(ctx: RouteContext) -> Response:
    """Create a new user cron job."""
    from erza.webui.cron_api import WebUICronError, create_cron_job

    if ctx.deps.cron_service is None:
        return service_unavailable("cron service is not available")
    query = ctx.query
    try:
        payload = create_cron_job(ctx.deps.cron_service, query)
    except WebUICronError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/cron/jobs/delete", methods={"GET", "POST"})
@require_auth
def delete(ctx: RouteContext) -> Response:
    """Delete a cron job by id (system jobs are protected)."""
    from erza.webui.cron_api import WebUICronError, delete_cron_job

    if ctx.deps.cron_service is None:
        return service_unavailable("cron service is not available")
    query = ctx.query
    try:
        payload = delete_cron_job(ctx.deps.cron_service, query)
    except WebUICronError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)


@router.route("/api/cron/jobs/toggle", methods={"GET", "POST"})
@require_auth
def toggle(ctx: RouteContext) -> Response:
    """Enable or disable a cron job by id."""
    from erza.webui.cron_api import WebUICronError, toggle_cron_job

    if ctx.deps.cron_service is None:
        return service_unavailable("cron service is not available")
    query = ctx.query
    try:
        payload = toggle_cron_job(ctx.deps.cron_service, query)
    except WebUICronError as e:
        return _http_error(e.status, e.message)
    except Exception as exc:
        return _http_error(500, str(exc))
    return _http_json_response(payload)
