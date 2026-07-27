"""Cron job REST helpers for the WebUI HTTP surface.

Serializes CronService jobs to JSON and validates user input for create/update.
System jobs (heartbeat/dream) are visible but cannot be modified or removed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from miniunicorn.cron.service import CronService
from miniunicorn.cron.types import CronJob, CronSchedule

from ._query import _query_first_alias
from ._runtime import QueryParams


class WebUICronError(ValueError):
    """User-facing cron validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _job_to_dict(job: CronJob) -> dict[str, Any]:
    """Serialize a CronJob to a JSON-friendly dict for the WebUI."""
    data = asdict(job)
    data["is_system"] = job.payload.kind == "system_event"
    return data


def list_cron_jobs(service: CronService, include_disabled: bool = True) -> dict[str, Any]:
    """Return all cron jobs as a JSON payload."""
    jobs = service.list_jobs(include_disabled=include_disabled)
    return {"jobs": [_job_to_dict(job) for job in jobs]}


def _parse_schedule(query: QueryParams) -> CronSchedule:
    """Build a CronSchedule from query params.

    Supports three kinds via the ``schedule`` parameter:
      - ``every``: uses ``every_seconds`` (converted to ms)
      - ``cron``: uses ``cron_expr`` and optional ``tz``
      - ``at``: uses ``at_ms`` (one-shot timestamp in ms)
    """
    kind = (_query_first_alias(query, "schedule", "scheduleKind") or "").strip().lower()
    if kind not in {"every", "cron", "at"}:
        raise WebUICronError("schedule must be one of: every, cron, at")

    if kind == "every":
        raw = _query_first_alias(query, "every_seconds", "everySeconds")
        if raw is None:
            raise WebUICronError("every_seconds is required for schedule=every")
        try:
            seconds = int(raw)
        except ValueError:
            raise WebUICronError("every_seconds must be an integer") from None
        if seconds < 60:
            raise WebUICronError("every_seconds must be at least 60")
        return CronSchedule(kind="every", every_ms=seconds * 1000)

    if kind == "cron":
        expr = _query_first_alias(query, "cron_expr", "cronExpr")
        if not expr or not expr.strip():
            raise WebUICronError("cron_expr is required for schedule=cron")
        tz = (_query_first_alias(query, "tz", "timezone") or "system").strip() or "system"
        return CronSchedule(kind="cron", expr=expr.strip(), tz=tz)

    # kind == "at"
    raw = _query_first_alias(query, "at_ms", "atMs")
    if raw is None:
        raise WebUICronError("at_ms is required for schedule=at")
    try:
        at_ms = int(raw)
    except ValueError:
        raise WebUICronError("at_ms must be an integer (epoch ms)") from None
    if at_ms <= 0:
        raise WebUICronError("at_ms must be a positive epoch millisecond timestamp")
    return CronSchedule(kind="at", at_ms=at_ms)


def create_cron_job(service: CronService, query: QueryParams) -> dict[str, Any]:
    """Create a new user cron job (agent_turn)."""
    name = (_query_first_alias(query, "name", "label") or "").strip()
    if not name:
        raise WebUICronError("name is required")
    message = (_query_first_alias(query, "message", "prompt") or "").strip()
    if not message:
        raise WebUICronError("message is required")
    deliver_raw = (_query_first_alias(query, "deliver", "deliver") or "false").strip().lower()
    deliver = deliver_raw in {"1", "true", "yes"}
    delete_after_raw = (
        (_query_first_alias(query, "delete_after_run", "deleteAfterRun") or "false").strip().lower()
    )
    delete_after_run = delete_after_raw in {"1", "true", "yes"}

    schedule = _parse_schedule(query)
    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        delete_after_run=delete_after_run,
    )
    return _job_to_dict(job)


def delete_cron_job(service: CronService, query: QueryParams) -> dict[str, Any]:
    """Remove a cron job by id (system jobs are protected)."""
    job_id = (_query_first_alias(query, "job_id", "jobId") or "").strip()
    if not job_id:
        raise WebUICronError("job_id is required")
    result = service.remove_job(job_id)
    if result == "not_found":
        raise WebUICronError("job not found", status=404)
    if result == "protected":
        raise WebUICronError("system jobs cannot be removed", status=403)
    return {"removed": True, "job_id": job_id}


def toggle_cron_job(service: CronService, query: QueryParams) -> dict[str, Any]:
    """Enable or disable a cron job by id."""
    job_id = (_query_first_alias(query, "job_id", "jobId") or "").strip()
    if not job_id:
        raise WebUICronError("job_id is required")
    enabled_raw = (_query_first_alias(query, "enabled", "enabled") or "").strip().lower()
    if enabled_raw not in {"1", "0", "true", "false", "yes", "no"}:
        raise WebUICronError("enabled must be a boolean")
    enabled = enabled_raw in {"1", "true", "yes"}
    job = service.enable_job(job_id, enabled=enabled)
    if job is None:
        raise WebUICronError("job not found", status=404)
    return _job_to_dict(job)
