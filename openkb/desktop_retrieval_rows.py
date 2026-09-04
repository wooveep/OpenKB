"""Shared SQLite row scoring and EvidenceRef adaptation for retrieval channels."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_knowledge_sources import AVAILABLE_EVIDENCE_OCCURRENCES_CTE
from openkb.desktop_retrieval_fusion import RetrievalCandidate
from openkb.desktop_scoped_evidence import ScopedEvidenceView

_CHANNEL_LIMIT = 12
_SCORE_COLUMNS = frozenset(("display_name", "heading_path", "text"))


def scored_rows(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    weighted_columns: tuple[tuple[str, int], ...],
    scoped_view: ScopedEvidenceView | None = None,
) -> list[tuple[object, ...]]:
    """Select a bounded channel ranking from every Available Knowledge occurrence."""
    score_parts: list[str] = []
    parameters: list[object] = []
    for term in terms:
        for column, weight in weighted_columns:
            if column not in _SCORE_COLUMNS:
                raise ValueError(f"Unsupported Desktop retrieval score column: {column}")
            score_parts.append(f"CASE WHEN instr(lower({column}), ?) > 0 THEN {weight} ELSE 0 END")
            parameters.append(term)
    score_expression = " + ".join(score_parts)
    occurrence_cte, scope_parameters = (
        scoped_view.sql_cte("available_evidence_occurrences")
        if scoped_view is not None
        else (AVAILABLE_EVIDENCE_OCCURRENCES_CTE, ())
    )
    return connection.execute(
        f"""
        {occurrence_cte}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM (
            SELECT evidence_id, document_id, display_name, heading_path, locator_json, text,
                ({score_expression}) AS channel_score
            FROM available_evidence_occurrences
            WHERE occurrence_rank = 1
        )
        WHERE channel_score > 0
        ORDER BY channel_score DESC, document_id, evidence_id
        LIMIT ?
        """,
        (*scope_parameters, *parameters, _CHANNEL_LIMIT),
    ).fetchall()


def ranked_candidates(
    rows: list[tuple[object, ...]], channel: str
) -> tuple[RetrievalCandidate, ...]:
    values: list[RetrievalCandidate] = []
    for rank, row in enumerate(rows, start=1):
        reference = DesktopEvidenceRef(
            evidence_id=str(row[0]),
            document_id=str(row[1]),
            document_name=str(row[2]),
            section=section_from_json(str(row[3])),
            locator=json_object(str(row[4])),
            excerpt=str(row[5]),
            channels=(channel,),
        )
        values.append(RetrievalCandidate(reference=reference, channel=channel, rank=rank))
    return tuple(values)


def section_from_json(value: str) -> str:
    try:
        path = json.loads(value)
    except json.JSONDecodeError:
        return "Document"
    if not isinstance(path, list) or not all(isinstance(item, str) for item in path):
        return "Document"
    return " / ".join(path) if path else "Document"


def json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def placeholders(values: tuple[object, ...]) -> str:
    if not values:
        raise ValueError("SQLite placeholders require at least one value.")
    return ", ".join("?" for _ in values)
