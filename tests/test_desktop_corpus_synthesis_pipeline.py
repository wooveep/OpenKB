"""Corpus Knowledge Synthesis Pipeline behavior at its public seam."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_corpus_entity_briefs import load_relevant_corpus_entity_briefs
from openkb.desktop_corpus_generation_quality import generation_content_quality_in
from openkb.desktop_corpus_knowledge_pipeline import CorpusKnowledgeSynthesisPipeline
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
    KnowledgeClaimApplicability,
)
from openkb.desktop_knowledge_analysis_reuse import analysis_evidence_for_document_in
from openkb.desktop_knowledge_candidate_pipeline import DesktopKnowledgeCandidatePipeline
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def _candidate_fixture(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "alpha.md"
    source.write_text(
        "# Alpha\n\nAlpha is a durable service. Alpha supports snapshots on Linux.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence = analysis_evidence_for_document_in(connection, document.document_id)
    evidence_id = evidence[0][0]
    analysis = DesktopKnowledgeAnalysis(
        document_description="Alpha service documentation.",
        concepts=(),
        entities=(
            KnowledgeAnalysisCandidate(
                kind="entity",
                title="Alpha",
                aliases=(),
                tags=("snapshots",),
                subtype="service",
                claims=(
                    KnowledgeAnalysisClaim(
                        "Alpha is a durable service.",
                        (evidence_id,),
                        "definition",
                    ),
                    KnowledgeAnalysisClaim(
                        "Alpha supports immutable snapshots.",
                        (evidence_id,),
                        "capability",
                        KnowledgeClaimApplicability(platform="Linux"),
                    ),
                ),
            ),
        ),
        corpus_ready=True,
    )
    candidate = _publish_candidate(
        kb_dir,
        document.document_id,
        analysis,
        evidence,
        marker="initial",
    )
    return kb_dir, document.document_id, analysis, evidence, candidate


def _publish_candidate(kb_dir, document_id, analysis, evidence, *, marker: str):
    return DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document_id,
        analysis=analysis,
        analysis_provenance_json=json.dumps(
            {
                "analysis_operation": "document_entity_inventory",
                "prompt_digest": f"test-prompt-{marker}",
                "contract_digest": "test-contract",
            }
        ),
        evidence=evidence,
    )


def _dossier_response(request) -> str:
    payload = json.loads(request.content)
    claim_ids = [claim["claim_id"] for claim in payload["claims"]]
    return json.dumps(
        {
            "generation_id": payload["generation_id"],
            "identity_id": payload["identity_id"],
            "summary_claim_ids": claim_ids[:1],
            "sections": [
                {
                    "title": "Capabilities",
                    "purpose": "capabilities",
                    "units": [{"presentation": "paragraph", "claim_ids": claim_ids[1:]}],
                }
            ],
            "related_identity_ids": [],
        }
    )


def _accept_structural_benchmark(monkeypatch) -> None:
    monkeypatch.setattr(
        "openkb.desktop_knowledge_generations._record_corpus_benchmark_in",
        lambda connection, generation_id: connection.execute(
            "UPDATE knowledge_generations SET qualification_report_json = ? "
            "WHERE generation_id = ?",
            ('{"schema_version":"openkb.corpus-benchmark.v3","passed":true}', generation_id),
        ),
    )


def test_corpus_pipeline_persists_a_generation_owned_entity_dossier_before_activation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _accept_structural_benchmark(monkeypatch)
    kb_dir, _document_id, analysis, _evidence, candidate = _candidate_fixture(tmp_path, monkeypatch)
    assert candidate.generation is not None

    dossier_requests = []

    def plan_dossier(request, _timeout_seconds):
        dossier_requests.append(request)
        return _dossier_response(request)

    gateway = DesktopModelGateway(
        plan_dossier,
        provider_name="scripted",
        model_name="dossier-v1",
    )

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        preferred_language="en",
        gateway=gateway,
    )

    assert outcome.generation_id is not None
    assert outcome.manifest is not None
    assert outcome.manifest.dossier_state == "ready"
    assert len(outcome.dossiers) == 1
    dossier = outcome.dossiers[0]
    assert dossier.generation_id == outcome.generation_id
    assert dossier.plan.generation_id == outcome.generation_id
    assert dossier.plan.identity_id == dossier.identity_id
    assert dossier.fact_count == 2
    assert {section.purpose for section in dossier.plan.sections} == {"capabilities"}
    assert [request.operation for request in dossier_requests] == ["entity_dossier_planning"]
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        operation, digest, provenance = connection.execute(
            "SELECT planning_operation, prompt_contract_digest, planner_provenance_json "
            "FROM knowledge_generation_dossier_plans WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone()
        quality = generation_content_quality_in(connection, outcome.generation_id)
        task = connection.execute(
            "SELECT status, phase, execution_token "
            "FROM knowledge_corpus_synthesis_tasks WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone()
    assert operation == "entity_dossier_planning"
    assert digest == prompt_contract_for("entity_dossier_planning").digest
    assert json.loads(provenance)["call_id"]
    assert quality.entity_noise_leakage_rate == 0.0
    assert quality.duplicate_identity_rate == 0.0
    assert quality.dossier_readability_passed is True
    assert quality.dossier_facet_coverage == 1.0
    assert task == ("completed", "completed", None)
    briefs = load_relevant_corpus_entity_briefs(
        desktop_state_database_path(kb_dir),
        analysis,
    )
    assert len(briefs) == 1
    assert briefs[0].identity_id == dossier.identity_id
    assert briefs[0].canonical_title == "Alpha"
    assert briefs[0].current_claim_count == 2
    assert briefs[0].match_signals == ("exact_title", "controlled_separator")


def test_late_dossier_response_cannot_publish_after_candidate_reanalysis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _accept_structural_benchmark(monkeypatch)
    kb_dir, document_id, analysis, evidence, initial = _candidate_fixture(tmp_path, monkeypatch)
    assert initial.generation is not None
    baseline = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation()
    assert baseline.status == "active"
    current = baseline.current_generation_id
    claimed = _publish_candidate(
        kb_dir,
        document_id,
        analysis,
        evidence,
        marker="claimed",
    )
    assert claimed.generation is not None
    newer_generation_ids: list[str] = []

    def reanalyze_while_model_is_running(request, _timeout_seconds):
        newer = _publish_candidate(
            kb_dir,
            document_id,
            analysis,
            evidence,
            marker="newer",
        )
        assert newer.generation is not None
        newer_generation_ids.append(newer.generation.generation_id)
        return _dossier_response(request)

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(claimed.generation.generation_id,),
        gateway=DesktopModelGateway(
            reanalyze_while_model_is_running,
            provider_name="scripted",
            model_name="dossier-v1",
        ),
    )

    assert newer_generation_ids
    assert outcome.status == "superseded"
    assert outcome.manifest is not None
    assert outcome.manifest.lifecycle_state == "superseded"
    assert outcome.current_generation_id == current
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM knowledge_corpus_synthesis_tasks "
            "WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone() == ("superseded", "candidate_generation_superseded")


def test_cancellation_after_provider_return_invalidates_pending_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _accept_structural_benchmark(monkeypatch)
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None
    baseline = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation()
    assert baseline.status == "active"
    cancelled = False

    def cancel_with_response(request, _timeout_seconds):
        nonlocal cancelled
        cancelled = True
        return _dossier_response(request)

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        force_generation=True,
        gateway=DesktopModelGateway(
            cancel_with_response,
            provider_name="scripted",
            model_name="dossier-v1",
        ),
        should_stop=lambda: cancelled,
    )

    assert outcome.status == "cancelled"
    assert outcome.manifest is not None
    assert outcome.manifest.lifecycle_state == "cancelled"
    assert outcome.current_generation_id == baseline.current_generation_id
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT status, error_code, execution_token "
            "FROM knowledge_corpus_synthesis_tasks WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone() == ("cancelled", "corpus_synthesis_cancelled", None)


def test_dossier_dispatch_consumes_the_supplied_retry_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _accept_structural_benchmark(monkeypatch)
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None
    dispatch_scopes: list[str | None] = []

    def record_dispatch(_kb_dir, _gateway, _request, *, retry_scope=None):
        dispatch_scopes.append(retry_scope)

    monkeypatch.setattr(
        "openkb.desktop_corpus_knowledge_pipeline.require_model_operation_dispatch",
        record_dispatch,
    )
    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=DesktopModelGateway(
            lambda request, _timeout_seconds: _dossier_response(request),
            provider_name="scripted",
            model_name="dossier-v1",
        ),
        retry_scope="retry-dossier-7",
    )

    assert outcome.status == "active"
    assert dispatch_scopes == ["retry-dossier-7"]
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT status, retry_scope FROM knowledge_corpus_synthesis_tasks "
            "WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone() == ("completed", "retry-dossier-7")


def test_explicit_cancel_revokes_the_durable_claim_before_the_response_returns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _accept_structural_benchmark(monkeypatch)
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None
    pipeline = CorpusKnowledgeSynthesisPipeline(kb_dir)
    cancellation_observed: list[bool] = []

    def cancel_before_return(request, _timeout_seconds):
        generation_id = int(json.loads(request.content)["generation_id"])
        cancellation_observed.append(pipeline.request_cancel(generation_id))
        with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
            cancellation_observed.append(
                connection.execute(
                    "SELECT execution_token IS NULL "
                    "FROM knowledge_corpus_synthesis_tasks WHERE generation_id = ?",
                    (generation_id,),
                ).fetchone()
                == (1,)
            )
        return _dossier_response(request)

    outcome = pipeline.run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=DesktopModelGateway(
            cancel_before_return,
            provider_name="scripted",
            model_name="dossier-v1",
        ),
    )

    assert cancellation_observed == [True, True]
    assert outcome.status == "cancelled"
