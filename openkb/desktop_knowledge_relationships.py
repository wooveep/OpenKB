"""Authoritative generated-knowledge relationships and their source bindings."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from openkb.desktop_knowledge_relationship_migrations import (
    relationship_rebuild_statements,
)


@dataclass(frozen=True)
class KnowledgeGenerationRelationship:
    source_item_key: str
    target_item_key: str
    target_kind: str
    target_title: str
    relation_kind: str
    provenance: str
    source_evidence_ids: tuple[str, ...]
    target_evidence_ids: tuple[str, ...]


def rebuild_generation_relationships_in(connection: sqlite3.Connection, generation_id: int) -> None:
    """Derive explicit relations from structured claims, titles, and source maps."""
    connection.execute(
        "DELETE FROM knowledge_generation_relationship_sources WHERE generation_id = ?",
        (generation_id,),
    )
    connection.execute(
        "DELETE FROM knowledge_generation_relationships WHERE generation_id = ?",
        (generation_id,),
    )
    for statement in relationship_rebuild_statements():
        connection.execute(statement, (generation_id,))


def generation_relationships_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[KnowledgeGenerationRelationship, ...]:
    """Read complete endpoint-bound relation records for one generation."""
    rows = connection.execute(
        """
        SELECT relationships.source_item_key, relationships.target_item_key,
            targets.kind, targets.title, relationships.relation_kind,
            relationships.provenance, sources.binding_role, sources.evidence_id
        FROM knowledge_generation_relationships AS relationships
        JOIN knowledge_generation_items AS targets
          ON targets.generation_id = relationships.generation_id
         AND targets.item_key = relationships.target_item_key
        JOIN knowledge_generation_relationship_sources AS sources
          ON sources.generation_id = relationships.generation_id
         AND sources.source_item_key = relationships.source_item_key
         AND sources.target_item_key = relationships.target_item_key
         AND sources.relation_kind = relationships.relation_kind
        WHERE relationships.generation_id = ?
        ORDER BY relationships.source_item_key, relationships.target_item_key,
            relationships.relation_kind, sources.binding_role, sources.evidence_id
        """,
        (generation_id,),
    ).fetchall()
    grouped: defaultdict[tuple[str, str, str], list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row[0]), str(row[1]), str(row[4]))].append(row)
    relationships: list[KnowledgeGenerationRelationship] = []
    for values in grouped.values():
        source_ids = tuple(str(row[7]) for row in values if str(row[6]) == "source")
        target_ids = tuple(str(row[7]) for row in values if str(row[6]) == "target")
        if not source_ids or not target_ids:
            continue
        relationships.append(
            KnowledgeGenerationRelationship(
                source_item_key=str(values[0][0]),
                target_item_key=str(values[0][1]),
                target_kind=str(values[0][2]),
                target_title=str(values[0][3]),
                relation_kind=str(values[0][4]),
                provenance=str(values[0][5]),
                source_evidence_ids=tuple(dict.fromkeys(source_ids)),
                target_evidence_ids=tuple(dict.fromkeys(target_ids)),
            )
        )
    return tuple(relationships)


def generation_relationship_issues_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[str, ...]:
    """Fail closed when a relation is incomplete, stale, or not endpoint-bound."""
    invalid = connection.execute(
        """
        SELECT COUNT(*)
        FROM knowledge_generation_relationship_sources AS relationship_sources
        LEFT JOIN knowledge_generation_item_sources AS item_sources
          ON item_sources.generation_id = relationship_sources.generation_id
         AND item_sources.item_key = CASE relationship_sources.binding_role
             WHEN 'source' THEN relationship_sources.source_item_key
             ELSE relationship_sources.target_item_key
         END
         AND item_sources.evidence_id = relationship_sources.evidence_id
        WHERE relationship_sources.generation_id = ?
          AND (
              item_sources.evidence_id IS NULL OR NOT EXISTS (
                  SELECT 1 FROM evidence_occurrences AS occurrences
                  JOIN source_documents AS documents
                    ON documents.document_id = occurrences.document_id
                  WHERE occurrences.evidence_id = relationship_sources.evidence_id
                    AND documents.availability = 'available'
              )
          )
        """,
        (generation_id,),
    ).fetchone()
    incomplete = connection.execute(
        """
        SELECT COUNT(*) FROM knowledge_generation_relationships AS relationships
        WHERE relationships.generation_id = ? AND (
            NOT EXISTS (
                SELECT 1 FROM knowledge_generation_relationship_sources AS sources
                WHERE sources.generation_id = relationships.generation_id
                  AND sources.source_item_key = relationships.source_item_key
                  AND sources.target_item_key = relationships.target_item_key
                  AND sources.relation_kind = relationships.relation_kind
                  AND sources.binding_role = 'source'
            ) OR NOT EXISTS (
                SELECT 1 FROM knowledge_generation_relationship_sources AS sources
                WHERE sources.generation_id = relationships.generation_id
                  AND sources.source_item_key = relationships.source_item_key
                  AND sources.target_item_key = relationships.target_item_key
                  AND sources.relation_kind = relationships.relation_kind
                  AND sources.binding_role = 'target'
            )
        )
        """,
        (generation_id,),
    ).fetchone()
    issues: list[str] = []
    if invalid is not None and int(invalid[0]) > 0:
        issues.append("invalid_relationship_source")
    if incomplete is not None and int(incomplete[0]) > 0:
        issues.append("incomplete_relationship")
    return tuple(issues)
