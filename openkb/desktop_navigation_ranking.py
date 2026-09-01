"""Scope-aware ranking helpers for bounded original-source navigation."""

from __future__ import annotations

import sqlite3

from openkb.desktop_source_sections import (
    is_administrative_section,
    section_from_heading_path,
    source_occurrence_sort_key,
    source_occurrences_in,
)


def broad_source_outline_anchor_in(
    connection: sqlite3.Connection,
    evidence_ids: tuple[str, ...],
    terms: tuple[str, ...],
) -> str | None:
    """Choose a shallow relevant chapter so one whole-source read exposes its phases."""
    candidates: list[tuple[int, int, int, int, str, str]] = []
    for ordinal, evidence_id in enumerate(evidence_ids):
        relevance = source_relevance_in(connection, evidence_id, terms)
        if relevance is None:
            continue
        matches, administrative, section, _document_id = relevance
        if administrative or matches <= 0:
            continue
        scope_score = matches - unrequested_scope_penalty(section, terms)
        candidates.append(
            (
                scope_score,
                matches,
                section.count(" / "),
                ordinal,
                evidence_id,
                section,
            )
        )
    if not candidates:
        return None
    strongest = max(item[0] for item in candidates)
    best = min(
        (item for item in candidates if item[0] == strongest),
        key=lambda item: (-item[1], item[3]),
    )
    ancestors = tuple(
        item
        for item in candidates
        if item[0] > 0 and (item[5] == best[5] or best[5].startswith(f"{item[5]} / "))
    )
    return min(
        ancestors or (best,),
        key=lambda item: (item[2], -item[0], -item[1], item[3]),
    )[4]


def source_relevance_in(
    connection: sqlite3.Connection,
    evidence_id: str,
    terms: tuple[str, ...],
) -> tuple[int, bool, str, str] | None:
    rows = source_occurrences_in(connection, evidence_id)
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


def unrequested_scope_penalty(section: str, terms: tuple[str, ...]) -> int:
    return min(
        12,
        unrequested_lifecycle_penalty(section, terms)
        + _unrequested_marker_penalty(
            section,
            terms,
            ("计算节点", "仅管理节点", "compute node", "management node"),
        ),
    )


def unrequested_lifecycle_penalty(section: str, terms: tuple[str, ...]) -> int:
    """Defer lifecycle branches without treating base node roles as optional."""
    return _unrequested_marker_penalty(
        section,
        terms,
        (
            "扩容",
            "缩容",
            "运维",
            "故障",
            "恢复",
            "升级",
            "附录",
            "faq",
            "expansion",
            "maintenance",
            "recovery",
            "scale-out",
            "upgrade",
            "troubleshoot",
        ),
    )


def _unrequested_marker_penalty(
    section: str,
    terms: tuple[str, ...],
    markers: tuple[str, ...],
) -> int:
    normalized = section.casefold()
    unmatched = sum(
        marker in normalized and not any(marker in term for term in terms) for marker in markers
    )
    return min(12, unmatched * 6)
