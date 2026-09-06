"""Corpus Knowledge Synthesis Pipeline behavior at its public seam."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

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
                identity_labels=("snapshots",),
                claims=(
                    KnowledgeAnalysisClaim(
                        "Alpha is a durable service.",
                        (evidence_id,),
                    ),
                    KnowledgeAnalysisClaim(
                        "Alpha supports immutable snapshots.",
                        (evidence_id,),
                        (
                            KnowledgeClaimApplicability(
                                "platform",
                                "Linux",
                                (evidence_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
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
                "analysis_operation": "knowledge_analysis",
                "prompt_digest": f"test-prompt-{marker}",
                "contract_digest": "test-contract",
            }
        ),
        evidence=evidence,
    )


def _page_response(request, *, title: str = "Snapshot behavior") -> str:
    payload = json.loads(request.content)
    claim_ids = [claim["claim_id"] for claim in payload["claims"]]
    return json.dumps(
        {
            "generation_id": payload["generation_id"],
            "identity_id": payload["identity_id"],
            "lead": {
                "presentation": "paragraph",
                "claim_ids": claim_ids[:1],
                "relation_assertion_ids": [],
            },
            "sections": [
                {
                    "title": title,
                    "units": [
                        {
                            "presentation": "unordered_list",
                            "claim_ids": claim_ids[1:],
                            "relation_assertion_ids": [],
                        }
                    ],
                    "sections": [],
                }
            ]
            if len(claim_ids) > 1
            else [],
        }
    )


def _identity_from_repair_request(request) -> str:
    payload = json.loads(request.content)
    source = json.loads(payload["evidence_bound_source_material"])
    return str(source["identity_id"])


def _gateway(transport, *, model: str = "planner-v1") -> DesktopModelGateway:
    return DesktopModelGateway(
        transport,
        provider_name="scripted",
        model_name=model,
    )


def test_corpus_pipeline_persists_a_dynamic_page_before_activation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None
    requests = []

    def plan_page(request, _timeout_seconds):
        requests.append(request)
        return _page_response(request)

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        preferred_language="en",
        gateway=_gateway(plan_page),
    )

    assert outcome.status == "active"
    assert outcome.generation_id is not None
    assert outcome.manifest is not None
    assert outcome.manifest.page_state == "ready"
    assert len(outcome.pages) == 1
    page = outcome.pages[0]
    assert page.status == "ready"
    assert page.plan is not None
    assert page.plan.generation_id == outcome.generation_id
    assert page.plan.sections[0].title == "Snapshot behavior"
    assert page.factual_unit_count == 2
    assert [request.operation for request in requests] == ["knowledge_page_planning"]
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        operation, digest, profile_digest, provenance = connection.execute(
            "SELECT planning_operation, prompt_contract_digest, "
            "execution_profile_digest, planner_provenance_json "
            "FROM knowledge_generation_page_plans WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone()
        markdown = connection.execute(
            "SELECT content_markdown FROM knowledge_generation_items WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone()[0]
        task = connection.execute(
            "SELECT status, phase, execution_token "
            "FROM knowledge_corpus_synthesis_tasks WHERE generation_id = ?",
            (outcome.generation_id,),
        ).fetchone()
    assert operation == "knowledge_page_planning"
    assert digest == prompt_contract_for("knowledge_page_planning").digest
    assert len(profile_digest) == 64
    assert json.loads(provenance)["call_id"]
    assert "## Snapshot behavior" in markdown
    assert "Identity and role" not in markdown
    assert "[^src-" in markdown
    assert task == ("completed", "completed", None)


def test_invalid_page_is_deferred_while_valid_sibling_activates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, document_id, analysis, evidence, _initial = _candidate_fixture(tmp_path, monkeypatch)
    evidence_id = evidence[0][0]
    expanded = replace(
        analysis,
        entities=(
            *analysis.entities,
            KnowledgeAnalysisCandidate(
                kind="entity",
                title="Beta",
                aliases=(),
                identity_labels=("replication",),
                claims=(
                    KnowledgeAnalysisClaim(
                        "Beta supports replication.",
                        (evidence_id,),
                    ),
                ),
            ),
        ),
    )
    candidate = _publish_candidate(
        kb_dir,
        document_id,
        expanded,
        evidence,
        marker="mixed",
    )
    assert candidate.generation is not None
    requests: list[tuple[str, str]] = []

    def mixed_page_plans(request, _timeout_seconds):
        identity_id = (
            _identity_from_repair_request(request)
            if request.operation == "structured_output_repair"
            else str(json.loads(request.content)["identity_id"])
        )
        requests.append((request.operation, identity_id))
        title = json.loads(request.content).get("title", "")
        return _page_response(request) if title == "Alpha" else "{}"

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=_gateway(mixed_page_plans),
    )

    assert outcome.status == "active"
    assert {page.status for page in outcome.pages} == {"ready", "deferred"}
    deferred = next(page for page in outcome.pages if page.status == "deferred")
    assert deferred.error_codes == ("knowledge_page_plan_invalid",)
    assert [operation for operation, _identity in requests].count("structured_output_repair") == 1
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        published_titles = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT title FROM knowledge_generation_items WHERE generation_id = ?",
                (outcome.generation_id,),
            )
        )
        evidence_still_available = connection.execute(
            "SELECT 1 FROM evidence_occurrences WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
    assert published_titles == ("Alpha",)
    assert evidence_still_available == (1,)


def test_invalid_replacement_carries_forward_the_previous_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, document_id, analysis, evidence, candidate = _candidate_fixture(tmp_path, monkeypatch)
    assert candidate.generation is not None
    baseline = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=_gateway(lambda request, _timeout_seconds: _page_response(request)),
    )
    assert baseline.status == "active"
    newer = _publish_candidate(kb_dir, document_id, analysis, evidence, marker="newer")
    assert newer.generation is not None

    replacement = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(newer.generation.generation_id,),
        force_generation=True,
        gateway=_gateway(lambda _request, _timeout_seconds: "{}"),
    )

    assert replacement.status == "active"
    assert replacement.pages[0].status == "carried_forward"
    assert replacement.pages[0].published_generation_id == baseline.generation_id
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        rows = connection.execute(
            "SELECT generation_id, content_markdown FROM knowledge_generation_items "
            "WHERE identity_id = ? ORDER BY generation_id",
            (replacement.pages[0].identity_id,),
        ).fetchall()
    assert len(rows) == 2
    assert str(rows[0][1]) == str(rows[1][1])


def test_runtime_report_contains_integrity_only_and_dynamic_heading_activates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=_gateway(
            lambda request, _timeout_seconds: _page_response(
                request,
                title="A domain expert's surprising but safe organization",
            )
        ),
    )

    assert outcome.status == "active"
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        report = json.loads(
            connection.execute(
                "SELECT integrity_report_json FROM knowledge_generations WHERE generation_id = ?",
                (outcome.generation_id,),
            ).fetchone()[0]
        )
    assert report["schema_version"] == "openkb.corpus-generation-integrity.v1"
    assert report["passed"] is True
    assert report["issues"] == []
    assert not {
        "noise_leakage_rate",
        "dossier_readability_rate",
        "procedure_stage_coverage",
        "real_corpus_benchmark",
    } & set(report)


def test_late_page_response_cannot_publish_after_candidate_reanalysis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, document_id, analysis, evidence, initial = _candidate_fixture(tmp_path, monkeypatch)
    assert initial.generation is not None
    baseline = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(initial.generation.generation_id,),
        gateway=_gateway(lambda request, _timeout_seconds: _page_response(request)),
    )
    claimed = _publish_candidate(kb_dir, document_id, analysis, evidence, marker="claimed")
    assert claimed.generation is not None

    def reanalyze_while_running(request, _timeout_seconds):
        _publish_candidate(kb_dir, document_id, analysis, evidence, marker="newest")
        return _page_response(request)

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(claimed.generation.generation_id,),
        gateway=_gateway(reanalyze_while_running),
    )

    assert outcome.status == "superseded"
    assert outcome.current_generation_id == baseline.current_generation_id


def test_cancellation_after_provider_return_invalidates_pending_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None
    cancelled = False

    def cancel_with_response(request, _timeout_seconds):
        nonlocal cancelled
        cancelled = True
        return _page_response(request)

    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=_gateway(cancel_with_response),
        should_stop=lambda: cancelled,
    )

    assert outcome.status == "cancelled"
    assert outcome.manifest is not None
    assert outcome.manifest.lifecycle_state == "cancelled"


def test_page_dispatch_consumes_the_supplied_retry_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kb_dir, _document_id, _analysis, _evidence, candidate = _candidate_fixture(
        tmp_path, monkeypatch
    )
    assert candidate.generation is not None
    dispatch_scopes: list[str | None] = []

    def record_dispatch(_kb_dir, _gateway, _request, *, retry_scope=None):
        dispatch_scopes.append(retry_scope)

    monkeypatch.setattr(
        "openkb.desktop_knowledge_page_model_planner.require_model_operation_dispatch",
        record_dispatch,
    )
    outcome = CorpusKnowledgeSynthesisPipeline(kb_dir).run_generation(
        candidate_generation_ids=(candidate.generation.generation_id,),
        gateway=_gateway(lambda request, _timeout_seconds: _page_response(request)),
        retry_scope="retry-page-7",
    )

    assert outcome.status == "active"
    assert dispatch_scopes == ["retry-page-7"]
