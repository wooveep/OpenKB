"""Contract checks for model-labelled, evidence-bound Knowledge relations."""

from __future__ import annotations

import json
import sqlite3

from openkb.knowledge.graph.semantic_graph import (
    SemanticGraphCandidate,
    SemanticGraphClaim,
    SemanticGraphDocument,
    SemanticRelationBoundary,
    merge_semantic_relation_interpretations,
    plan_semantic_relation_batches,
)
from openkb.knowledge.graph.semantic_graph_contract import semantic_relation_output_schema
from openkb.knowledge.pages.relationships import (
    generation_relationship_issues_in,
    generation_relationships_in,
    rebuild_generation_relationships_in,
)


def _claim(
    candidate_id: str,
    ordinal: int,
    text: str,
    *,
    evidence_id: str | None = None,
) -> SemanticGraphClaim:
    return SemanticGraphClaim(
        candidate_id=candidate_id,
        claim_ordinal=ordinal,
        text=text,
        applicability_json="[]",
        evidence_ids=(evidence_id or f"evidence-{candidate_id}-{ordinal}",),
    )


def _candidate(
    candidate_id: str,
    kind: str,
    title: str,
    *claims: SemanticGraphClaim,
    identity_labels: tuple[str, ...] = (),
) -> SemanticGraphCandidate:
    return SemanticGraphCandidate(
        candidate_id=candidate_id,
        kind=kind,
        title=title,
        aliases=(),
        identity_labels=identity_labels,
        claims=claims,
    )


def _document() -> SemanticGraphDocument:
    return SemanticGraphDocument(
        document_id="document-1",
        document_name="mixed-domain.md",
        candidates=(
            _candidate(
                "curie",
                "entity",
                "Marie Curie",
                _claim("curie", 0, "Her research changed the study of radioactivity."),
                identity_labels=("person", "scientist"),
            ),
            _candidate(
                "radioactivity",
                "concept",
                "Radioactivity",
                _claim("radioactivity", 0, "It became a major field of scientific study."),
                identity_labels=("scientific phenomenon",),
            ),
            _candidate(
                "industry",
                "concept",
                "Industrialization",
                _claim("industry", 0, "The transition changed social organization."),
                identity_labels=("historical process",),
            ),
        ),
        candidate_generation_id="candidate-generation-1",
        candidate_generation_digest="digest-1",
    )


def _batch():
    return plan_semantic_relation_batches(_document(), input_budget_tokens=8_000)[0]


def test_relation_schema_leaves_labels_to_the_model() -> None:
    relation = semantic_relation_output_schema()["properties"]["relations"]["items"]

    assert set(relation["properties"]) == {
        "source_candidate_id",
        "target_candidate_id",
        "label",
        "supporting_claims",
    }
    assert "enum" not in relation["properties"]["label"]


def test_dynamic_scientific_historical_and_chinese_labels_are_accepted() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "radioactivity",
                        "label": "奠定研究基础",
                        "supporting_claims": [{"candidate_id": "curie", "claim_ordinal": 0}],
                    },
                    {
                        "source_candidate_id": "industry",
                        "target_candidate_id": "radioactivity",
                        "label": "changed the institutional context for",
                        "supporting_claims": [{"candidate_id": "industry", "claim_ordinal": 0}],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        _batch(),
    )

    assert interpreted.lifecycle == "completed"
    assert [relation.label for relation in interpreted.relations] == [
        "奠定研究基础",
        "changed the institutional context for",
    ]


def test_unknown_references_are_rejected_but_a_valid_sibling_can_be_retained() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "radioactivity",
                        "label": "advanced understanding of",
                        "supporting_claims": [{"candidate_id": "curie", "claim_ordinal": 0}],
                    },
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "invented",
                        "label": "mentioned",
                        "supporting_claims": [{"candidate_id": "curie", "claim_ordinal": 0}],
                    },
                ]
            }
        ),
        _batch(),
        reject_partial=False,
    )

    assert interpreted.quality == "degraded"
    assert [relation.label for relation in interpreted.relations] == ["advanced understanding of"]
    assert [issue.code for issue in interpreted.issues] == ["unknown_target_candidate"]


def test_supporting_claim_must_belong_to_one_of_the_relation_endpoints() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "radioactivity",
                        "label": "advanced understanding of",
                        "supporting_claims": [{"candidate_id": "industry", "claim_ordinal": 0}],
                    }
                ]
            }
        ),
        _batch(),
    )

    assert interpreted.lifecycle == "failed"
    assert [issue.code for issue in interpreted.issues] == ["support_not_endpoint_bound"]


def test_relation_merge_is_exact_on_direction_and_normalized_label() -> None:
    interpreted = SemanticRelationBoundary.interpret(
        json.dumps(
            {
                "relations": [
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "radioactivity",
                        "label": "Influenced",
                        "supporting_claims": [{"candidate_id": "curie", "claim_ordinal": 0}],
                    },
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "radioactivity",
                        "label": "INFLUENCED",
                        "supporting_claims": [
                            {"candidate_id": "radioactivity", "claim_ordinal": 0}
                        ],
                    },
                    {
                        "source_candidate_id": "radioactivity",
                        "target_candidate_id": "curie",
                        "label": "Influenced",
                        "supporting_claims": [
                            {"candidate_id": "radioactivity", "claim_ordinal": 0}
                        ],
                    },
                    {
                        "source_candidate_id": "curie",
                        "target_candidate_id": "radioactivity",
                        "label": "Studied",
                        "supporting_claims": [{"candidate_id": "curie", "claim_ordinal": 0}],
                    },
                ]
            }
        ),
        _batch(),
    )

    assert [
        (relation.source_candidate_id, relation.target_candidate_id, relation.label)
        for relation in interpreted.relations
    ] == [
        ("curie", "radioactivity", "Influenced"),
        ("radioactivity", "curie", "Influenced"),
        ("curie", "radioactivity", "Studied"),
    ]
    assert interpreted.relations[0].assertion_evidence_ids == (
        "evidence-curie-0",
        "evidence-radioactivity-0",
    )


def test_batch_payload_has_no_role_subtype_or_literal_endpoint_filter() -> None:
    payload = json.loads(_batch().source_material)

    assert {candidate["candidate_id"] for candidate in payload["candidate_registry"]} == {
        "curie",
        "radioactivity",
        "industry",
    }
    assert all("identity_labels" in candidate for candidate in payload["candidate_registry"])
    assert all("entity_subtype" not in candidate for candidate in payload["candidate_registry"])
    assert all("role" not in claim for claim in payload["claims"])


def test_batch_interpretations_preserve_reverse_and_distinct_labels() -> None:
    first = SemanticRelationBoundary.interpret(
        '{"relations":[{"source_candidate_id":"curie",'
        '"target_candidate_id":"radioactivity","label":"studied",'
        '"supporting_claims":[{"candidate_id":"curie","claim_ordinal":0}]}]}',
        _batch(),
    )
    second = SemanticRelationBoundary.interpret(
        '{"relations":[{"source_candidate_id":"radioactivity",'
        '"target_candidate_id":"curie","label":"studied by",'
        '"supporting_claims":[{"candidate_id":"radioactivity","claim_ordinal":0}]}]}',
        _batch(),
    )

    merged = merge_semantic_relation_interpretations(_document(), (first, second))

    assert len(merged.relations) == 2


def test_generation_materialization_aggregates_evidence_under_a_stable_assertion_id() -> None:
    connection = sqlite3.connect(":memory:")
    _create_materialization_schema(connection)
    _seed_materialization(connection)

    rebuild_generation_relationships_in(connection, 7)
    first = generation_relationships_in(connection, 7)
    first_id = first[0].assertion_id
    rebuild_generation_relationships_in(connection, 7)
    second = generation_relationships_in(connection, 7)

    assert len(first) == 1
    assert first[0].label == "影响"
    assert first[0].assertion_evidence_ids == ("evidence-1", "evidence-3")
    assert second[0].assertion_id == first_id
    assert generation_relationship_issues_in(connection, 7) == ()


def _create_materialization_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE source_documents(document_id TEXT PRIMARY KEY, availability TEXT);
        CREATE TABLE evidence_occurrences(evidence_id TEXT, document_id TEXT);
        CREATE TABLE knowledge_generation_graph_inputs(
            generation_id INTEGER, document_id TEXT, candidate_generation_id TEXT,
            result_id TEXT
        );
        CREATE TABLE knowledge_generation_identity_mappings(
            generation_id INTEGER, identity_id TEXT, candidate_generation_id TEXT,
            candidate_id TEXT
        );
        CREATE TABLE knowledge_generation_items(
            generation_id INTEGER, identity_id TEXT, item_key TEXT, kind TEXT, title TEXT
        );
        CREATE TABLE knowledge_generation_item_sources(
            generation_id INTEGER, item_key TEXT, evidence_id TEXT
        );
        CREATE TABLE knowledge_candidate_generation_claim_sources(
            candidate_generation_id TEXT, candidate_id TEXT, claim_ordinal INTEGER,
            evidence_id TEXT
        );
        CREATE TABLE knowledge_document_relation_assertions(
            document_id TEXT, candidate_generation_id TEXT, graph_result_id TEXT,
            assertion_id TEXT, source_candidate_id TEXT, target_candidate_id TEXT,
            label TEXT, normalized_label TEXT, applicability_json TEXT
        );
        CREATE TABLE knowledge_document_relation_sources(
            document_id TEXT, assertion_id TEXT, support_candidate_id TEXT,
            claim_ordinal INTEGER, evidence_id TEXT
        );
        CREATE TABLE knowledge_generation_relation_assertions(
            generation_id INTEGER, assertion_id TEXT, source_identity_id TEXT,
            target_identity_id TEXT, label TEXT, normalized_label TEXT,
            applicability_json TEXT
        );
        CREATE TABLE knowledge_generation_relation_sources(
            generation_id INTEGER, assertion_id TEXT, evidence_id TEXT
        );
        """
    )


def _seed_materialization(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO source_documents VALUES (?, 'available')",
        (("document-1",), ("document-2",)),
    )
    connection.executemany(
        "INSERT INTO evidence_occurrences VALUES (?, ?)",
        (
            ("evidence-1", "document-1"),
            ("evidence-2", "document-1"),
            ("evidence-3", "document-2"),
            ("evidence-4", "document-2"),
        ),
    )
    for ordinal, document_id in enumerate(("document-1", "document-2"), start=1):
        candidate_generation_id = f"candidate-generation-{ordinal}"
        connection.execute(
            "INSERT INTO knowledge_generation_graph_inputs VALUES (7, ?, ?, ?)",
            (document_id, candidate_generation_id, f"result-{ordinal}"),
        )
        connection.executemany(
            "INSERT INTO knowledge_generation_identity_mappings VALUES (7, ?, ?, ?)",
            (
                ("identity-source", candidate_generation_id, f"source-{ordinal}"),
                ("identity-target", candidate_generation_id, f"target-{ordinal}"),
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_candidate_generation_claim_sources VALUES (?, ?, 0, ?)",
            (
                (candidate_generation_id, f"source-{ordinal}", f"evidence-{ordinal * 2 - 1}"),
                (candidate_generation_id, f"target-{ordinal}", f"evidence-{ordinal * 2}"),
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_document_relation_assertions "
            "VALUES (?, ?, ?, ?, ?, ?, '影响', '影响', '[]')",
            (
                document_id,
                candidate_generation_id,
                f"result-{ordinal}",
                f"document-assertion-{ordinal}",
                f"source-{ordinal}",
                f"target-{ordinal}",
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_document_relation_sources VALUES (?, ?, ?, 0, ?)",
            (
                document_id,
                f"document-assertion-{ordinal}",
                f"source-{ordinal}",
                f"evidence-{ordinal * 2 - 1}",
            ),
        )
    connection.executemany(
        "INSERT INTO knowledge_generation_items VALUES (7, ?, ?, ?, ?)",
        (
            ("identity-source", "identity-source", "entity", "Source"),
            ("identity-target", "identity-target", "concept", "Target"),
        ),
    )
    connection.executemany(
        "INSERT INTO knowledge_generation_item_sources VALUES (7, ?, ?)",
        (
            ("identity-source", "evidence-1"),
            ("identity-source", "evidence-3"),
            ("identity-target", "evidence-2"),
            ("identity-target", "evidence-4"),
        ),
    )
