"""Behavioral matrix for the Knowledge Graph extraction boundary."""

from __future__ import annotations

import json

import pytest

from openkb import desktop_knowledge_graph_interpretation
from openkb.desktop_knowledge_graph_interpretation import (
    GraphEvidence,
    GraphExtractionBoundary,
)


def test_boundary_uses_the_shared_structured_output_transport_normalizer(monkeypatch) -> None:
    calls: list[str] = []

    def normalize(content: str) -> str:
        calls.append(content)
        return '{"nodes":[],"edges":[]}'

    monkeypatch.setattr(
        desktop_knowledge_graph_interpretation,
        "normalize_structured_output",
        normalize,
    )

    interpretation = GraphExtractionBoundary.interpret(
        "provider transport wrapper",
        (),
    )

    assert calls == ["provider transport wrapper"]
    assert interpretation.lifecycle == "completed_empty"


def test_canonical_candidates_are_source_anchored_as_a_full_graph() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "gateway",
                        "evidence_id": "evidence-1",
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "atlas",
                        "target_id": "gateway",
                        "type": "USES",
                        "support_quote": "Atlas uses Gateway.",
                    }
                ],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "full"
    assert interpretation.issues == ()
    assert interpretation.counts.retained == 3
    assert interpretation.counts.weakened == 0
    assert interpretation.counts.rejected == 0
    assert interpretation.payload is not None
    assert [
        (node.local_id, node.support_start, node.support_end, node.verification_state)
        for node in interpretation.payload.nodes
    ] == [
        ("atlas", 0, 5, "source_anchored"),
        ("gateway", 11, 18, "source_anchored"),
    ]
    [edge] = interpretation.payload.edges
    assert edge.edge_type == "USES"
    assert edge.relation_label == "USES"
    assert (edge.support_start, edge.support_end) == (0, 19)
    assert edge.verification_state == "source_anchored"


def test_supported_unknown_relationship_is_retained_as_ambiguous_related_to() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "cloudyi",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Cloudyi",
                        "support_quote": "Cloudyi",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "atlas",
                        "target_id": "cloudyi",
                        "type": "OPERATED_BY",
                        "support_quote": "Atlas is operated by Cloudyi.",
                    }
                ],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas is operated by Cloudyi."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 3
    assert interpretation.counts.weakened == 1
    assert interpretation.counts.rejected == 0
    assert [
        (issue.code, issue.path, issue.disposition, issue.failure_class)
        for issue in interpretation.issues
    ] == [("unsupported_relationship", "edges[0].type", "weakened", "semantic")]
    assert interpretation.payload is not None
    [edge] = interpretation.payload.edges
    assert edge.edge_type == "RELATED_TO"
    assert edge.relation_label == "OPERATED_BY"
    assert edge.verification_state == "ambiguous"


def test_invalid_edge_is_rejected_without_discarding_safe_candidates() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "gateway",
                        "evidence_id": "evidence-1",
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "atlas",
                        "target_id": "gateway",
                        "type": "USES",
                        "support_quote": "Atlas uses Gateway.",
                    },
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "atlas",
                        "target_id": "atlas",
                        "type": "RELATED_TO",
                        "support_quote": "Atlas",
                    },
                ],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 3
    assert interpretation.counts.weakened == 0
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path, issue.disposition) for issue in interpretation.issues] == [
        ("self_edge", "edges[1]", "rejected")
    ]
    assert interpretation.payload is not None
    assert len(interpretation.payload.nodes) == 2
    assert [edge.edge_type for edge in interpretation.payload.edges] == ["USES"]


def test_all_rejected_nonempty_response_fails_instead_of_publishing_empty() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "invented",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Invented",
                        "support_quote": "Not in the source",
                    }
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.quality is None
    assert interpretation.payload is None
    assert interpretation.repairable
    assert interpretation.counts.retained == 0
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path, issue.disposition) for issue in interpretation.issues] == [
        ("support_quote_not_found", "nodes[0].support_quote", "rejected")
    ]
    assert interpretation.failure_signature is not None
    assert "Not in the source" not in interpretation.failure_signature


def test_part_of_requires_durable_entity_endpoints() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "step-one",
                        "evidence_id": "evidence-1",
                        "type": "claim",
                        "label": "Stop services",
                        "support_quote": "Stop services",
                    },
                    {
                        "id": "step-two",
                        "evidence_id": "evidence-1",
                        "type": "claim",
                        "label": "Back up the database",
                        "support_quote": "Back up the database",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "step-one",
                        "target_id": "step-two",
                        "type": "PART_OF",
                        "support_quote": "Stop services, then back up the database.",
                    }
                ],
            }
        ),
        (
            GraphEvidence(
                "evidence-1",
                "Stop services. Back up the database. Stop services, then back up the database.",
            ),
        ),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.payload is not None
    assert interpretation.payload.edges == ()
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [
        ("invalid_relation_endpoints", "edges[0]")
    ]


def test_unusable_top_level_content_is_a_precise_repairable_failure() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        "not-json",
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.payload is None
    assert interpretation.repairable
    assert interpretation.counts.retained == 0
    assert interpretation.counts.rejected == 0
    assert [(issue.code, issue.path, issue.disposition) for issue in interpretation.issues] == [
        ("invalid_json", "$", "fatal")
    ]


def test_truncated_json_has_the_same_content_free_structural_signature() -> None:
    evidence = (GraphEvidence("evidence-1", "Atlas uses Gateway."),)

    malformed = GraphExtractionBoundary.interpret("not-json", evidence)
    truncated = GraphExtractionBoundary.interpret('{"nodes": [], "edges": [', evidence)

    assert truncated.lifecycle == "failed"
    assert truncated.repairable
    assert [(issue.code, issue.path) for issue in truncated.issues] == [("invalid_json", "$")]
    assert truncated.failure_signature == malformed.failure_signature


def test_unambiguous_case_and_separator_alias_is_lossless() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "team",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Team",
                        "support_quote": "team",
                    },
                ],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "atlas",
                        "target_id": "team",
                        "type": "created-by",
                        "support_quote": "Atlas was created by the team.",
                    }
                ],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas was created by the team."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "full"
    assert interpretation.issues == ()
    assert interpretation.payload is not None
    [edge] = interpretation.payload.edges
    assert edge.edge_type == "CREATED_BY"
    assert edge.relation_label == "created-by"
    assert edge.verification_state == "source_anchored"


def test_duplicate_node_identity_rejects_only_the_duplicate_candidate() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 1
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [
        ("duplicate_node_id", "nodes[1].id")
    ]
    assert interpretation.payload is not None
    assert [node.label for node in interpretation.payload.nodes] == ["Atlas"]


def test_model_confidence_is_ignored_and_reported_as_an_unexpected_field() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                        "confidence": 0.99,
                    }
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 1
    assert interpretation.counts.weakened == 1
    assert [(issue.code, issue.path, issue.disposition) for issue in interpretation.issues] == [
        ("unexpected_field", "nodes[0].*", "weakened")
    ]
    assert interpretation.payload is not None
    assert interpretation.payload.nodes[0].verification_state == "source_anchored"


def test_per_evidence_node_budget_rejects_only_excess_candidates() -> None:
    text = " ".join(f"Node{index}" for index in range(13))
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": f"node-{index}",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": f"Node{index}",
                        "support_quote": f"Node{index}",
                    }
                    for index in range(13)
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", text),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 12
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [
        ("node_budget_exceeded", "nodes[12]")
    ]


def test_explicit_empty_candidates_publish_a_full_empty_generation() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps({"nodes": [], "edges": []}),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "completed_empty"
    assert interpretation.quality == "full"
    assert interpretation.payload is not None
    assert interpretation.payload.nodes == ()
    assert interpretation.payload.edges == ()
    assert interpretation.issues == ()
    assert not interpretation.repairable


def test_empty_candidates_with_an_extra_top_level_field_fail_for_one_repair() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps({"nodes": [], "edges": [], "summary": "No graph found."}),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.quality is None
    assert interpretation.payload is None
    assert interpretation.repairable
    assert [(issue.code, issue.path, issue.disposition) for issue in interpretation.issues] == [
        ("unexpected_field", "$.*", "fatal")
    ]


@pytest.mark.parametrize(
    ("payload", "code", "path"),
    [
        ({"nodes": [None], "edges": []}, "invalid_candidate", "nodes[0]"),
        (
            {
                "nodes": [
                    {
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    }
                ],
                "edges": [],
            },
            "invalid_scalar",
            "nodes[0].id",
        ),
        (
            {
                "nodes": [],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "target_id": "atlas",
                        "type": "USES",
                        "support_quote": "Atlas",
                    }
                ],
            },
            "invalid_scalar",
            "edges[0].source_id",
        ),
    ],
)
def test_all_rejected_candidate_shape_errors_are_repairable(
    payload: dict[str, object],
    code: str,
    path: str,
) -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(payload),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.payload is None
    assert interpretation.repairable
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [(code, path)]


def test_deeply_nested_json_is_a_bounded_repairable_failure() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        "[" * 1_100 + "0" + "]" * 1_100,
        (),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.payload is None
    assert interpretation.repairable
    assert [(issue.code, issue.path, issue.failure_class) for issue in interpretation.issues] == [
        ("json_nesting_exceeded", "$", "shape")
    ]


def test_oversized_json_integer_is_a_bounded_repairable_failure() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        '{"nodes":[],"edges":[],"numeric_value":' + "1" * 5_000 + "}",
        (),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.payload is None
    assert interpretation.repairable
    assert [(issue.code, issue.path, issue.failure_class) for issue in interpretation.issues] == [
        ("json_value_limit_exceeded", "$", "budget")
    ]


@pytest.mark.parametrize(
    ("content", "code", "path"),
    [
        (json.dumps([]), "top_level_not_object", "$"),
        (json.dumps({"nodes": []}), "missing_edges_array", "$.edges"),
        (
            json.dumps({"nodes": {}, "edges": []}),
            "invalid_nodes_array",
            "$.nodes",
        ),
    ],
)
def test_unusable_top_level_shapes_are_precise_repairable_failures(
    content: str,
    code: str,
    path: str,
) -> None:
    interpretation = GraphExtractionBoundary.interpret(
        content,
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.repairable
    assert [(issue.code, issue.path, issue.disposition) for issue in interpretation.issues] == [
        (code, path, "fatal")
    ]


@pytest.mark.parametrize(
    ("edge_overrides", "code", "path"),
    [
        ({"source_id": "missing"}, "unknown_source", "edges[0].source_id"),
        ({"target_id": "missing"}, "unknown_target", "edges[0].target_id"),
        ({"evidence_id": "evidence-2"}, "cross_evidence_edge", "edges[0]"),
    ],
)
def test_edge_endpoint_and_evidence_failures_are_path_specific(
    edge_overrides: dict[str, str],
    code: str,
    path: str,
) -> None:
    edge = {
        "evidence_id": "evidence-1",
        "source_id": "atlas",
        "target_id": "gateway",
        "type": "USES",
        "support_quote": "Atlas uses Gateway.",
        **edge_overrides,
    }
    if edge["evidence_id"] == "evidence-2":
        edge["support_quote"] = "A separate sentence."
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "gateway",
                        "evidence_id": "evidence-1",
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [edge],
            }
        ),
        (
            GraphEvidence("evidence-1", "Atlas uses Gateway."),
            GraphEvidence("evidence-2", "A separate sentence."),
        ),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 2
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [(code, path)]
    assert interpretation.payload is not None
    assert interpretation.payload.edges == ()


@pytest.mark.parametrize(
    ("candidate_overrides", "code", "path"),
    [
        ({"evidence_id": "missing"}, "unknown_evidence", "nodes[0].evidence_id"),
        ({"support_quote": None}, "missing_support_quote", "nodes[0].support_quote"),
        (
            {"support_quote": "Invented source text"},
            "support_quote_not_found",
            "nodes[0].support_quote",
        ),
    ],
)
def test_node_evidence_failures_are_path_specific_and_repairable(
    candidate_overrides: dict[str, str | None],
    code: str,
    path: str,
) -> None:
    candidate: dict[str, str | None] = {
        "id": "atlas",
        "evidence_id": "evidence-1",
        "type": "entity",
        "label": "Atlas",
        "support_quote": "Atlas",
        **candidate_overrides,
    }

    interpretation = GraphExtractionBoundary.interpret(
        json.dumps({"nodes": [candidate], "edges": []}),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.repairable
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [(code, path)]


@pytest.mark.parametrize(
    ("edge_overrides", "code", "path"),
    [
        ({"evidence_id": "missing"}, "unknown_evidence", "edges[0].evidence_id"),
        ({"support_quote": None}, "missing_support_quote", "edges[0].support_quote"),
        (
            {"support_quote": "Invented source text"},
            "support_quote_not_found",
            "edges[0].support_quote",
        ),
    ],
)
def test_edge_evidence_failures_reject_only_the_edge(
    edge_overrides: dict[str, str | None],
    code: str,
    path: str,
) -> None:
    edge: dict[str, str | None] = {
        "evidence_id": "evidence-1",
        "source_id": "atlas",
        "target_id": "gateway",
        "type": "USES",
        "support_quote": "Atlas uses Gateway.",
        **edge_overrides,
    }
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    },
                    {
                        "id": "gateway",
                        "evidence_id": "evidence-1",
                        "type": "concept",
                        "label": "Gateway",
                        "support_quote": "Gateway",
                    },
                ],
                "edges": [edge],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas uses Gateway."),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == 2
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [(code, path)]
    assert interpretation.payload is not None
    assert interpretation.payload.edges == ()


def test_per_evidence_edge_budget_rejects_only_excess_candidates() -> None:
    node_count = 12
    text = " ".join(f"Node{index}" for index in range(node_count))
    nodes = [
        {
            "id": f"node-{index}",
            "evidence_id": "evidence-1",
            "type": "entity",
            "label": f"Node{index}",
            "support_quote": f"Node{index}",
        }
        for index in range(node_count)
    ]
    edges = [
        {
            "evidence_id": "evidence-1",
            "source_id": f"node-{index % node_count}",
            "target_id": f"node-{(index + 1) % node_count}",
            "type": "RELATED_TO",
            "support_quote": text,
        }
        for index in range(17)
    ]

    interpretation = GraphExtractionBoundary.interpret(
        json.dumps({"nodes": nodes, "edges": edges}),
        (GraphEvidence("evidence-1", text),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert interpretation.counts.retained == node_count + 16
    assert interpretation.counts.rejected == 1
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [
        ("edge_budget_exceeded", "edges[16]")
    ]


@pytest.mark.parametrize(
    ("field", "value", "code", "path"),
    [
        ("id", "x" * 81, "scalar_too_long", "nodes[0].id"),
        ("label", "x" * 321, "scalar_too_long", "nodes[0].label"),
        (
            "support_quote",
            "x" * 1_201,
            "scalar_too_long",
            "nodes[0].support_quote",
        ),
    ],
)
def test_oversized_node_scalars_are_bounded_and_repairable(
    field: str,
    value: str,
    code: str,
    path: str,
) -> None:
    candidate = {
        "id": "atlas",
        "evidence_id": "evidence-1",
        "type": "entity",
        "label": "Atlas",
        "support_quote": "Atlas",
    }
    candidate[field] = value
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps({"nodes": [candidate], "edges": []}),
        (GraphEvidence("evidence-1", value if field == "support_quote" else "Atlas"),),
    )

    assert interpretation.lifecycle == "failed"
    assert interpretation.repairable
    assert [(issue.code, issue.path) for issue in interpretation.issues] == [(code, path)]


def test_unknown_relationship_does_not_consume_repair_budget() -> None:
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": "Atlas",
                    }
                ],
                "edges": [
                    {
                        "evidence_id": "evidence-1",
                        "source_id": "atlas",
                        "target_id": "atlas",
                        "type": "OPERATED_BY",
                        "support_quote": "Atlas",
                    }
                ],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas"),),
    )

    assert interpretation.lifecycle == "completed"
    assert interpretation.quality == "degraded"
    assert not interpretation.repairable
