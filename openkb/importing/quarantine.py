"""Durable direct-failure isolation before a Desktop document is published."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.locks import kb_ingest_lock
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir


class DesktopImportQuarantineStore:
    """Persist failures that must be surfaced without an automatic retry."""

    def __init__(self, kb_dir: Path) -> None:
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = desktop_state_database_path(kb_dir)

    def quarantine(
        self,
        *,
        job_id: str,
        stage_run_id: str,
        stage: str,
        error_code: str,
        reason: str,
        suggested_action: str,
        attempt_count: int = 1,
    ) -> None:
        """Atomically mark an unpublished document as persistent failure work."""
        now = _timestamp()
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                quarantine_import_in(
                    connection,
                    job_id=job_id,
                    stage_run_id=stage_run_id,
                    stage=stage,
                    error_code=error_code,
                    reason=reason,
                    suggested_action=suggested_action,
                    attempt_count=attempt_count,
                    now=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()


def quarantine_import_in(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    stage_run_id: str,
    stage: str,
    error_code: str,
    reason: str,
    suggested_action: str,
    attempt_count: int,
    now: str,
) -> None:
    """Write shared job/stage isolation state inside the caller's transaction."""
    connection.execute(
        """
        UPDATE stage_runs
        SET status = 'failed', progress = 100, error_code = ?,
            completed_at = COALESCE(completed_at, ?)
        WHERE stage_run_id = ? AND job_id = ?
        """,
        (error_code, now, stage_run_id, job_id),
    )
    connection.execute(
        """
        UPDATE stage_run_runtime
        SET status = 'failed', error_code = ?, updated_at = ?
        WHERE stage_run_id = ? AND job_id = ?
        """,
        (error_code, now, stage_run_id, job_id),
    )
    connection.execute(
        """
        UPDATE import_jobs
        SET status = 'failed', progress = 100, error_code = ?, completed_at = ?
        WHERE job_id = ?
        """,
        (error_code, now, job_id),
    )
    connection.execute(
        """
        UPDATE import_job_runtime
        SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
        WHERE job_id = ?
        """,
        (now, job_id),
    )
    connection.execute(
        """
        INSERT INTO quarantined_documents (
            job_id, stage_run_id, stage, error_code, reason, suggested_action,
            attempt_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            stage_run_id = excluded.stage_run_id,
            stage = excluded.stage,
            error_code = excluded.error_code,
            reason = excluded.reason,
            suggested_action = excluded.suggested_action,
            attempt_count = excluded.attempt_count,
            created_at = excluded.created_at
        """,
        (
            job_id,
            stage_run_id,
            stage,
            error_code,
            reason,
            suggested_action,
            attempt_count,
            now,
        ),
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = connect_database(database_path)
    return connection
