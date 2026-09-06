"""Canonicalize an entire claim without losing nested Evidence ownership."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openkb.knowledge.analysis.service import KnowledgeAnalysisClaim


def require_applicability_binding(value: object, evidence_ids: object) -> None:
    """Validate persisted nested sources as well as newly produced model claims."""
    if (
        not isinstance(evidence_ids, (tuple, list))
        or not evidence_ids
        or any(not isinstance(source, str) or not source for source in evidence_ids)
    ):
        raise ValueError("Claims must bind nonempty Evidence IDs.")
    if not isinstance(value, list):
        raise ValueError("Claim applicability must be a list.")
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Claim applicability entries must be objects.")
        sources = entry.get("source_evidence_ids")
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) or source not in evidence_ids for source in sources)
        ):
            raise ValueError("Applicability Evidence must be a nonempty subset of its claim.")


def canonical_claim(
    claim: KnowledgeAnalysisClaim, evidence_id_map: Mapping[str, str]
) -> KnowledgeAnalysisClaim | None:
    """Resolve all references together; unresolved claims stay in source review."""
    sources = frozenset(claim.source_evidence_ids)
    if not sources or not sources.issubset(evidence_id_map):
        return None
    for entry in claim.applicability:
        if not entry.source_evidence_ids or not set(entry.source_evidence_ids).issubset(sources):
            raise ValueError("Applicability Evidence must be a nonempty subset of its claim.")

    def mapped(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(evidence_id_map[value] for value in values))

    return replace(
        claim,
        source_evidence_ids=mapped(claim.source_evidence_ids),
        applicability=tuple(
            replace(entry, source_evidence_ids=mapped(entry.source_evidence_ids))
            for entry in claim.applicability
        ),
    )
