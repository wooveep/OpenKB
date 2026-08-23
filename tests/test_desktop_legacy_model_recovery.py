"""Explicit recovery from historical application-imposed model deadlines."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_legacy_model_recovery import (
    CONTINUE_COMPATIBLE,
    RESTART_CURRENT_PLAN,
    DesktopLegacyModelRecoveryService,
)
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
