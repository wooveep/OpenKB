"""Operation-owned repair eligibility at the structured-output boundary."""

from __future__ import annotations

import json

import pytest

from openkb.desktop_knowledge_analysis import parse_knowledge_analysis
from openkb.desktop_model_gateway import (
    DesktopModelOutputObservations,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
    structured_output_repair_contract_digest,
)


class _RepairableError(ValueError):
    pass


class _LocalDispositionError(ValueError):
    pass


def test_explicit_output_limit_is_not_treated_as_repairable_json() -> None:
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        return DesktopModelResult(
            "truncated-call",
            '{"terms":["incomplete',
            1,
            observations=DesktopModelOutputObservations(
                finish_reason="length",
                final_content_observed=True,
                final_chunk_count=1,
                final_character_count=21,
                output_limit_reached=True,
            ),
        )

    with pytest.raises(DesktopStructuredOutputInvalidError) as captured:
        run_structured_output(
            operation="query_planning",
            document_name="question",
            source_material="evidence",
            invoke=invoke,
            validate=lambda _content: (_ for _ in ()).throw(
                AssertionError("Truncated JSON must not reach semantic validation.")
            ),
        )

    assert [request.operation for request in requests] == ["query_planning"]
    assert captured.value.attempt_count == 1
    assert captured.value.final_result is captured.value.initial_result
    assert not captured.value.repair_attempted


def test_operation_can_end_an_unrepairable_structured_failure_without_a_second_call() -> None:
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        return DesktopModelResult("initial-call", "{}", 1)

    with pytest.raises(DesktopStructuredOutputInvalidError) as captured:
        run_structured_output(
            operation="query_planning",
            document_name="question",
            source_material="evidence",
            invoke=invoke,
            validate=lambda _content: (_ for _ in ()).throw(_LocalDispositionError()),
            should_repair=lambda error: isinstance(error, _RepairableError),
        )

    assert [request.operation for request in requests] == ["query_planning"]
    assert captured.value.attempt_count == 1
    assert captured.value.final_result is captured.value.initial_result
    assert not captured.value.repair_attempted
    assert isinstance(captured.value.__cause__, _LocalDispositionError)


def test_operation_can_allow_the_single_repair_for_an_eligible_failure() -> None:
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        content = "{}" if len(requests) == 1 else json.dumps({"terms": ["OpenKB"]})
        return DesktopModelResult(f"call-{len(requests)}", content, 1)

    def validate(content: str) -> list[str]:
        payload = json.loads(content)
        if "terms" not in payload:
            raise _RepairableError("missing_terms at $.terms")
        return list(payload["terms"])

    output = run_structured_output(
        operation="query_planning",
        document_name="question",
        source_material="evidence",
        invoke=invoke,
        validate=validate,
        should_repair=lambda error: isinstance(error, _RepairableError),
    )

    assert output.value == ["OpenKB"]
    assert output.repaired
    assert [request.operation for request in requests] == [
        "query_planning",
        "structured_output_repair",
    ]
    assert requests[1].prompt_contract_digest == structured_output_repair_contract_digest(
        "query_planning"
    )
    repair = json.loads(requests[1].content)
    assert repair["validation_errors"] == ["missing_terms at $.terms"]


def test_knowledge_analysis_repair_receives_all_independent_validation_errors() -> None:
    requests: list[DesktopModelRequest] = []
    valid = {
        "schema_version": "openkb.knowledge-analysis.v2",
        "analysis_scope": "batch",
        "document_description": "Deployment manual table of contents.",
        "document_summary": [],
        "candidates": [],
    }
    invalid = {
        **valid,
        "document_summary": [
            {
                "label": "Key topic",
                "text": "The manual covers deployment topics.",
                "source_evidence_ids": [f"evidence-{index}" for index in range(33)],
            }
        ],
        "candidates": [{} for _index in range(97)],
    }

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        content = json.dumps(invalid if len(requests) == 1 else valid)
        return DesktopModelResult(f"call-{len(requests)}", content, 1)

    output = run_structured_output(
        operation="knowledge_analysis_batch",
        document_name="deployment-manual.docx",
        source_material="table-of-contents evidence",
        invoke=invoke,
        validate=lambda content: parse_knowledge_analysis(content, expected_scope="batch"),
    )

    assert output.repaired
    repair = json.loads(requests[1].content)
    errors = repair["validation_errors"]
    assert len(errors) == 2
    assert any("candidates" in error for error in errors)
    assert any("at most 32 supplied Evidence IDs" in error for error in errors)
    assert any("document_summary[0].source_evidence_ids has 33 items" in error for error in errors)
