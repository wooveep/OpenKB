"""Acceptance checks for the fixed Desktop retrieval-ablation gate."""

from __future__ import annotations

import json
import sqlite3

import pytest

from openkb.desktop_graph_feature_flags import local_graph_default_enabled
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_generation_changes_in,
)
from openkb.desktop_knowledge_graph import DesktopKnowledgeGraphService
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_evaluation import (
    DesktopRetrievalEvaluationReport,
    DesktopRetrievalEvaluationSuite,
    DesktopRetrievalEvaluator,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_fixed_suite_compares_all_vectorless_variants_and_gates_graph_gain(tmp_path, monkeypatch):
    """A snapshot suite covers every required question type and reports all metrics."""
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    assert not local_graph_default_enabled(kb_dir)
    sources = {
        "delivery-system.txt": (
            "# Delivery\n\nThe Relay forwards deliveries.\n\nThe Gate validates deliveries.\n"
        ),
        "policy-a.txt": "The archive policy permits encrypted storage.\n",
        "policy-b.txt": "The archive policy rejects plaintext storage.\n",
        "archive.txt": "The Archive keeps records for audit.\n",
        "ledger.txt": "The Ledger carries audit history.\n",
    }
    importer = DesktopTextImportService(kb_dir)
    documents = []
    for name, content in sources.items():
        source = tmp_path / name
        source.write_text(content, encoding="utf-8")
        documents.append(importer.import_text(source).document)

    def graph_response(request, _timeout_seconds):
        evidence = json.loads(request.content)["evidence"]
        nodes = []
        for ordinal, item in enumerate(evidence):
            text = item["text"]
            if "Relay" in text or "Gate" in text:
                label = "controlplane"
            elif "policy" in text:
                label = "policyconflict"
            elif "Archive" in text or "Ledger" in text:
                label = "retentiontheme"
            else:
                label = f"other-{ordinal}"
            nodes.append(
                {
                    "id": f"node-{ordinal}",
                    "evidence_id": item["evidence_id"],
                    "type": "entity",
                    "label": label,
                }
            )
        return json.dumps({"nodes": nodes, "edges": []})

    graph = DesktopKnowledgeGraphService(kb_dir, model_gateway=DesktopModelGateway(graph_response))
    for document in documents:
        assert graph.extract_document(document.document_id)

    answer_requests = []

    def answer_response(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return json.dumps({"terms": ["evaluation"]})
        assert request.operation == "grounded_answer"
        answer_requests.append(request)
        return request.content

    suite_path = tmp_path / "fixed-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_id": "desktop-vectorless-fixture-v1",
                "max_graph_latency_ms": 1_000,
                "max_graph_model_calls": 20,
                "cases": [
                    {
                        "case_id": "local-relay",
                        "category": "local_fact",
                        "question": "relay forward",
                        "expected_evidence": [
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "Relay forwards",
                            },
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "Gate validates",
                            },
                        ],
                        "expected_answer_terms": ["Relay", "forwards"],
                    },
                    {
                        "case_id": "control-path",
                        "category": "multi_hop",
                        "question": "controlplane raptor",
                        "expected_evidence": [
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "Relay forwards",
                            },
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "Gate validates",
                            },
                        ],
                        "expected_answer_terms": ["Relay", "Gate"],
                    },
                    {
                        "case_id": "policy-conflict",
                        "category": "cross_document_conflict",
                        "question": "policyconflict raptor",
                        "expected_evidence": [
                            {"document_name": "policy-a.txt", "text_contains": "permits encrypted"},
                            {"document_name": "policy-b.txt", "text_contains": "rejects plaintext"},
                        ],
                        "expected_answer_terms": ["permits", "rejects"],
                    },
                    {
                        "case_id": "retention-theme",
                        "category": "global_theme",
                        "question": "retentiontheme raptor",
                        "expected_evidence": [
                            {"document_name": "archive.txt", "text_contains": "keeps records"},
                            {"document_name": "ledger.txt", "text_contains": "carries audit"},
                        ],
                        "expected_answer_terms": ["Archive", "Ledger"],
                    },
                    {
                        "case_id": "not-present",
                        "category": "absent_answer",
                        "question": "lunarfridge quantumzzle",
                        "expected_evidence": [],
                        "expected_answer_terms": [],
                        "expect_absent_answer": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    evaluator = DesktopRetrievalEvaluator(
        kb_dir,
        model_gateway=DesktopModelGateway(answer_response),
    )
    report = evaluator.evaluate(DesktopRetrievalEvaluationSuite.from_json(suite_path))

    assert set(report.metrics) == {
        "fts",
        "structure_lexical",
        "wiki",
        "baseline",
        "local_graph",
    }
    assert (
        report.metrics["local_graph"].evidence_recall_at_k
        > report.metrics["baseline"].evidence_recall_at_k
    )
    assert (
        report.metrics["local_graph"].citation_precision
        >= report.metrics["baseline"].citation_precision
    )
    assert (
        report.metrics["local_graph"].answer_faithfulness
        >= report.metrics["baseline"].answer_faithfulness
    )
    assert report.metrics["local_graph"].model_cost.model_calls > 0
    assert len(answer_requests) >= 25
    assert report.gate.passed
    generation_content = "This generated page is unrelated to the fixture questions."
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        publish_generation_changes_in(
            connection,
            current_generation_id=current_generation_id_in(connection),
            changes=(
                KnowledgeGenerationChange(
                    document_id=documents[0].document_id,
                    kind="concept",
                    title="Unrelated generated page",
                    normalized_title="unrelated generated page",
                    content_markdown=generation_content,
                    content_sha256=knowledge_content_sha256(generation_content),
                ),
            ),
            now="2026-08-15T00:00:00+00:00",
        )
        connection.commit()
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
        evaluator.promote_local_graph(report)
    assert not local_graph_default_enabled(kb_dir)

    report = evaluator.evaluate(DesktopRetrievalEvaluationSuite.from_json(suite_path))
    assert report.gate.passed
    evaluator.promote_local_graph(report)
    assert local_graph_default_enabled(kb_dir)
    assert "knowledge_graph" in {
        channel
        for reference in DesktopEvidenceRetriever(kb_dir).retrieve("controlplane raptor").evidence
        for channel in reference.channels
    }
    report_path = tmp_path / "report.json"
    report.write(report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["suite_snapshot_id"] == "desktop-vectorless-fixture-v1"
    assert payload["knowledge_snapshot_digest"] == report.knowledge_snapshot_digest
    assert payload["knowledge_snapshot_revision"] == report.knowledge_snapshot_revision
    assert payload["metrics"]["local_graph"]["evidence_recall_k"] == 6
    assert payload["gate"]["passed"] is True

    legacy_payload = report.as_dict()
    legacy_payload["metrics"]["page_tree"] = legacy_payload["metrics"].pop("structure_lexical")
    for result in legacy_payload["results"]:
        if result["variant"] == "structure_lexical":
            result["variant"] = "page_tree"
    legacy_report_path = tmp_path / "legacy-report.json"
    legacy_report_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    restored = DesktopRetrievalEvaluationReport.read(legacy_report_path)

    assert "structure_lexical" in restored.metrics
    assert "page_tree" not in restored.metrics
    assert {result.variant for result in restored.results} == set(restored.metrics)

    other_kb_dir = tmp_path / "other-desktop-kb"
    DesktopKnowledgeBaseRuntime().create(other_kb_dir)
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
        DesktopRetrievalEvaluator(other_kb_dir).promote_local_graph(report)
    assert not local_graph_default_enabled(other_kb_dir)

    late_source = tmp_path / "late-update.txt"
    late_source.write_text("The later source changes the retrieval corpus.\n", encoding="utf-8")
    importer.import_text(late_source)
    assert not local_graph_default_enabled(kb_dir)
