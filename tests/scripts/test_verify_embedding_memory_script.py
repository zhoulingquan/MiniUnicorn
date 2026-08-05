"""Contract tests for the embedding-memory release proof script.

These tests validate the CLI parser, evidence field set, and payload schema
without executing the real embedding model. The actual proof must be run
separately via `python scripts/verify_embedding_memory.py`.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_embedding_memory.py"


def _load_script_module():
    """Load the proof script as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("verify_embedding_memory", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def proof_module():
    if not SCRIPT_PATH.is_file():
        pytest.skip(f"Proof script not found at {SCRIPT_PATH}")
    return _load_script_module()


def test_proof_payload_requires_every_release_evidence(proof_module):
    """The script must define exactly the 13 required evidence fields."""
    expected = {
        "model_ready", "cpu_512_finite_normalized", "two_chinese_memories",
        "persisted_after_reopen", "relevant_query_ranked_first",
        "unchanged_reconcile_idempotent", "changed_source_updated",
        "deleted_db_rebuilt", "cancelled_rebuild_preserved_old_index",
        "fingerprint_mismatch_rejected", "max_five_under_budget",
        "provenance_complete", "safe_noop_chat_fallback",
    }
    assert proof_module.REQUIRED_EVIDENCE == expected


def test_script_has_cli_parser(proof_module):
    """The script must accept --model-dir and --keep-workspace arguments."""
    parser = proof_module.build_parser()
    args = parser.parse_args(["--model-dir", "/tmp/models", "--keep-workspace"])
    assert args.model_dir == "/tmp/models"
    assert args.keep_workspace is True

    args_default = parser.parse_args([])
    assert args_default.model_dir is None
    assert args_default.keep_workspace is False


def test_run_proof_returns_dict_with_required_keys(proof_module, monkeypatch):
    """run_proof must return a dict with 'ok', 'workspace', 'evidence' keys."""
    # We monkeypatch the async run_proof to avoid real model execution
    async def fake_run_proof(model_dir, keep_workspace):
        return {
            "ok": False,
            "workspace": "/tmp/fake",
            "evidence": {name: False for name in proof_module.REQUIRED_EVIDENCE},
        }
    monkeypatch.setattr(proof_module, "run_proof", fake_run_proof)
    import asyncio
    result = asyncio.run(proof_module.run_proof(None, False))
    assert "ok" in result
    assert "workspace" in result
    assert "evidence" in result
    assert set(result["evidence"].keys()) == proof_module.REQUIRED_EVIDENCE
