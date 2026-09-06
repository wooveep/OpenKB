"""Behavior checks for document-scoped Candidate Registry generations."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_candidate_registry import DesktopKnowledgeCandidateRegistry
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_reuse import analysis_evidence_for_document_in
from openkb.desktop_knowledge_candidate_pipeline import DesktopKnowledgeCandidatePipeline
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_semantic_graph_service import DesktopSemanticGraphService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def _analysis(*, candidates: list[dict[str, object]] | None = None):
    return parse_knowledge_analysis(
        json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Evidence-backed knowledge proposals.",
                "document_summary": [],
                "candidates": candidates or [],
            }
        )
    )


def _import_without_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nNo reusable identity is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    return kb_dir, document


def test_absent_registry_is_unavailable_and_completed_empty_is_distinct(
    tmp_path, monkeypatch
) -> None:
    kb_dir, document = _import_without_graph(tmp_path, monkeypatch)
    registry = DesktopKnowledgeCandidateRegistry(kb_dir)

    assert registry.inspect(document.document_id).status == "dependency_unavailable"

    completed = DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document.document_id,
        analysis=_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v1"}',
        evidence=(),
    )

    assert completed.status == "empty"
    assert completed.generation is not None
    assert completed.generation.candidate_count == 0
    assert completed.generation.admitted_count == 0
    assert completed.generation.completion_state == "empty"
    assert completed.generation.document_ir_digest
    assert completed.generation.evidence_digest
    assert completed.generation.analysis_operation == "knowledge_analysis"
    assert completed.generation.analysis_contract_digest
    assert completed.generation.analysis_prompt_digest
    assert completed.generation.model_capability_provenance_json
    assert registry.inspect(document.document_id) == completed


def test_candidate_pipeline_keeps_superseded_generations_immutable(tmp_path, monkeypatch) -> None:
    kb_dir, document = _import_without_graph(tmp_path, monkeypatch)
    pipeline = DesktopKnowledgeCandidatePipeline(kb_dir)
    registry = DesktopKnowledgeCandidateRegistry(kb_dir)

    first = pipeline.run_document(
        document_id=document.document_id,
        analysis=_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v1"}',
        evidence=(),
    )
    second = pipeline.run_document(
        document_id=document.document_id,
        analysis=_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v2"}',
        evidence=(),
    )

    assert first.generation is not None
    assert second.generation is not None
    assert first.generation.generation_id != second.generation.generation_id
    assert registry.generation(first.generation.generation_id) == first.generation
    assert registry.inspect(document.document_id) == second


def test_review_candidate_is_retained_but_does_not_become_admitted(tmp_path, monkeypatch) -> None:
    kb_dir, document = _import_without_graph(tmp_path, monkeypatch)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence = analysis_evidence_for_document_in(connection, document.document_id)
    analysis = _analysis(
        candidates=[
            {
                "kind": "entity",
                "title": "Notes",
                "aliases": [],
                "identity_labels": ["source-defined label"],
                "admission": "review",
                "claims": [
                    {
                        "text": "No reusable identity is present.",
                        "source_evidence_ids": [evidence[0][0]],
                        "applicability": [],
                    }
                ],
            }
        ]
    )

    outcome = DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document.document_id,
        analysis=analysis,
        analysis_provenance_json='{"checkpoint_digest":"review-v1"}',
        evidence=evidence,
    )

    assert outcome.status == "empty"
    assert outcome.generation is not None
    assert outcome.generation.candidate_count == 1
    assert outcome.generation.admitted_count == 0


def test_completed_empty_registry_publishes_empty_graph_without_model_call(
    tmp_path, monkeypatch
) -> None:
    kb_dir, document = _import_without_graph(tmp_path, monkeypatch)
    DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document.document_id,
        analysis=_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v1"}',
        evidence=(),
    )
    model_calls = 0

    def unexpected_model_call(_request, _timeout):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("An empty registry must not call the relation model.")

    result = DesktopSemanticGraphService(
        kb_dir,
        model_gateway=DesktopModelGateway(unexpected_model_call),
    ).extract_document_if_admitted(document.document_id)

    assert result is True
    assert model_calls == 0
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            """
            SELECT status, quality, node_count, edge_count
            FROM knowledge_graph_results
            WHERE document_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (document.document_id,),
        ).fetchone() == ("completed_empty", "full", 0, 0)


def test_d1_reuse_publishes_a_distinct_candidate_generation_without_model_call(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    first_source = tmp_path / "first.md"
    second_source = tmp_path / "second.md"
    first_source.write_text("# Guide\n\nA reusable stable fact.\n", encoding="utf-8")
    second_source.write_text("# Guide\r\n\r\nA reusable stable fact.  \r\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    model_calls = 0

    def analyze(request, _timeout):
        nonlocal model_calls
        model_calls += 1
        evidence_id = str(json.loads(request.content)["evidence"][-1]["evidence_id"])
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Reusable guide.",
                "document_summary": [
                    {
                        "label": "Overview",
                        "text": "Describes a stable fact.",
                        "source_evidence_ids": [evidence_id],
                    }
                ],
                "candidates": [
                    {
                        "kind": "concept",
                        "title": "Stable concept",
                        "aliases": [],
                        "identity_labels": [],
                        "admission": "admit",
                        "claims": [
                            {
                                "text": "A reusable stable fact defines the concept.",
                                "applicability": [],
                                "source_evidence_ids": [evidence_id],
                            }
                        ],
                    }
                ],
            }
        )

    first = DesktopTextImportService(
        kb_dir, model_gateway=DesktopModelGateway(analyze)
    ).import_text(first_source)
    calls_after_first = model_calls
    second = DesktopTextImportService(kb_dir).import_text(second_source)

    assert second.job.deduplication is not None
    assert second.job.deduplication.level == "D1"
    assert model_calls == calls_after_first
    first_outcome = DesktopKnowledgeCandidateRegistry(kb_dir).inspect(first.document.document_id)
    second_outcome = DesktopKnowledgeCandidateRegistry(kb_dir).inspect(second.document.document_id)
    assert first_outcome.status == second_outcome.status == "ready"
    assert first_outcome.generation is not None
    assert second_outcome.generation is not None
    assert first_outcome.generation.generation_id != second_outcome.generation.generation_id
