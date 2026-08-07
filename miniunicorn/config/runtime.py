"""Root-owned Runtime configuration (hard-cutover design §27).

The configuration shape mirrors the JSON example in design §27.  Validation
enforces the cross-field invariants from §27.  The ``enabled`` switch is gone:
the durable Runtime is the only execution path.  ``mode`` is left optional
here and resolved at launcher time via :func:`resolve_runtime_mode`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

RuntimeMode = Literal["lightweight", "supervised"]


class RuntimeConfig(BaseModel):
    """Parsed and validated runtime configuration (design §27)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    mode: RuntimeMode | None = None
    database_path: str = "runtime/runtime.sqlite"
    backup_path: str = "runtime/backups"
    worker_count: int = Field(default=3, ge=2)
    lightweight_execution_slots: int = Field(default=1, ge=1, le=3)
    worker_concurrency: int = Field(default=1, ge=1, le=1)
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
    database_path_resolved: Path | None = None
    backup_path_resolved: Path | None = None

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "RuntimeConfig":
        if self.heartbeat_interval_s * 3 >= self.lease_timeout_s:
            raise ValueError("heartbeat interval must be less than one third of lease timeout")
        if self.progress_timeout_s <= self.heartbeat_interval_s:
            raise ValueError("progress timeout must exceed heartbeat interval")
        if self.queue_poll_max_ms >= self.lease_scan_interval_s * 1000:
            raise ValueError("maximum queue poll must be below lease scan interval")
        if self.outbox_lease_timeout_s <= self.channel_send_timeout_s:
            raise ValueError("Outbox lease timeout must exceed Channel send timeout")
        if self.sqlite_busy_timeout_ms >= self.lease_timeout_s * 1000:
            raise ValueError("SQLite busy timeout must be shorter than task lease timeout")
        if self.backup_path == self.database_path:
            raise ValueError("backup path must differ from database path")
        return self


def resolve_runtime_mode(
    *,
    configured: RuntimeMode | None,
    cli_value: RuntimeMode | None,
    environment: RuntimeMode | None,
    launcher_default: RuntimeMode,
) -> RuntimeMode:
    """Resolve the effective runtime mode using CLI > env > config > default."""
    return cli_value or environment or configured or launcher_default


def resolve_runtime_paths(config: RuntimeConfig, data_root: Path) -> RuntimeConfig:
    """Return a copy of *config* with resolved absolute database/backup paths."""
    update = {
        "database_path_resolved": (
            Path(config.database_path)
            if Path(config.database_path).is_absolute()
            else (data_root / config.database_path).resolve()
        ),
        "backup_path_resolved": (
            Path(config.backup_path)
            if Path(config.backup_path).is_absolute()
            else (data_root / config.backup_path).resolve()
        ),
    }
    return config.model_copy(update=update)


def parse_runtime_config(
    raw: dict[str, Any] | None,
    *,
    data_root: Path | None = None,
) -> RuntimeConfig:
    """Compatibility wrapper: validate *raw* and optionally resolve paths."""
    cfg = RuntimeConfig.model_validate(dict(raw or {}))
    if data_root is not None:
        cfg = resolve_runtime_paths(cfg, data_root)
    return cfg


__all__ = [
    "RuntimeConfig",
    "RuntimeMode",
    "resolve_runtime_mode",
    "resolve_runtime_paths",
    "parse_runtime_config",
]
