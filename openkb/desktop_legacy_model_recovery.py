"""Explicit, estimated recovery choices for the retired model deadline policy."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_import_artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    document_ir_from_checkpoint,
    evidence_from_checkpoint,
)
from openkb.desktop_knowledge_analysis_plan import (
    KnowledgeAnalysisPlan,
    build_knowledge_analysis_plan,
    estimate_model_tokens,
    hierarchical_merge_topology,
)
from openkb.desktop_model_capabilities import model_capability_profile
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

CONTINUE_COMPATIBLE = "continue_compatible"
RESTART_CURRENT_PLAN = "restart_current_plan"
_CHOICES = frozenset({CONTINUE_COMPATIBLE, RESTART_CURRENT_PLAN})
_PROFILE_REPLAN_ERROR_CODES = frozenset(
    {
        "empty_final_result",
        "reasoning_only_result",
        "reasoning_output_exhausted",
        "model_response_invalid",
        "knowledge_analysis_replan_required",
    }
)


@dataclass(frozen=True)
class LegacyModelRecoveryAssessment:
    compatible: bool
    compatibility_reason: str
    previous_prompt_digest: str | None
    provider: str | None
    model: str | None
    completed_batches: int
    total_batches: int
    continue_remaining_calls: int
    continue_input_tokens: int
    restart_remaining_calls: int
    restart_input_tokens: int
    recommended_choice: str
    selected_choice: str | None = None
    kind: str = "legacy_model_deadline"
    discarded_model_checkpoints: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "compatible": self.compatible,
            "compatibility_reason": self.compatibility_reason,
            "previous_prompt_digest": self.previous_prompt_digest,
            "provider": self.provider,
            "model": self.model,
            "completed_batches": self.completed_batches,
            "total_batches": self.total_batches,
            "choices": {
                CONTINUE_COMPATIBLE: {
                    "allowed": self.compatible,
                    "estimated_remaining_calls": self.continue_remaining_calls,
                    "estimated_input_tokens": self.continue_input_tokens,
                    "reuses_completed_batches": self.completed_batches,
                },
                RESTART_CURRENT_PLAN: {
                    "allowed": True,
                    "estimated_remaining_calls": self.restart_remaining_calls,
                    "estimated_input_tokens": self.restart_input_tokens,
                    "reuses_parser_document_ir_evidence": True,
                    "discarded_model_checkpoints": self.discarded_model_checkpoints,
                },
            },
            "recommended_choice": self.recommended_choice,
            "selected_choice": self.selected_choice,
            "discarded_model_checkpoints": self.discarded_model_checkpoints,
            "starts_automatically": False,
        }


class DesktopLegacyModelRecoveryService:
    """Validate and prepare exactly one user-selected legacy recovery path."""

    def __init__(self, kb_dir: Path) -> None:
        resolved = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(resolved)
        self._database_path = desktop_state_database_path(resolved)

    def assessment(self, job_id: str) -> LegacyModelRecoveryAssessment | None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                return legacy_model_recovery_assessment_in(connection, job_id)
            finally:
                connection.close()

    def select(
        self,
        job_id: str,
        choice: str,
        *,
        model_override: str | None = None,
        context_capacity: int | None = None,
    ) -> LegacyModelRecoveryAssessment:
        if choice not in _CHOICES:
            raise DesktopImportError(
                "legacy_model_recovery_choice_invalid",
                "Choose whether to continue compatible batches or start a current plan.",
            )
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                assessment = legacy_model_recovery_assessment_in(connection, job_id)
                if assessment is None:
                    raise DesktopImportError(
                        "legacy_model_recovery_unavailable",
                        "This import did not end under the retired model deadline policy.",
                    )
                if choice == CONTINUE_COMPATIBLE and not assessment.compatible:
                    raise DesktopImportError(
                        "legacy_model_recovery_incompatible",
                        "The saved Prompt Contract or batch inputs are unsafe to continue.",
                        suggested_action="Start a current Knowledge Analysis Plan instead.",
                    )
                if choice == CONTINUE_COMPATIBLE:
                    self._synthesize_plan_in(
                        connection,
                        job_id,
                        assessment,
                        model_override=model_override,
                        context_capacity=context_capacity,
                    )
                    _reset_incomplete_analysis_in(connection, job_id)
                else:
                    _discard_legacy_analysis_in(connection, job_id)
                plan_identity = _plan_identity_in(connection, job_id)
                connection.execute(
                    """
                    INSERT INTO legacy_model_recovery_audit (
                        recovery_id, job_id, recovery_choice, compatible,
                        previous_prompt_digest, provider, model,
                        continue_remaining_calls, continue_input_tokens,
                        restart_remaining_calls, restart_input_tokens,
                        resulting_plan_identity, selected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        plan_identity,
                        _timestamp(),
                    ),
                )
                connection.commit()
                return replace(assessment, selected_choice=choice)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def record_resulting_plan(self, job_id: str) -> None:
        """Attach the immutable plan identity after a restart creates it."""
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
                        (_plan_identity_in(connection, job_id), job_id),
                    )
            finally:
                connection.close()

    def _synthesize_plan_in(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        assessment: LegacyModelRecoveryAssessment,
        *,
        model_override: str | None,
        context_capacity: int | None,
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,)
        ).fetchone():
            return
        evidence = _evidence_in(connection, job_id)
        rows = _batch_rows(connection, job_id)
        by_id = dict(evidence)
        planned: list[tuple[tuple[str, DocumentIRBlock], ...]] = []
        estimated: list[int] = []
        for row in rows:
            evidence_ids = _string_list(row[3])
            batch = tuple((evidence_id, by_id[evidence_id]) for evidence_id in evidence_ids)
            planned.append(batch)
            estimated.append(sum(estimate_model_tokens(block.text) for _, block in batch))
        model = model_override or assessment.model
        if model is None:
            raise DesktopImportError(
                "legacy_model_recovery_incompatible",
                "The saved Analysis Model provenance is unavailable.",
            )
        capability = model_capability_profile(model, context_capacity=context_capacity)
        plan = build_knowledge_analysis_plan(
            evidence=evidence,
            planned_batches=tuple(planned),
            provider=assessment.provider or "custom",
            model=model,
            capability=capability,
            contract=prompt_contract_for("knowledge_analysis_batch"),
            estimated_batch_tokens=tuple(estimated),
        )
        stage_run_id = str(rows[0][1])
        now = _timestamp()
        connection.execute(
            """
            INSERT INTO knowledge_analysis_plans (
                job_id, stage_run_id, document_ir_digest, analysis_model,
                prompt_contract_digest, plan_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                stage_run_id,
                plan.document_ir_digest,
                plan.analysis_model,
                plan.prompt_contract_digest,
                _json(plan.as_dict()),
                now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_analysis_merges (
                job_id, stage_run_id, status, checkpoint_json,
                error_code, created_at, updated_at
            ) VALUES (?, ?, 'pending', NULL, NULL, ?, ?)
            """,
            (job_id, stage_run_id, now, now),
        )
        for node in plan.merge_topology:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_analysis_merge_nodes (
                    node_id, job_id, level, node_ordinal, child_ids_json,
                    status, checkpoint_json, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                """,
                (
                    node.node_id,
                    job_id,
                    node.level,
                    node.ordinal,
                    _json(list(node.child_ids)),
                    now,
                    now,
                ),
            )


def legacy_model_recovery_assessment_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> LegacyModelRecoveryAssessment | None:
    quarantine = connection.execute(
        "SELECT error_code, stage FROM quarantined_documents WHERE job_id = ?", (job_id,)
    ).fetchone()
    if quarantine is None:
        return None
    error_code = str(quarantine[0])
    if error_code != "model_deadline_exceeded":
        return (
            _model_profile_replan_assessment_in(connection, job_id)
            if str(quarantine[1]) == "model_analysis"
            and error_code in _PROFILE_REPLAN_ERROR_CODES
            else None
        )
    evidence = _evidence_in(connection, job_id)
    rows = _batch_rows(connection, job_id)
    evidence_ids = {item[0] for item in evidence}
    identifiable = bool(rows)
    seen: set[str] = set()
    completed = 0
    previous_digest: str | None = None
    provider: str | None = None
    model: str | None = None
    digest_known = False
    continue_input = 0
    by_id = dict(evidence)
    for row in rows:
        ids = _string_list(row[3])
        identifiable = identifiable and bool(ids) and not seen.intersection(ids)
        identifiable = identifiable and set(ids).issubset(evidence_ids)
        seen.update(ids)
        if str(row[2]) == "completed":
            completed += 1
            checkpoint = _mapping_json(row[4])
            digest = checkpoint.get("prompt_digest")
            if isinstance(digest, str):
                previous_digest = previous_digest or digest
                digest_known = digest == prompt_contract_for("knowledge_analysis_batch").digest
            provider_value = checkpoint.get("provider")
            model_value = checkpoint.get("model")
            provider = provider or (provider_value if isinstance(provider_value, str) else None)
            model = model or (model_value if isinstance(model_value, str) else None)
        else:
            continue_input += sum(estimate_model_tokens(by_id[item].text) for item in ids)
    identifiable = identifiable and seen == evidence_ids
    plan_row = connection.execute(
        "SELECT plan_json, prompt_contract_digest FROM knowledge_analysis_plans WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if plan_row is not None:
        try:
            plan = KnowledgeAnalysisPlan.from_dict(json.loads(str(plan_row[0])))
        except (json.JSONDecodeError, ValueError):
            identifiable = False
        else:
            digest_known = _known_plan(plan)
            previous_digest = str(plan_row[1])
            provider = plan.provider
            model = plan.analysis_model
    compatible = identifiable and digest_known and provider is not None and model is not None
    total = len(rows)
    continue_calls = max(0, total - completed) + len(hierarchical_merge_topology(total))
    total_input = sum(estimate_model_tokens(block.text) for _, block in evidence)
    restart_batches = max(1, math.ceil(total_input / 8_000))
    restart_calls = restart_batches + len(hierarchical_merge_topology(restart_batches))
    continue_score = (continue_calls, continue_input)
    restart_score = (restart_calls, total_input)
    recommendation = (
        CONTINUE_COMPATIBLE
        if compatible and continue_score <= restart_score
        else RESTART_CURRENT_PLAN
    )
    selected = connection.execute(
        """
        SELECT recovery_choice FROM legacy_model_recovery_audit
        WHERE job_id = ? ORDER BY selected_at DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return LegacyModelRecoveryAssessment(
        compatible=compatible,
        compatibility_reason=(
            "known_prompt_and_identifiable_batches"
            if compatible
            else "unknown_prompt_or_unidentifiable_batches"
        ),
        previous_prompt_digest=previous_digest,
        provider=provider,
        model=model,
        completed_batches=completed,
        total_batches=total,
        continue_remaining_calls=continue_calls,
        continue_input_tokens=continue_input,
        restart_remaining_calls=restart_calls,
        restart_input_tokens=total_input,
        recommended_choice=recommendation,
        selected_choice=str(selected[0]) if selected is not None else None,
    )


def _model_profile_replan_assessment_in(
    connection: sqlite3.Connection,
    job_id: str,
) -> LegacyModelRecoveryAssessment:
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
        len(plan.batches)
        if plan is not None and plan.batches
        else len(rows)
        if rows
        else 1
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
    return LegacyModelRecoveryAssessment(
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


def _known_plan(plan: KnowledgeAnalysisPlan) -> bool:
    contracts = plan.prompt_contract_snapshot.get("contracts")
    if not isinstance(contracts, dict):
        return False
    for operation, snapshot in contracts.items():
        if not isinstance(operation, str) or not isinstance(snapshot, dict):
            return False
        try:
            known = prompt_contract_for(operation)
        except ValueError:
            return False
        if snapshot != known.snapshot():
            return False
    return True


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
                "import_checkpoint_invalid", "Legacy analysis inputs are unavailable."
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


def _reset_incomplete_analysis_in(connection: sqlite3.Connection, job_id: str) -> None:
    now = _timestamp()
    connection.execute(
        """
        UPDATE knowledge_analysis_batches
        SET status = 'pending', checkpoint_json = NULL, error_code = NULL, updated_at = ?
        WHERE job_id = ? AND status != 'completed'
        """,
        (now, job_id),
    )
    connection.execute(
        """
        UPDATE knowledge_analysis_merges
        SET status = 'pending', checkpoint_json = NULL, error_code = NULL, updated_at = ?
        WHERE job_id = ? AND status != 'completed'
        """,
        (now, job_id),
    )
    connection.execute(
        """
        UPDATE knowledge_analysis_merge_nodes
        SET status = 'pending', checkpoint_json = NULL, error_code = NULL, updated_at = ?
        WHERE job_id = ? AND status != 'completed'
        """,
        (now, job_id),
    )


def _discard_legacy_analysis_in(connection: sqlite3.Connection, job_id: str) -> None:
    connection.execute("DELETE FROM knowledge_analysis_merge_nodes WHERE job_id = ?", (job_id,))
    connection.execute("DELETE FROM knowledge_analysis_merges WHERE job_id = ?", (job_id,))
    connection.execute("DELETE FROM knowledge_analysis_batches WHERE job_id = ?", (job_id,))
    connection.execute("DELETE FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,))


def _plan_identity_in(connection: sqlite3.Connection, job_id: str) -> str | None:
    row = connection.execute(
        "SELECT plan_json FROM knowledge_analysis_plans WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    serialized = str(row[0])
    try:
        return KnowledgeAnalysisPlan.from_dict(json.loads(serialized)).plan_identity
    except (json.JSONDecodeError, ValueError):
        # Historical plans predate the embedded identity; retain their stable
        # raw snapshot digest for audit readability without making them resumable.
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _string_list(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise DesktopImportError(
            "legacy_model_recovery_incompatible", "Saved batch identities are invalid."
        ) from error
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise DesktopImportError(
            "legacy_model_recovery_incompatible", "Saved batch identities are invalid."
        )
    return tuple(parsed)


def _mapping_json(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
