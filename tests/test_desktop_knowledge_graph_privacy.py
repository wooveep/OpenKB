"""Privacy boundaries for graph issues, signatures, and application logs."""

from __future__ import annotations

import json
import logging

from openkb import desktop_model_failure_logging
from openkb.desktop_knowledge_graph import _log_graph_interpretation
from openkb.desktop_knowledge_graph_interpretation import (
    GraphEvidence,
    GraphExtractionBoundary,
    KnowledgeGraphInterpretationError,
)
from openkb.desktop_log_handler import HybridDiagnosticHandler
from openkb.desktop_logging_settings import DiagnosticLoggingSettings
from openkb.desktop_model_failure_logging import own_unrepaired_structured_model_failure
from openkb.desktop_model_gateway import DesktopModelResult


def test_graph_issue_paths_never_copy_untrusted_field_names() -> None:
    secret_field = "source phrase that must not enter diagnostics"
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
                        secret_field: "anything",
                    }
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas"),),
    )

    assert [(issue.code, issue.path) for issue in interpretation.issues] == [
        ("unexpected_field", "nodes[0].*")
    ]
    assert secret_field not in repr(interpretation.issues)


def test_regular_failure_log_excludes_source_quotes_and_raw_model_content(
    caplog, monkeypatch
) -> None:
    source_secret = "source-secret-9d205"
    quote_secret = "invented-quote-3e6c1"
    relation_secret = "RAW_RELATION_SECRET"
    interpretation = GraphExtractionBoundary.interpret(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "atlas",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Atlas",
                        "support_quote": quote_secret,
                    }
                ],
                "edges": [],
                relation_secret: source_secret,
            }
        ),
        (GraphEvidence("evidence-1", source_secret),),
    )
    error = KnowledgeGraphInterpretationError(interpretation)
    raw_response = json.dumps(
        {"support_quote": quote_secret, "type": relation_secret, "source": source_secret}
    )
    monkeypatch.setattr(
        desktop_model_failure_logging,
        "sensitive_trace_component_enabled",
        lambda _component: False,
    )

    with caplog.at_level(logging.WARNING):
        failure_event_id = own_unrepaired_structured_model_failure(
            operation="knowledge_graph_extraction",
            document_name="private.md",
            source_material=source_secret,
            initial=DesktopModelResult("call-1", raw_response, 1),
            error=error,
        )
        _log_graph_interpretation(
            interpretation,
            failure_event_id=failure_event_id,
        )

    logged = json.dumps(
        [record.__dict__ for record in caplog.records],
        ensure_ascii=False,
        default=str,
    )
    assert "support_quote_not_found" in logged
    assert source_secret not in logged
    assert quote_secret not in logged
    assert relation_secret not in logged
    terminal = [record for record in caplog.records if record.__dict__.get("openkb_terminal")]
    assert len(terminal) == 1
    [graph_record] = [
        record
        for record in caplog.records
        if record.__dict__.get("openkb_event") == "knowledge_graph_interpreted"
    ]
    assert graph_record.__dict__.get("openkb_terminal") is False
    assert graph_record.__dict__["openkb_fields"]["failure_event_id"] == failure_event_id


def test_degraded_graph_interpretation_survives_actual_jsonl_filtering(tmp_path) -> None:
    source_secret = "Atlas private source text 8fda6"
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
                        "id": "rejected",
                        "evidence_id": "evidence-1",
                        "type": "entity",
                        "label": "Rejected",
                        "support_quote": "not in evidence",
                    },
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", source_secret),),
    )
    assert interpretation.quality == "degraded"
    log_path = tmp_path / "engine.jsonl"
    settings = DiagnosticLoggingSettings(
        level_name="WARN",
        component_levels={"knowledge": "INFO"},
        runtime_session_id="graph-jsonl-test",
    )
    handler = HybridDiagnosticHandler(log_path, settings)
    graph_logger = logging.getLogger("openkb.desktop_knowledge_graph")
    previous_level = graph_logger.level
    previous_propagate = graph_logger.propagate
    graph_logger.setLevel(logging.INFO)
    graph_logger.propagate = False
    graph_logger.addHandler(handler)
    try:
        _log_graph_interpretation(interpretation)
        handler.flush()
    finally:
        graph_logger.removeHandler(handler)
        graph_logger.setLevel(previous_level)
        graph_logger.propagate = previous_propagate
        handler.close()

    [payload] = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert payload["component"] == "knowledge"
    assert payload["result_status"] == "completed"
    assert payload["result_quality"] == "degraded"
    assert payload["retained_count"] == 1
    assert payload["weakened_count"] == 0
    assert payload["rejected_count"] == 1
    assert payload["issue_count"] == 1
    assert payload["issue_codes"] == ["support_quote_not_found"]
    assert payload["issue_paths"] == ["nodes[1].support_quote"]
    assert payload["issue_failure_classes"] == ["evidence"]
    assert payload["issues_truncated"] is False
    serialized = json.dumps(payload, ensure_ascii=False)
    assert source_secret not in serialized
    assert "not in evidence" not in serialized


def test_graph_issue_details_are_bounded_in_regular_logs(caplog) -> None:
    invalid_nodes = [
        {
            "id": f"rejected-{index}",
            "evidence_id": "evidence-1",
            "type": "entity",
            "label": f"Rejected {index}",
            "support_quote": f"absent-{index}",
        }
        for index in range(40)
    ]
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
                    *invalid_nodes,
                ],
                "edges": [],
            }
        ),
        (GraphEvidence("evidence-1", "Atlas"),),
    )

    with caplog.at_level(logging.INFO, logger="openkb.desktop_knowledge_graph"):
        _log_graph_interpretation(interpretation)

    [record] = [
        item
        for item in caplog.records
        if item.__dict__.get("openkb_event") == "knowledge_graph_interpreted"
    ]
    fields = record.__dict__["openkb_fields"]
    assert fields["issue_count"] == 40
    assert len(fields["issue_codes"]) == 32
    assert len(fields["issue_paths"]) == 32
    assert fields["issues_truncated"] is True
