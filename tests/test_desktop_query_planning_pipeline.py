"""Public Query Planning seam and Unknown Semantic Structure fallback."""

from __future__ import annotations

import json

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval_planning import build_query_plan


def _evidence(evidence_id: str = "evidence-1") -> DesktopEvidenceRef:
    return DesktopEvidenceRef(
        evidence_id=evidence_id,
        document_id="document-1",
        document_name="History.md",
        section="Context",
        locator={},
        excerpt="The reform changed the balance between the institutions.",
        channels=("lexical",),
    )


def test_seeded_query_planning_returns_dynamic_facets_and_bound_coverage() -> None:
    requests = []

    def respond(request, _timeout_seconds):
        requests.append(request)
        return json.dumps(
            {
                "retrieval_plan": {"terms": ["institutional balance", "reform"]},
                "question_facet_plan": {
                    "goal": "Explain the reform's institutional consequences.",
                    "facets": [
                        {
                            "label": "Power redistribution",
                            "description": "How authority shifted among institutions.",
                            "importance": "required",
                        },
                        {
                            "label": "Contemporary reaction",
                            "description": "How observers responded at the time.",
                            "importance": "supporting",
                        },
                    ],
                },
                "initial_answer_coverage": [
                    {"facet_ordinal": 0, "state": "covered", "evidence_ids": ["evidence-1"]},
                    {"facet_ordinal": 1, "state": "missing", "evidence_ids": []},
                ],
            }
        )

    result = build_query_plan(
        "What changed after the reform?",
        DesktopModelGateway(respond, provider_name="scripted", model_name="analysis"),
        seed_evidence=(_evidence(),),
        conversation_context=(("Which reform?", "The reform described in History.md."),),
    )

    assert [request.operation for request in requests] == ["query_planning"]
    assert json.loads(requests[0].content)["seed_observations"][0]["evidence_id"] == "evidence-1"
    assert result.semantic_structure_state == "known"
    assert result.facet_plan is not None
    assert [facet.label for facet in result.facet_plan.facets] == [
        "Power redistribution",
        "Contemporary reaction",
    ]
    assert result.coverage[0].evidence_ids == ("evidence-1",)
    assert "institutional balance" in result.plan.terms


def test_invalid_semantic_branch_repairs_once_then_returns_unknown_but_keeps_retrieval() -> None:
    operations: list[str] = []

    def respond(request, _timeout_seconds):
        operations.append(request.operation)
        return json.dumps(
            {
                "retrieval_plan": {"terms": ["radiogenic heat"]},
                "question_facet_plan": {"goal": "Explain heat", "facets": []},
                "initial_answer_coverage": [],
            }
        )

    result = build_query_plan(
        "Why is the interior warm?",
        DesktopModelGateway(respond, provider_name="scripted", model_name="analysis"),
        seed_evidence=(_evidence(),),
    )

    assert operations == ["query_planning", "structured_output_repair"]
    assert result.semantic_structure_state == "unknown"
    assert result.facet_plan is None
    assert result.coverage == ()
    assert "radiogenic heat" in result.plan.terms
    assert result.degradations == ("query_semantic_structure_unknown",)


def test_no_model_returns_baseline_terms_and_explicit_unknown_semantics() -> None:
    result = build_query_plan(
        "Pourquoi les saisons changent-elles ?",
        None,
        seed_evidence=(_evidence(),),
    )

    assert result.plan.terms
    assert result.semantic_structure_state == "unknown"
    assert result.facet_plan is None
    assert result.coverage == ()
    assert result.degradations == ("query_planning_unavailable",)
