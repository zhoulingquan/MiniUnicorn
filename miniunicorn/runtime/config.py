"""Runtime configuration parser and validation (design §27).

The configuration shape mirrors the JSON example in design §27. Validation
enforces the cross-field invariants from §27 ("Validation" list), e.g.
heartbeat < lease_timeout / 3, progress_timeout > heartbeat, and
outbox_lease_timeout > channel_send_timeout.

The configuration is intentionally separate from the main
:mod:`miniunicorn.config.schema` so the runtime package remains
self-contained and the legacy configuration path can opt out via
``runtime.enabled=false`` during rollout (design §31.1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class _RuntimeBase(BaseModel):
    """Base model accepting both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


RuntimeMode = Literal["lightweight", "supervised"]


class RuntimeConfig(_RuntimeBase):
    """Parsed and validated runtime configuration (design §27).

    Defaults match the recommended shape in §27. ``database_path`` and
    ``backup_path`` are resolved against the runtime data root by
    :func:`parse_runtime_config`.
    """

    enabled: bool = True
    mode: RuntimeMode = "lightweight"
    database_path: str = "runtime/runtime.sqlite"
    backup_path: str = "runtime/backups"
    worker_count: int = Field(default=3, ge=2)
    lightweight_execution_slots: int = Field(default=1, ge=1, le=3)
    worker_concurrency: int = Field(default=1, ge=1)
    heartbeat_interval_s: int = Field(default=15, ge=1)
    lease_timeout_s: int = Field(default=180, ge=1)
    lease_scan_interval_s: int = Field(default=15, ge=1)
    progress_timeout_s: int = Field(default=600, ge=1)
    task_max_attempts: int = Field(default=3, ge=1)
    queue_poll_min_ms: int = Field(default=250, ge=10)
    queue_poll_max_ms: int = Field(default=2000, ge=10)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=1)
    realtime_event_queue_capacity: int = Field(default=1000, ge=1)
    shutdown_grace_s: int = Field(default=60, ge=1)
    approval_timeout_m: int = Field(default=30, ge=1)
    waiting_alert_m: int = Field(default=60, ge=1)
    outbox_lease_timeout_s: int = Field(default=120, ge=1)
    outbox_max_attempts: int = Field(default=8, ge=1)
    channel_send_timeout_s: int = Field(default=60, ge=1)
    successful_retention_d: int = Field(default=7, ge=1)
    failure_retention_d: int = Field(default=30, ge=1)
    backup_interval_h: int = Field(default=6, ge=1)
    backup_retention_d: int = Field(default=7, ge=1)
    inline_blob_max_bytes: int = Field(default=1_048_576, ge=1)
    minimum_free_disk_mb: int = Field(default=1024, ge=1)
    max_turn_wall_time_m: int = Field(default=120, ge=1)
    stable_max_tool_iterations: int = Field(default=50, ge=1)
    global_max_subagents: int = Field(default=4, ge=1)
    worker_max_rss_mb: int | None = Field(default=None, ge=1)
    worker_max_uptime_h: int | None = Field(default=None, ge=1)
    worker_max_tasks_before_recycle: int | None = Field(default=None, ge=1)

    # Resolved absolute paths (populated by parse_runtime_config).
    database_path_resolved: Path | None = None
    backup_path_resolved: Path | None = None

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("lightweight", "supervised"):
            raise ValueError(
                f"runtime.mode must be 'lightweight' or 'supervised', got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _validate_cross_field(self) -> RuntimeConfig:
        # heartbeat < lease_timeout / 3 (design §24.1, §27)
        if self.heartbeat_interval_s * 3 >= self.lease_timeout_s:
            raise ValueError(
                f"heartbeat_interval_s ({self.heartbeat_interval_s}) must be less than "
                f"one third of lease_timeout_s ({self.lease_timeout_s})"
            )
        # progress timeout exceeds heartbeat (§27)
        if self.progress_timeout_s <= self.heartbeat_interval_s:
            raise ValueError(
                f"progress_timeout_s ({self.progress_timeout_s}) must exceed "
                f"heartbeat_interval_s ({self.heartbeat_interval_s})"
            )
        # maximum queue poll is below lease scan interval (§27)
        if self.queue_poll_max_ms >= self.lease_scan_interval_s * 1000:
            raise ValueError(
                f"queue_poll_max_ms ({self.queue_poll_max_ms}) must be below "
                f"lease_scan_interval_s * 1000 ({self.lease_scan_interval_s * 1000})"
            )
        # Outbox lease timeout exceeds Channel send timeout (§27)
        if self.outbox_lease_timeout_s <= self.channel_send_timeout_s:
            raise ValueError(
                f"outbox_lease_timeout_s ({self.outbox_lease_timeout_s}) must exceed "
                f"channel_send_timeout_s ({self.channel_send_timeout_s})"
            )
        # SQLite busy timeout is positive and shorter than task lease timeout (§27)
        if self.sqlite_busy_timeout_ms >= self.lease_timeout_s * 1000:
            raise ValueError(
                f"sqlite_busy_timeout_ms ({self.sqlite_busy_timeout_ms}) must be shorter "
                f"than lease_timeout_s * 1000 ({self.lease_timeout_s * 1000})"
            )
        # supervised version one requires worker_concurrency=1 (§27)
        if self.mode == "supervised" and self.worker_concurrency != 1:
            raise ValueError(
                "supervised version one requires worker_concurrency=1"
            )
        # lightweight slots are 1..3 (§27, §9.1)
        if not 1 <= self.lightweight_execution_slots <= 3:
            raise ValueError(
                f"lightweight_execution_slots must be 1..3, "
                f"got {self.lightweight_execution_slots}"
            )
        # supervised worker count is at least two (§27)
        if self.mode == "supervised" and self.worker_count < 2:
            raise ValueError(
                f"supervised worker_count must be at least 2, got {self.worker_count}"
            )
        # backup path differs from the live database directory (§27)
        if self.backup_path == self.database_path:
            raise ValueError(
                "backup_path must differ from database_path"
            )
        return self


def parse_runtime_config(
    raw: dict[str, Any] | None,
    *,
    data_root: Path | None = None,
) -> RuntimeConfig:
    """Parse and validate a runtime configuration dict (design §27).

    ``data_root`` is the workspace runtime data root. When provided,
    ``database_path`` and ``backup_path`` are resolved against it (relative
    paths stay inside the data root; absolute paths are honored as-is but
    must be explicitly allowed by the caller).

    Returns a :class:`RuntimeConfig` with ``database_path_resolved`` and
    ``backup_path_resolved`` populated.
    """
    raw = dict(raw or {})
    cfg = RuntimeConfig.model_validate(raw)
    if data_root is not None:
        db_path = Path(cfg.database_path)
        cfg.database_path_resolved = (
            db_path if db_path.is_absolute() else (data_root / db_path).resolve()
        )
        bk_path = Path(cfg.backup_path)
        cfg.backup_path_resolved = (
            bk_path if bk_path.is_absolute() else (data_root / bk_path).resolve()
        )
    return cfg


__all__ = ["RuntimeConfig", "RuntimeMode", "parse_runtime_config"]
