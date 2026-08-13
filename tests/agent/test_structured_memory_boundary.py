"""Hard boundaries for governed structured memory."""

from __future__ import annotations

import ast
import hashlib
import json
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from miniunicorn.agent.context import ContextBuilder
from miniunicorn.agent.memory import MemoryStore
from miniunicorn.agent.memory_lifecycle import IngestContext
from miniunicorn.agent.memory_models import (
    ActorKind,
    CandidateProposal,
    EvidenceKind,
    EvidenceRef,
    MemoryKind,
    MemoryScope,
    RecallQuery,
    RecallResult,
    ScopeKind,
    SourceLevel,
)
from miniunicorn.config.schema import StructuredMemoryConfig

UTC = timezone.utc


def query(secret: str = "private query text") -> RecallQuery:
    return RecallQuery(
        query_text=secret,
        allowed_scopes=(
            MemoryScope(kind=ScopeKind.USER, key="user:alice-secret"),
            MemoryScope(kind=ScopeKind.PROJECT, key="project:abc123"),
        ),
        now=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )


def test_recall_audit_is_redacted_rotated_and_not_git_tracked(tmp_path):
    store = MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
    audit_path = tmp_path / "memory" / "structured" / "recall-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    old = {"timestamp": "old", "scope_hashes": [], "hits": []}
    audit_path.write_text(
        "".join(json.dumps({**old, "index": index}) + "\n" for index in range(1000)),
        encoding="utf-8",
    )

    store.write_recall_audit(query(), RecallResult(candidates=3, filtered=2, tokens_used=0))

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1000
    assert '"index": 0' not in lines[0]
    latest = json.loads(lines[-1])
    serialized = json.dumps(latest, ensure_ascii=False)
    assert "private query text" not in serialized
    assert "alice-secret" not in serialized
    assert "project:abc123" not in serialized
    assert latest["scope_hashes"] == [
        hashlib.sha256("user:alice-secret".encode()).hexdigest(),
        hashlib.sha256("project:abc123".encode()).hexdigest(),
    ]
    assert set(latest) == {
        "timestamp",
        "scope_hashes",
        "hits",
        "candidates",
        "filtered",
        "excluded_by_budget",
        "tokens_used",
        "degraded",
        "error_code",
    }
    assert "memory/structured/recall-audit.jsonl" not in store.git._tracked_files


def test_recall_audit_concurrent_writers_do_not_lose_rows(tmp_path):
    stores = [
        MemoryStore(tmp_path, structured_config=StructuredMemoryConfig())
        for _ in range(4)
    ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                stores[index % len(stores)].write_recall_audit,
                query(f"query-{index}"),
                RecallResult(candidates=index),
            )
            for index in range(40)
        ]
        for future in futures:
            future.result()

    audit_path = tmp_path / "memory" / "structured" / "recall-audit.jsonl"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 40
    assert {row["candidates"] for row in rows} == set(range(40))


def test_governed_recall_writes_audit_when_enabled(tmp_path, monkeypatch):
    builder = ContextBuilder(
        tmp_path,
        structured_memory_config=StructuredMemoryConfig(
            mode="governed", recall_audit_enabled=True
        ),
    )
    written: list[tuple[RecallQuery, RecallResult]] = []
    result = RecallResult(candidates=1, filtered=1)
    monkeypatch.setattr(builder.memory, "recall_structured", lambda _query: result)
    monkeypatch.setattr(
        builder.memory,
        "write_recall_audit",
        lambda recall_query, recall_result: written.append((recall_query, recall_result)),
    )

    builder.build_system_prompt(recall_query="private governed query")

    assert len(written) == 1
    assert written[0][0].query_text == "private governed query"
    assert written[0][1] is result


FORBIDDEN_VECTOR_IMPORTS = {
    "annoy",
    "chromadb",
    "faiss",
    "hnswlib",
    "lancedb",
    "pinecone",
    "pymilvus",
    "qdrant_client",
    "sentence_transformers",
    "weaviate",
}

_FORBIDDEN_CANONICAL = {canonicalize_name(name) for name in FORBIDDEN_VECTOR_IMPORTS}


def declared_package_names(dependencies: list[str]) -> set[str]:
    """Canonical distribution names of a PEP 508 requirement list.

    Version specifiers, extras, markers, and URL requirements are handled by
    ``packaging.requirements.Requirement``. An unparseable declared dependency
    is a boundary violation and must fail loudly, not disappear.
    """
    names: set[str] = set()
    for spec in dependencies:
        try:
            names.add(canonicalize_name(Requirement(spec).name))
        except InvalidRequirement as exc:
            raise AssertionError(f"unparseable declared dependency: {spec!r}: {exc}")
    return names

_EXPLICIT_RUNTIME_FILES = (
    "miniunicorn/agent/context.py",
    "miniunicorn/agent/loop.py",
    "miniunicorn/agent/memory.py",
    "miniunicorn/agent/reflection.py",
    "miniunicorn/command/memory.py",
    "miniunicorn/config/schema.py",
)


def _runtime_memory_files(root: Path) -> list[Path]:
    agent = root / "miniunicorn" / "agent"
    explicit = [root / relative for relative in _EXPLICIT_RUNTIME_FILES]
    memory_glob = sorted(agent.glob("memory_*.py"))
    return [*explicit, *memory_glob]


def collect_import_roots(source: str) -> set[str]:
    """Top-level import roots of a Python source string (AST-based)."""
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_import_scanner_detects_forbidden_backend():
    assert collect_import_roots("import lancedb\n") == {"lancedb"}
    assert not collect_import_roots("import lancedb\n").isdisjoint(FORBIDDEN_VECTOR_IMPORTS)


def test_runtime_memory_imports_never_touch_vector_backends():
    root = Path(__file__).resolve().parents[2]

    for path in _runtime_memory_files(root):
        assert path.exists(), path
        source = path.read_bytes().decode("utf-8-sig")
        imports = collect_import_roots(source)
        assert imports.isdisjoint(FORBIDDEN_VECTOR_IMPORTS), (
            f"{path.relative_to(root)} imports forbidden vector backend(s): "
            f"{sorted(imports & FORBIDDEN_VECTOR_IMPORTS)}"
        )


def test_runtime_config_and_dependencies_expose_no_vector_memory_entrypoint():
    root = Path(__file__).resolve().parents[2]
    forbidden_config_names = {
        "embedding_provider",
        "embedding_model",
        "vector_store",
        "vector_database",
        "vector_backend",
    }
    schema_text = (root / "miniunicorn" / "config" / "schema.py").read_text(
        encoding="utf-8"
    ).lower()

    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    project = pyproject.get("project", {})
    dependency_lists: list[list[str]] = []
    if isinstance(project.get("dependencies"), list):
        dependency_lists.append(project["dependencies"])
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        dependency_lists.extend(
            spec for spec in optional.values() if isinstance(spec, list)
        )

    declared = set().union(*(declared_package_names(deps) for deps in dependency_lists))

    assert all(name not in schema_text for name in forbidden_config_names)
    assert declared.isdisjoint(_FORBIDDEN_CANONICAL), sorted(
        declared & _FORBIDDEN_CANONICAL
    )
    assert len(dependency_lists) >= 2  # both regular and optional tables scanned


def test_declared_package_names_parses_pep508_specifiers():
    dependencies = [
        "lancedb>=0.1",
        "chromadb[server]~=1.0; python_version >= '3.11'",
        "LanceDB==1.2.3",
        "qdrant_client>=0.10",
        "sentence_transformers @ https://example.com/st-1.0.tar.gz",
        "requests>=2.31",
    ]

    names = declared_package_names(dependencies)

    assert canonicalize_name("lancedb") in names
    assert canonicalize_name("chromadb") in names
    assert canonicalize_name("qdrant-client") in names
    assert canonicalize_name("sentence-transformers") in names
    assert canonicalize_name("requests") in names
    assert names.isdisjoint(_FORBIDDEN_CANONICAL) is False


def test_unparseable_declared_dependency_fails_loudly():
    with pytest.raises(AssertionError, match="unparseable declared dependency"):
        declared_package_names(["lancedb =="])


def test_governed_prompt_never_whole_injects_shared_legacy_file(tmp_path):
    shared = tmp_path / "memory" / "shared" / "MEMORY_SHARED.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("secret shared legacy fact", encoding="utf-8")
    builder = ContextBuilder(
        tmp_path,
        structured_memory_config=StructuredMemoryConfig(mode="governed"),
    )

    prompt = builder.build_system_prompt(recall_query="unrelated")

    assert "secret shared legacy fact" not in prompt


def test_governed_prompt_never_injects_candidate_record(tmp_path):
    builder = ContextBuilder(
        tmp_path,
        structured_memory_config=StructuredMemoryConfig(mode="governed"),
    )
    statement = "Candidate-only private memory fact."
    evidence = EvidenceRef(
        kind=EvidenceKind.MODEL_INFERENCE,
        ref="history:1",
        excerpt=statement,
        observed_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )
    proposal = CandidateProposal(
        proposal_index=0,
        kind=MemoryKind.FACT,
        scope_hint=ScopeKind.PROJECT,
        subject="MiniUnicorn",
        slot="boundary.candidate",
        statement=statement,
        tags=("architecture.memory",),
        confidence=0.7,
        importance=3,
        evidence_refs=("history:1",),
        speech_act=SourceLevel.INFERRED,
    )
    builder.memory.structured_lifecycle.ingest(
        proposal,
        IngestContext(
            actor=ActorKind.DREAM,
            reason="boundary test",
            source_batch="boundary:candidate",
            scope=MemoryScope(
                kind=ScopeKind.PROJECT, key=builder.memory.project_scope_key
            ),
            evidence_catalog={"history:1": evidence},
            now=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
        ),
    )

    prompt = builder.build_system_prompt(recall_query="MiniUnicorn architecture.memory")

    assert statement not in prompt
