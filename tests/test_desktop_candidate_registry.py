"""Behavior checks for document-scoped Candidate Registry Generations."""

from __future__ import annotations

import json
import sqlite3

import pytest

from openkb.desktop_candidate_registry import DesktopKnowledgeCandidateRegistry
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_reuse import analysis_evidence_for_document_in
from openkb.desktop_knowledge_candidate_pipeline import DesktopKnowledgeCandidatePipeline
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_semantic_graph_service import DesktopSemanticGraphService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def _empty_analysis():
    return parse_knowledge_analysis(
        json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "No reusable knowledge identities.",
                "document_summary": [],
                "concepts": [],
                "entities": [],
                "procedures": [],
            }
        )
    )


def test_candidate_pipeline_distinguishes_explicit_legacy_from_completed_empty(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nNo reusable identity is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document

    registry = DesktopKnowledgeCandidateRegistry(kb_dir)
    legacy = registry.inspect(document.document_id)

    assert legacy.status == "explicit_legacy"
    assert legacy.generation is None

    completed = DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document.document_id,
        analysis=_empty_analysis(),
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
    assert completed.generation.page_tree_generation_id
    assert completed.generation.page_tree_digest
    assert completed.generation.analysis_operation == "knowledge_analysis"
    assert completed.generation.analysis_contract_digest
    assert completed.generation.analysis_prompt_digest
    assert completed.generation.model_capability_provenance_json
    assert registry.inspect(document.document_id) == completed


def test_candidate_pipeline_keeps_superseded_generations_immutable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nNo reusable identity is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    pipeline = DesktopKnowledgeCandidatePipeline(kb_dir)
    registry = DesktopKnowledgeCandidateRegistry(kb_dir)

    first = pipeline.run_document(
        document_id=document.document_id,
        analysis=_empty_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v1"}',
        evidence=(),
    )
    second = pipeline.run_document(
        document_id=document.document_id,
        analysis=_empty_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v2"}',
        evidence=(),
    )

    assert first.generation is not None
    assert second.generation is not None
    assert first.generation.generation_id != second.generation.generation_id
    assert registry.generation(first.generation.generation_id) == first.generation
    assert registry.inspect(document.document_id) == second


def test_candidate_pipeline_rejects_a_stale_inventory_identity_target(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "alpha.md"
    source.write_text("# Alpha\n\nAlpha is a durable service.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence = analysis_evidence_for_document_in(connection, document.document_id)
    analysis = DesktopKnowledgeAnalysis(
        document_description="Alpha service documentation.",
        concepts=(),
        entities=(
            KnowledgeAnalysisCandidate(
                kind="entity",
                title="Alpha",
                aliases=(),
                tags=(),
                subtype="service",
                claims=(
                    KnowledgeAnalysisClaim(
                        text="Alpha is a durable service.",
                        source_evidence_ids=(evidence[0][0],),
                        role="definition",
                    ),
                ),
                admission_reason_codes=("existing_identity_match",),
                inventory_decision="update",
                inventory_target_identity_id="missing-identity",
                inventory_target_generation_id=999,
            ),
        ),
        corpus_ready=True,
    )

    with pytest.raises(ValueError, match="target identity generation"):
        DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
            document_id=document.document_id,
            analysis=analysis,
            analysis_provenance_json='{"checkpoint_digest":"stale-target"}',
            evidence=evidence,
        )

    assert DesktopKnowledgeCandidateRegistry(kb_dir).inspect(document.document_id).status == (
        "explicit_legacy"
    )


def test_completed_empty_registry_publishes_healthy_empty_graph_without_model_call(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nNo reusable identity is present.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document.document_id,
        analysis=_empty_analysis(),
        analysis_provenance_json='{"checkpoint_digest":"empty-v1"}',
        evidence=(),
    )
    model_calls = 0

    def unexpected_model_call(_request, _timeout):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("A completed-empty registry must not call the relation model.")

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
                        "role": "purpose",
                        "text": "Describes a stable fact.",
                        "source_evidence_ids": [evidence_id],
                    }
                ],
                "concepts": [
                    {
                        "title": "Stable concept",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "role": "definition",
                                "text": "A reusable stable fact defines the concept.",
                                "applicability": {
                                    "product_version": "",
                                    "platform": "",
                                    "deployment_scenario": "",
                                    "time_boundary": "",
                                },
                                "source_evidence_ids": [evidence_id],
                            }
                        ],
                    }
                ],
                "entities": [],
                "procedures": [],
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
