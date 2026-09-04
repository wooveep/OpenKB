"""Materialize evidence-bound semantic relations over one Knowledge generation."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from openkb.desktop_semantic_graph_contract import relation_endpoint_allowed

logger = logging.getLogger(__name__)


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
    assertion_evidence_ids: tuple[str, ...]
    applicability_json: str


@dataclass
class _MaterializedRelationship:
    source_item_key: str
    target_item_key: str
    relation_kind: str
    applicability: list[object]
    source_evidence_ids: set[str]
    target_evidence_ids: set[str]
    assertion_evidence_ids: set[str]


def rebuild_generation_relationships_in(connection: sqlite3.Connection, generation_id: int) -> None:
    """Map validated document assertions through canonical Knowledge Identities."""
    connection.execute(
        "DELETE FROM knowledge_generation_relationship_sources WHERE generation_id = ?",
        (generation_id,),
    )
    connection.execute(
        "DELETE FROM knowledge_generation_relationships WHERE generation_id = ?",
        (generation_id,),
    )
    item_sources = _generation_item_sources_in(connection, generation_id)
    relationships: dict[tuple[str, str, str], _MaterializedRelationship] = {}
    rows = connection.execute(
        """
        SELECT document_relations.document_id,
            document_relations.source_candidate_id,
            document_relations.target_candidate_id,
            document_relations.relation_kind,
            document_relations.applicability_json,
            source_items.item_key, source_items.kind,
            target_items.item_key, target_items.kind,
            graph_inputs.candidate_generation_id
        FROM knowledge_document_relationships AS document_relations
        JOIN knowledge_generation_graph_inputs AS graph_inputs
          ON graph_inputs.generation_id = ?
         AND graph_inputs.document_id = document_relations.document_id
         AND graph_inputs.candidate_generation_id =
             document_relations.candidate_generation_id
         AND graph_inputs.result_id = document_relations.graph_result_id
        JOIN knowledge_generation_identity_mappings AS source_identity_candidates
          ON source_identity_candidates.generation_id = graph_inputs.generation_id
         AND source_identity_candidates.candidate_generation_id =
             graph_inputs.candidate_generation_id
         AND source_identity_candidates.candidate_id =
             document_relations.source_candidate_id
        JOIN knowledge_generation_items AS source_items
          ON source_items.generation_id = graph_inputs.generation_id
         AND source_items.identity_id = source_identity_candidates.identity_id
        JOIN knowledge_generation_identity_mappings AS target_identity_candidates
          ON target_identity_candidates.generation_id = graph_inputs.generation_id
         AND target_identity_candidates.candidate_generation_id =
             graph_inputs.candidate_generation_id
         AND target_identity_candidates.candidate_id =
             document_relations.target_candidate_id
        JOIN knowledge_generation_items AS target_items
          ON target_items.generation_id = graph_inputs.generation_id
         AND target_items.identity_id = target_identity_candidates.identity_id
        WHERE document_relations.candidate_generation_id IS NOT NULL
        ORDER BY document_relations.document_id,
            document_relations.source_candidate_id,
            document_relations.target_candidate_id,
            document_relations.relation_kind
        """,
        (generation_id,),
    ).fetchall()
    for row in rows:
        source_candidate_id = str(row[1])
        target_candidate_id = str(row[2])
        relation_kind = str(row[3])
        source_item_key = str(row[5])
        target_item_key = str(row[7])
        if source_item_key == target_item_key or not relation_endpoint_allowed(
            relation_kind, str(row[6]), str(row[8])
        ):
            continue
        candidate_generation_id = str(row[9])
        source_evidence = _candidate_evidence_in(
            connection, candidate_generation_id, source_candidate_id
        ) & item_sources.get(source_item_key, set())
        target_evidence = _candidate_evidence_in(
            connection, candidate_generation_id, target_candidate_id
        ) & item_sources.get(target_item_key, set())
        assertion_evidence = _assertion_evidence_in(
            connection,
            document_id=str(row[0]),
            source_candidate_id=source_candidate_id,
            target_candidate_id=target_candidate_id,
            relation_kind=relation_kind,
        ) & (source_evidence | target_evidence)
        if not source_evidence or not target_evidence or not assertion_evidence:
            continue
        try:
            applicability = _applicability_values(row[4])
        except ValueError:
            logger.warning(
                "semantic_relationship_skipped reason=invalid_applicability "
                "generation_id=%d relation_kind=%s",
                generation_id,
                relation_kind,
            )
            continue
        key = (source_item_key, target_item_key, relation_kind)
        relationship = relationships.get(key)
        if relationship is None:
            relationship = _MaterializedRelationship(
                source_item_key=source_item_key,
                target_item_key=target_item_key,
                relation_kind=relation_kind,
                applicability=[],
                source_evidence_ids=set(),
                target_evidence_ids=set(),
                assertion_evidence_ids=set(),
            )
            relationships[key] = relationship
        relationship.applicability.extend(applicability)
        relationship.source_evidence_ids.update(source_evidence)
        relationship.target_evidence_ids.update(target_evidence)
        relationship.assertion_evidence_ids.update(assertion_evidence)

    for relationship in relationships.values():
        applicability_json = _canonical_applicability(relationship.applicability)
        connection.execute(
            """
            INSERT INTO knowledge_generation_relationships (
                generation_id, source_item_key, target_item_key, relation_kind,
                applicability_json, provenance
            ) VALUES (?, ?, ?, ?, ?, 'semantic_relation_analysis')
            """,
            (
                generation_id,
                relationship.source_item_key,
                relationship.target_item_key,
                relationship.relation_kind,
                applicability_json,
            ),
        )
        bindings = (
            ("source", relationship.source_evidence_ids),
            ("target", relationship.target_evidence_ids),
            ("assertion", relationship.assertion_evidence_ids),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_generation_relationship_sources (
                generation_id, source_item_key, target_item_key, relation_kind,
                binding_role, evidence_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    generation_id,
                    relationship.source_item_key,
                    relationship.target_item_key,
                    relationship.relation_kind,
                    binding_role,
                    evidence_id,
                )
                for binding_role, evidence_ids in bindings
                for evidence_id in sorted(evidence_ids)
            ),
        )


def generation_relationships_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[KnowledgeGenerationRelationship, ...]:
    """Read complete endpoint- and assertion-bound relations for one generation."""
    rows = connection.execute(
        """
        SELECT relationships.source_item_key, relationships.target_item_key,
            targets.kind, targets.title, relationships.relation_kind,
            relationships.provenance, relationships.applicability_json,
            sources.binding_role, sources.evidence_id
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
        evidence = {
            role: tuple(dict.fromkeys(str(row[8]) for row in values if str(row[7]) == role))
            for role in ("source", "target", "assertion")
        }
        if any(not evidence[role] for role in evidence):
            continue
        relationships.append(
            KnowledgeGenerationRelationship(
                source_item_key=str(values[0][0]),
                target_item_key=str(values[0][1]),
                target_kind=str(values[0][2]),
                target_title=str(values[0][3]),
                relation_kind=str(values[0][4]),
                provenance=str(values[0][5]),
                applicability_json=str(values[0][6]),
                source_evidence_ids=evidence["source"],
                target_evidence_ids=evidence["target"],
                assertion_evidence_ids=evidence["assertion"],
            )
        )
    return tuple(relationships)


def generation_relationship_issues_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[str, ...]:
    """Fail closed when a semantic relation is incomplete, stale, or invalid."""
    invalid = connection.execute(
        """
        SELECT COUNT(*)
        FROM knowledge_generation_relationship_sources AS relationship_sources
        LEFT JOIN knowledge_generation_item_sources AS item_sources
          ON item_sources.generation_id = relationship_sources.generation_id
         AND item_sources.evidence_id = relationship_sources.evidence_id
         AND (
             (relationship_sources.binding_role = 'source'
              AND item_sources.item_key = relationship_sources.source_item_key)
             OR (relationship_sources.binding_role = 'target'
                 AND item_sources.item_key = relationship_sources.target_item_key)
             OR (relationship_sources.binding_role = 'assertion'
                 AND item_sources.item_key IN (
                     relationship_sources.source_item_key,
                     relationship_sources.target_item_key
                 ))
         )
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
        WHERE relationships.generation_id = ? AND EXISTS (
            SELECT 1 FROM (SELECT 'source' AS role UNION ALL SELECT 'target'
                           UNION ALL SELECT 'assertion') AS required
            WHERE NOT EXISTS (
                SELECT 1 FROM knowledge_generation_relationship_sources AS sources
                WHERE sources.generation_id = relationships.generation_id
                  AND sources.source_item_key = relationships.source_item_key
                  AND sources.target_item_key = relationships.target_item_key
                  AND sources.relation_kind = relationships.relation_kind
                  AND sources.binding_role = required.role
            )
        )
        """,
        (generation_id,),
    ).fetchone()
    endpoint_rows = connection.execute(
        """
        SELECT relationships.relation_kind, source_items.kind, target_items.kind
        FROM knowledge_generation_relationships AS relationships
        JOIN knowledge_generation_items AS source_items
          ON source_items.generation_id = relationships.generation_id
         AND source_items.item_key = relationships.source_item_key
        JOIN knowledge_generation_items AS target_items
          ON target_items.generation_id = relationships.generation_id
         AND target_items.item_key = relationships.target_item_key
        WHERE relationships.generation_id = ?
        """,
        (generation_id,),
    ).fetchall()
    issues: list[str] = []
    if invalid is not None and int(invalid[0]) > 0:
        issues.append("invalid_relationship_source")
    if incomplete is not None and int(incomplete[0]) > 0:
        issues.append("incomplete_relationship")
    if any(
        not relation_endpoint_allowed(str(row[0]), str(row[1]), str(row[2]))
        for row in endpoint_rows
    ):
        issues.append("invalid_relationship_endpoint")
    return tuple(issues)


def _generation_item_sources_in(
    connection: sqlite3.Connection, generation_id: int
) -> dict[str, set[str]]:
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT item_key, evidence_id FROM knowledge_generation_item_sources "
        "WHERE generation_id = ?",
        (generation_id,),
    ):
        sources[str(row[0])].add(str(row[1]))
    return dict(sources)


def _candidate_evidence_in(
    connection: sqlite3.Connection,
    candidate_generation_id: str,
    candidate_id: str,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT evidence_id FROM knowledge_candidate_generation_claim_sources "
            "WHERE candidate_generation_id = ? AND candidate_id = ?",
            (candidate_generation_id, candidate_id),
        )
    }


def _assertion_evidence_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    source_candidate_id: str,
    target_candidate_id: str,
    relation_kind: str,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT claim_sources.evidence_id
            FROM knowledge_document_relationship_claims AS relationship_claims
            JOIN knowledge_document_candidate_claim_sources AS claim_sources
              ON claim_sources.candidate_id = relationship_claims.support_candidate_id
             AND claim_sources.claim_ordinal = relationship_claims.claim_ordinal
            WHERE relationship_claims.document_id = ?
              AND relationship_claims.source_candidate_id = ?
              AND relationship_claims.target_candidate_id = ?
              AND relationship_claims.relation_kind = ?
            """,
            (document_id, source_candidate_id, target_candidate_id, relation_kind),
        )
    }


def _applicability_values(value: object) -> list[object]:
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("Semantic relationship applicability is invalid JSON.") from error
    if not isinstance(decoded, list):
        raise ValueError("Semantic relationship applicability must be a JSON array.")
    return decoded


def _canonical_applicability(values: list[object]) -> str:
    unique = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")): value
        for value in values
    }
    return json.dumps(
        [unique[key] for key in sorted(unique)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
