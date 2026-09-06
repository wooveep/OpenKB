"""General Recovery Assessment for failed or incompatible Model Execution Profiles."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

from openkb.importing.artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    document_ir_from_checkpoint,
    evidence_from_checkpoint,
)
from openkb.knowledge.analysis.plan import (
    KnowledgeAnalysisPlan,
    estimate_model_tokens,
    hierarchical_merge_topology,
)
from openkb.knowledge.analysis.recovery_store import discard_analysis_in, plan_identity_in
from openkb.locks import kb_ingest_lock
from openkb.models.legacy_recovery import (
    DesktopLegacyModelRecoveryService,
    legacy_model_recovery_assessment_in,
)
from openkb.models.recovery_types import (
    RESTART_CURRENT_PLAN,
    ModelRecoveryAssessment,
)
from openkb.models.result_failure import MODEL_RESULT_FAILURE_CODES
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

_PROFILE_REPLAN_ERROR_CODES = MODEL_RESULT_FAILURE_CODES | {"knowledge_analysis_replan_required"}


class DesktopModelRecoveryService:
    """Assess all model recovery kinds and delegate only retired deadlines to Legacy."""

    def __init__(self, kb_dir: Path) -> None:
        resolved = kb_dir.expanduser().resolve()
        self._kb_dir = resolved
        self._state_dir = desktop_state_dir(resolved)
        self._database_path = desktop_state_database_path(resolved)

    def assessment(self, job_id: str) -> ModelRecoveryAssessment | None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                return model_recovery_assessment_in(connection, job_id)
            finally:
                connection.close()

    def select_required(
        self,
        job_id: str,
        assessment: ModelRecoveryAssessment,
        choice: str | None,
        *,
        model_override: str | None = None,
        context_capacity: int | None = None,
    ) -> ModelRecoveryAssessment:
        """Require an explicit choice with wording appropriate to the recovery kind."""
        if choice is None:
            legacy_deadline = assessment.kind == "legacy_model_deadline"
            raise DesktopImportError(
                (
                    "legacy_model_recovery_choice_required"
                    if legacy_deadline
                    else "model_recovery_choice_required"
                ),
                (
                    "Choose a legacy Knowledge Analysis recovery path before continuing."
                    if legacy_deadline
                    else "Choose the current Knowledge Analysis Replan before continuing."
                ),
            )
        return self.select(
            job_id,
            choice,
            model_override=model_override,
            context_capacity=context_capacity,
        )

    def select(
        self,
        job_id: str,
        choice: str,
        *,
        model_override: str | None = None,
        context_capacity: int | None = None,
    ) -> ModelRecoveryAssessment:
        assessment = self.assessment(job_id)
        if assessment is None:
            raise DesktopImportError(
                "model_recovery_unavailable",
                "This import has no available Model Recovery Assessment.",
            )
        if assessment.kind == "legacy_model_deadline":
            return DesktopLegacyModelRecoveryService(self._kb_dir).select(
                job_id,
                choice,
                model_override=model_override,
                context_capacity=context_capacity,
            )
        if choice != RESTART_CURRENT_PLAN:
            raise DesktopImportError(
                "model_recovery_incompatible",
                "A failed Model Execution Profile must restart from a current plan.",
                suggested_action="Start a current Knowledge Analysis Plan.",
            )
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = model_recovery_assessment_in(connection, job_id)
                if current is None or current.kind != "model_execution_profile_replan":
                    raise DesktopImportError(
                        "model_recovery_unavailable",
                        "The Model Recovery Assessment changed before selection.",
                    )
                discard_analysis_in(connection, job_id)
                _record_selection_in(
                    connection,
                    job_id=job_id,
                    assessment=current,
                    choice=choice,
                    model_override=model_override,
                )
                connection.commit()
                return replace(current, selected_choice=choice)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def record_resulting_plan(self, job_id: str) -> None:
        """Attach the current immutable plan identity to the shared recovery audit."""
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE legacy_model_recovery_audit
                        SET resulting_plan_identity = ?
                        WHERE recovery_id = (
                            SELECT recovery_id FROM legacy_model_recovery_audit
                            WHERE job_id = ? ORDER BY selected_at DESC LIMIT 1
                        )
                        """,
                        (plan_identity_in(connection, job_id), job_id),
                    )
            finally:
                connection.close()


def model_recovery_assessment_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> ModelRecoveryAssessment | None:
    """Project the shared Failed Documents recovery wire without mutating state."""
    quarantine = connection.execute(
        "SELECT error_code, stage FROM quarantined_documents WHERE job_id = ?", (job_id,)
    ).fetchone()
    if quarantine is None:
        return None
    error_code = str(quarantine[0])
    if error_code == "model_deadline_exceeded":
        return legacy_model_recovery_assessment_in(connection, job_id)
    if str(quarantine[1]) != "model_analysis" or error_code not in _PROFILE_REPLAN_ERROR_CODES:
        return None
    return _model_profile_replan_assessment_in(connection, job_id)


def _model_profile_replan_assessment_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> ModelRecoveryAssessment:
    evidence = _evidence_in(connection, job_id)
    rows = _batch_rows(connection, job_id)
    plan_row = connection.execute(
        "SELECT plan_json, prompt_contract_digest FROM knowledge_analysis_plans WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    plan: KnowledgeAnalysisPlan | None = None
    if plan_row is not None:
        try:
            plan = KnowledgeAnalysisPlan.from_dict(json.loads(str(plan_row[0])))
        except (json.JSONDecodeError, ValueError):
            plan = None
    provider = plan.provider if plan is not None else None
    model = plan.analysis_model if plan is not None else None
    if provider is None or model is None:
        for row in rows:
            checkpoint = _mapping_json(row[4])
            provider_value = checkpoint.get("provider")
            model_value = checkpoint.get("model")
            if provider is None and isinstance(provider_value, str):
                provider = provider_value
            if model is None and isinstance(model_value, str):
                model = model_value
    completed_batches = sum(1 for row in rows if str(row[2]) == "completed")
    discarded = sum(1 for row in rows if row[4] is not None)
    discarded += int(
        connection.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM knowledge_analysis_merges
                WHERE job_id = ? AND checkpoint_json IS NOT NULL
            )
            """,
            (job_id,),
        ).fetchone()[0]
    )
    discarded += int(
        connection.execute(
            """
            SELECT COUNT(*) FROM knowledge_analysis_merge_nodes
            WHERE job_id = ? AND checkpoint_json IS NOT NULL
            """,
            (job_id,),
        ).fetchone()[0]
    )
    total_batches = (
        len(plan.batches) if plan is not None and plan.batches else len(rows) if rows else 1
    )
    replacement_calls = total_batches + len(hierarchical_merge_topology(total_batches))
    replacement_input = sum(estimate_model_tokens(block.text) for _, block in evidence)
    selected = connection.execute(
        """
        SELECT recovery_choice FROM legacy_model_recovery_audit
        WHERE job_id = ? ORDER BY selected_at DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return ModelRecoveryAssessment(
        compatible=False,
        compatibility_reason="incompatible_or_failed_model_execution_profile",
        previous_prompt_digest=(str(plan_row[1]) if plan_row is not None else None),
        provider=provider,
        model=model,
        completed_batches=completed_batches,
        total_batches=total_batches,
        continue_remaining_calls=replacement_calls,
        continue_input_tokens=replacement_input,
        restart_remaining_calls=replacement_calls,
        restart_input_tokens=replacement_input,
        recommended_choice=RESTART_CURRENT_PLAN,
        selected_choice=str(selected[0]) if selected is not None else None,
        kind="model_execution_profile_replan",
        discarded_model_checkpoints=discarded,
    )


def _record_selection_in(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    assessment: ModelRecoveryAssessment,
    choice: str,
    model_override: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO legacy_model_recovery_audit (
            recovery_id, job_id, recovery_choice, compatible,
            previous_prompt_digest, provider, model,
            continue_remaining_calls, continue_input_tokens,
            restart_remaining_calls, restart_input_tokens,
            resulting_plan_identity, selected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            uuid.uuid4().hex,
            job_id,
            choice,
            int(assessment.compatible),
            assessment.previous_prompt_digest,
            assessment.provider,
            model_override or assessment.model,
            assessment.continue_remaining_calls,
            assessment.continue_input_tokens,
            assessment.restart_remaining_calls,
            assessment.restart_input_tokens,
            _timestamp(),
        ),
    )


def _evidence_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> tuple[tuple[str, DocumentIRBlock], ...]:
    checkpoints: dict[str, object] = {}
    rows = connection.execute(
        """
        SELECT stage_runs.stage, stage_run_runtime.checkpoint_json
        FROM stage_runs JOIN stage_run_runtime
            ON stage_run_runtime.stage_run_id = stage_runs.stage_run_id
        WHERE stage_runs.job_id = ? AND stage_runs.stage IN ('document_ir', 'evidence')
        """,
        (job_id,),
    ).fetchall()
    for stage, payload in rows:
        try:
            checkpoints[str(stage)] = json.loads(str(payload))
        except (json.JSONDecodeError, TypeError) as error:
            raise DesktopImportError(
                "import_checkpoint_invalid", "Model Recovery inputs are unavailable."
            ) from error
    blocks = document_ir_from_checkpoint(checkpoints.get("document_ir"))
    return evidence_from_checkpoint(checkpoints.get("evidence"), blocks)


def _batch_rows(connection: sqlite3.Connection, job_id: str) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        SELECT batch_id, stage_run_id, status, evidence_ids_json, checkpoint_json
        FROM knowledge_analysis_batches WHERE job_id = ? ORDER BY batch_ordinal
        """,
        (job_id,),
    ).fetchall()


def _mapping_json(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _connect(path: Path) -> sqlite3.Connection:
    connection = connect_database(path)
    return connection
