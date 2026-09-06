"""Materialize evidence-bound, model-labelled relations for one Knowledge generation."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from openkb.desktop_semantic_structure_contracts import (
    SEMANTIC_STRUCTURE_LIMITS,
    normalize_dynamic_semantic_text,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgeGenerationRelationship:
    """One directed display assertion; traversal may treat it as adjacency."""

    assertion_id: str
    source_identity_id: str
    target_identity_id: str
    source_item_key: str
    target_item_key: str
    target_kind: str
    target_title: str
    label: str
    source_evidence_ids: tuple[str, ...]
    target_evidence_ids: tuple[str, ...]
    assertion_evidence_ids: tuple[str, ...]
    applicability_json: str

    @property
    def relation_kind(self) -> str:
        """Expose the dynamic label to catalog projections using their generic field name."""
        return self.label

    @property
    def provenance(self) -> str:
        return "semantic_relation_analysis"


@dataclass
class _MaterializedRelation:
    source_identity_id: str
    target_identity_id: str
    label: str
    normalized_label: str
    applicability: list[object]
    evidence_ids: set[str]


def rebuild_generation_relationships_in(connection: sqlite3.Connection, generation_id: int) -> None:
    """Map validated document assertions through canonical Knowledge Identities."""
    connection.execute(
        "DELETE FROM knowledge_generation_relation_sources WHERE generation_id = ?",
        (generation_id,),
    )
    connection.execute(
        "DELETE FROM knowledge_generation_relation_assertions WHERE generation_id = ?",
        (generation_id,),
    )
    item_sources = _generation_item_sources_in(connection, generation_id)
    relations: dict[tuple[str, str, str], _MaterializedRelation] = {}
    rows = connection.execute(
        """
        SELECT document_relations.document_id, document_relations.assertion_id,
            document_relations.source_candidate_id,
            document_relations.target_candidate_id,
            document_relations.label, document_relations.normalized_label,
            document_relations.applicability_json,
            source_mappings.identity_id, source_items.item_key,
            target_mappings.identity_id, target_items.item_key,
            graph_inputs.candidate_generation_id
        FROM knowledge_document_relation_assertions AS document_relations
        JOIN knowledge_generation_graph_inputs AS graph_inputs
          ON graph_inputs.generation_id = ?
         AND graph_inputs.document_id = document_relations.document_id
         AND graph_inputs.candidate_generation_id =
             document_relations.candidate_generation_id
         AND graph_inputs.result_id = document_relations.graph_result_id
        JOIN knowledge_generation_identity_mappings AS source_mappings
          ON source_mappings.generation_id = graph_inputs.generation_id
         AND source_mappings.candidate_generation_id = graph_inputs.candidate_generation_id
         AND source_mappings.candidate_id = document_relations.source_candidate_id
        JOIN knowledge_generation_items AS source_items
          ON source_items.generation_id = graph_inputs.generation_id
         AND source_items.identity_id = source_mappings.identity_id
        JOIN knowledge_generation_identity_mappings AS target_mappings
          ON target_mappings.generation_id = graph_inputs.generation_id
         AND target_mappings.candidate_generation_id = graph_inputs.candidate_generation_id
         AND target_mappings.candidate_id = document_relations.target_candidate_id
        JOIN knowledge_generation_items AS target_items
          ON target_items.generation_id = graph_inputs.generation_id
         AND target_items.identity_id = target_mappings.identity_id
        ORDER BY document_relations.document_id, document_relations.assertion_id
        """,
        (generation_id,),
    ).fetchall()
    for row in rows:
        source_identity_id = str(row[7])
        target_identity_id = str(row[9])
        if source_identity_id == target_identity_id:
            continue
        try:
            label = normalize_dynamic_semantic_text(
                row[4],
                field="relation label",
                maximum_characters=SEMANTIC_STRUCTURE_LIMITS.max_label_characters,
            )
            normalized_label = _normalized_label(label)
            if normalized_label != str(row[5]):
                raise ValueError("Stored normalized relation label does not match.")
            applicability = _applicability_values(row[6])
        except ValueError:
            logger.warning(
                "semantic_relation_skipped reason=invalid_stored_relation generation_id=%d",
                generation_id,
            )
            continue
        source_item_key = str(row[8])
        target_item_key = str(row[10])
        candidate_generation_id = str(row[11])
        source_evidence = _candidate_evidence_in(
            connection, candidate_generation_id, str(row[2])
        ) & item_sources.get(source_item_key, set())
        target_evidence = _candidate_evidence_in(
            connection, candidate_generation_id, str(row[3])
        ) & item_sources.get(target_item_key, set())
        assertion_evidence = _document_assertion_evidence_in(
            connection,
            document_id=str(row[0]),
            assertion_id=str(row[1]),
        ) & (source_evidence | target_evidence)
        if not source_evidence or not target_evidence or not assertion_evidence:
            continue
        key = (source_identity_id, target_identity_id, normalized_label)
        relation = relations.get(key)
        if relation is None:
            relation = _MaterializedRelation(
                source_identity_id=source_identity_id,
                target_identity_id=target_identity_id,
                label=label,
                normalized_label=normalized_label,
                applicability=[],
                evidence_ids=set(),
            )
            relations[key] = relation
        relation.applicability.extend(applicability)
        relation.evidence_ids.update(assertion_evidence)

    for relation in relations.values():
        assertion_id = _generation_assertion_id(generation_id, relation)
        connection.execute(
            """
            INSERT INTO knowledge_generation_relation_assertions (
                generation_id, assertion_id, source_identity_id, target_identity_id,
                label, normalized_label, applicability_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                assertion_id,
                relation.source_identity_id,
                relation.target_identity_id,
                relation.label,
                relation.normalized_label,
                _canonical_applicability(relation.applicability),
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_generation_relation_sources "
            "(generation_id, assertion_id, evidence_id) VALUES (?, ?, ?)",
            (
                (generation_id, assertion_id, evidence_id)
                for evidence_id in sorted(relation.evidence_ids)
            ),
        )


def generation_relationships_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[KnowledgeGenerationRelationship, ...]:
    """Read complete directed assertions with endpoint and assertion Evidence."""
    rows = connection.execute(
        """
        SELECT assertions.assertion_id, assertions.source_identity_id,
            source_items.item_key, assertions.target_identity_id,
            target_items.item_key, target_items.kind, target_items.title,
            assertions.label, assertions.applicability_json, sources.evidence_id
        FROM knowledge_generation_relation_assertions AS assertions
        JOIN knowledge_generation_items AS source_items
          ON source_items.generation_id = assertions.generation_id
         AND source_items.identity_id = assertions.source_identity_id
        JOIN knowledge_generation_items AS target_items
          ON target_items.generation_id = assertions.generation_id
         AND target_items.identity_id = assertions.target_identity_id
        JOIN knowledge_generation_relation_sources AS sources
          ON sources.generation_id = assertions.generation_id
         AND sources.assertion_id = assertions.assertion_id
        WHERE assertions.generation_id = ?
        ORDER BY assertions.assertion_id, sources.evidence_id
        """,
        (generation_id,),
    ).fetchall()
    item_sources = _generation_item_sources_in(connection, generation_id)
    grouped: defaultdict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[0])].append(row)
    relationships: list[KnowledgeGenerationRelationship] = []
    for assertion_id, values in grouped.items():
        source_evidence = tuple(sorted(item_sources.get(str(values[0][2]), set())))
        target_evidence = tuple(sorted(item_sources.get(str(values[0][4]), set())))
        assertion_evidence = tuple(dict.fromkeys(str(row[9]) for row in values))
        if not source_evidence or not target_evidence or not assertion_evidence:
            continue
        relationships.append(
            KnowledgeGenerationRelationship(
                assertion_id=assertion_id,
                source_identity_id=str(values[0][1]),
                source_item_key=str(values[0][2]),
                target_identity_id=str(values[0][3]),
                target_item_key=str(values[0][4]),
                target_kind=str(values[0][5]),
                target_title=str(values[0][6]),
                label=str(values[0][7]),
                applicability_json=str(values[0][8]),
                source_evidence_ids=source_evidence,
                target_evidence_ids=target_evidence,
                assertion_evidence_ids=assertion_evidence,
            )
        )
    return tuple(relationships)


def generation_relationship_issues_in(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[str, ...]:
    """Report only structural, lineage, and Evidence-binding integrity failures."""
    assertions = connection.execute(
        """
        SELECT assertion_id, source_identity_id, target_identity_id,
            label, normalized_label
        FROM knowledge_generation_relation_assertions
        WHERE generation_id = ? ORDER BY assertion_id
        """,
        (generation_id,),
    ).fetchall()
    item_sources = _generation_item_sources_by_identity_in(connection, generation_id)
    available_evidence = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT occurrences.evidence_id
            FROM evidence_occurrences AS occurrences
            JOIN source_documents AS documents
              ON documents.document_id = occurrences.document_id
            WHERE documents.availability = 'available'
            """
        )
    }
    issues: list[str] = []
    for row in assertions:
        assertion_id = str(row[0])
        source_identity_id = str(row[1])
        target_identity_id = str(row[2])
        source_evidence = item_sources.get(source_identity_id)
        target_evidence = item_sources.get(target_identity_id)
        if not source_evidence or not target_evidence or source_identity_id == target_identity_id:
            issues.append("invalid_relationship_endpoint")
        try:
            label = normalize_dynamic_semantic_text(
                row[3],
                field="relation label",
                maximum_characters=SEMANTIC_STRUCTURE_LIMITS.max_label_characters,
            )
        except ValueError:
            issues.append("invalid_relationship_label")
            continue
        if _normalized_label(label) != str(row[4]):
            issues.append("invalid_relationship_label")
        expected_id = _generation_assertion_id_from_parts(
            generation_id,
            source_identity_id,
            target_identity_id,
            _normalized_label(label),
        )
        if assertion_id != expected_id:
            issues.append("unstable_relationship_id")
        evidence = {
            str(value[0])
            for value in connection.execute(
                "SELECT evidence_id FROM knowledge_generation_relation_sources "
                "WHERE generation_id = ? AND assertion_id = ?",
                (generation_id, assertion_id),
            )
        }
        if not evidence:
            issues.append("incomplete_relationship")
        elif not evidence <= available_evidence or not evidence <= (
            (source_evidence or set()) | (target_evidence or set())
        ):
            issues.append("invalid_relationship_source")
    return tuple(dict.fromkeys(issues))


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


def _generation_item_sources_by_identity_in(
    connection: sqlite3.Connection, generation_id: int
) -> dict[str, set[str]]:
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT items.identity_id, sources.evidence_id
        FROM knowledge_generation_items AS items
        JOIN knowledge_generation_item_sources AS sources
          ON sources.generation_id = items.generation_id
         AND sources.item_key = items.item_key
        WHERE items.generation_id = ? AND items.identity_id IS NOT NULL
        """,
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


def _document_assertion_evidence_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    assertion_id: str,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT evidence_id FROM knowledge_document_relation_sources "
            "WHERE document_id = ? AND assertion_id = ?",
            (document_id, assertion_id),
        )
    }


def _normalized_label(label: str) -> str:
    return " ".join(label.casefold().split())


def _generation_assertion_id(generation_id: int, relation: _MaterializedRelation) -> str:
    return _generation_assertion_id_from_parts(
        generation_id,
        relation.source_identity_id,
        relation.target_identity_id,
        relation.normalized_label,
    )


def _generation_assertion_id_from_parts(
    generation_id: int,
    source_identity_id: str,
    target_identity_id: str,
    normalized_label: str,
) -> str:
    material = "\x1f".join(
        (str(generation_id), source_identity_id, target_identity_id, normalized_label)
    )
    return f"relation:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


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
