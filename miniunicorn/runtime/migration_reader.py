"""Legacy checkpoint migration reader (design §31.2, WP8).

At first durable startup, while intake is stopped, the migration reader
scans legacy session files for ``pending_user_turn`` and
``runtime_checkpoint`` metadata. Incomplete sessions are converted into
durable tasks so that accepted work survives the cutover.

Design references:

- §31.2: legacy checkpoint reader rules (never fabricate tool failures,
  never replay pending tool calls, preserve metadata read-only).
- §31.1: no per-task dual write; one task never writes both durable
  checkpoints and legacy session checkpoint metadata.
- §31.3: upgrade procedure step 7 (run legacy checkpoint scan).

The scanner is read-only: it never modifies session files. Conversion
decisions are recorded as a :class:`LegacyScanResult` for operator
review. Actual task creation is deferred to the caller (the Host or
cutover tooling) so the scan can be dry-run.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Scan result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyCheckpointInfo:
    """Information about a single legacy session checkpoint."""

    session_key: str
    session_path: str
    revision: int
    has_pending_user_turn: bool
    has_runtime_checkpoint: bool
    pending_user_turn_text: str = ""
    checkpoint_phase: str = ""
    message_count: int = 0
    needs_conversion: bool = False
    conversion_reason: str = ""


@dataclass(frozen=True)
class LegacyScanResult:
    """Result of scanning legacy session files for incomplete work."""

    scanned: int = 0
    needs_conversion: int = 0
    skipped_no_metadata: int = 0
    skipped_terminal: int = 0
    sessions: list[LegacyCheckpointInfo] = field(default_factory=list)
    scan_time_ms: int = 0
    migration_complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "needs_conversion": self.needs_conversion,
            "skipped_no_metadata": self.skipped_no_metadata,
            "skipped_terminal": self.skipped_terminal,
            "sessions": [s.__dict__ for s in self.sessions],
            "scan_time_ms": self.scan_time_ms,
            "migration_complete": self.migration_complete,
        }


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class LegacyCheckpointScanner:
    """Scans legacy session files for incomplete work (design §31.2, WP8).

    The scanner is read-only. It never modifies session files or creates
    tasks. The caller decides whether to convert flagged sessions into
    durable tasks based on the scan result.

    Usage::

        scanner = LegacyCheckpointScanner(sessions_dir)
        result = scanner.scan()
        if result.needs_conversion > 0:
            # operator review or automated conversion
            for info in result.sessions:
                if info.needs_conversion:
                    ...
    """

    def __init__(self, sessions_dir: Path | str) -> None:
        self._sessions_dir = Path(sessions_dir)

    def scan(self) -> LegacyScanResult:
        """Scan all legacy session files for incomplete work.

        Returns a :class:`LegacyScanResult`. The scan is safe to run
        multiple times; it never modifies files.
        """
        start = time.monotonic()
        sessions: list[LegacyCheckpointInfo] = []
        scanned = 0
        needs_conversion = 0
        skipped_no_metadata = 0
        skipped_terminal = 0

        if not self._sessions_dir.exists():
            return LegacyScanResult(
                scanned=0,
                migration_complete=True,
                scan_time_ms=int((time.monotonic() - start) * 1000),
            )

        for path in sorted(self._sessions_dir.glob("*.jsonl")):
            scanned += 1
            info = self._scan_file(path)
            if info is None:
                skipped_no_metadata += 1
                continue
            if info.needs_conversion:
                needs_conversion += 1
            elif not info.has_pending_user_turn and not info.has_runtime_checkpoint:
                skipped_terminal += 1
            sessions.append(info)

        return LegacyScanResult(
            scanned=scanned,
            needs_conversion=needs_conversion,
            skipped_no_metadata=skipped_no_metadata,
            skipped_terminal=skipped_terminal,
            sessions=sessions,
            migration_complete=needs_conversion == 0,
            scan_time_ms=int((time.monotonic() - start) * 1000),
        )

    def _scan_file(self, path: Path) -> LegacyCheckpointInfo | None:
        """Scan a single session file for incomplete work.

        Returns ``None`` if the file has no metadata line (not a valid
        session file).
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                first_line = f.readline()
                if not first_line:
                    return None
                metadata = json.loads(first_line)
                if metadata.get("_type") != "metadata":
                    return None

                session_key = metadata.get("key", path.stem)
                revision = metadata.get("revision", 0)

                # Count messages and look for pending work markers.
                message_count = 0
                has_pending_user_turn = False
                pending_user_turn_text = ""
                has_runtime_checkpoint = False
                checkpoint_phase = ""

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        msg = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    # Check for pending_user_turn marker (design §31.2).
                    if msg.get("_type") == "pending_user_turn":
                        has_pending_user_turn = True
                        pending_user_turn_text = msg.get("text", "")

                    # Check for runtime_checkpoint marker (design §31.2).
                    if msg.get("_type") == "runtime_checkpoint":
                        has_runtime_checkpoint = True
                        checkpoint_phase = msg.get("phase", "")

                needs_conversion = has_pending_user_turn or has_runtime_checkpoint
                reason = ""
                if has_pending_user_turn:
                    reason = "pending_user_turn: unprocessed inbound message"
                elif has_runtime_checkpoint:
                    reason = f"runtime_checkpoint: phase={checkpoint_phase}"

                return LegacyCheckpointInfo(
                    session_key=session_key,
                    session_path=str(path),
                    revision=revision,
                    has_pending_user_turn=has_pending_user_turn,
                    has_runtime_checkpoint=has_runtime_checkpoint,
                    pending_user_turn_text=pending_user_turn_text,
                    checkpoint_phase=checkpoint_phase,
                    message_count=message_count,
                    needs_conversion=needs_conversion,
                    conversion_reason=reason,
                )
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("LegacyCheckpointScanner: failed to read {}: {}", path, exc)
            return None


def scan_legacy_checkpoints(sessions_dir: Path | str) -> LegacyScanResult:
    """Convenience function: scan legacy session files (design §31.2, WP8)."""
    return LegacyCheckpointScanner(sessions_dir).scan()


__all__ = [
    "LegacyCheckpointInfo",
    "LegacyCheckpointScanner",
    "LegacyScanResult",
    "scan_legacy_checkpoints",
]
