"""Durable, run-scoped recovery transitions for quarantined Desktop imports."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

from portalocker import LockException

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_clock import lease_expiry, timestamp
from openkb.desktop_import_store import IMPORT_STAGES, ImportJobState
from openkb.desktop_import_types import (
    DesktopRecoveryOverride,
    DesktopStageRun,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

StageProgressCallback = Callable[[dict[str, object]], None]
logger = logging.getLogger(__name__)

_RECOVERY_FAILURE_DETAILS = {
    "import_checkpoint_invalid": (
        "A saved import checkpoint could not be verified.",
        "Import the document again to rebuild its saved stages.",
    ),
    "raw_asset_integrity_failed": (
        "The saved raw document does not match its checkpoint.",
        "Restore the raw document or import it again.",
    ),
    "recovery_model_not_configured": (
        "A configured model is required to resume model analysis.",
        "Choose a model for this retry or update the knowledge-base settings.",
    ),
    "model_operation_suspended": (
        "This exact Analysis operation contract remains suspended after an unusable result.",
        "Correct the model configuration, then explicitly recover this import.",
    ),
}


class DesktopImportRecoveryStore:
    """Start and finish a recovery without changing knowledge-base defaults."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        on_stage_progress: StageProgressCallback | None = None,
    ) -> None:
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = desktop_state_database_path(kb_dir)
        self._on_stage_progress = on_stage_progress
        self._lease_owner = uuid.uuid4().hex

    def begin(self, job_id: str, override: DesktopRecoveryOverride) -> ImportJobState:
        """Reopen exactly the quarantined stage and record its one-time override."""
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                now = timestamp()
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT import_jobs.source_path, import_job_runtime.status,
                        quarantined_documents.stage_run_id, quarantined_documents.stage,
                        stage_runs.stage
                    FROM quarantined_documents
                    JOIN import_jobs ON import_jobs.job_id = quarantined_documents.job_id
                    JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
                    JOIN stage_runs ON stage_runs.stage_run_id = quarantined_documents.stage_run_id
                    WHERE quarantined_documents.job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DesktopImportError(
                        "import_job_not_quarantined", "This import is not an isolated document."
                    )
                source_path, status, stage_run_id, stage, stored_stage = row
                if str(stage) != str(stored_stage) or str(status) not in {
                    "cancelled",
                    "failed",
                    "paused",
                    "recoverable",
                }:
                    raise DesktopImportError(
                        "import_job_not_recoverable",
                        "This isolated document cannot be recovered now.",
                    )
                stage_ids = _stage_ids_in(connection, job_id)
                stage_progress = _verified_progress_in(connection, job_id)
                recovery_run_id = uuid.uuid4().hex
                connection.execute(
                    """
                    UPDATE recovery_runs
                    SET status = 'failed', completed_at = COALESCE(completed_at, ?)
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (now, job_id),
                )
                connection.execute(
                    """
                    UPDATE stage_runs
                    SET status = 'pending', progress = ?, error_code = NULL,
                        started_at = NULL, completed_at = NULL
                    WHERE stage_run_id = ? AND job_id = ?
                    """,
                    (stage_progress, str(stage_run_id), job_id),
                )
                connection.execute(
                    """
                    UPDATE stage_run_runtime
                    SET status = 'pending', error_code = NULL, updated_at = ?
                    WHERE stage_run_id = ? AND job_id = ?
                    """,
                    (now, str(stage_run_id), job_id),
                )
                connection.execute(
                    """
                    UPDATE import_jobs
                    SET status = 'running', progress = ?, error_code = NULL, completed_at = NULL
                    WHERE job_id = ?
                    """,
                    (stage_progress, job_id),
                )
                connection.execute(
                    """
                    UPDATE import_job_runtime
                    SET status = 'running', lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (self._lease_owner, lease_expiry(), now, job_id),
                )
                connection.execute(
                    """
                    INSERT INTO recovery_runs (
                        recovery_run_id, job_id, stage_run_id, model_override,
                        initial_timeout_seconds, status, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, NULL)
                    """,
                    (
                        recovery_run_id,
                        job_id,
                        str(stage_run_id),
                        override.model,
                        None,
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        state = ImportJobState(
            job_id=job_id,
            source=Path(str(source_path)),
            status="running",
            stage_ids=stage_ids,
            recovery_run_id=recovery_run_id,
        )
        self._emit(
            job_id,
            DesktopStageRun(str(stage_run_id), str(stage), "pending", stage_progress),
        )
        return state

    def mark_failed(self, state: ImportJobState, stage: str, error_code: str) -> None:
        """Keep checkpoint validation failures in the persistent failure menu."""
        if state.recovery_run_id is None:
            return
        reason, suggested_action = _RECOVERY_FAILURE_DETAILS.get(
            error_code,
            (
                "The document could not continue from its saved checkpoint.",
                "Retry the document after checking its source and model settings.",
            ),
        )
        try:
            with kb_ingest_lock(self._state_dir):
                connection = _connect(self._database_path)
                try:
                    now = timestamp()
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE recovery_runs
                        SET status = 'failed', completed_at = ?
                        WHERE recovery_run_id = ?
                        """,
                        (now, state.recovery_run_id),
                    )
                    connection.execute(
                        """
                        UPDATE quarantined_documents
                        SET stage_run_id = ?, stage = ?, error_code = ?, reason = ?,
                            suggested_action = ?, created_at = ?
                        WHERE job_id = ?
                        """,
                        (
                            state.stage_ids[stage],
                            stage,
                            error_code,
                            reason,
                            suggested_action,
                            now,
                            state.job_id,
                        ),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, LockException, DesktopImportError):
            logger.warning("recovery state write %s/%s", state.job_id, error_code, exc_info=True)

    def mark_finished(self, state: ImportJobState, status: str) -> None:
        """Close a failed or cancelled recovery attempt without touching defaults."""
        if state.recovery_run_id is None:
            return
        try:
            with kb_ingest_lock(self._state_dir):
                connection = _connect(self._database_path)
                try:
                    with connection:
                        connection.execute(
                            """
                            UPDATE recovery_runs
                            SET status = ?, completed_at = ?
                            WHERE recovery_run_id = ?
                            """,
                            (_recovery_status(status), timestamp(), state.recovery_run_id),
                        )
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, LockException):
            logger.warning("recovery completion write %s", state.job_id, exc_info=True)

    def _emit(self, job_id: str, stage_run: DesktopStageRun) -> None:
        if self._on_stage_progress is None:
            return
        try:
            self._on_stage_progress({"job_id": job_id, **stage_run.as_dict()})
        except Exception:
            logger.debug("Desktop recovery stage callback failed for job %s", job_id, exc_info=True)


def _stage_ids_in(connection: sqlite3.Connection, job_id: str) -> dict[str, str]:
    rows = connection.execute(
        "SELECT stage, stage_run_id FROM stage_runs WHERE job_id = ?", (job_id,)
    ).fetchall()
    stage_ids = {str(row[0]): str(row[1]) for row in rows}
    if set(stage_ids) != set(IMPORT_STAGES):
        raise DesktopImportError(
            "desktop_import_state_invalid", f"Import job {job_id} has incomplete stage state."
        )
    return stage_ids


def _verified_progress_in(connection: sqlite3.Connection, job_id: str) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(stage_runs.progress), 0)
        FROM stage_runs
        LEFT JOIN stage_run_runtime ON stage_run_runtime.stage_run_id = stage_runs.stage_run_id
        WHERE stage_runs.job_id = ?
            AND COALESCE(stage_run_runtime.status, stage_runs.status) IN ('completed', 'skipped')
        """,
        (job_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _recovery_status(status: str) -> str:
    return status if status in {"failed", "cancelled"} else "failed"


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
