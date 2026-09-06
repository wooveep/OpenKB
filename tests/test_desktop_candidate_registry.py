"""Behavior checks for document-scoped Candidate Registry generations."""

from __future__ import annotations

import json
import sqlite3

import pytest

from openkb.importing.runner import DesktopTextImportService
from openkb.knowledge.analysis.candidate_pipeline import DesktopKnowledgeCandidatePipeline
from openkb.knowledge.analysis.reuse import analysis_evidence_for_document_in
from openkb.knowledge.analysis.service import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    parse_knowledge_analysis,
)
from openkb.knowledge.corpus.candidate_registry import (
    DesktopKnowledgeCandidateRegistry,
    publish_candidate_registry_generation_in,
)
from openkb.knowledge.graph.semantic_graph_service import DesktopSemanticGraphService
from openkb.models.gateway import DesktopModelGateway
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime, desktop_state_database_path


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


def test_registry_rejects_invalid_persisted_applicability_without_rewriting_history(
    tmp_path, monkeypatch
):
    from test_desktop_corpus_synthesis_pipeline import _candidate_fixture

    kb, document_id, *_ = _candidate_fixture(tmp_path, monkeypatch)
    registry = DesktopKnowledgeCandidateRegistry(kb)
    before = registry.inspect(document_id)
    invalid = json.dumps(
        [{"dimension": "version", "value": "1", "source_evidence_ids": ["foreign"]}]
    )
    with sqlite3.connect(desktop_state_database_path(kb)) as db:
        db.execute(
            "UPDATE knowledge_document_candidate_claims SET applicability_json = ?", (invalid,)
        )
        with pytest.raises(ValueError, match="subset"):
            publish_candidate_registry_generation_in(
                db,
                document_id=document_id,
                analysis_provenance_json="{}",
                now="2026-09-06T00:00:00Z",
            )
    assert registry.inspect(document_id) == before
    with sqlite3.connect(desktop_state_database_path(kb)) as db:
        db.execute(
            "UPDATE knowledge_candidate_generation_claims SET applicability_json = ?", (invalid,)
        )
    assert registry.inspect(document_id).status == "dependency_unavailable"


def _import_without_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
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
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
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
                                "applicability": [
                                    {
                                        "dimension": "Version",
                                        "value": "1",
                                        "source_evidence_ids": [evidence_id],
                                    }
                                ],
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

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        for outcome in (first_outcome, second_outcome):
            generation_id = outcome.generation.generation_id
            scope = json.loads(
                connection.execute(
                    "SELECT applicability_json FROM knowledge_candidate_generation_claims "
                    "WHERE candidate_generation_id = ?",
                    (generation_id,),
                ).fetchone()[0]
            )
            sources = {
                row[0]
                for row in connection.execute(
                    "SELECT evidence_id FROM knowledge_candidate_generation_claim_sources "
                    "WHERE candidate_generation_id = ?",
                    (generation_id,),
                )
            }
            assert set(scope[0]["source_evidence_ids"]) == sources
            assert sources <= {
                row[0]
                for row in connection.execute(
                    "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ?",
                    (outcome.generation.document_id,),
                )
            }
