"""Explicit recovery from historical application-imposed model deadlines."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from openkb import desktop_model_transport
from openkb.desktop_engine import DesktopEngineServer, DesktopRequestError
from openkb.desktop_engine_imports import run_import
from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_legacy_model_recovery import (
    CONTINUE_COMPATIBLE,
    RESTART_CURRENT_PLAN,
    DesktopLegacyModelRecoveryService,
)
from openkb.desktop_model_capability_store import DesktopModelCapabilityStore
from openkb.desktop_model_execution_profile import analysis_execution_profile_for_settings
from openkb.desktop_model_gateway import DesktopModelCancelledError
from openkb.desktop_model_recovery import DesktopModelRecoveryService
from openkb.desktop_model_settings import save_desktop_model_settings
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _legacy_deadline_job(tmp_path, *, digest: str | None = None):
    kb_dir = tmp_path / "kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir, name="Legacy KB")
    source = tmp_path / "legacy.txt"
    source.write_text("\n\n".join(f"## Section {i}\nEvidence {i}." for i in range(8)))
    importer = DesktopTextImportService(kb_dir, require_model_analysis=True)
    with pytest.raises(DesktopImportError) as captured:
        importer.import_text(source)
    assert captured.value.code == "awaiting_model_configuration"
    job_id = importer.list_import_jobs()["jobs"][0]["job"]["job_id"]
    now = datetime.now(tz=timezone.utc).isoformat()
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        model_stage = connection.execute(
            "SELECT stage_run_id FROM stage_runs WHERE job_id = ? AND stage = 'model_analysis'",
            (job_id,),
        ).fetchone()[0]
        evidence_payload = json.loads(
            connection.execute(
                """
                SELECT checkpoint_json FROM stage_run_runtime
                JOIN stage_runs USING(stage_run_id)
                WHERE stage_runs.job_id = ? AND stage_runs.stage = 'evidence'
                """,
                (job_id,),
            ).fetchone()[0]
        )
        evidence_ids = [item["evidence_id"] for item in evidence_payload]
        midpoint = len(evidence_ids) // 2
        batches = (evidence_ids[:midpoint], evidence_ids[midpoint:])
        checkpoint = {
            "prompt_digest": digest or prompt_contract_for("knowledge_analysis_batch").digest,
            "provider": "custom",
            "model": "legacy-analysis-model",
        }
        connection.execute(
            "UPDATE import_jobs SET status = 'failed', error_code = ? WHERE job_id = ?",
            ("model_deadline_exceeded", job_id),
        )
        connection.execute(
            "UPDATE import_job_runtime SET status = 'failed' WHERE job_id = ?",
            (job_id,),
        )
        connection.execute(
            "UPDATE stage_runs SET status = 'failed', error_code = ? WHERE stage_run_id = ?",
            ("model_deadline_exceeded", model_stage),
        )
        connection.execute(
            "UPDATE stage_run_runtime SET status = 'failed', error_code = ? WHERE stage_run_id = ?",
            ("model_deadline_exceeded", model_stage),
        )
        connection.execute(
            """
            INSERT INTO quarantined_documents (
                job_id, stage_run_id, stage, error_code, reason,
                suggested_action, attempt_count, created_at
            ) VALUES (?, ?, 'model_analysis', 'model_deadline_exceeded', ?, ?, 1, ?)
            """,
            (
                job_id,
                model_stage,
                "Historical 60-second model deadline.",
                "Choose an explicit recovery path.",
                now,
            ),
        )
        for ordinal, ids in enumerate(batches):
            connection.execute(
                """
                INSERT INTO knowledge_analysis_batches (
                    batch_id, job_id, stage_run_id, batch_ordinal,
                    section_paths_json, evidence_ids_json, status,
                    checkpoint_json, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"legacy-batch-{ordinal}",
                    job_id,
                    model_stage,
                    ordinal,
                    json.dumps(ids),
                    "completed" if ordinal == 0 else "failed",
                    json.dumps(checkpoint) if ordinal == 0 else None,
                    None if ordinal == 0 else "model_deadline_exceeded",
                    now,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO knowledge_analysis_merges (
                job_id, stage_run_id, status, checkpoint_json,
                error_code, created_at, updated_at
            ) VALUES (?, ?, 'pending', NULL, NULL, ?, ?)
            """,
            (job_id, model_stage, now, now),
        )
        connection.commit()
    return kb_dir, job_id


def test_compatible_legacy_batches_are_estimated_and_only_prepared_after_selection(tmp_path):
    kb_dir, job_id = _legacy_deadline_job(tmp_path)
    service = DesktopLegacyModelRecoveryService(kb_dir)

    assessment = service.assessment(job_id)
    assert assessment is not None
    assert assessment.compatible is True
    assert assessment.completed_batches == 1
    assert assessment.total_batches == 2
    assert assessment.continue_remaining_calls == 2
    assert assessment.recommended_choice == RESTART_CURRENT_PLAN
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            == 0
        )

    selected = service.select(job_id, CONTINUE_COMPATIBLE)

    assert selected.selected_choice == CONTINUE_COMPATIBLE
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM knowledge_analysis_batches WHERE job_id = ? ORDER BY batch_ordinal",
            (job_id,),
        ).fetchall() == [("completed",), ("pending",)]
        audit = connection.execute(
            """
            SELECT recovery_choice, provider, model, resulting_plan_identity
            FROM legacy_model_recovery_audit WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
    assert audit[:3] == (CONTINUE_COMPATIBLE, "custom", "legacy-analysis-model")
    assert len(audit[3]) == 64


def test_unknown_prompt_requires_restart_and_preserves_parser_ir_and_evidence(tmp_path):
    kb_dir, job_id = _legacy_deadline_job(tmp_path, digest="unknown-prompt-digest")
    service = DesktopLegacyModelRecoveryService(kb_dir)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        evidence_before = connection.execute(
            """
            SELECT checkpoint_json FROM stage_run_runtime
            JOIN stage_runs USING(stage_run_id)
            WHERE stage_runs.job_id = ? AND stage_runs.stage = 'evidence'
            """,
            (job_id,),
        ).fetchone()[0]

    assessment = service.assessment(job_id)
    assert assessment is not None and assessment.compatible is False
    assert assessment.recommended_choice == RESTART_CURRENT_PLAN
    with pytest.raises(DesktopImportError) as captured:
        service.select(job_id, CONTINUE_COMPATIBLE)
    assert captured.value.code == "legacy_model_recovery_incompatible"

    service.select(job_id, RESTART_CURRENT_PLAN)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        evidence_after = connection.execute(
            """
            SELECT checkpoint_json FROM stage_run_runtime
            JOIN stage_runs USING(stage_run_id)
            WHERE stage_runs.job_id = ? AND stage_runs.stage = 'evidence'
            """,
            (job_id,),
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_analysis_batches WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            == 0
        )
    assert evidence_after == evidence_before


def test_historical_deadline_recovery_requires_an_explicit_choice(tmp_path):
    kb_dir, job_id = _legacy_deadline_job(tmp_path)

    with pytest.raises(DesktopImportError) as captured:
        DesktopTextImportService(kb_dir).recover_text(job_id, DesktopRecoveryOverride())

    assert captured.value.code == "legacy_model_recovery_choice_required"
    task = DesktopTextImportService(kb_dir).task(job_id).as_dict()
    assert task["legacy_model_recovery"]["starts_automatically"] is False
    assert set(task["legacy_model_recovery"]["choices"]) == {
        CONTINUE_COMPATIBLE,
        RESTART_CURRENT_PLAN,
    }


def test_protocol_failure_requires_replan_and_counts_discarded_model_checkpoints(tmp_path):
    kb_dir, job_id = _legacy_deadline_job(tmp_path)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE quarantined_documents SET error_code = ? WHERE job_id = ?",
            ("reasoning_output_exhausted", job_id),
        )
        connection.execute(
            "UPDATE import_jobs SET error_code = ? WHERE job_id = ?",
            ("reasoning_output_exhausted", job_id),
        )
        connection.commit()
    assert DesktopLegacyModelRecoveryService(kb_dir).assessment(job_id) is None
    service = DesktopModelRecoveryService(kb_dir)

    assessment = service.assessment(job_id)

    assert assessment is not None
    payload = assessment.as_dict()
    assert payload["kind"] == "model_execution_profile_replan"
    assert payload["compatible"] is False
    assert payload["discarded_model_checkpoints"] == 1
    assert payload["choices"][RESTART_CURRENT_PLAN]["estimated_remaining_calls"] >= 1
    assert payload["choices"][CONTINUE_COMPATIBLE]["allowed"] is False

    with pytest.raises(DesktopImportError) as captured:
        DesktopTextImportService(kb_dir).recover_text(job_id, DesktopRecoveryOverride())
    assert captured.value.code == "model_recovery_choice_required"
    assert "legacy" not in str(captured.value).lower()

    service.select(job_id, RESTART_CURRENT_PLAN)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_analysis_batches WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT checkpoint_json FROM stage_run_runtime JOIN stage_runs USING(stage_run_id)
            WHERE stage_runs.job_id = ? AND stage_runs.stage = 'evidence'
            """,
                (job_id,),
            ).fetchone()[0]
            is not None
        )


def test_check_and_recover_verifies_replacement_before_discarding_model_state(
    tmp_path, monkeypatch, caplog
) -> None:
    kb_dir, job_id = _legacy_deadline_job(tmp_path)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE quarantined_documents SET error_code = ? WHERE job_id = ?",
            ("reasoning_output_exhausted", job_id),
        )
        connection.execute(
            "UPDATE import_jobs SET error_code = ? WHERE job_id = ?",
            ("reasoning_output_exhausted", job_id),
        )
        deterministic_before = connection.execute(
            """
            SELECT stage_runs.stage, stage_run_runtime.checkpoint_json
            FROM stage_runs JOIN stage_run_runtime USING(stage_run_id)
            WHERE stage_runs.job_id = ?
              AND stage_runs.stage IN ('raw_asset', 'document_ir', 'evidence')
            ORDER BY stage_runs.stage
            """,
            (job_id,),
        ).fetchall()
        model_before = connection.execute(
            """
            SELECT batch_id, status, checkpoint_json
            FROM knowledge_analysis_batches WHERE job_id = ? ORDER BY batch_ordinal
            """,
            (job_id,),
        ).fetchall()
        connection.commit()
    save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
        analysis_reasoning="high",
    )
    calls: list[str] = []
    reasoning_efforts: list[str | None] = []

    class FailingCapabilityTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            reasoning_efforts.append(request.reasoning_effort)
            return '{"status":"not-ok"}'

    monkeypatch.setattr(
        desktop_model_transport, "DesktopLiteLLMTransport", FailingCapabilityTransport
    )
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "openkb.desktop_engine_imports.page_tree_enrichment_engine.start_page_tree_enrichments",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "openkb.desktop_engine_imports.knowledge_graph_engine.start_knowledge_graph_extractions",
        lambda *_args, **_kwargs: None,
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    override = DesktopRecoveryOverride(
        reasoning="off",
        legacy_recovery_choice=RESTART_CURRENT_PLAN,
        check_and_recover=True,
    )
    recovery_gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir, override)
    assert recovery_gateway is not None
    recovery_profile = recovery_gateway.execution_profile_for_operation("knowledge_analysis")

    with caplog.at_level(logging.WARNING, logger="openkb.desktop_model_failure_logging"):
        with pytest.raises(DesktopRequestError, match="schema-valid structured output"):
            run_import(
                server,
                kb_dir,
                request_id="failed-check",
                job_id=job_id,
                recovery_override=override,
            )

    assert calls == ["model_capability_analysis"]
    validation_logs = [
        record for record in caplog.records if record.msg == "model_result_validation_failed"
    ]
    assert len(validation_logs) == 1
    validation_fields = validation_logs[0].openkb_fields
    assert validation_fields["failure_kind"] == "model_result_failure"
    assert validation_fields["phase"] == "capability_validation"
    assert validation_fields["error_code"] == "model_response_invalid"
    assert validation_fields["failure_event_id"]
    failed_capability = DesktopModelCapabilityStore(kb_dir).state(recovery_profile)
    assert failed_capability.status == "failed"
    assert failed_capability.failure_code == "model_capability_check_failed"
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                """
            SELECT stage_runs.stage, stage_run_runtime.checkpoint_json
            FROM stage_runs JOIN stage_run_runtime USING(stage_run_id)
            WHERE stage_runs.job_id = ?
              AND stage_runs.stage IN ('raw_asset', 'document_ir', 'evidence')
            ORDER BY stage_runs.stage
            """,
                (job_id,),
            ).fetchall()
            == deterministic_before
        )
        assert (
            connection.execute(
                """
            SELECT batch_id, status, checkpoint_json
            FROM knowledge_analysis_batches WHERE job_id = ? ORDER BY batch_ordinal
            """,
                (job_id,),
            ).fetchall()
            == model_before
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM legacy_model_recovery_audit WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT status FROM import_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            == "failed"
        )

    class SuccessfulTransport:
        def __init__(self, *, model, bundle):
            del model, bundle

        def __call__(self, request, _timeout_seconds):
            calls.append(request.operation)
            reasoning_efforts.append(request.reasoning_effort)
            if request.operation == "model_capability_analysis":
                return '{"status":"ok"}'
            if request.operation == "knowledge_analysis_merge":
                return '{"document_description":"Recovered analysis."}'
            scope = "batch" if request.operation == "knowledge_analysis_batch" else "document"
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-analysis.v2",
                    "analysis_scope": scope,
                    "document_description": "Recovered analysis.",
                    "document_summary": [],
                    "candidates": [],
                }
            )

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", SuccessfulTransport)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())

    recovered = run_import(
        server,
        kb_dir,
        request_id="successful-check",
        job_id=job_id,
        recovery_override=override,
    )

    assert recovered["job"]["job_id"] == job_id
    assert recovered["job"]["status"] == "completed"
    assert recovered["document"]["availability"] == "available"
    assert calls.count("model_capability_analysis") == 2
    assert DesktopModelCapabilityStore(kb_dir).state(recovery_profile).status == "verified"
    assert reasoning_efforts[0] == "off"
    assert reasoning_efforts[1] == "off"
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                """
            SELECT stage_runs.stage, stage_run_runtime.checkpoint_json
            FROM stage_runs JOIN stage_run_runtime USING(stage_run_id)
            WHERE stage_runs.job_id = ?
              AND stage_runs.stage IN ('raw_asset', 'document_ir', 'evidence')
            ORDER BY stage_runs.stage
            """,
                (job_id,),
            ).fetchall()
            == deterministic_before
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM knowledge_analysis_batches
                WHERE batch_id LIKE 'legacy-batch-%'
                """,
            ).fetchone()[0]
            == 0
        )
        plan_identity = connection.execute(
            """
            SELECT json_extract(plan_json, '$.plan_identity')
            FROM knowledge_analysis_plans WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()[0]
        plan_json = json.loads(
            connection.execute(
                "SELECT plan_json FROM knowledge_analysis_plans WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
        )
        assert plan_json["execution_profile"]["reasoning_effort"] == "off"
        audit_identity = connection.execute(
            """
            SELECT resulting_plan_identity FROM legacy_model_recovery_audit
            WHERE job_id = ? ORDER BY selected_at DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()[0]
    assert audit_identity == plan_identity


def test_check_and_recover_cancellation_preserves_recovery_state(tmp_path) -> None:
    kb_dir, job_id = _legacy_deadline_job(tmp_path)
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE quarantined_documents SET error_code = ? WHERE job_id = ?",
            ("reasoning_output_exhausted", job_id),
        )
        connection.execute(
            "UPDATE import_jobs SET error_code = ? WHERE job_id = ?",
            ("reasoning_output_exhausted", job_id),
        )
        batches_before = connection.execute(
            "SELECT batch_id, status, checkpoint_json FROM knowledge_analysis_batches "
            "WHERE job_id = ? ORDER BY batch_ordinal",
            (job_id,),
        ).fetchall()
        connection.commit()
    settings = save_desktop_model_settings(
        kb_dir,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_base_url="https://api.deepseek.com",
        api_key="test-key",
        max_concurrent_model_calls=1,
    )
    profile = analysis_execution_profile_for_settings(settings)

    class CancelledRecoveryGateway:
        def execution_profile_for_operation(self, operation):
            assert operation == "knowledge_analysis"
            return profile

        def analyze(self, request, *, on_event, is_cancelled):
            del request, on_event, is_cancelled
            raise DesktopModelCancelledError()

    gateway = CancelledRecoveryGateway()
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        model_gateway_factory=lambda _kb_dir, _override: gateway,  # type: ignore[arg-type]
    )
    override = DesktopRecoveryOverride(
        legacy_recovery_choice=RESTART_CURRENT_PLAN,
        check_and_recover=True,
    )

    with pytest.raises(DesktopRequestError) as captured:
        run_import(
            server,
            kb_dir,
            request_id="cancelled-check",
            job_id=job_id,
            recovery_override=override,
        )

    assert captured.value.code == "request_cancelled"
    assert "before Replan" in str(captured.value)
    state = DesktopModelCapabilityStore(kb_dir).state(profile)
    assert state.status == "cancelled"
    assert state.failure_code == "request_cancelled"
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT batch_id, status, checkpoint_json FROM knowledge_analysis_batches "
                "WHERE job_id = ? ORDER BY batch_ordinal",
                (job_id,),
            ).fetchall()
            == batches_before
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_model_recovery_audit WHERE job_id = ?",
            (job_id,),
        ).fetchone() == (0,)
