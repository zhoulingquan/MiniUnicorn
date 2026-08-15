"""Reproducible SQLite memory storage benchmark (design section 16).

Measures the single-path SQLite structured-memory stack at the design's
target scale (10,000 current records per scope / 100,000 transactions) and
prints a structured JSON report. Structural invariants (bounded SQL per
append, index-backed recall, bounded audit range reads, no in-process replay
index) are guarded by pytest tests; this script reports wall-clock numbers
for human comparison only.

Usage:
    python scripts/benchmark_memory_sqlite.py \
        --transactions 100000 --active-per-scope 10000 --writers 4 \
        --json-output .benchmark-memory-sqlite.json

The default workspace is a fresh temporary directory that is removed after
the run unless ``--keep`` is given; cleanup only ever targets directories
created by this script (resolved containment verification).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from miniunicorn.agent.memory_audit_export import MemoryAuditExporter
from miniunicorn.agent.memory_jsonl_import import migrate_legacy_journal
from miniunicorn.agent.memory_models import (
    ActorKind,
    EvidenceKind,
    EvidenceRef,
    MemoryKind,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryTransaction,
    RecallQuery,
    ScopeKind,
    SourceLevel,
    new_memory_id,
    new_transaction_id,
    transaction_checksum,
)
from miniunicorn.agent.memory_recall import StructuredMemoryRecall
from miniunicorn.agent.memory_repository import StructuredMemoryRepository

_SCHEMA_VERSION = "benchmark/memory-sqlite/v1"
_PEAK_RSS_UNAVAILABLE = "n/a (platform unsupported)"

_SCOPES = (
    "project:bench-alpha",
    "project:bench-beta",
    "project:bench-gamma",
    "project:bench-delta",
    "project:bench-epsilon",
    "user:bench-1",
    "user:bench-2",
    "shared:*",
)
_TAG = "project.fact"
_TEMP_PREFIX = "benchmark-memory-"


# ---------------------------------------------------------------------------
# Test-facing interface (stable for structural tests)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None, help="existing workspace; default: fresh temp dir")
    parser.add_argument("--transactions", type=int, default=100_000, help="total append transactions")
    parser.add_argument("--active-per-scope", type=int, default=10_000, help="seeded active records per recall scope")
    parser.add_argument("--writers", type=int, default=4, help="concurrent writer processes")
    parser.add_argument("--json-output", type=Path, default=None, help="report JSON destination")
    parser.add_argument("--keep", action="store_true", help="keep temp workspace on exit")
    return parser


def workspace_contained(workspace: Path, target: Path) -> bool:
    """True iff *target* resolves to *workspace* itself or a path inside it."""
    root = workspace.resolve()
    candidate = target.resolve()
    return candidate == root or root in candidate.parents


def cleanup_workspace(workspace: Path, target: Path) -> None:
    """Remove *target* after verifying it stays inside *workspace*."""
    if not workspace_contained(workspace, target):
        raise RuntimeError(f"refusing to remove {target}: outside workspace {workspace}")
    if target.exists():
        shutil.rmtree(target)


def collect_environment() -> dict[str, str]:
    import platform as _platform

    return {
        "python": _platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "os": _platform.platform(),
    }


def build_report(command: str, dataset: dict, results: dict) -> dict:
    """Assemble the versioned benchmark report (unit-tested shape)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "environment": collect_environment(),
        "dataset": dataset,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Deterministic data generation
# ---------------------------------------------------------------------------


def _evidence(ref: str, excerpt: str) -> EvidenceRef:
    return EvidenceRef(kind=EvidenceKind.MANUAL, ref=ref, excerpt=excerpt)


def _record(
    *,
    memory_id: str,
    revision: int,
    status: MemoryStatus,
    scope: MemoryScope,
    subject: str,
    slot: str,
    statement: str,
    importance: int,
    base: datetime,
    evidence: EvidenceRef,
) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        revision=revision,
        status=status,
        kind=MemoryKind.FACT,
        scope=scope,
        subject=subject,
        slot=slot,
        statement=statement,
        tags=(_TAG,),
        source_level=SourceLevel.INFERRED,
        confidence=0.9,
        importance=importance,
        evidence=(evidence,),
        content_hash="a" * 64,
        valid_from=base,
        created_at=base,
        updated_at=base + timedelta(minutes=1),
        status_reason="benchmark",
    )


def _transaction(
    *records: MemoryRecord,
    actor: ActorKind,
    reason: str,
    source_batch: str,
    base: datetime,
) -> MemoryTransaction:
    expected = {record.id: record.revision - 1 for record in records}
    tx = MemoryTransaction(
        tx_id=new_transaction_id(),
        recorded_at=base + timedelta(minutes=1),
        actor=actor,
        reason=reason,
        source_batch=source_batch,
        expected_revisions=expected,
        operations=[MemoryOperation(op="put", record=record) for record in records],
        checksum_sha256="f" * 64,
    )
    return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})


def _canonical_line(transaction: MemoryTransaction) -> str:
    return json.dumps(
        transaction.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _scope_from_label(label: str) -> MemoryScope:
    kind, key = label.split(":", 1)
    return MemoryScope(kind=ScopeKind(kind), key=label)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[index]


def db_bytes(workspace: Path) -> int:
    structured = workspace / "memory" / "structured"
    total = 0
    for name in ("memory.db", "memory.db-wal", "memory.db-shm"):
        path = structured / name
        total += path.stat().st_size if path.exists() else 0
    return total


# ---------------------------------------------------------------------------
# Writer workers (spawn-safe on Windows)
# ---------------------------------------------------------------------------


def _writer_main(rank: int, workspace: str, per_writer: int, seed: int) -> tuple[int, int, list[float]]:
    workspace_path = Path(workspace)
    scope = MemoryScope(kind=ScopeKind.PROJECT, key=f"project:bench-concurrent-{rank}")
    repository = StructuredMemoryRepository(workspace_path, lock_timeout_s=30.0)
    rng = random.Random(seed)
    base = datetime.now(timezone.utc)
    latencies: list[float] = []
    created: list[MemoryRecord] = []
    active: list[MemoryRecord] = []
    failures = 0
    for index in range(per_writer):
        started = time.perf_counter()
        try:
            roll = rng.random()
            if roll < 0.75 or not created:
                record = _record(
                    memory_id=new_memory_id(),
                    revision=1,
                    status=MemoryStatus.CANDIDATE,
                    scope=scope,
                    subject=f"Benchmark Subject {rank}",
                    slot=f"bench.s{rank}.{index}",
                    statement=f"Deterministic benchmark fact {rank}.{index}.",
                    importance=1 + rng.randrange(5),
                    base=base,
                    evidence=_evidence(f"bench:{rank}:{index}", "benchmark evidence"),
                )
                created.append(record)
            elif roll < 0.95:
                previous = created.pop(rng.randrange(len(created)))
                record = previous.model_copy(
                    update={
                        "revision": previous.revision + 1,
                        "status": MemoryStatus.ACTIVE,
                        "status_reason": "benchmark promote",
                        "updated_at": base + timedelta(minutes=2),
                    }
                )
                active.append(record)
            else:
                update_index = rng.randrange(len(active))
                previous = active[update_index]
                record = previous.model_copy(
                    update={
                        "revision": previous.revision + 1,
                        "evidence": (previous.evidence[0], _evidence("bench:update", "additional evidence")),
                        "updated_at": base + timedelta(minutes=2),
                    }
                )
                active[update_index] = record
            tx = _transaction(record, actor=ActorKind.DREAM, reason="benchmark", source_batch=f"writer:{rank}", base=base)
            repository.append_transaction(tx)
            latencies.append(time.perf_counter() - started)
        except Exception:
            failures += 1
    return len(latencies), failures, latencies


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def _phase_insert(repository: StructuredMemoryRepository, count: int, seed: int) -> dict:
    """Append *count* ACTIVE records via the governed single-writer path."""
    rng = random.Random(seed)
    base = datetime.now(timezone.utc)
    latencies: list[float] = []
    started = time.perf_counter()
    for index in range(count):
        scope = _SCOPES[index % len(_SCOPES)]
        step = time.perf_counter()
        record = _record(
            memory_id=new_memory_id(),
            revision=1,
            status=MemoryStatus.ACTIVE,
            scope=_scope_from_label(scope),
            subject=f"Seeded Subject {index}",
            slot=f"seed.{index}",
            statement=f"Seeded benchmark fact {index}.",
            importance=1 + rng.randrange(5),
            base=base,
            evidence=_evidence(f"seed:{index}", "seed evidence"),
        )
        repository.append_transaction(
            _transaction(record, actor=ActorKind.SYSTEM, reason="seed", source_batch="seed", base=base)
        )
        latencies.append(time.perf_counter() - step)
    elapsed = time.perf_counter() - started
    latencies.sort()
    return {
        "elapsed_s": round(elapsed, 3),
        "throughput_tx_per_s": round(count / elapsed, 1) if elapsed > 0 else 0.0,
        "p50_ms": round(_percentile(latencies, 0.50) * 1000.0, 2),
        "p95_ms": round(_percentile(latencies, 0.95) * 1000.0, 2),
        "samples": len(latencies),
    }


def _phase_concurrency(workspace: Path, transactions: int, writers: int) -> dict:
    if writers <= 0 or transactions <= 0:
        return {"elapsed_s": 0.0, "appended": 0, "lost": 0}
    per_writer = max(1, transactions // writers)
    context = multiprocessing.get_context("spawn")
    started = time.perf_counter()
    with context.Pool(processes=writers) as pool:
        results = pool.starmap(
            _writer_main,
            [(rank, str(workspace), per_writer, 0xB0_0F_00_0D + rank) for rank in range(writers)],
        )
    elapsed = time.perf_counter() - started
    appended = sum(item[0] for item in results)
    lost = sum(item[1] for item in results)
    return {"elapsed_s": round(elapsed, 3), "appended": appended, "lost": lost}


def _phase_recall(repository: StructuredMemoryRepository, iterations: int = 100) -> dict:
    scopes = [_scope_from_label(label) for label in _SCOPES]
    now = datetime.now(timezone.utc)
    recaller = StructuredMemoryRecall(repository, repository.tag_catalog)
    sql_latencies: list[float] = []
    full_latencies: list[float] = []
    for index in range(iterations):
        scope = scopes[index % len(scopes)]
        query = RecallQuery(query_text="Seeded benchmark fact", allowed_scopes=(scope,), now=now)
        step = time.perf_counter()
        repository.recall_candidates(allowed_scopes=(scope,), requested_kinds=(), now=now)
        sql_latencies.append(time.perf_counter() - step)
        step = time.perf_counter()
        recaller.recall(query)
        full_latencies.append(time.perf_counter() - step)
    sql_latencies.sort()
    full_latencies.sort()
    return {
        "iterations": iterations,
        "sql_p50_ms": round(_percentile(sql_latencies, 0.50) * 1000.0, 2),
        "sql_p95_ms": round(_percentile(sql_latencies, 0.95) * 1000.0, 2),
        "full_p50_ms": round(_percentile(full_latencies, 0.50) * 1000.0, 2),
        "full_p95_ms": round(_percentile(full_latencies, 0.95) * 1000.0, 2),
    }


def _phase_audit_export(repository: StructuredMemoryRepository) -> dict:
    exporter = MemoryAuditExporter(repository)
    before = repository.storage_stats()
    started = time.perf_counter()
    result = exporter.export_pending()
    elapsed = time.perf_counter() - started
    rows = result.exported_rows
    return {
        "exported_rows": rows,
        "elapsed_s": round(elapsed, 3),
        "throughput_tx_per_s": round(rows / elapsed, 1) if elapsed > 0 else 0.0,
        "sealed_segments": result.sealed_segments,
        "audit_lag_before": before.audit_lag,
        "audit_lag_after": result.lag,
    }


def _phase_migration(workspace: Path, count: int) -> dict:
    """Import *count* legacy journal entries into a fresh sub-workspace."""
    if count <= 0:
        return {"count": 0, "elapsed_s": 0.0, "throughput_tx_per_s": 0.0}
    structured = workspace / "memory" / "structured"
    structured.mkdir(parents=True)
    bundled = Path(__file__).resolve().parents[1] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    rng = random.Random(0xC0FFEE_11)
    base = datetime.now(timezone.utc)
    journal = structured / "journal.jsonl"
    with journal.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(count):
            record = _record(
                memory_id=new_memory_id(),
                revision=1,
                status=MemoryStatus.CANDIDATE,
                scope=MemoryScope(kind=ScopeKind.PROJECT, key="project:legacy-bench"),
                subject="Legacy Subject",
                slot=f"legacy.{index}",
                statement=f"Legacy benchmark fact {index}.",
                importance=1 + rng.randrange(5),
                base=base,
                evidence=_evidence(f"legacy:{index}", "legacy evidence"),
            )
            tx = _transaction(record, actor=ActorKind.DREAM, reason="legacy", source_batch="legacy", base=base)
            stream.write(_canonical_line(tx))
            stream.write("\n")
    started = time.perf_counter()
    result = migrate_legacy_journal(workspace, 30.0)
    elapsed = time.perf_counter() - started
    return {
        "count": result.transaction_count,
        "elapsed_s": round(elapsed, 3),
        "throughput_tx_per_s": round(result.transaction_count / elapsed, 1) if elapsed > 0 else 0.0,
    }


def _phase_integrity(workspace: Path) -> dict[str, bool]:
    database = workspace / "memory" / "structured" / "memory.db"
    from miniunicorn.agent.memory_sqlite_schema import connect_memory_db

    with connect_memory_db(database, lock_timeout_s=30.0) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    return {"integrity_ok": integrity == "ok", "foreign_keys_ok": len(foreign_keys) == 0}


def _peak_rss_mb() -> str:
    try:
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except (ImportError, AttributeError):
        return _PEAK_RSS_UNAVAILABLE


def _prepare_workspace(args: argparse.Namespace) -> tuple[Path, bool]:
    workspace = args.workspace
    created = False
    if workspace is None:
        workspace = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
        created = True
    workspace = workspace.resolve()
    structured = workspace / "memory" / "structured"
    structured.mkdir(parents=True, exist_ok=True)
    bundled = Path(__file__).resolve().parents[1] / "miniunicorn" / "templates" / "memory" / "TAGS.json"
    shutil.copy(bundled, structured / "tags.json")
    return workspace, created


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.transactions <= 0 or args.writers <= 0 or args.active_per_scope <= 0:
        print("--transactions/--writers/--active-per-scope must be positive", file=sys.stderr)
        return 2
    migrated = max(1, args.transactions // 5)
    appended = args.transactions - migrated
    workspace, created = _prepare_workspace(args)
    try:
        repository = StructuredMemoryRepository(workspace, lock_timeout_s=30.0)

        insert = _phase_insert(repository, appended, seed=0xF00D_BEE5)
        recall = _phase_recall(repository)
        concurrency = _phase_concurrency(workspace, max(1, appended // 4), args.writers)

        restart_started = time.perf_counter()
        restarted = StructuredMemoryRepository(workspace, lock_timeout_s=30.0)
        startup_s = time.perf_counter() - restart_started
        health_started = time.perf_counter()
        restarted.health
        health_s = time.perf_counter() - health_started

        audit = _phase_audit_export(restarted)

        migration_workspace = workspace / "bench-legacy-migration"
        migration = _phase_migration(migration_workspace, migrated)

        integrity = _phase_integrity(workspace)

        results = {
            "database_bytes": db_bytes(workspace),
            "migration_seconds": migration["elapsed_s"],
            "migration_throughput_tx_per_s": migration["throughput_tx_per_s"],
            "insert_seconds": insert["elapsed_s"],
            "insert_throughput_tx_per_s": insert["throughput_tx_per_s"],
            "startup_seconds": round(startup_s, 3),
            "health_seconds": round(health_s, 3),
            "append_p50_ms": insert["p50_ms"],
            "append_p95_ms": insert["p95_ms"],
            "recall_sql_p50_ms": recall["sql_p50_ms"],
            "recall_sql_p95_ms": recall["sql_p95_ms"],
            "recall_full_p50_ms": recall["full_p50_ms"],
            "recall_full_p95_ms": recall["full_p95_ms"],
            "audit_export_seconds": audit["elapsed_s"],
            "audit_export_throughput_tx_per_s": audit["throughput_tx_per_s"],
            "concurrency_writers": args.writers,
            "concurrency_appended": concurrency["appended"],
            "concurrency_lost": concurrency["lost"],
            "concurrency_elapsed_s": concurrency["elapsed_s"],
            "peak_rss": _peak_rss_mb(),
            "integrity_ok": integrity["integrity_ok"],
            "foreign_keys_ok": integrity["foreign_keys_ok"],
        }
        dataset = {
            "transactions": args.transactions,
            "migrated": migrated,
            "appended": appended,
            "active_per_scope": args.active_per_scope,
            "writers": args.writers,
        }
        report = build_report(" ".join(sys.argv[1:] if argv is None else argv), dataset, results)
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if args.json_output is not None:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(payload, encoding="utf-8")
        print(payload)
        return 0
    finally:
        if created and not args.keep:
            cleanup_workspace(workspace, workspace)


if __name__ == "__main__":
    raise SystemExit(main())
