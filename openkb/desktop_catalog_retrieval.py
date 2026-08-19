"""Low-weight one-hop Catalog routing back to Available original Evidence."""

from __future__ import annotations

import sqlite3

from openkb.desktop_knowledge_sources import AVAILABLE_EVIDENCE_OCCURRENCES_CTE

CATALOG_DIRECT_WEIGHT = 0.45
CATALOG_LINK_WEIGHT = 0.15
CATALOG_STALE_MULTIPLIER = 0.5


def catalog_route_rows_in(
    connection: sqlite3.Connection,
    generation_id: str,
    terms: tuple[str, ...],
    *,
    limit: int,
) -> list[tuple[object, ...]]:
    """Return unique evidence reached from matched nodes and one ordinary link hop."""
    if not terms:
        return []
    score_parts: list[str] = []
    parameters: list[object] = []
    for term in terms:
        score_parts.append("CASE WHEN instr(nodes.search_text, ?) > 0 THEN 1 ELSE 0 END")
        parameters.append(term)
    score_expression = " + ".join(score_parts)
    return connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        , matched_nodes AS (
            SELECT nodes.node_id, ({score_expression}) AS node_score,
                CASE
                    WHEN json_extract(nodes.metadata_json, '$.stale_after') IS NOT NULL
                        AND julianday(json_extract(nodes.metadata_json, '$.stale_after'))
                            <= julianday('now')
                    THEN {CATALOG_STALE_MULTIPLIER}
                    ELSE 1.0
                END AS lifecycle_weight
            FROM knowledge_catalog_nodes AS nodes
            WHERE nodes.generation_id = ?
                AND nodes.kind IN ('concept', 'entity', 'source_document')
                AND COALESCE(nodes.lifecycle_state, 'stable') != 'deprecated'
                AND COALESCE(nodes.availability, 'available') = 'available'
        ), direct_nodes AS (
            SELECT node_id, node_score,
                {CATALOG_DIRECT_WEIGHT} * lifecycle_weight AS route_weight,
                0 AS hop, lifecycle_weight
            FROM matched_nodes WHERE node_score > 0
        ), routed_candidates AS (
            SELECT node_id, node_score, route_weight, hop FROM direct_nodes
            UNION ALL
            SELECT links.to_node_id, direct.node_score,
                {CATALOG_LINK_WEIGHT} * MIN(
                    direct.lifecycle_weight,
                    CASE
                        WHEN json_extract(targets.metadata_json, '$.stale_after') IS NOT NULL
                            AND julianday(json_extract(targets.metadata_json, '$.stale_after'))
                                <= julianday('now')
                        THEN {CATALOG_STALE_MULTIPLIER}
                        ELSE 1.0
                    END
                ) AS route_weight,
                1 AS hop
            FROM direct_nodes AS direct
            JOIN knowledge_catalog_links AS links
                ON links.generation_id = ? AND links.from_node_id = direct.node_id
            JOIN knowledge_catalog_nodes AS targets
                ON targets.generation_id = links.generation_id
                AND targets.node_id = links.to_node_id
                AND COALESCE(targets.lifecycle_state, 'stable') != 'deprecated'
                AND COALESCE(targets.availability, 'available') = 'available'
        ), routed_nodes AS (
            SELECT node_id, node_score, route_weight, hop FROM (
                SELECT routed_candidates.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY node_id
                        ORDER BY route_weight DESC, node_score DESC, hop
                    ) AS route_rank
                FROM routed_candidates
            ) WHERE route_rank = 1
        ), node_evidence AS (
            SELECT routed.node_id, sources.evidence_id, routed.node_score,
                routed.route_weight, routed.hop, sources.association_order,
                ROW_NUMBER() OVER (
                    PARTITION BY routed.node_id
                    ORDER BY sources.association_order, sources.evidence_id
                ) AS node_evidence_rank
            FROM routed_nodes AS routed
            JOIN knowledge_catalog_node_sources AS sources
                ON sources.generation_id = ? AND sources.node_id = routed.node_id
        ), ranked AS (
            SELECT available.evidence_id, available.document_id, available.display_name,
                available.heading_path, available.locator_json, available.text,
                node_evidence.route_weight, node_evidence.node_score, node_evidence.hop,
                ROW_NUMBER() OVER (
                    PARTITION BY available.evidence_id
                    ORDER BY node_evidence.route_weight DESC, node_evidence.node_score DESC,
                        node_evidence.hop, node_evidence.node_id
                ) AS evidence_rank
            FROM node_evidence
            JOIN available_evidence_occurrences AS available
                ON available.evidence_id = node_evidence.evidence_id
                AND available.occurrence_rank = 1
            WHERE node_evidence.node_evidence_rank <= 2
        )
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text,
            route_weight
        FROM ranked WHERE evidence_rank = 1
        ORDER BY route_weight DESC, node_score DESC, hop, evidence_id
        LIMIT ?
        """,
        (*parameters, generation_id, generation_id, generation_id, limit),
    ).fetchall()
