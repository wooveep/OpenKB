"""Explicit, non-destructive Knowledge Reanalysis over persisted Evidence."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from openkb import __version__
from openkb.desktop_catalog_store import queue_catalog_rebuild_in, start_catalog_rebuilds
from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_PROMPT_DIGEST,
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
)
from openkb.desktop_knowledge_analysis_batch_store import (
    DesktopKnowledgeAnalysisBatchStore,
)
from openkb.desktop_knowledge_analysis_batches import (
    KNOWLEDGE_ANALYSIS_BATCH_PIPELINE_DIGEST,
    plan_knowledge_analysis_batches,
    run_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_reuse import (
    ReusableKnowledgeAnalysis,
    analysis_evidence_for_document_in,
    canonical_analysis_document_id_in,
    canonical_analysis_evidence_map_in,
    latest_knowledge_analysis_checkpoint_in,
    persisted_analysis_prompt_digest_in,
)
from openkb.desktop_knowledge_generations import current_generation_id_in
from openkb.desktop_knowledge_reanalysis_models import (
    DesktopDocumentAnalysisStatus,
    DesktopKnowledgeReanalysisRun,
    knowledge_reanalysis_runs_in,
    require_knowledge_reanalysis_run_in,
)
from openkb.desktop_knowledge_reanalysis_recovery import (
    recover_interrupted_knowledge_reanalysis,
)
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_missing_sources import record_missing_source_candidates_in
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelGateway,
    DesktopModelRequest,
)
from openkb.desktop_okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.desktop_page_tree import PageTreeGeneration, page_tree_analysis_sections
from openkb.desktop_page_tree_store import lease_current_page_tree, load_current_page_tree_in
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

__all__ = ["DesktopKnowledgeReanalysisService", "recover_interrupted_knowledge_reanalysis"]

logger = logging.getLogger(__name__)
_MAX_DOCUMENTS_PER_RUN = 200


class DesktopKnowledgeReanalysisService:
    """Own explicit reanalysis jobs without mutating Import Jobs or document assets."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._reconciliation = DesktopKnowledgeReconciliationService(self._kb_dir)

    def overview(self) -> dict[str, object]:
        self._require_database()
        connection = _connect(self._database_path)
        try:
            documents = connection.execute(
                """
                SELECT document_id, display_name FROM source_documents
                WHERE availability = 'available' ORDER BY created_at DESC
                """
            ).fetchall()
            statuses = tuple(self._document_status(connection, row) for row in documents)
            runs = knowledge_reanalysis_runs_in(connection)
        finally:
            connection.close()
        return {
            "documents": [status.as_dict() for status in statuses],
            "runs": [run.as_dict() for run in runs],
        }

    def create_run(
        self, document_ids: tuple[str, ...], *, provider: str, model: str
    ) -> DesktopKnowledgeReanalysisRun:
        selected = tuple(dict.fromkeys(document_ids))
        if not selected or len(selected) > _MAX_DOCUMENTS_PER_RUN:
            raise DesktopImportError(
                "knowledge_reanalysis_documents_invalid",
                f"Choose between 1 and {_MAX_DOCUMENTS_PER_RUN} Available documents.",
            )
        now = _timestamp()
        run_id = uuid.uuid4().hex
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                documents = _available_documents_in(connection, selected)
                if set(documents) != set(selected):
                    raise DesktopImportError(
                        "knowledge_reanalysis_document_unavailable",
                        "Every selected document must still be Available.",
                    )
                canonical_selection: dict[str, str] = {}
                for document_id in selected:
                    canonical_selection.setdefault(
                        canonical_analysis_document_id_in(connection, document_id), document_id
                    )
                active_documents = _active_canonical_documents_in(
                    connection, tuple(canonical_selection)
                )
                canonical_selection = {
                    authority: document_id
                    for authority, document_id in canonical_selection.items()
                    if authority not in active_documents
                }
                if not canonical_selection:
                    raise DesktopImportError(
                        "knowledge_reanalysis_already_running",
                        "A selected document already has active Knowledge Reanalysis work.",
                    )
                selected = tuple(canonical_selection.values())
                connection.execute(
                    """
                    INSERT INTO knowledge_reanalysis_runs (
                        run_id, mode, status, created_at, completed_at
                    ) VALUES (?, ?, 'pending', ?, NULL)
                    """,
                    (run_id, "single" if len(selected) == 1 else "bulk", now),
                )
                for document_id in selected:
                    evidence = analysis_evidence_for_document_in(connection, document_id)
                    page_tree = load_current_page_tree_in(connection, document_id)
                    connection.execute(
                        """
                        INSERT INTO knowledge_reanalysis_jobs (
                            job_id, run_id, document_id, status, phase, progress,
                            provider, model, engine_version, expected_prompt_digest,
                            checkpoint_json, error_code, reason, current_operation,
                            attempt_count, timeout_seconds, remaining_seconds,
                            next_timeout_seconds, execution_token,
                            created_at, started_at, completed_at
                        ) VALUES (?, ?, ?, 'pending', 'pending', 0, ?, ?, ?, ?,
                            NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                            ?, NULL, NULL)
                        """,
                        (
                            uuid.uuid4().hex,
                            run_id,
                            document_id,
                            provider,
                            model,
                            __version__,
                            expected_prompt_digest(evidence, page_tree),
                            now,
                        ),
                    )
                connection.commit()
                return require_knowledge_reanalysis_run_in(connection, run_id)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def retry_job(self, job_id: str, *, provider: str, model: str) -> DesktopKnowledgeReanalysisRun:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT jobs.run_id, jobs.document_id, jobs.status, jobs.provider,
                        jobs.model, jobs.engine_version, jobs.expected_prompt_digest,
                        runs.status
                    FROM knowledge_reanalysis_jobs AS jobs
                    JOIN knowledge_reanalysis_runs AS runs ON runs.run_id = jobs.run_id
                    WHERE jobs.job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if row is None or str(row[2]) != "failed":
                    raise DesktopImportError(
                        "knowledge_reanalysis_job_not_retryable",
                        "The selected Knowledge Reanalysis job is not failed.",
                    )
                if str(row[7]) in {"pending", "running"}:
                    raise DesktopImportError(
                        "knowledge_reanalysis_run_active",
                        "Wait for the active bulk Reanalysis run to finish before retrying.",
                    )
                evidence = analysis_evidence_for_document_in(connection, str(row[1]))
                page_tree = load_current_page_tree_in(connection, str(row[1]))
                identity = (
                    provider,
                    model,
                    __version__,
                    expected_prompt_digest(evidence, page_tree),
                )
                if identity != tuple(str(value) for value in row[3:7]):
                    raise DesktopImportError(
                        "knowledge_reanalysis_configuration_changed",
                        "Start a new Reanalysis because its model or analysis behavior changed.",
                    )
                canonical_document_id = canonical_analysis_document_id_in(connection, str(row[1]))
                if _active_canonical_documents_in(
                    connection, (canonical_document_id,), excluding_job_id=job_id
                ):
                    raise DesktopImportError(
                        "knowledge_reanalysis_already_running",
                        "This document content already has active Knowledge Reanalysis work.",
                    )
                now = _timestamp()
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_jobs SET status = 'pending', phase = 'pending',
                        progress = 0, error_code = NULL, reason = NULL,
                        current_operation = NULL, attempt_count = NULL,
                        timeout_seconds = NULL, remaining_seconds = NULL,
                        next_timeout_seconds = NULL, execution_token = NULL,
                        started_at = NULL, completed_at = NULL
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_runs
                    SET status = 'running', completed_at = NULL WHERE run_id = ?
                    """,
                    (str(row[0]),),
                )
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_batches
                    SET status = 'pending', error_code = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'failed'
                    """,
                    (now, job_id),
                )
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_merges
                    SET status = 'pending', error_code = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'failed'
                    """,
                    (now, job_id),
                )
                connection.commit()
                return require_knowledge_reanalysis_run_in(connection, str(row[0]))
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def pending_job_ids(self, run_id: str) -> tuple[str, ...]:
        connection = _connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT job_id FROM knowledge_reanalysis_jobs
                WHERE run_id = ? AND status = 'pending' ORDER BY created_at, rowid
                """,
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(str(row[0]) for row in rows)

    def run_job(
        self,
        job_id: str,
        gateway: DesktopModelGateway,
        *,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        execution_token: str | None = None
        try:
            document_id, document_name, expected_digest, execution_token = self._begin_job(
                job_id, gateway
            )
            with lease_current_page_tree(self._kb_dir, document_id) as page_tree:
                connection = _connect(self._database_path)
                try:
                    evidence = analysis_evidence_for_document_in(connection, document_id)
                finally:
                    connection.close()
                if expected_prompt_digest(evidence, page_tree) != expected_digest:
                    raise DesktopImportError(
                        "knowledge_reanalysis_behavior_changed",
                        "The analysis behavior changed after this Reanalysis was created.",
                    )

                def honor_control() -> None:
                    if should_stop():
                        raise DesktopImportError(
                            "knowledge_reanalysis_interrupted",
                            "Knowledge Reanalysis stopped with the Desktop Runtime.",
                        )

                def analyze(request: DesktopModelRequest):
                    honor_control()
                    self._set_phase(job_id, execution_token, request.operation)
                    return gateway.analyze(
                        replace(
                            request,
                            job_id=job_id,
                            stage_run_id=execution_token,
                        ),
                        on_event=lambda event: self._record_attempt(
                            job_id, execution_token, request.operation, event
                        ),
                        is_cancelled=should_stop,
                    )

                run = run_knowledge_analysis(
                    store=DesktopKnowledgeAnalysisBatchStore(
                        self._kb_dir,
                        reanalysis=True,
                        execution_token=execution_token,
                    ),
                    job_id=job_id,
                    stage_run_id=job_id,
                    document_name=document_name,
                    evidence=evidence,
                    page_tree=page_tree,
                    provider=gateway.provider_name,
                    model=gateway.model_name,
                    engine_version=__version__,
                    analyze=analyze,
                    honor_control=honor_control,
                    on_batch_completed=lambda completed, total: self._batch_completed(
                        job_id, execution_token, completed, total
                    ),
                    max_parallel_batches=getattr(gateway, "analysis_concurrency", 1),
                    capability_profile=(
                        capability("knowledge_analysis")
                        if callable(
                            capability := getattr(
                                gateway,
                                "capability_for_operation",
                                None,
                            )
                        )
                        else None
                    ),
                )
                honor_control()
                self._apply_result(
                    job_id,
                    document_id,
                    evidence,
                    run.analysis,
                    run.provenance_json,
                    run.checkpoint,
                    execution_token,
                )
        except DesktopModelCallError as error:
            if execution_token is not None:
                self._fail_job(job_id, execution_token, error.failure.code, error.failure.reason)
        except DesktopImportError as error:
            if execution_token is not None:
                self._fail_job(job_id, execution_token, error.code, str(error))
        except Exception as error:
            logger.exception("Knowledge Reanalysis failed for job %s", job_id)
            if execution_token is not None:
                self._fail_job(job_id, execution_token, "knowledge_reanalysis_failed", str(error))

    def _begin_job(self, job_id: str, gateway: DesktopModelGateway) -> tuple[str, str, str, str]:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = _timestamp()
                execution_token = uuid.uuid4().hex
                row = connection.execute(
                    """
                    SELECT jobs.document_id, documents.display_name,
                        jobs.provider, jobs.model, jobs.expected_prompt_digest
                    FROM knowledge_reanalysis_jobs AS jobs
                    JOIN source_documents AS documents ON documents.document_id = jobs.document_id
                        AND documents.availability = 'available'
                    WHERE jobs.job_id = ? AND jobs.status = 'pending'
                    """,
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DesktopImportError(
                        "knowledge_reanalysis_job_not_runnable",
                        "The Knowledge Reanalysis job is not pending or Available.",
                    )
                if (str(row[2]), str(row[3])) != (
                    gateway.provider_name,
                    gateway.model_name,
                ):
                    raise DesktopImportError(
                        "knowledge_reanalysis_configuration_changed",
                        "Start a new Reanalysis because its model configuration changed.",
                    )
                cursor = connection.execute(
                    """
                    UPDATE knowledge_reanalysis_jobs
                    SET status = 'running', phase = 'batches', progress = 5,
                        started_at = ?, execution_token = ?, error_code = NULL, reason = NULL
                    WHERE job_id = ? AND status = 'pending' AND execution_token IS NULL
                    """,
                    (now, execution_token, job_id),
                )
                if cursor.rowcount != 1:
                    raise DesktopImportError(
                        "knowledge_reanalysis_job_not_runnable",
                        "The Knowledge Reanalysis job was claimed by another worker.",
                    )
                connection.execute(
                    """
                    UPDATE knowledge_reanalysis_runs SET status = 'running'
                    WHERE run_id = (SELECT run_id FROM knowledge_reanalysis_jobs WHERE job_id = ?)
                    """,
                    (job_id,),
                )
                connection.commit()
                return str(row[0]), str(row[1]), str(row[4]), execution_token
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _set_phase(self, job_id: str, execution_token: str, operation: str) -> None:
        phase = "merge" if operation == "knowledge_analysis_merge" else "batches"
        progress = 80 if phase == "merge" else 10
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_reanalysis_jobs
                        SET phase = ?, progress = MAX(progress, ?), current_operation = ?
                        WHERE job_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (phase, progress, operation, job_id, execution_token),
                    )
                    _require_execution_update(cursor)
            finally:
                connection.close()

    def _record_attempt(
        self,
        job_id: str,
        execution_token: str,
        operation: str,
        event: object,
    ) -> None:
        lifecycle = normalize_model_event(event)
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_reanalysis_jobs SET current_operation = ?,
                            attempt_count = ?, timeout_seconds = ?, remaining_seconds = ?,
                            next_timeout_seconds = ?, error_code = ?
                        WHERE job_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (
                            operation,
                            lifecycle.attempt,
                            None,
                            None,
                            None,
                            lifecycle.error_code,
                            job_id,
                            execution_token,
                        ),
                    )
                    _require_execution_update(cursor)
            finally:
                connection.close()

    def _batch_completed(
        self, job_id: str, execution_token: str, completed: int, total: int
    ) -> None:
        progress = 10 + round((completed / max(1, total)) * 65)
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_reanalysis_jobs SET progress = ?
                        WHERE job_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (progress, job_id, execution_token),
                    )
                    _require_execution_update(cursor)
            finally:
                connection.close()

    def _apply_result(
        self,
        job_id: str,
        document_id: str,
        evidence: tuple[tuple[str, DocumentIRBlock], ...],
        analysis: DesktopKnowledgeAnalysis,
        provenance_json: str,
        checkpoint: dict[str, object],
        execution_token: str,
    ) -> None:
        staged_projection: Path | None = None
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE knowledge_reanalysis_jobs
                    SET phase = 'reconciliation', progress = MAX(progress, 90)
                    WHERE job_id = ? AND status = 'running' AND execution_token = ?
                    """,
                    (job_id, execution_token),
                )
                _require_execution_update(cursor)
                available = connection.execute(
                    """
                    SELECT 1 FROM source_documents
                    WHERE document_id = ? AND availability = 'available'
                    """,
                    (document_id,),
                ).fetchone()
                if available is None:
                    raise DesktopImportError(
                        "knowledge_reanalysis_document_unavailable",
                        "The document became unavailable before Reanalysis completed.",
                    )
                reusable = ReusableKnowledgeAnalysis(analysis, provenance_json, evidence)
                evidence_map = canonical_analysis_evidence_map_in(connection, document_id, reusable)
                changes = analysis.incoming_changes(
                    evidence_map, analysis_provenance_json=provenance_json
                )
                initial_generation = current_generation_id_in(connection)
                self._reconciliation.record_analysis_changes_in(connection, document_id, changes)
                record_missing_source_candidates_in(
                    connection,
                    document_id=canonical_analysis_document_id_in(connection, document_id),
                    claims=analysis.missing_source_claims(evidence_map),
                    evidence=evidence,
                    analysis_provenance_json=provenance_json,
                )
                queue_catalog_rebuild_in(connection, "successful_reanalysis")
                if current_generation_id_in(connection) != initial_generation:
                    staged_projection = stage_okf_projection_in(connection, self._kb_dir)
                now = _timestamp()
                cursor = connection.execute(
                    """
                    UPDATE knowledge_reanalysis_jobs
                    SET status = 'completed', phase = 'completed', progress = 100,
                        checkpoint_json = ?, error_code = NULL, reason = NULL,
                        remaining_seconds = 0, completed_at = ?, execution_token = NULL
                    WHERE job_id = ? AND status = 'running' AND execution_token = ?
                    """,
                    (_json(checkpoint), now, job_id, execution_token),
                )
                _require_execution_update(cursor)
                _refresh_run_in(connection, _run_id_for_job_in(connection, job_id), now)
                connection.commit()
            except BaseException:
                connection.rollback()
                if staged_projection is not None:
                    discard_okf_projection_staging(staged_projection)
                raise
            finally:
                connection.close()
            if staged_projection is not None:
                try:
                    activate_okf_projection(self._kb_dir, staged_projection)
                except Exception:
                    logger.exception("Could not activate Knowledge Reanalysis OKF projection.")
                finally:
                    discard_okf_projection_staging(staged_projection)
            start_catalog_rebuilds(self._kb_dir)

    def _fail_job(self, job_id: str, execution_token: str, error_code: str, reason: str) -> None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    now = _timestamp()
                    run_id = _run_id_for_job_in(connection, job_id)
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_reanalysis_jobs
                        SET status = 'failed', phase = 'failed', error_code = ?, reason = ?,
                            completed_at = ?, execution_token = NULL
                        WHERE job_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (error_code, reason[:1000], now, job_id, execution_token),
                    )
                    if cursor.rowcount == 1:
                        _refresh_run_in(connection, run_id, now)
            finally:
                connection.close()

    def _document_status(
        self, connection: sqlite3.Connection, row: tuple[object, ...]
    ) -> DesktopDocumentAnalysisStatus:
        document_id, document_name = str(row[0]), str(row[1])
        stored = latest_knowledge_analysis_checkpoint_in(connection, document_id)
        if stored is None:
            return DesktopDocumentAnalysisStatus(
                document_id, document_name, "missing", None, None, None, None, None, None
            )
        checkpoint = stored.checkpoint
        normalized = checkpoint.get("normalized_result")
        schema_version = (
            _optional_string(normalized.get("schema_version"))
            if isinstance(normalized, dict)
            else None
        )
        prompt_digest = persisted_analysis_prompt_digest_in(connection, stored)
        evidence = analysis_evidence_for_document_in(connection, document_id)
        page_tree = load_current_page_tree_in(connection, document_id)
        expected = expected_prompt_digest(evidence, page_tree)
        current = (
            schema_version == KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
            and prompt_digest == expected
            and checkpoint.get("engine_version") == __version__
        )
        return DesktopDocumentAnalysisStatus(
            document_id=document_id,
            document_name=document_name,
            state="current" if current else "analysis_outdated",
            schema_version=schema_version,
            provider=_optional_string(checkpoint.get("provider")),
            model=_optional_string(checkpoint.get("model")),
            prompt_digest=prompt_digest,
            engine_version=_optional_string(checkpoint.get("engine_version")),
            analyzed_at=stored.analyzed_at,
        )

    def _require_database(self) -> None:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                "Open a Desktop Knowledge Base before using Knowledge Reanalysis.",
            )


def expected_prompt_digest(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    page_tree: PageTreeGeneration | None = None,
) -> str:
    sections = page_tree_analysis_sections(page_tree, evidence) if page_tree is not None else ()
    batches = plan_knowledge_analysis_batches(evidence, natural_sections=sections or None)
    return (
        KNOWLEDGE_ANALYSIS_PROMPT_DIGEST
        if len(batches) <= 1
        else KNOWLEDGE_ANALYSIS_BATCH_PIPELINE_DIGEST
    )


def _available_documents_in(
    connection: sqlite3.Connection, document_ids: tuple[str, ...]
) -> dict[str, str]:
    placeholders = ",".join("?" for _ in document_ids)
    rows = connection.execute(
        f"""
        SELECT document_id, display_name FROM source_documents
        WHERE availability = 'available' AND document_id IN ({placeholders})
        """,
        document_ids,
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _active_canonical_documents_in(
    connection: sqlite3.Connection,
    canonical_document_ids: tuple[str, ...],
    *,
    excluding_job_id: str | None = None,
) -> set[str]:
    placeholders = ",".join("?" for _ in canonical_document_ids)
    exclusion = "AND jobs.job_id != ?" if excluding_job_id is not None else ""
    params: tuple[object, ...] = (*canonical_document_ids,)
    if excluding_job_id is not None:
        params = (*params, excluding_job_id)
    rows = connection.execute(
        f"""
        SELECT DISTINCT COALESCE(fingerprints.canonical_document_id, jobs.document_id)
        FROM knowledge_reanalysis_jobs AS jobs
        LEFT JOIN document_content_fingerprints AS fingerprints
            ON fingerprints.document_id = jobs.document_id
        WHERE COALESCE(fingerprints.canonical_document_id, jobs.document_id)
            IN ({placeholders})
            AND jobs.status IN ('pending', 'running') {exclusion}
        """,
        params,
    ).fetchall()
    return {str(row[0]) for row in rows}


def _refresh_run_in(connection: sqlite3.Connection, run_id: str, now: str) -> None:
    counts = dict(
        connection.execute(
            """
            SELECT status, COUNT(*) FROM knowledge_reanalysis_jobs
            WHERE run_id = ? GROUP BY status
            """,
            (run_id,),
        ).fetchall()
    )
    active = int(counts.get("pending", 0)) + int(counts.get("running", 0))
    failed = int(counts.get("failed", 0))
    completed = int(counts.get("completed", 0))
    if active:
        status = "running"
        completed_at = None
    elif failed and completed:
        status = "partial_failure"
        completed_at = now
    elif failed:
        status = "failed"
        completed_at = now
    else:
        status = "completed"
        completed_at = now
    connection.execute(
        """
        UPDATE knowledge_reanalysis_runs SET status = ?, completed_at = ? WHERE run_id = ?
        """,
        (status, completed_at, run_id),
    )


def _run_id_for_job_in(connection: sqlite3.Connection, job_id: str) -> str:
    row = connection.execute(
        "SELECT run_id FROM knowledge_reanalysis_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise DesktopImportError(
            "knowledge_reanalysis_job_not_found", "Knowledge Reanalysis job was not found."
        )
    return str(row[0])


def _require_execution_update(cursor: sqlite3.Cursor) -> None:
    if cursor.rowcount != 1:
        raise DesktopImportError(
            "knowledge_reanalysis_interrupted",
            "Knowledge Reanalysis is no longer the active execution for this document.",
        )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_string(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
