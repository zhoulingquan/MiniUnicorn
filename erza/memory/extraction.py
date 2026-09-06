"""Strict parsing of Dream extraction output (design sections 3.2 / 11.3).

The model proposes ``CandidateProposal`` records; this module rejects anything
that is not the exact contract shape, that references an unresolvable evidence
ref, that uses an unknown Tag, or that states multiple facts in one record.
The model can never assign status/revision/id — those fields do not exist in
the proposal schema at all.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from erza.memory.models import (
    CandidateProposal,
    MemoryExtractionBatch,
    ScopeKind,
    TagCatalog,
)


class MemoryExtractionError(Exception):
    """Extraction output is malformed, non-contract, or unresolvable."""


# Non-atomic statement markers (design section 3.3): one record expresses one
# independently correctable claim. Co-ordinating connectors that stitch two
# distinct claims into a single statement are rejected so the model splits
# them into separate proposals.
_NON_ATOMIC_MARKERS = (
    "并且",
    "以及",
    "并决定",
    "并选择",
    "同时",
    "但是",
    "然而",
    "此外",
    "而且",
)
_NON_ATOMIC_ASCII_RE = re.compile(r"\b(?:and|also|then|but)\b", re.IGNORECASE)

_DEFAULT_SCOPE_HINTS = frozenset({ScopeKind.PROJECT, ScopeKind.SHARED})

# Non-greedy, no end anchor: take the first closing fence and tolerate
# trailing prose after it (greedy `(.*)` + `$` used to miss the whole match
# whenever the model appended explanation text after the fenced block).
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _assert_atomic(statement: str) -> None:
    for marker in _NON_ATOMIC_MARKERS:
        if marker in statement:
            raise MemoryExtractionError(f"non-atomic statement contains {marker!r}")
    if _NON_ATOMIC_ASCII_RE.search(statement):
        raise MemoryExtractionError(
            f"non-atomic statement contains an English connector: {statement[:80]!r}"
        )


def _assert_proposal(
    proposal: CandidateProposal,
    evidence_catalog: dict[str, object],
    tag_catalog: TagCatalog,
    allowed_scope_hints: frozenset[ScopeKind],
) -> None:
    _assert_atomic(proposal.statement)
    if proposal.scope_hint not in allowed_scope_hints:
        raise MemoryExtractionError(
            f"unsupported scope_hint for background dream extraction: {proposal.scope_hint.value}"
        )
    for tag in proposal.tags:
        if not tag_catalog.contains(tag):
            raise MemoryExtractionError(f"unknown tag in proposal {proposal.proposal_index}: {tag}")
    for ref in proposal.evidence_refs:
        evidence = evidence_catalog.get(ref)
        if evidence is None:
            raise MemoryExtractionError(
                f"unresolved evidence ref in proposal {proposal.proposal_index}: {ref}"
            )
        if not getattr(evidence, "excerpt", ""):
            raise MemoryExtractionError(
                f"evidence ref in proposal {proposal.proposal_index} has no excerpt: {ref}"
            )


def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            from json_repair import repair_json

            repaired = repair_json(text)
        except Exception as exc:
            raise MemoryExtractionError("extraction output is not valid JSON") from exc
        if isinstance(repaired, str):
            repaired = repaired.strip()
        try:
            data = json.loads(repaired) if isinstance(repaired, str) else repaired
        except (json.JSONDecodeError, TypeError) as exc:
            raise MemoryExtractionError("extraction output could not be repaired") from exc
    if not isinstance(data, dict):
        raise MemoryExtractionError("extraction output must be a JSON object")
    return data


def parse_extraction_batch(
    raw: str,
    evidence_catalog: dict[str, object],
    tag_catalog: TagCatalog,
    *,
    allowed_scope_hints: set[ScopeKind] | frozenset[ScopeKind] | None = None,
) -> MemoryExtractionBatch:
    """Parse and strictly validate one Dream extraction response."""
    if not isinstance(raw, str) or not raw.strip():
        raise MemoryExtractionError("extraction output is empty")
    text = raw.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    data = _parse_json(text)
    # WHY: only "proposals" is required here. Small models systematically omit
    # the top-level "schema_version" key; MemoryExtractionBatch defaults it to
    # SCHEMA_VERSION and its field validator still rejects wrong versions, so
    # demanding the key was redundant strictness (the direct cause of the
    # production backlog wedged at 526 permanently-failing batches). The
    # "proposals" check itself must stay: the model defaults both fields, so a
    # bare "{}" would otherwise validate as an empty batch.
    if "proposals" not in data:
        raise MemoryExtractionError("extraction batch missing required key: proposals")
    try:
        batch = MemoryExtractionBatch.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
        raise MemoryExtractionError(
            f"extraction batch violates the contract at {location}: {first.get('msg', 'invalid')}"
        ) from exc
    allowed = frozenset(allowed_scope_hints or _DEFAULT_SCOPE_HINTS)
    for proposal in batch.proposals:
        _assert_proposal(proposal, evidence_catalog, tag_catalog, allowed)
    return batch
