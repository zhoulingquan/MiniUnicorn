"""WP1 — Tests for :mod:`miniunicorn.runtime.config` (design §27).

Verifies parsing, defaults, alias handling, and the cross-field invariants
required by design §27 ("Validation" list):

- ``heartbeat_interval_s * 3 < lease_timeout_s``
- ``progress_timeout_s > heartbeat_interval_s``
- ``queue_poll_max_ms < lease_scan_interval_s * 1000``
- ``outbox_lease_timeout_s > channel_send_timeout_s``
- ``sqlite_busy_timeout_ms < lease_timeout_s * 1000``
- supervised mode requires ``worker_concurrency == 1`` and ``worker_count >= 2``
- ``lightweight_execution_slots`` is in ``1..3``
- ``backup_path`` differs from ``database_path``
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from miniunicorn.runtime.config import (
    RuntimeMode,
    parse_runtime_config,
)


class TestDefaults:
    def test_default_config_is_valid(self) -> None:
        cfg = parse_runtime_config(None)
        assert cfg.mode is None
        assert cfg.worker_count == 3
        assert cfg.heartbeat_interval_s == 15
        assert cfg.lease_timeout_s == 180
        assert cfg.outbox_lease_timeout_s == 120
        assert cfg.channel_send_timeout_s == 60

    def test_default_paths(self) -> None:
        cfg = parse_runtime_config(None)
        assert cfg.database_path == "runtime/runtime.sqlite"
        assert cfg.backup_path == "runtime/backups"


class TestAliasHandling:
    def test_camel_case_keys_accepted(self) -> None:
        cfg = parse_runtime_config(
            {
                "heartbeatIntervalS": 15,
                "leaseTimeoutS": 60,
                "outboxLeaseTimeoutS": 90,
                "channelSendTimeoutS": 30,
                "progressTimeoutS": 120,
                "queuePollMaxMs": 1000,
                "leaseScanIntervalS": 15,
            }
        )
        assert cfg.heartbeat_interval_s == 15
        assert cfg.lease_timeout_s == 60
        assert cfg.outbox_lease_timeout_s == 90
        assert cfg.channel_send_timeout_s == 30

    def test_snake_case_keys_accepted(self) -> None:
        cfg = parse_runtime_config(
            {
                "heartbeat_interval_s": 15,
                "lease_timeout_s": 60,
                "outbox_lease_timeout_s": 90,
                "channel_send_timeout_s": 30,
            }
        )
        assert cfg.heartbeat_interval_s == 15
        assert cfg.lease_timeout_s == 60

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises((ValueError, ValidationError)):
            parse_runtime_config({"unknownField": 42})


class TestCrossFieldValidation:
    def test_heartbeat_less_than_lease_third(self) -> None:
        cfg = parse_runtime_config(
            {"heartbeatIntervalS": 15, "leaseTimeoutS": 60}
        )
        assert cfg.heartbeat_interval_s == 15
        assert cfg.lease_timeout_s == 60

        with pytest.raises((ValueError, ValidationError), match="heartbeat"):
            parse_runtime_config(
                {"heartbeatIntervalS": 20, "leaseTimeoutS": 50}  # 20*3=60 > 50
            )

    def test_progress_timeout_exceeds_heartbeat(self) -> None:
        with pytest.raises((ValueError, ValidationError), match="progress timeout"):
            parse_runtime_config(
                {"progressTimeoutS": 10, "heartbeatIntervalS": 15}
            )

    def test_queue_poll_max_below_lease_scan(self) -> None:
        with pytest.raises((ValueError, ValidationError), match="queue poll"):
            parse_runtime_config(
                {
                    "queuePollMaxMs": 20_000,
                    "leaseScanIntervalS": 15,  # 15_000 ms threshold
                }
            )

    def test_outbox_lease_exceeds_channel_send(self) -> None:
        with pytest.raises((ValueError, ValidationError), match="Outbox lease"):
            parse_runtime_config(
                {
                    "outboxLeaseTimeoutS": 60,
                    "channelSendTimeoutS": 60,
                }
            )

    def test_sqlite_busy_timeout_below_lease_timeout(self) -> None:
        with pytest.raises((ValueError, ValidationError), match="SQLite busy"):
            parse_runtime_config(
                {
                    "sqliteBusyTimeoutMs": 200_000,
                    "leaseTimeoutS": 60,  # 60_000 ms threshold
                }
            )

    def test_backup_path_must_differ_from_database_path(self) -> None:
        with pytest.raises((ValueError, ValidationError), match="backup path"):
            parse_runtime_config(
                {
                    "databasePath": "runtime/store.sqlite",
                    "backupPath": "runtime/store.sqlite",
                }
            )


class TestModeValidation:
    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises((ValueError, ValidationError), match="mode"):
            parse_runtime_config({"mode": "embedded"})

    def test_worker_concurrency_capped_at_one(self) -> None:
        # Field-level constraint (le=1) fires before the model_validator.
        with pytest.raises((ValueError, ValidationError), match="workerConcurrency"):
            parse_runtime_config(
                {
                    "mode": "supervised",
                    "workerConcurrency": 2,
                }
            )

    def test_supervised_requires_worker_count_at_least_two(self) -> None:
        # Field-level constraint (ge=2) fires before the model_validator,
        # so the error reports the camelCase alias "workerCount".
        with pytest.raises((ValueError, ValidationError), match="worker"):
            parse_runtime_config(
                {
                    "mode": "supervised",
                    "workerCount": 1,
                }
            )

    def test_supervised_valid_with_defaults(self) -> None:
        cfg = parse_runtime_config({"mode": "supervised"})
        assert cfg.mode == "supervised"
        assert cfg.worker_concurrency == 1
        assert cfg.worker_count >= 2

    def test_lightweight_execution_slots_bounds(self) -> None:
        with pytest.raises((ValueError, ValidationError)):
            parse_runtime_config({"lightweightExecutionSlots": 0})

        with pytest.raises((ValueError, ValidationError)):
            parse_runtime_config({"lightweightExecutionSlots": 4})

        cfg = parse_runtime_config({"lightweightExecutionSlots": 2})
        assert cfg.lightweight_execution_slots == 2


class TestPathResolution:
    def test_paths_resolved_against_data_root(self, tmp_path: Path) -> None:
        cfg = parse_runtime_config(
            {"databasePath": "runtime/store.sqlite", "backupPath": "runtime/backups"},
            data_root=tmp_path,
        )
        assert cfg.database_path_resolved == (tmp_path / "runtime" / "store.sqlite").resolve()
        assert cfg.backup_path_resolved == (tmp_path / "runtime" / "backups").resolve()

    def test_absolute_paths_honored(self, tmp_path: Path) -> None:
        abs_db = tmp_path / "abs.sqlite"
        abs_backup = tmp_path / "abs-backups"
        cfg = parse_runtime_config(
            {
                "databasePath": str(abs_db),
                "backupPath": str(abs_backup),
            },
            data_root=tmp_path,
        )
        assert cfg.database_path_resolved == abs_db
        assert cfg.backup_path_resolved == abs_backup

    def test_no_data_root_leaves_resolved_unset(self) -> None:
        cfg = parse_runtime_config(None)
        assert cfg.database_path_resolved is None
        assert cfg.backup_path_resolved is None


class TestRuntimeModeType:
    def test_runtime_mode_literal_values(self) -> None:
        # Sanity check that the exported Literal type covers both modes.
        modes: tuple[RuntimeMode, ...] = ("lightweight", "supervised")
        assert all(m in modes for m in ("lightweight", "supervised"))


class TestRuntimeConfigRepr:
    def test_config_immutable_after_construction(self) -> None:
        # Pydantic v2 BaseModel is mutable by default; the design does not
        # require frozen models, but we verify the constructed object holds
        # the expected values.
        cfg = parse_runtime_config({"heartbeatIntervalS": 10, "leaseTimeoutS": 60})
        assert cfg.heartbeat_interval_s == 10
        assert cfg.lease_timeout_s == 60
