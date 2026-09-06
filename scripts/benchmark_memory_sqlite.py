"""Deterministic scale/concurrency/recovery benchmark for the SQLite memory store.

Runs one end-to-end workload against a fresh workspace (design section 16):
legacy journal migration, runtime appends, a multi-process concurrency phase,
restart + health check on the 100k-transaction database, scoped recall reads,
and a full audit export. The dataset is fully deterministic (fixed seed, index
derived ids), and the report carries throughput, latency percentiles, and peak
RSS alongside Python/SQLite/OS versions.

Durability is never weakened: every write keeps ``synchronous=FULL`` WAL, each
audit segment is fsynced before its atomic replace, and the legacy journal is
read by the real migrator. A user-provided ``--workspace`` is never deleted;
a script-created temp workspace is removed only after a resolved containment
check unless ``--keep`` is given.

Result targets (non-binding, see design section 16): startup+health < 2s,
single-op write p95 < 50ms, scoped candidate SQL p95 < 100ms.

Normative source:
docs/superpowers/specs/2026-08-14-sqlite-memory-storage-design.md
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import platform
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from erza.memory.audit_export import MemoryAuditExporter
from erza.memory.models import (
    SCHEMA_VERSION,
    ActorKind,
    MemoryKind,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryTransaction,
    ScopeKind,
    transaction_checksum,
)
from erza.memory.repository import StructuredMemoryRepository
from erza.memory.sqlite_schema import (
    SQL_ORDER_BY_ID,
    SQL_RECALL_SELECT,
    SQL_RECALL_SUFFIX,
    connect_memory_db,
)

try:
    import resource  # POSIX only; Windows has no resource module
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None

_SEED = 42
_RECORDED_AT = datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)
_BENCH_SCOPE = MemoryScope(kind=ScopeKind.PROJECT, key="project:bench-0000")
_APPEND_SCOPES = (
    MemoryScope(kind=ScopeKind.PROJECT, key="project:bench-other-0"),
    MemoryScope(kind=ScopeKind.USER, key="user:bench-1"),
    MemoryScope(kind=ScopeKind.SHARED, key="shared:*"),
)
_SQL_READ_SAMPLES = 100
_RECALL_SAMPLES = 20
_CONCURRENT_CREATES_PER_WRITER = 40
_CONCURRENT_CONTENDED_PER_WRITER = 10
_REPORT_SCHEMA_VERSION = 1
_PROGRESS_EVERY = 10_000


def _mem_id(index: int) -> str:
    return f"mem_{index:032x}"


def _tx_id(index: int) -> str:
    return f"mtx_{index:032x}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _percentile_ms(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = round(percentile * (len(ordered) - 1))
    return ordered[index] * 1000.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark_memory_sqlite",
        description=(
            "Deterministic scale/concurrency/recovery benchmark for the SQLite "
            "structured memory fact store (design section 16)."
        ),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace to benchmark in (must be fresh); defaults to a temp dir",
    )
    parser.add_argument(
        "--transactions",
        type=int,
        default=100_000,
        help="total transactions generated (default: 100000)",
    )
    parser.add_argument(
        "--active-per-scope",
        dest="active_per_scope",
        type=int,
        default=10_000,
        help="active records created in the benchmark scope (default: 10000)",
    )
    parser.add_argument(
        "--writers", type=int, default=4, help="concurrency phase processes (default: 4)"
    )
    parser.add_argument(
        "--json-output",
        dest="json_output",
        type=Path,
        default=None,
        help="write the full JSON report to this path",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the script-created temp workspace after the run",
    )
    return parser


def workspace_contained(created_root: Path, candidate: Path) -> bool:
    """True when *candidate* resolves to *created_root* or strictly inside it."""
    root = os.path.normcase(os.path.realpath(created_root))
    target = os.path.normcase(os.path.realpath(candidate))
    return target == root or target.startswith(root + os.sep)


def cleanup_workspace(created_root: Path, workspace: Path) -> None:
    """Remove a script-created workspace only after a containment check."""
    if not workspace_contained(created_root, workspace):
        raise RuntimeError(
            f"refusing to remove {workspace}: not contained in created root {created_root}"
        )
    shutil.rmtree(workspace)


def collect_environment() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "python_platform": sys.platform,
        "sqlite": sqlite3.sqlite_version,
        "os": platform.platform(),
        "machine": platform.machine(),
    }


def peak_rss() -> int | None:
    """Peak resident set size in bytes, or None when the platform lacks ``resource``."""
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF)
    scale = 1024 if sys.platform.startswith("linux") else 1
    return usage.ru_maxrss * scale


def build_report(command: str, dataset: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "command": command,
        "environment": collect_environment(),
        "dataset": dataset,
        "results": results,
        "design_targets": {
            "startup_health_seconds_max": 2.0,
            "append_p95_ms_max": 50.0,
            "scoped_recall_sql_p95_ms_max": 100.0,
        },
    }


def _base_record(
    index: int,
    *,
    scope: MemoryScope,
    slot: str,
    statement: str,
    rng: random.Random,
) -> MemoryRecord:
    return MemoryRecord.model_validate(
        {
            "schema_version": SCHEMA_VERSION,
            "id": _mem_id(index),
            "revision": 1,
            "status": MemoryStatus.CANDIDATE.value,
            "kind": MemoryKind.FACT.value,
            "scope": {"kind": scope.kind.value, "key": scope.key},
            "subject": f"benchmark subject {index}",
            "slot": slot,
            "statement": statement,
            "detail": "",
            "tags": ["project.fact"],
            "aliases": [],
            "source_level": "inferred",
            "confidence": 0.9,
            "importance": 4,
            "evidence": [
                {
                    "kind": "manual",
                    "ref": f"benchmark:{index}",
                    "excerpt": "benchmark evidence",
                    "sha256": None,
                    "observed_at": "2026-08-15T00:00:00Z",
                }
            ],
            "content_hash": "c" * 64,
            "derived_from": [],
            "supersedes": [],
            "replacement_id": None,
            "blocked_by": [],
            "valid_from": "2026-08-15T00:00:00Z",
            "created_at": "2026-08-15T00:00:00Z",
            "updated_at": "2026-08-15T00:00:00Z",
            "status_reason": "benchmark",
        }
    )


def _create_transaction(
    record: MemoryRecord, index: int, *, source_batch: str
) -> MemoryTransaction:
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id=_tx_id(index),
        recorded_at=_RECORDED_AT,
        actor=ActorKind.DREAM,
        reason="benchmark",
        source_batch=source_batch,
        expected_revisions={record.id: 0},
        operations=[MemoryOperation(op="put", record=record)],
        checksum_sha256="f" * 64,
    )
    return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})


def _promote_transaction(record: MemoryRecord, index: int) -> MemoryTransaction:
    promoted = record.model_copy(
        update={
            "revision": 2,
            "status": MemoryStatus.ACTIVE,
            "status_reason": "benchmark activation",
        }
    )
    tx = MemoryTransaction(
        schema_version=SCHEMA_VERSION,
        tx_id=_tx_id(index),
        recorded_at=_RECORDED_AT,
        actor=ActorKind.DREAM,
        reason="benchmark activation",
        source_batch="",
        expected_revisions={record.id: 1},
        operations=[MemoryOperation(op="put", record=promoted)],
        checksum_sha256="f" * 64,
    )
    return tx.model_copy(update={"checksum_sha256": transaction_checksum(tx)})


def iter_bench_transactions(active: int, seed: int = _SEED):
    """Yield ``(tx_index, transaction)`` for the active-scope creates + promotes."""
    rng = random.Random(seed)
    for record_index in range(active):
        slot = f"db.bench.{record_index}"
        statement = f"benchmark fact {record_index} about subject {rng.randint(0, 999)}"
        record = _base_record(
            record_index, scope=_BENCH_SCOPE, slot=slot, statement=statement, rng=rng
        )
        yield (
            record_index * 2,
            _create_transaction(record, record_index * 2, source_batch=f"bench:{record_index}"),
        )
        yield record_index * 2 + 1, _promote_transaction(record, record_index * 2 + 1)


def iter_append_transactions(append: int, migrate: int, seed: int = _SEED):
    """Yield ``(tx_index, transaction)`` for creates in secondary scopes."""
    rng = random.Random(seed + 1)
    for offset in range(append):
        index = migrate + offset
        scope = _APPEND_SCOPES[offset % len(_APPEND_SCOPES)]
        slot = f"db.other.{offset}"
        statement = f"other fact {offset} about subject {rng.randint(0, 999)}"
        record = _base_record(index, scope=scope, slot=slot, statement=statement, rng=rng)
        yield index, _create_transaction(record, index, source_batch=f"batch:{offset % 100}")


def _write_journal(journal_path: Path, transactions) -> int:
    bytes_written = 0
    with open(journal_path, "w", encoding="utf-8", newline="\n") as stream:
        for _, transaction in transactions:
            line = _canonical_json(transaction.model_dump(mode="json"))
            stream.write(line)
            stream.write("\n")
            bytes_written += len(line) + 1
    return bytes_written


def _prepare_workspace(workspace: Path) -> None:
    structured = workspace / "memory" / "structured"
    structured.mkdir(parents=True, exist_ok=True)
    tags_path = structured / "tags.json"
    if not tags_path.exists():
        bundled = (
            Path(__file__).resolve().parent.parent
            / "erza"
            / "templates"
            / "memory"
            / "TAGS.json"
        )
        shutil.copy(bundled, tags_path)


def _import_into(repository, transactions, *, label: str) -> list[float]:
    timings: list[float] = []
    for index, transaction in transactions:
        start = time.perf_counter()
        repository.append_transaction(transaction)
        timings.append(time.perf_counter() - start)
        if (index + 1) % _PROGRESS_EVERY == 0:
            print(f"  {label}: {index + 1} transactions", flush=True)
    return timings


def _writer_main(workspace: str, writer_index: int, queue) -> None:
    """Concurrency-phase worker: 40 distinct creates + 10 contended creates."""
    try:
        repository = StructuredMemoryRepository(Path(workspace), lock_timeout_s=30.0)
        created_count = 0
        contended_created = 0
        contended_outcomes: list[tuple[str, bool]] = []
        for i in range(_CONCURRENT_CREATES_PER_WRITER):
            index = 1_000_000 + writer_index * 10_000 + i
            slot = f"db.conc.{writer_index}.{i}"
            statement = f"concurrent fact {writer_index}-{i}"
            record = _base_record(
                index,
                scope=MemoryScope(kind=ScopeKind.PROJECT, key=f"project:bench-conc-{writer_index}"),
                slot=slot,
                statement=statement,
                rng=random.Random(_SEED + writer_index),
            )
            _, created = repository.append_create_if_absent(
                _create_transaction(record, index, source_batch=f"conc:{writer_index}:{i}")
            )
            created_count += 1 if created else 0
        contended = _base_record(
            2_000_000,
            scope=MemoryScope(kind=ScopeKind.PROJECT, key="project:bench-conc-shared"),
            slot="db.conc.shared",
            statement="contended fact",
            rng=random.Random(_SEED),
        )
        for i in range(_CONCURRENT_CONTENDED_PER_WRITER):
            _, created = repository.append_create_if_absent(
                _create_transaction(
                    contended, 2_000_001 + writer_index * 100 + i, source_batch="conc:shared"
                )
            )
            contended_created += 1 if created else 0
            contended_outcomes.append((contended.id, created))
        queue.put(
            {
                "writer": writer_index,
                "created": created_count,
                "contended_created": contended_created,
                "contended_outcomes": contended_outcomes,
                "exit": 0,
            }
        )
    except Exception as exc:  # pragma: no cover - failure path of a worker
        queue.put({"writer": writer_index, "error": str(exc), "exit": 1})


def run_benchmark(
    workspace: Path, transactions: int, active_per_scope: int, writers: int
) -> dict[str, Any]:
    migrate = min(2 * active_per_scope, transactions)
    append = transactions - migrate
    print(
        f"benchmark transactions={transactions} migrate={migrate} append={append} "
        f"active_per_scope={active_per_scope} writers={writers}",
        flush=True,
    )
    if migrate == 0:
        print(
            "  warning: no migrated transactions (transactions < 2 * active_per_scope)", flush=True
        )

    journal = workspace / "memory" / "structured" / "journal.jsonl"
    journal_bytes = _write_journal(journal, iter_bench_transactions(active_per_scope))

    migration_start = time.perf_counter()
    repository = StructuredMemoryRepository(workspace, lock_timeout_s=30.0)
    migration_seconds = time.perf_counter() - migration_start
    if repository.health.state != "healthy":
        raise RuntimeError(f"repository degraded after migration: {repository.health.error_code}")
    migrated_count = repository.storage_stats().transaction_count
    if migrated_count != migrate:
        raise RuntimeError(f"migration imported {migrated_count} transactions, expected {migrate}")

    insert_start = time.perf_counter()
    append_times = _import_into(
        repository, iter_append_transactions(append, migrate), label="append"
    )
    insert_seconds = time.perf_counter() - insert_start

    concurrency_start = time.perf_counter()
    context = multiprocessing.get_context("spawn")
    results_queue = context.Queue()
    processes = [
        context.Process(target=_writer_main, args=(str(workspace), writer, results_queue))
        for writer in range(writers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=900)
    for process in processes:
        if process.exitcode != 0:
            raise RuntimeError(f"concurrency writer exited with code {process.exitcode}")
    worker_reports = [results_queue.get(timeout=60) for _ in processes]
    for report in worker_reports:
        if report.get("exit") != 0:
            raise RuntimeError(f"concurrency writer failed: {report.get('error')}")
    concurrency_elapsed = time.perf_counter() - concurrency_start
    created_total = sum(report["created"] for report in worker_reports)
    contended_created_total = sum(report["contended_created"] for report in worker_reports)
    contended_ids = {
        memory_id for report in worker_reports for memory_id, _ in report["contended_outcomes"]
    }

    expected_tx = migrate + append + created_total + contended_created_total
    actual_tx = repository.storage_stats().transaction_count
    if actual_tx != expected_tx:
        raise RuntimeError(
            f"transaction count {actual_tx} != expected {expected_tx} after concurrency"
        )
    if created_total != writers * _CONCURRENT_CREATES_PER_WRITER:
        raise RuntimeError(
            f"lost {writers * _CONCURRENT_CREATES_PER_WRITER - created_total} concurrent creates"
        )
    if contended_created_total != 1:
        raise RuntimeError(f"contended create duplicated: {contended_created_total} creations")
    contended_id = next(iter(contended_ids)) if contended_ids else None
    if contended_id is None or repository.get(contended_id) is None:
        raise RuntimeError("contended record missing after concurrency phase")

    startup_start = time.perf_counter()
    reopened = StructuredMemoryRepository(workspace, lock_timeout_s=30.0)
    startup_seconds = time.perf_counter() - startup_start
    health_start = time.perf_counter()
    reopened.rebuild()
    health_seconds = time.perf_counter() - health_start

    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    sql_times: list[float] = []
    with connect_memory_db(repository.database_path, lock_timeout_s=30.0) as connection:
        params = [_BENCH_SCOPE.kind.value, _BENCH_SCOPE.key]
        recall_sql = (
            SQL_RECALL_SELECT
            + "(scope_kind = ? AND scope_key = ?)"
            + SQL_RECALL_SUFFIX
            + SQL_ORDER_BY_ID
        )
        params.append(now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
        for _ in range(_SQL_READ_SAMPLES):
            start = time.perf_counter()
            rows = connection.execute(recall_sql, params).fetchall()
            sql_times.append(time.perf_counter() - start)
            if len(rows) != active_per_scope:
                raise RuntimeError(
                    f"recall SQL returned {len(rows)} rows, expected {active_per_scope}"
                )
    recall_full_times: list[float] = []
    for _ in range(_RECALL_SAMPLES):
        start = time.perf_counter()
        candidates = repository.recall_candidates(
            allowed_scopes=(_BENCH_SCOPE,), requested_kinds=(), now=now
        )
        recall_full_times.append(time.perf_counter() - start)
        if len(candidates) != active_per_scope:
            raise RuntimeError(
                f"recall returned {len(candidates)} records, expected {active_per_scope}"
            )

    exporter = MemoryAuditExporter(repository)
    audit_start = time.perf_counter()
    audit_result = exporter.export_pending()
    audit_export_seconds = time.perf_counter() - audit_start
    if repository.storage_stats().audit_lag != 0:
        raise RuntimeError("audit lag not zero after export")

    with connect_memory_db(repository.database_path, lock_timeout_s=30.0) as connection:
        integrity_ok = connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        foreign_keys_ok = not connection.execute("PRAGMA foreign_key_check").fetchall()
    database_bytes = repository.database_path.stat().st_size

    peak = peak_rss()
    results: dict[str, Any] = {
        "database_bytes": database_bytes,
        "migration_seconds": migration_seconds,
        "migration_throughput_tx_per_s": migrate / migration_seconds if migration_seconds else 0.0,
        "insert_seconds": insert_seconds,
        "insert_throughput_tx_per_s": append / insert_seconds if insert_seconds and append else 0.0,
        "startup_seconds": startup_seconds,
        "health_seconds": health_seconds,
        "append_p50_ms": _percentile_ms(append_times, 0.5) if append_times else 0.0,
        "append_p95_ms": _percentile_ms(append_times, 0.95) if append_times else 0.0,
        "recall_sql_p50_ms": _percentile_ms(sql_times, 0.5),
        "recall_sql_p95_ms": _percentile_ms(sql_times, 0.95),
        "recall_full_p50_ms": _percentile_ms(recall_full_times, 0.5),
        "recall_full_p95_ms": _percentile_ms(recall_full_times, 0.95),
        "audit_export_seconds": audit_export_seconds,
        "audit_export_throughput_tx_per_s": audit_result.exported_rows / audit_export_seconds,
        "audit_exported_rows": audit_result.exported_rows,
        "concurrency_writers": writers,
        "concurrency_appended": actual_tx - (migrate + append),
        "concurrency_lost": 0,
        "concurrency_elapsed_s": concurrency_elapsed,
        "peak_rss": peak if peak is not None else "n/a (platform unsupported)",
        "integrity_ok": integrity_ok,
        "foreign_keys_ok": foreign_keys_ok,
        "final_transaction_count": actual_tx,
    }
    print(
        f"  audit export: {audit_result.exported_rows} rows in {audit_export_seconds:.2f}s "
        f"({results['audit_export_throughput_tx_per_s']:.0f} tx/s)",
        flush=True,
    )
    return {"journal_bytes": journal_bytes, **results}


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(args_list)
    if args.active_per_scope < 1:
        print("--active-per-scope must be at least 1", file=sys.stderr)
        return 1
    if args.transactions < 2 * args.active_per_scope:
        print(
            "--transactions must be at least 2 * --active-per-scope so the benchmark scope "
            "has a migrate + append split",
            file=sys.stderr,
        )
        return 1
    if args.writers < 1:
        print("--writers must be at least 1", file=sys.stderr)
        return 1

    created_root: Path | None = None
    if args.workspace is not None:
        workspace = Path(args.workspace)
        if (workspace / "memory" / "structured" / "memory.db").exists():
            print("benchmark requires a fresh workspace (no existing memory.db)", file=sys.stderr)
            return 1
        _prepare_workspace(workspace)
    else:
        created_root = Path(tempfile.mkdtemp(prefix="memory-benchmark-"))
        workspace = created_root
        _prepare_workspace(workspace)

    try:
        dataset = {
            "transactions": args.transactions,
            "migrated": min(2 * args.active_per_scope, args.transactions),
            "appended": args.transactions - min(2 * args.active_per_scope, args.transactions),
            "active_per_scope": args.active_per_scope,
            "writers": args.writers,
            "seed": _SEED,
        }
        results = run_benchmark(workspace, args.transactions, args.active_per_scope, args.writers)
        dataset["journal_bytes"] = results.pop("journal_bytes")
        report = build_report(" ".join(args_list), dataset, results)
        if args.json_output is not None:
            args.json_output.write_text(
                json.dumps(report, sort_keys=True, indent=2), encoding="utf-8"
            )
            print(f"report written to {args.json_output}", flush=True)
        print(json.dumps(report, sort_keys=True, indent=2), flush=True)
        return 0
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        if args.workspace is not None:
            print(f"workspace preserved at {workspace}", file=sys.stderr)
        return 1
    finally:
        if created_root is not None and not args.keep:
            cleanup_workspace(created_root, workspace)


if __name__ == "__main__":
    raise SystemExit(main())
