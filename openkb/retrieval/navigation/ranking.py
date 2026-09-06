"""Scope-aware ranking helpers for bounded original-source navigation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from openkb.documents.source_sections import (
    is_administrative_section,
    section_from_heading_path,
    source_occurrence_sort_key,
    source_occurrences_in,
)
from openkb.retrieval.scoped_evidence import ScopedEvidenceView


@dataclass(frozen=True)
class _OutlineCandidate:
    matches: int
    depth: int
    ordinal: int
    evidence_id: str
    section: str


def broad_source_outline_anchor_in(
    connection: sqlite3.Connection,
    evidence_ids: tuple[str, ...],
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView | None = None,
) -> str | None:
    """Choose a shallow relevant chapter so one whole-source read exposes its phases."""
    candidates: list[_OutlineCandidate] = []
    for ordinal, evidence_id in enumerate(evidence_ids):
        relevance = source_relevance_in(connection, evidence_id, terms, scoped_view=scoped_view)
        if relevance is None:
            continue
        matches, administrative, section, _document_id = relevance
        if administrative or matches <= 0:
            continue
        candidates.append(
            _OutlineCandidate(
                matches=matches,
                depth=section.count(" / "),
                ordinal=ordinal,
                evidence_id=evidence_id,
                section=section,
            )
        )
    if not candidates:
        return None
    strongest = max(item.matches for item in candidates)
    best = min(
        (item for item in candidates if item.matches == strongest),
        key=lambda item: item.ordinal,
    )
    ancestors = tuple(
        item
        for item in candidates
        if item.matches > 0
        and (item.section == best.section or best.section.startswith(f"{item.section} / "))
    )
    return min(
        ancestors or (best,),
        key=lambda item: (item.depth, -item.matches, item.ordinal),
    ).evidence_id


def source_relevance_in(
    connection: sqlite3.Connection,
    evidence_id: str,
    terms: tuple[str, ...],
    *,
    scoped_view: ScopedEvidenceView | None = None,
) -> tuple[int, bool, str, str] | None:
    rows = source_occurrences_in(connection, evidence_id, scoped_view=scoped_view)
    if not rows:
        return None
    best = min(rows, key=lambda row: source_occurrence_sort_key(row, terms))
    section = section_from_heading_path(str(best[3]))
    return (
        sum(1 for term in terms if term in section.casefold()),
        is_administrative_section(section),
        section,
        str(best[0]),
    )
