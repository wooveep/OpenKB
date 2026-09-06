from __future__ import annotations

import json

from openkb.knowledge.pages.page import (
    KnowledgePageClaimSnapshot,
    knowledge_page_claim_snapshot_digest,
    render_knowledge_page,
)
from openkb.knowledge.pages.planner import run_knowledge_page_planning
from openkb.knowledge.pages.planning import parse_knowledge_page_plan
from openkb.models.gateway import DesktopModelResult
from openkb.models.prompt_contracts import prompt_contract_for
from openkb.models.roles import model_lane_for_operation, model_role_for_operation
from openkb.models.semantic_structure_contracts import normalize_dynamic_semantic_text
from openkb.retrieval.query_planning import parse_query_planning_result


def test_semantic_planning_operations_are_provider_neutral_analysis_contracts() -> None:
    query = prompt_contract_for("query_planning")
    page = prompt_contract_for("knowledge_page_planning")

    assert query.operation == "query_planning"
    assert page.operation == "knowledge_page_planning"
    assert query.version == "openkb.prompt.query_planning.v1"
    assert page.version == "openkb.prompt.knowledge_page_planning.v1"
    assert query.structured is True
    assert page.structured is True
    assert model_role_for_operation(query.operation) == "analysis"
    assert model_role_for_operation(page.operation) == "analysis"
    assert model_lane_for_operation(query.operation) == "interactive"
    assert model_lane_for_operation(page.operation) == "background"
    assert "deepseek" not in query.canonical_json().casefold()
    assert "deepseek" not in page.canonical_json().casefold()


def test_dynamic_semantic_text_normalizes_unicode_without_domain_filtering() -> None:
    value = normalize_dynamic_semantic_text(
        "  Cafe\u0301 /etc/openkb https://example.test/a?b=c  ",
        field="facet.label",
        maximum_characters=80,
    )

    assert value == "Café /etc/openkb https://example.test/a?b=c"


def test_query_plan_derives_stable_facet_ids_after_evidence_validation() -> None:
    content = json.dumps(
        {
            "retrieval_plan": {"terms": ["photosynthesis"]},
            "question_facet_plan": {
                "goal": "Explain the energy conversion.",
                "facets": [
                    {
                        "label": "Light reactions",
                        "description": "How captured light becomes chemical energy.",
                        "importance": "required",
                    }
                ],
            },
            "initial_answer_coverage": [
                {
                    "facet_ordinal": 0,
                    "state": "covered",
                    "evidence_ids": ["evidence-light"],
                }
            ],
        }
    )

    first = parse_query_planning_result(
        content,
        question="How does photosynthesis convert light?",
        conversation_context_digest="context-digest",
        seed_evidence_ids=frozenset({"evidence-light"}),
    )
    second = parse_query_planning_result(
        content,
        question="How does photosynthesis convert light?",
        conversation_context_digest="context-digest",
        seed_evidence_ids=frozenset({"evidence-light"}),
    )

    assert first.semantic_structure_state == "known"
    assert first.facet_plan == second.facet_plan
    assert first.facet_plan is not None
    assert first.facet_plan.facets[0].facet_id.startswith("facet-")
    assert first.coverage[0].facet_id == first.facet_plan.facets[0].facet_id
    assert first.coverage[0].evidence_ids == ("evidence-light",)


def test_knowledge_page_plan_uses_dynamic_structure_and_code_derived_ids() -> None:
    content = json.dumps(
        {
            "generation_id": 7,
            "identity_id": "identity-photosynthesis",
            "lead": {
                "presentation": "paragraph",
                "claim_ids": ["claim-definition"],
                "relation_assertion_ids": [],
            },
            "sections": [
                {
                    "title": "Energy conversion",
                    "units": [],
                    "sections": [
                        {
                            "title": "Light-dependent reactions",
                            "units": [
                                {
                                    "presentation": "unordered_list",
                                    "claim_ids": ["claim-light"],
                                    "relation_assertion_ids": ["relation-chloroplast"],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    plan = parse_knowledge_page_plan(
        content,
        expected_generation_id=7,
        expected_identity_id="identity-photosynthesis",
        claim_snapshot_digest="claim-snapshot",
        eligible_claim_ids=("claim-definition", "claim-light"),
        available_relation_assertion_ids=frozenset({"relation-chloroplast"}),
    )

    assert plan.sections[0].title == "Energy conversion"
    assert plan.sections[0].section_id.startswith("section-")
    assert plan.sections[0].sections[0].units[0].unit_id.startswith("unit-")
    assert plan.placed_claim_ids == ("claim-definition", "claim-light")


def test_knowledge_page_renderer_emits_only_planned_claims_and_source_markers() -> None:
    claims = (
        KnowledgePageClaimSnapshot(
            generation_id=7,
            identity_id="identity-photosynthesis",
            candidate_generation_id="candidate-generation",
            candidate_id="candidate-photosynthesis",
            claim_ordinal=0,
            claim_id="claim-definition",
            text="Photosynthesis stores light energy as chemical energy.",
            applicability=(),
            evidence_ids=("evidence-definition",),
        ),
        KnowledgePageClaimSnapshot(
            generation_id=7,
            identity_id="identity-photosynthesis",
            candidate_generation_id="candidate-generation",
            candidate_id="candidate-photosynthesis",
            claim_ordinal=1,
            claim_id="claim-light",
            text="Light-dependent reactions produce ATP and NADPH.",
            applicability=(("location", "thylakoid membrane"),),
            evidence_ids=("evidence-light",),
        ),
    )
    plan = parse_knowledge_page_plan(
        json.dumps(
            {
                "generation_id": 7,
                "identity_id": "identity-photosynthesis",
                "lead": {
                    "presentation": "paragraph",
                    "claim_ids": ["claim-definition"],
                    "relation_assertion_ids": [],
                },
                "sections": [
                    {
                        "title": "Energy conversion",
                        "units": [],
                        "sections": [
                            {
                                "title": "Light-dependent reactions",
                                "units": [
                                    {
                                        "presentation": "unordered_list",
                                        "claim_ids": ["claim-light"],
                                        "relation_assertion_ids": [],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        expected_generation_id=7,
        expected_identity_id="identity-photosynthesis",
        claim_snapshot_digest=knowledge_page_claim_snapshot_digest(claims),
        eligible_claim_ids=("claim-definition", "claim-light"),
    )

    rendered = render_knowledge_page(plan, claims, relations=())

    assert "## Energy conversion" in rendered.markdown
    assert "### Light-dependent reactions" in rendered.markdown
    assert "Identity and role" not in rendered.markdown
    assert "- Light-dependent reactions produce ATP and NADPH." in rendered.markdown
    assert "location: thylakoid membrane" in rendered.markdown
    assert "[^src-" in rendered.markdown
    assert rendered.factual_unit_count == 2
    assert rendered.evidence_ids == ("evidence-definition", "evidence-light")


def test_knowledge_page_planning_repairs_only_the_invalid_identity_result() -> None:
    claims = (
        KnowledgePageClaimSnapshot(
            generation_id=7,
            identity_id="identity-photosynthesis",
            candidate_generation_id="candidate-generation",
            candidate_id="candidate-photosynthesis",
            claim_ordinal=0,
            claim_id="claim-definition",
            text="Photosynthesis stores light energy as chemical energy.",
            applicability=(),
            evidence_ids=("evidence-definition",),
        ),
    )
    requests = []

    def invoke(request):
        requests.append(request)
        if request.operation == "knowledge_page_planning":
            return DesktopModelResult("initial", "{}", 1)
        assert request.operation == "structured_output_repair"
        return DesktopModelResult(
            "repair",
            json.dumps(
                {
                    "generation_id": 7,
                    "identity_id": "identity-photosynthesis",
                    "lead": {
                        "presentation": "paragraph",
                        "claim_ids": ["claim-definition"],
                        "relation_assertion_ids": [],
                    },
                    "sections": [],
                }
            ),
            1,
        )

    run = run_knowledge_page_planning(
        document_name="Photosynthesis",
        generation_id=7,
        identity_id="identity-photosynthesis",
        title="Photosynthesis",
        claims=claims,
        relations=(),
        knowledge_language="en",
        invoke=invoke,
    )

    assert run.repaired is True
    assert run.plan.placed_claim_ids == ("claim-definition",)
    assert [request.operation for request in requests] == [
        "knowledge_page_planning",
        "structured_output_repair",
    ]
    source_payload = json.loads(requests[0].content)
    assert "role" not in source_payload["claims"][0]
    assert source_payload["claim_snapshot_digest"] == run.plan.claim_snapshot_digest
