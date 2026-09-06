from __future__ import annotations

import json

from openkb.retrieval.trace import retrieval_trace_from_json


def _known_trace() -> dict[str, object]:
    return {
        "semantic_structure_state": "known",
        "question_goal": "Compare the versions",
        "question_facets": [
            {
                "facet_id": "changes",
                "label": "Changes",
                "description": "Version-specific changes",
                "importance": "required",
            }
        ],
        "question_facet_plan_digest": "facet-plan-digest",
        "query_planning_prompt_contract_digest": "prompt-digest",
        "query_planning_execution_profile_json": "{}",
        "query_planning_execution_profile_digest": "profile-digest",
        "facet_coverage": [
            {
                "facet_id": "changes",
                "state": "covered",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "coverage_gate_state": "covered",
    }


def test_current_trace_accepts_one_complete_known_semantic_branch() -> None:
    trace = retrieval_trace_from_json(json.dumps(_known_trace()))

    assert trace.semantic_structure_state == "known"
    assert [facet.facet_id for facet in trace.question_facets] == ["changes"]
    assert [coverage.state for coverage in trace.facet_coverage] == ["covered"]


def test_current_trace_downgrades_the_whole_invalid_semantic_branch() -> None:
    payload = _known_trace()
    facets = payload["question_facets"]
    assert isinstance(facets, list)
    assert isinstance(facets[0], dict)
    facets[0]["importance"] = 1

    trace = retrieval_trace_from_json(json.dumps(payload))

    assert trace.semantic_structure_state == "unknown"
    assert trace.question_goal == ""
    assert trace.question_facets == ()
    assert trace.question_facet_plan_digest == ""
    assert trace.facet_coverage == ()
    assert trace.coverage_gate_state == "unknown"
