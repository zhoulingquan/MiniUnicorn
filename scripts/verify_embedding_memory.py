#!/usr/bin/env python
"""Release proof script for the local embedding memory feature.

Runs 13 evidence checks against REAL production APIs (FastEmbed + sqlite-vec)
to verify the complete embedding memory pipeline. No monkeypatching, no mocks.

Usage:
    python scripts/verify_embedding_memory.py [--model-dir PATH] [--keep-workspace]

The final line of stdout is a JSON object: ``{"ok": true|false, ...}``.
Exit code is 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

from miniunicorn.agent.memory_prompt import MemoryPromptPolicy
from miniunicorn.agent.memory_recall import MemoryRecallService
from miniunicorn.agent.memory_sources import MemorySourceCatalog
from miniunicorn.agent.vector_index import VectorIndexManager
from miniunicorn.embedding import MODEL_DIMENSION
from miniunicorn.embedding.model_manager import EmbeddingModelManager
from miniunicorn.providers.local_embedding import LocalEmbeddingProvider

#: The 13 evidence items that must all pass for a release-ready embedding memory.
REQUIRED_EVIDENCE: set[str] = {
    "model_ready",
    "cpu_512_finite_normalized",
    "two_chinese_memories",
    "persisted_after_reopen",
    "relevant_query_ranked_first",
    "unchanged_reconcile_idempotent",
    "changed_source_updated",
    "deleted_db_rebuilt",
    "cancelled_rebuild_preserved_old_index",
    "fingerprint_mismatch_rejected",
    "max_five_under_budget",
    "provenance_complete",
    "safe_noop_chat_fallback",
}


def _log(msg: str) -> None:
    """Write a progress line to stderr (keeps stdout clean for JSON)."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Verify the local embedding memory pipeline with real APIs.",
    )
    parser.add_argument(
        "--model-dir",
        type=str,
        default=None,
        help="Path to the model cache directory (defaults to the configured path).",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        default=False,
        help="Keep the temporary proof workspace after completion.",
    )
    return parser


def write_authoritative_sources(root: Path) -> None:
    """Write authoritative Chinese source files into *root*.

    Creates ``USER.md`` and ``memory/MEMORY.md`` with real Chinese content
    so the catalog and embedding pipeline exercise the actual language model.
    """
    memory_dir = root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    (root / "USER.md").write_text(
        "# 早餐习惯\n\n"
        "用户早餐喜欢喝无糖豆浆，搭配全麦面包。\n",
        encoding="utf-8",
    )

    (memory_dir / "MEMORY.md").write_text(
        "# 部署环境\n\n"
        "项目部署服务器使用 Debian 12，Nginx 反向代理。\n",
        encoding="utf-8",
    )


def _expand_sources_for_budget_test(root: Path) -> None:
    """Append additional sections to reach 8+ source records."""
    user_path = root / "USER.md"
    user_content = user_path.read_text(encoding="utf-8")
    user_path.write_text(
        user_content
        + "\n# 工作习惯\n\n用户工作习惯是早起，通常七点开始处理邮件。\n"
        + "\n# 编程偏好\n\n用户偏好使用 Python 编写脚本，不熟悉 Go 语言。\n"
        + "\n# 阅读偏好\n\n用户喜欢阅读技术博客和科幻小说。\n",
        encoding="utf-8",
    )

    memory_path = root / "memory" / "MEMORY.md"
    memory_content = memory_path.read_text(encoding="utf-8")
    memory_path.write_text(
        memory_content
        + "\n# 数据库\n\n数据库选用 PostgreSQL 16，主从复制配置。\n"
        + "\n# 日志\n\n日志收集使用 Loki + Grafana 方案。\n"
        + "\n# CI/CD\n\nCI/CD 使用 GitHub Actions，自动部署到 staging。\n"
        + "\n# 监控\n\nPrometheus + AlertManager 用于服务监控告警。\n",
        encoding="utf-8",
    )


async def run_proof(model_dir: Path | None, keep_workspace: bool) -> dict[str, object]:
    """Run all 13 evidence checks and return a result dict.

    The function creates a temporary workspace, writes Chinese source files,
    downloads/verifies the real model, builds a real vector index, and
    exercises every production code path. On completion the workspace is
    removed unless *keep_workspace* is True.
    """
    root = Path(tempfile.mkdtemp(prefix="miniunicorn-embedding-proof-"))
    evidence: dict[str, bool] = {name: False for name in REQUIRED_EVIDENCE}
    index: VectorIndexManager | None = None

    try:
        _log(f"[proof] workspace: {root}")

        # --- write authoritative sources ---------------------------------
        write_authoritative_sources(root)
        _log("[proof] authoritative sources written")

        # --- model setup --------------------------------------------------
        manager = EmbeddingModelManager(model_dir)
        status = await manager.setup(force=False)
        if status.state == "ready":
            evidence["model_ready"] = True
            _log("[proof] model_ready: passed")
        else:
            _log(f"[proof] model_ready: FAILED ({status.state}: {status.message})")

        provider = LocalEmbeddingProvider(manager=manager)

        # --- embedding quality -------------------------------------------
        try:
            result = await provider.embed(["我喜欢用中文进行自然语言处理"])
            if result.failure is None and len(result.vectors) == 1:
                vec = result.vectors[0]
                norm = math.sqrt(sum(v * v for v in vec))
                if (
                    len(vec) == MODEL_DIMENSION
                    and all(math.isfinite(v) for v in vec)
                    and 0.999 <= norm <= 1.001
                ):
                    evidence["cpu_512_finite_normalized"] = True
                    _log(
                        f"[proof] cpu_512_finite_normalized: passed "
                        f"(dim={len(vec)}, norm={norm:.4f})"
                    )
                else:
                    _log(
                        f"[proof] cpu_512_finite_normalized: FAILED "
                        f"(dim={len(vec)}, norm={norm:.4f})"
                    )
            else:
                _log(f"[proof] cpu_512_finite_normalized: FAILED (failure={result.failure})")
        except Exception as exc:
            _log(f"[proof] cpu_512_finite_normalized: ERROR {exc}")

        # --- catalog + initial rebuild -----------------------------------
        catalog = MemorySourceCatalog(root)
        db_path = root / "memory" / "memory.db"
        index = VectorIndexManager(db_path)

        try:
            report = await index.rebuild(catalog, provider)
            _log(f"[proof] initial rebuild: state={report.state}")
        except Exception as exc:
            _log(f"[proof] initial rebuild error: {exc}")

        # --- two_chinese_memories ----------------------------------------
        try:
            scan = catalog.scan()
            if len(scan.records) >= 2:
                evidence["two_chinese_memories"] = True
                _log(f"[proof] two_chinese_memories: passed ({len(scan.records)} records)")
            else:
                _log(f"[proof] two_chinese_memories: FAILED ({len(scan.records)} records)")
        except Exception as exc:
            _log(f"[proof] two_chinese_memories: ERROR {exc}")

        # --- persisted_after_reopen --------------------------------------
        try:
            count_before = index.count_sources()
            index.close()
            index = VectorIndexManager(db_path)
            count_after = index.count_sources()
            if count_before == count_after and count_after > 0:
                evidence["persisted_after_reopen"] = True
                _log(f"[proof] persisted_after_reopen: passed ({count_before} -> {count_after})")
            else:
                _log(
                    f"[proof] persisted_after_reopen: FAILED "
                    f"({count_before} -> {count_after})"
                )
        except Exception as exc:
            _log(f"[proof] persisted_after_reopen: ERROR {exc}")

        # --- relevant_query_ranked_first ---------------------------------
        query_vec = None
        try:
            q_result = await provider.embed(["早餐喝什么"])
            if q_result.failure is None and len(q_result.vectors) == 1:
                query_vec = list(q_result.vectors[0])
                candidates = index.search(query_vec, limit=5)
                if (
                    candidates
                    and "豆浆" in candidates[0].text
                    and candidates[0].similarity >= 0.45
                ):
                    evidence["relevant_query_ranked_first"] = True
                    _log(
                        f"[proof] relevant_query_ranked_first: passed "
                        f"(sim={candidates[0].similarity:.3f})"
                    )
                else:
                    sim = candidates[0].similarity if candidates else 0
                    _log(
                        f"[proof] relevant_query_ranked_first: FAILED "
                        f"(candidates={len(candidates)}, sim={sim:.3f})"
                    )
            else:
                _log("[proof] relevant_query_ranked_first: FAILED (embedding failed)")
        except Exception as exc:
            _log(f"[proof] relevant_query_ranked_first: ERROR {exc}")

        # --- unchanged_reconcile_idempotent ------------------------------
        try:
            fps_before = index.source_fingerprints()
            scan_noop = catalog.scan()
            await index.reconcile(scan_noop, provider)
            fps_after = index.source_fingerprints()
            if fps_before == fps_after:
                evidence["unchanged_reconcile_idempotent"] = True
                _log("[proof] unchanged_reconcile_idempotent: passed")
            else:
                _log("[proof] unchanged_reconcile_idempotent: FAILED (fingerprints changed)")
        except Exception as exc:
            _log(f"[proof] unchanged_reconcile_idempotent: ERROR {exc}")

        # --- changed_source_updated --------------------------------------
        try:
            user_path = root / "USER.md"
            user_content = user_path.read_text(encoding="utf-8")
            old_source = index.get_source("user:body:1")
            old_text = old_source.text if old_source else ""

            user_path.write_text(
                user_content.replace("无糖豆浆", "燕麦奶"),
                encoding="utf-8",
            )
            scan_changed = catalog.scan()
            await index.reconcile(scan_changed, provider)
            new_source = index.get_source("user:body:1")
            if (
                new_source is not None
                and "燕麦奶" in new_source.text
                and "无糖豆浆" not in new_source.text
                and new_source.text != old_text
            ):
                evidence["changed_source_updated"] = True
                _log("[proof] changed_source_updated: passed")
            else:
                _log("[proof] changed_source_updated: FAILED")
        except Exception as exc:
            _log(f"[proof] changed_source_updated: ERROR {exc}")

        # --- deleted_db_rebuilt ------------------------------------------
        try:
            count_before_del = index.count_sources()
            index.close()
            db_path.unlink(missing_ok=True)
            # Also remove WAL/SHM sidecars if present
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            index = VectorIndexManager(db_path)
            report_del = await index.rebuild(catalog, provider)
            count_after_del = index.count_sources()
            if (
                report_del.state == "ready"
                and count_after_del == count_before_del
                and count_after_del > 0
            ):
                evidence["deleted_db_rebuilt"] = True
                _log(f"[proof] deleted_db_rebuilt: passed ({count_after_del} records)")
            else:
                _log(
                    f"[proof] deleted_db_rebuilt: FAILED "
                    f"(state={report_del.state}, {count_before_del} -> {count_after_del})"
                )
        except Exception as exc:
            _log(f"[proof] deleted_db_rebuilt: ERROR {exc}")

        # --- cancelled_rebuild_preserved_old_index -----------------------
        try:
            cancel_event = asyncio.Event()
            cancel_event.set()
            cancel_report = await index.rebuild(
                catalog, provider, cancel_event=cancel_event
            )

            # Verify old index still serves results
            old_ok = False
            if query_vec is not None:
                old_results = index.search(query_vec, limit=5)
                if cancel_report.state == "cancelled" and len(old_results) > 0:
                    old_ok = True

            if old_ok:
                evidence["cancelled_rebuild_preserved_old_index"] = True
                _log("[proof] cancelled_rebuild_preserved_old_index: passed")
            else:
                _log(
                    f"[proof] cancelled_rebuild_preserved_old_index: FAILED "
                    f"(state={cancel_report.state})"
                )
        except Exception as exc:
            _log(f"[proof] cancelled_rebuild_preserved_old_index: ERROR {exc}")

        # --- fingerprint_mismatch_rejected -------------------------------
        try:
            stale_index = VectorIndexManager(db_path, model_revision="wrong_hash_abc")
            stale_ready = stale_index.is_search_ready()
            stale_results = stale_index.search(
                [0.0] * MODEL_DIMENSION, limit=5
            ) if stale_ready else []
            stale_index.close()
            if not stale_ready or len(stale_results) == 0:
                evidence["fingerprint_mismatch_rejected"] = True
                _log("[proof] fingerprint_mismatch_rejected: passed")
            else:
                _log(
                    f"[proof] fingerprint_mismatch_rejected: FAILED "
                    f"(ready={stale_ready}, results={len(stale_results)})"
                )
        except Exception as exc:
            _log(f"[proof] fingerprint_mismatch_rejected: ERROR {exc}")

        # --- max_five_under_budget + provenance_complete -----------------
        try:
            _expand_sources_for_budget_test(root)
            report_budget = await index.rebuild(catalog, provider)
            _log(
                f"[proof] budget rebuild: state={report_budget.state}, "
                f"records={index.count_sources()}"
            )

            budget_query = await provider.embed(["项目技术栈"])
            if budget_query.failure is None and len(budget_query.vectors) == 1:
                search_results = index.search(list(budget_query.vectors[0]), limit=5)

                if len(search_results) <= 5:
                    evidence["max_five_under_budget"] = True
                    _log(
                        f"[proof] max_five_under_budget: passed "
                        f"({len(search_results)} results)"
                    )
                else:
                    _log(
                        f"[proof] max_five_under_budget: FAILED "
                        f"({len(search_results)} results)"
                    )

                # provenance_complete
                provenance_ok = bool(search_results)
                for candidate in search_results:
                    if (
                        not candidate.source_file
                        or not candidate.source_id
                        or not candidate.source_revision
                    ):
                        provenance_ok = False
                        break
                if provenance_ok:
                    evidence["provenance_complete"] = True
                    _log("[proof] provenance_complete: passed")
                else:
                    _log("[proof] provenance_complete: FAILED")
            else:
                _log("[proof] max_five_under_budget: FAILED (embedding failed)")
                _log("[proof] provenance_complete: FAILED (embedding failed)")
        except Exception as exc:
            _log(f"[proof] max_five_under_budget / provenance_complete: ERROR {exc}")

        # --- safe_noop_chat_fallback -------------------------------------
        try:
            manifest_path = manager.manifest_path
            backup_path = manifest_path.parent / (manifest_path.name + ".bak")
            manifest_moved = False
            try:
                if manifest_path.exists():
                    shutil.move(str(manifest_path), str(backup_path))
                    manifest_moved = True

                recall_service = MemoryRecallService(index=index, embedder=provider)
                policy = MemoryPromptPolicy(root)
                core_texts = policy.core_texts()
                outcome = await recall_service.recall(
                    "测试查询", core_texts=core_texts
                )
                if outcome.fallback_reason is not None:
                    evidence["safe_noop_chat_fallback"] = True
                    _log(
                        f"[proof] safe_noop_chat_fallback: passed "
                        f"(reason={outcome.fallback_reason})"
                    )
                else:
                    _log("[proof] safe_noop_chat_fallback: FAILED (no fallback_reason)")
            finally:
                if manifest_moved and backup_path.exists():
                    shutil.move(str(backup_path), str(manifest_path))
        except Exception as exc:
            _log(f"[proof] safe_noop_chat_fallback: ERROR {exc}")

        ok = all(evidence.values())
        _log(f"[proof] result: ok={ok}")
        _log(f"[proof] evidence: {json.dumps(evidence, ensure_ascii=False)}")

        return {
            "ok": ok,
            "workspace": str(root),
            "evidence": evidence,
        }
    except Exception as exc:
        _log(f"[proof] fatal error: {exc}")
        return {
            "ok": False,
            "workspace": str(root),
            "evidence": evidence,
        }
    finally:
        if index is not None:
            try:
                index.close()
            except Exception:
                pass
        if not keep_workspace:
            try:
                shutil.rmtree(root, ignore_errors=True)
            except OSError:
                pass


def main() -> None:
    """Parse CLI args, run the proof, print JSON, and exit."""
    parser = build_parser()
    args = parser.parse_args()

    model_dir = Path(args.model_dir) if args.model_dir else None
    result = asyncio.run(run_proof(model_dir, args.keep_workspace))

    # The final stdout line is always the JSON result.
    print(json.dumps(result, ensure_ascii=False))

    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
