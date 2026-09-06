"""Explicit Knowledge Reanalysis preserves published knowledge and document availability."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openkb.engine import knowledge_reanalysis as reanalysis_engine
from openkb.importing.service import DesktopImportError, DesktopTextImportService
from openkb.knowledge.analysis.batch_store import DesktopKnowledgeAnalysisBatchStore
from openkb.knowledge.analysis.requests import (
    CURRENT_KNOWLEDGE_ANALYSIS_PIPELINE_OPERATIONS,
)
from openkb.knowledge.analysis.service import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.knowledge.reanalysis import service as reanalysis_service
from openkb.knowledge.reanalysis.service import (
    DesktopKnowledgeReanalysisService,
    recover_interrupted_knowledge_reanalysis,
)
from openkb.models.capability_store import DesktopModelCapabilityStore
from openkb.models.execution_profile import analysis_execution_profile_for_settings
from openkb.models.gateway import DesktopModelGateway, DesktopModelTransportError
from openkb.models.roles import DesktopRoleModelGateway
from openkb.models.settings import DesktopModelSettings
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def test_reanalysis_gates_every_current_analysis_pipeline_operation() -> None:
    assert set(CURRENT_KNOWLEDGE_ANALYSIS_PIPELINE_OPERATIONS) <= set(
        reanalysis_service._ANALYSIS_OPERATIONS
    )


def _analysis_response(evidence_id: str, claim: str) -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "Reanalysis fixture.",
            "document_summary": [],
            "candidates": [
                {
                    "kind": "concept",
                    "title": "Runtime mode",
                    "aliases": [],
                    "identity_labels": [],
                    "admission": "admit",
                    "claims": [
                        {
                            "text": claim,
                            "source_evidence_ids": [evidence_id],
                            "applicability": [],
                        }
                    ],
                }
            ],
        }
    )


def _gateway(claim: str, *, provider: str = "provider", model: str = "model"):
    def transport(request, _timeout_seconds):
        if request.operation == "knowledge_page_planning":
            return _page_plan_response(request)
        evidence_id = str(json.loads(request.content)["evidence"][0]["evidence_id"])
        return _analysis_response(evidence_id, claim)

    return DesktopModelGateway(transport, provider_name=provider, model_name=model)


def _imported_document(tmp_path: Path) -> tuple[Path, str]:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "source.md"
    source.write_text("# Runtime\n\nThe runtime uses a local mode.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(
        kb_dir, model_gateway=_gateway("The runtime uses a local mode.")
    ).import_text(source)
    return kb_dir, imported.document.document_id


def _empty_analysis(scope: str) -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": scope,
            "document_description": "Batch fixture.",
            "document_summary": [],
            "candidates": [],
        }
    )


def _corpus_analysis_response(evidence_id: str) -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "Runtime service documentation.",
            "document_summary": [
                {
                    "label": "Overview",
                    "text": "This document describes the Runtime service.",
                    "source_evidence_ids": [evidence_id],
                }
            ],
            "candidates": [
                {
                    "kind": "entity",
                    "title": "Runtime",
                    "aliases": [],
                    "identity_labels": ["service", "local"],
                    "admission": "admit",
                    "claims": [
                        {
                            "text": "The Runtime service uses a local mode.",
                            "applicability": [
                                {
                                    "dimension": "deployment scenario",
                                    "value": "local",
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


def _page_plan_response(request) -> str:
    snapshot = json.loads(request.content)
    claim_ids = [claim["claim_id"] for claim in snapshot["claims"]]
    return json.dumps(
        {
            "generation_id": snapshot["generation_id"],
            "identity_id": snapshot["identity_id"],
            "lead": {
                "presentation": "paragraph",
                "claim_ids": claim_ids,
                "relation_assertion_ids": [],
            },
            "sections": [],
        }
    )


def test_reanalysis_replaces_generated_knowledge_without_isolating_the_document(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    service = DesktopKnowledgeReanalysisService(kb_dir)
    before = service.overview()
    assert before["documents"][0]["state"] == "current"

    run = service.create_run((document_id,), provider="provider", model="model")
    service.run_job(run.jobs[0].job_id, _gateway("The runtime uses a global mode."))

    overview = service.overview()
    assert overview["runs"][0]["status"] == "completed"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?", (document_id,)
        ).fetchone() == ("available",)
        current = connection.execute(
            """
            SELECT items.content_markdown
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            """
        ).fetchone()[0]
        assert "global mode" in current
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_reconciliation_candidates "
            "WHERE status = 'pending_conflict'"
        ).fetchone() == (0,)


def test_corpus_reanalysis_publishes_candidates_then_plans_pages_outside_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir, document_id = _imported_document(tmp_path)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        previous_candidate_generations = connection.execute(
            "SELECT COUNT(*) FROM knowledge_candidate_generations WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
    operations: list[str] = []

    def transport(request, _timeout_seconds):
        operations.append(request.operation)
        if request.operation == "knowledge_fact_harvest":
            evidence_id = str(json.loads(request.content)["evidence"][0]["evidence_id"])
            return _corpus_analysis_response(evidence_id)
        if request.operation == "knowledge_page_planning":
            with sqlite3.connect(database_path, timeout=0.1) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            return _page_plan_response(request)
        raise AssertionError(request.operation)

    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document_id,), provider="provider", model="model")
    assert service.run_job(
        run.jobs[0].job_id,
        DesktopModelGateway(transport, provider_name="provider", model_name="model"),
        retry_scope="reanalysis-page-plan-retry",
    ) == (document_id,)

    assert service.overview()["runs"][0]["status"] == "completed"
    assert operations == [
        "knowledge_fact_harvest",
        "knowledge_page_planning",
    ]
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT current_candidate_generation_id "
                "FROM knowledge_candidate_registry_state WHERE document_id = ?",
                (document_id,),
            ).fetchone()[0]
            is not None
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_candidate_generations WHERE document_id = ?",
            (document_id,),
        ).fetchone() == (previous_candidate_generations + 1,)
        assert connection.execute(
            "SELECT tasks.status, manifests.page_state, tasks.retry_scope "
            "FROM knowledge_corpus_synthesis_tasks AS tasks "
            "JOIN knowledge_generation_manifests AS manifests "
            "ON manifests.generation_id = tasks.generation_id "
            "ORDER BY tasks.generation_id DESC LIMIT 1"
        ).fetchone() == ("completed", "ready", "reanalysis-page-plan-retry")
        assert connection.execute(
            """
            SELECT tasks.status,
                tasks.candidate_generation_id = registry.current_candidate_generation_id,
                tasks.candidate_generation_digest = generations.registry_digest
            FROM knowledge_graph_extraction_tasks AS tasks
            JOIN knowledge_candidate_registry_state AS registry
              ON registry.document_id = tasks.document_id
            JOIN knowledge_candidate_generations AS generations
              ON generations.candidate_generation_id = registry.current_candidate_generation_id
            WHERE tasks.document_id = ?
            """,
            (document_id,),
        ).fetchone() == ("pending", 1, 1)


def test_historical_schema_is_outdated_without_blocking_reanalysis_overview(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT runtime.stage_run_id, runtime.checkpoint_json
            FROM stage_run_runtime AS runtime
            JOIN stage_runs AS stages ON stages.stage_run_id = runtime.stage_run_id
            JOIN import_jobs AS jobs ON jobs.job_id = stages.job_id
            WHERE jobs.document_id = ? AND stages.stage = 'model_analysis'
            """,
            (document_id,),
        ).fetchone()
        checkpoint = json.loads(row[1])
        checkpoint["normalized_result"]["schema_version"] = "openkb.knowledge-analysis.v0"
        connection.execute(
            "UPDATE stage_run_runtime SET checkpoint_json = ? WHERE stage_run_id = ?",
            (json.dumps(checkpoint), row[0]),
        )

    service = DesktopKnowledgeReanalysisService(kb_dir)
    status = service.overview()["documents"][0]

    assert status["state"] == "analysis_outdated"
    assert status["schema_version"] == "openkb.knowledge-analysis.v0"
    assert len(service.create_run((document_id,), provider="provider", model="model").jobs) == 1


def test_reanalysis_failure_does_not_isolate_the_document_or_clear_knowledge(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        generation_before = connection.execute(
            "SELECT current_generation_id FROM knowledge_generation_state"
        ).fetchone()
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document_id,), provider="provider", model="model")

    def reject(_request, _timeout_seconds):
        raise DesktopModelTransportError("input")

    service.run_job(
        run.jobs[0].job_id,
        DesktopModelGateway(reject, provider_name="provider", model_name="model"),
    )

    overview = service.overview()
    assert overview["runs"][0]["status"] == "failed"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?", (document_id,)
        ).fetchone() == ("available",)
        assert (
            connection.execute(
                "SELECT current_generation_id FROM knowledge_generation_state"
            ).fetchone()
            == generation_before
        )


def test_d0_and_d1_reuse_latest_reanalysis_with_available_occurrence_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir, canonical_document_id = _imported_document(tmp_path)
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((canonical_document_id,), provider="provider", model="model")
    service.run_job(run.jobs[0].job_id, _gateway("The runtime uses a global mode."))
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    model_calls = 0

    def unexpected_call(_request, _timeout_seconds):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("D0/D1 reuse must not call the model")

    reuse_gateway = DesktopModelGateway(
        unexpected_call, provider_name="new-provider", model_name="new-model"
    )
    source = tmp_path / "source.md"
    d0 = DesktopTextImportService(kb_dir, model_gateway=reuse_gateway).import_text(source)
    assert d0.job.deduplication is not None
    assert d0.job.deduplication.level == "D0"
    d1_source = tmp_path / "source-copy.md"
    d1_source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    d1 = DesktopTextImportService(kb_dir, model_gateway=reuse_gateway).import_text(d1_source)
    assert d1.job.deduplication is not None
    assert d1.job.deduplication.level == "D1"
    assert model_calls == 0
    with sqlite3.connect(database_path) as connection:
        current = connection.execute(
            "SELECT items.content_markdown FROM knowledge_generation_state AS state "
            "JOIN knowledge_generation_items AS items "
            "ON items.generation_id = state.current_generation_id"
        ).fetchone()
        assert current is not None
        assert "global mode" in current[0]

    deduplicated_run = service.create_run(
        (canonical_document_id, d1.document.document_id),
        provider="new-provider",
        model="new-model",
    )
    assert len(deduplicated_run.jobs) == 1
    with pytest.raises(DesktopImportError) as active_error:
        service.create_run((d1.document.document_id,), provider="new-provider", model="new-model")
    assert active_error.value.code == "knowledge_reanalysis_already_running"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (canonical_document_id,),
        )

    statuses = DesktopKnowledgeReanalysisService(kb_dir).overview()["documents"]
    assert len(statuses) == 1
    assert statuses[0]["document_id"] == d1.document.document_id
    assert statuses[0]["state"] == "current"


def test_open_recovery_marks_interrupted_reanalysis_failed_without_running_it(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document_id,), provider="provider", model="model")

    recover_interrupted_knowledge_reanalysis(kb_dir)

    recovered = service.overview()["runs"][0]
    assert recovered["run_id"] == run.run_id
    assert recovered["status"] == "failed"
    assert recovered["jobs"][0]["error_code"] == "knowledge_reanalysis_interrupted"


def test_interrupted_inflight_result_cannot_mutate_knowledge(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document_id,), provider="provider", model="model")
    entered = threading.Event()
    release = threading.Event()

    def transport(request, _timeout_seconds):
        entered.set()
        assert release.wait(5)
        evidence_id = str(json.loads(request.content)["evidence"][0]["evidence_id"])
        return _analysis_response(evidence_id, "The runtime uses a global mode.")

    worker = threading.Thread(
        target=service.run_job,
        args=(
            run.jobs[0].job_id,
            DesktopModelGateway(transport, provider_name="provider", model_name="model"),
        ),
    )
    worker.start()
    assert entered.wait(5)
    recover_interrupted_knowledge_reanalysis(kb_dir)
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    recovered = service.overview()["runs"][0]["jobs"][0]
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "knowledge_reanalysis_interrupted"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT checkpoint_json FROM knowledge_reanalysis_jobs WHERE job_id = ?",
            (run.jobs[0].job_id,),
        ).fetchone() == (None,)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_reconciliation_candidates
            WHERE status = 'pending_conflict' AND content_markdown LIKE '%global mode%'
            """
        ).fetchone() == (0,)


def test_retry_waits_for_the_original_bulk_run_to_finish(tmp_path: Path) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document_id,), provider="provider", model="model")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE knowledge_reanalysis_jobs SET status = 'failed' WHERE job_id = ?",
            (run.jobs[0].job_id,),
        )

    with pytest.raises(DesktopImportError) as error:
        service.retry_job(run.jobs[0].job_id, provider="provider", model="model")

    assert error.value.code == "knowledge_reanalysis_run_active"


def test_reanalysis_batches_preserve_duplicate_evidence_occurrences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "repeated-sections.md"
    repeated_paragraph = "Shared operational prerequisite. " * 400
    source.write_text(
        "\n\n".join(
            f"# Section {ordinal}\n\n{repeated_paragraph}\n\n{repeated_paragraph}"
            for ordinal in range(7)
        ),
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        occurrence_count, unique_evidence_count = connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT evidence_id)
            FROM evidence_occurrences WHERE document_id = ?
            """,
            (document.document_id,),
        ).fetchone()
    assert occurrence_count > unique_evidence_count

    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document.document_id,), provider="provider", model="model")

    def transport(request, _timeout_seconds):
        if request.operation == "knowledge_fact_harvest":
            return _empty_analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            return json.dumps({"document_description": "Merged duplicate evidence."})
        raise AssertionError(request.operation)

    service.run_job(
        run.jobs[0].job_id,
        DesktopModelGateway(transport, provider_name="provider", model_name="model"),
    )

    job = service.overview()["runs"][0]["jobs"][0]
    assert job["status"] == "completed", job["reason"]
    assert job["batch_total"] > 1


def test_reanalysis_retry_reuses_completed_long_document_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "long.md"
    source.write_text(
        "\n\n".join(
            f"# Section {ordinal}\n\n" + (f"Durable fact for section {ordinal}. " * 400)
            for ordinal in range(7)
        ),
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document.document_id,), provider="provider", model="model")
    operations: list[str] = []
    failed_once = False

    def transport(request, _timeout_seconds):
        nonlocal failed_once
        if request.operation == "knowledge_fact_harvest":
            ordinal = int(json.loads(request.content)["batch_ordinal"])
            operations.append(f"batch:{ordinal}")
            if ordinal == 1 and not failed_once:
                failed_once = True
                raise DesktopModelTransportError("input")
            return _empty_analysis("batch")
        if request.operation == "knowledge_analysis_merge":
            operations.append("merge")
            return json.dumps({"document_description": "Merged long document."})
        raise AssertionError(request.operation)

    gateway = DesktopModelGateway(transport, provider_name="provider", model_name="model")
    service.run_job(run.jobs[0].job_id, gateway)
    assert service.overview()["runs"][0]["status"] == "failed"

    retried = service.retry_job(run.jobs[0].job_id, provider="provider", model="model")
    service.run_job(retried.jobs[0].job_id, gateway)

    assert operations[:3] == ["batch:0", "batch:1", "batch:1"]
    assert operations.count("batch:0") == 1
    assert operations[-1] == "merge"
    assert service.overview()["runs"][0]["status"] == "completed"
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        checkpoint = json.loads(
            connection.execute(
                "SELECT checkpoint_json FROM knowledge_reanalysis_jobs WHERE job_id = ?",
                (retried.jobs[0].job_id,),
            ).fetchone()[0]
        )
        checkpoint.pop("analysis_prompt_digest")
        connection.execute(
            "UPDATE knowledge_reanalysis_jobs SET checkpoint_json = ? WHERE job_id = ?",
            (json.dumps(checkpoint), retried.jobs[0].job_id),
        )
    assert service.overview()["documents"][0]["state"] == "current"
    monkeypatch.setattr(
        "openkb.knowledge.reanalysis.service.KNOWLEDGE_ANALYSIS_BATCH_PIPELINE_DIGEST",
        "changed-batch-pipeline-digest",
    )
    assert service.overview()["documents"][0]["state"] == "analysis_outdated"


def test_reanalysis_resume_gates_the_profile_pinned_by_the_persisted_plan(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _imported_document(tmp_path)
    service = DesktopKnowledgeReanalysisService(kb_dir)
    run = service.create_run((document_id,), provider="deepseek", model="deepseek-v4-pro")

    def role_gateway(settings: DesktopModelSettings, transport):
        terminal = DesktopModelGateway(
            transport,
            provider_name="deepseek",
            model_name=settings.analysis_model_name,
        )
        return DesktopRoleModelGateway(
            settings=settings,
            default_gateway=terminal,
            analysis_gateway=terminal,
            answer_gateway=terminal,
        )

    old_settings = DesktopModelSettings(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
        analysis_reasoning="off",
    )
    old_profile = analysis_execution_profile_for_settings(old_settings)
    capability_store = DesktopModelCapabilityStore(kb_dir)
    capability_store.mark_verified(old_profile)

    def fail_first(_request, _timeout_seconds):
        raise DesktopModelTransportError("input")

    service.run_job(run.jobs[0].job_id, role_gateway(old_settings, fail_first))
    persisted = DesktopKnowledgeAnalysisBatchStore(
        kb_dir,
        reanalysis=True,
    ).persisted_plan(run.jobs[0].job_id)
    assert persisted is not None
    assert persisted.execution_profile == old_profile
    capability_store.invalidate(
        old_profile,
        failure_code="model_execution_profile_changed",
        reason="Profile changed in the test.",
    )

    retried = service.retry_job(
        run.jobs[0].job_id,
        provider="deepseek",
        model="deepseek-v4-pro",
    )
    new_settings = DesktopModelSettings(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
        analysis_reasoning="high",
    )
    capability_store.mark_verified(analysis_execution_profile_for_settings(new_settings))
    resumed_calls = 0

    def unexpected_resume(_request, _timeout_seconds):
        nonlocal resumed_calls
        resumed_calls += 1
        raise AssertionError("An unverified persisted profile must stop before dispatch.")

    service.run_job(retried.jobs[0].job_id, role_gateway(new_settings, unexpected_resume))

    assert resumed_calls == 0
    assert service.overview()["runs"][0]["jobs"][0]["status"] == "pending"


def test_engine_starts_graph_worker_after_reanalysis_requeues_candidate_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = object()
    graph_starts: list[tuple[Path, object]] = []
    retry_authorizations: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, str]]] = []

    class ReanalysisService:
        def __init__(self, kb_dir: Path) -> None:
            assert kb_dir == tmp_path

        def pending_job_ids(self, run_id: str) -> tuple[str, ...]:
            assert run_id == "run-id"
            return ("job-id",)

        def run_job(
            self, job_id: str, selected_gateway: object, **controls: object
        ) -> tuple[str, ...]:
            assert job_id == "job-id"
            assert selected_gateway is gateway
            assert callable(controls["should_stop"])
            assert callable(controls["authorize_retry"])
            assert controls["retry_scope"] == "run-id"
            controls["authorize_retry"](
                SimpleNamespace(
                    operation="knowledge_page_planning",
                    capability_identity=None,
                    prompt_contract_digest=None,
                )
            )
            return ("document-id",)

    class Server:
        _shutdown = threading.Event()
        _knowledge_reanalysis_lease = 7
        _workers_lock = threading.Lock()
        _workers = {threading.current_thread()}
        _knowledge_graph_extraction_cancelled = {(tmp_path, "document-id")}

        @staticmethod
        def _emit_event(kind: str, data: dict[str, str]) -> None:
            events.append((kind, data))

    monkeypatch.setattr(reanalysis_engine, "DesktopKnowledgeReanalysisService", ReanalysisService)
    monkeypatch.setattr(
        reanalysis_engine,
        "authorize_model_operation_retry",
        lambda _kb_dir, _gateway, **kwargs: retry_authorizations.append(
            (str(kwargs["operation"]), str(kwargs["retry_scope"]))
        ),
    )
    monkeypatch.setattr(
        reanalysis_engine,
        "revoke_model_operation_retry_scope",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        reanalysis_engine.knowledge_graph_engine,
        "start_knowledge_graph_extractions",
        lambda _server, kb_dir, selected_gateway: graph_starts.append((kb_dir, selected_gateway)),
    )

    reanalysis_engine._run_jobs(Server(), tmp_path, "run-id", gateway, 7)  # type: ignore[arg-type]

    assert graph_starts == [(tmp_path, gateway)]
    assert retry_authorizations == [("knowledge_page_planning", "run-id")]
    assert Server._knowledge_graph_extraction_cancelled == set()
    assert events == [("knowledge_reanalysis.updated", {"run_id": "run-id", "job_id": "job-id"})]
