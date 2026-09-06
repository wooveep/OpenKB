"""Vectorless routing from published Knowledge Claims to original Evidence."""

from __future__ import annotations

import sqlite3

from openkb.knowledge.pages.sources import AVAILABLE_EVIDENCE_OCCURRENCES_CTE
from openkb.retrieval.scoped_evidence import ScopedEvidenceView


def knowledge_source_rows_in(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    *,
    limit: int,
    scoped_view: ScopedEvidenceView | None = None,
) -> list[tuple[object, ...]]:
    """Rank unique mapped Evidence by published Knowledge Claim wording."""
    if not terms:
        return []
    score_parts: list[str] = []
    parameters: list[object] = []
    for term in terms:
        score_parts.extend(
            (
                "CASE WHEN instr(lower(mapped.title), ?) > 0 THEN 2 ELSE 0 END",
                "CASE WHEN instr(lower(mapped.claim_text), ?) > 0 THEN 1 ELSE 0 END",
            )
        )
        parameters.extend((term, term))
    occurrence_cte, scope_parameters = (
        scoped_view.sql_cte("available_evidence_occurrences")
        if scoped_view is not None
        else (AVAILABLE_EVIDENCE_OCCURRENCES_CTE, ())
    )
    return connection.execute(
        f"""
        {occurrence_cte}
        , mapped AS (
            SELECT pages.title, sources.claim_text, sources.evidence_id,
                pages.page_id AS identity,
                CASE
                    WHEN pages.stale_after IS NOT NULL
                        AND julianday(pages.stale_after) <= julianday('now') THEN 0
                    ELSE 1
                END AS lifecycle_tier,
                CASE WHEN verifications.verification_id IS NULL THEN 0 ELSE 1 END AS trust_tier
            FROM knowledge_pages AS pages
            JOIN knowledge_page_revision_sources AS sources
                ON sources.revision_id = pages.current_revision_id
            LEFT JOIN knowledge_page_verifications AS verifications
                ON verifications.revision_id = sources.revision_id
                AND verifications.invalidated_at IS NULL
            WHERE pages.lifecycle_state = 'stable'
            UNION ALL
            SELECT items.title, sources.claim_text, sources.evidence_id,
                items.item_key, 1, 0
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
            WHERE state.singleton = 1 AND items.provenance_state = 'source_backed'
        ), scored_mapped_evidence AS (
            SELECT available_evidence_occurrences.evidence_id,
                available_evidence_occurrences.document_id,
                available_evidence_occurrences.display_name,
                available_evidence_occurrences.heading_path,
                available_evidence_occurrences.locator_json,
                available_evidence_occurrences.text,
                ({" + ".join(score_parts)}) AS channel_score,
                mapped.identity, available_evidence_occurrences.ordinal,
                mapped.lifecycle_tier, mapped.trust_tier
            FROM mapped
            JOIN available_evidence_occurrences
                ON available_evidence_occurrences.evidence_id = mapped.evidence_id
            WHERE available_evidence_occurrences.occurrence_rank = 1
        ), ranked_mapped_evidence AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY evidence_id
                ORDER BY channel_score DESC, lifecycle_tier DESC, trust_tier DESC,
                    identity, ordinal
            ) AS evidence_rank
            FROM scored_mapped_evidence WHERE channel_score > 0
        )
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM ranked_mapped_evidence
        WHERE evidence_rank = 1
        ORDER BY channel_score DESC, lifecycle_tier DESC, trust_tier DESC, identity, ordinal
        LIMIT ?
        """,
        (*scope_parameters, *parameters, limit),
    ).fetchall()
