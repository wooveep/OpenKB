"""SQLite ledger and quarantine transitions for Desktop Model Calls."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_import_quarantine import quarantine_import_in
from openkb.desktop_import_types import (
    DesktopModelAttempt,
    DesktopModelCall,
    DesktopQuarantinedDocument,
)
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import DesktopModelFailure
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock


class DesktopImportModelLedger:
    """Persist safe Model Attempt history without retaining prompt or response bodies."""

    def __init__(self, kb_dir: Path) -> None:
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = desktop_state_database_path(kb_dir)

    def record_attempt(
        self,
        *,
        job_id: str,
        stage_run_id: str,
        operation: str,
        event: object,
    ) -> None:
        normalized = normalize_model_event(event)
        now = _timestamp()
        completed_at = now if normalized.lifecycle_status in {"completed", "cancelled"} else None
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO model_calls (
                            call_id, job_id, stage_run_id, operation, status, attempt_count,
                            timeout_seconds, next_timeout_seconds, remaining_seconds,
                            error_code, reason, suggested_action, created_at, completed_at,
                            lifecycle_status, elapsed_seconds, retry_after_seconds
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                        ON CONFLICT(call_id) DO UPDATE SET
                            status = excluded.status,
                            attempt_count = excluded.attempt_count,
                            timeout_seconds = excluded.timeout_seconds,
                            next_timeout_seconds = excluded.next_timeout_seconds,
                            remaining_seconds = excluded.remaining_seconds,
                            error_code = excluded.error_code,
                            reason = excluded.reason,
                            completed_at = excluded.completed_at,
                            lifecycle_status = excluded.lifecycle_status,
                            elapsed_seconds = excluded.elapsed_seconds,
                            retry_after_seconds = excluded.retry_after_seconds
                        """,
                        (
                            normalized.call_id,
                            job_id,
                            stage_run_id,
                            operation,
                            normalized.storage_status,
                            normalized.attempt,
                            0.0,
                            None,
                            0.0,
                            normalized.error_code,
                            normalized.reason,
                            now,
                            completed_at,
                            normalized.lifecycle_status,
                            normalized.elapsed_seconds,
                            normalized.retry_after_seconds,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO model_attempts (
                            call_id, attempt, status, timeout_seconds, remaining_seconds,
                            error_code, reason, created_at, completed_at, lifecycle_status,
                            elapsed_seconds, retry_after_seconds
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(call_id, attempt) DO UPDATE SET
                            status = excluded.status,
                            timeout_seconds = excluded.timeout_seconds,
                            remaining_seconds = excluded.remaining_seconds,
                            error_code = excluded.error_code,
                            reason = excluded.reason,
                            completed_at = excluded.completed_at,
                            lifecycle_status = excluded.lifecycle_status,
                            elapsed_seconds = excluded.elapsed_seconds,
                            retry_after_seconds = excluded.retry_after_seconds
                        """,
                        (
                            normalized.call_id,
                            normalized.attempt,
                            normalized.storage_status,
                            0.0,
                            0.0,
                            normalized.error_code,
                            normalized.reason,
                            now,
                            completed_at,
                            normalized.lifecycle_status,
                            normalized.elapsed_seconds,
                            normalized.retry_after_seconds,
                        ),
                    )
            finally:
                connection.close()

    def quarantine(
        self,
        *,
        job_id: str,
        stage_run_id: str,
        stage: str,
        call_id: str,
        failure: DesktopModelFailure,
        attempt_count: int,
    ) -> None:
        """Atomically mark a required model-stage failure as unpublished quarantine work."""
        now = _timestamp()
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE model_calls
                    SET status = 'failed', error_code = ?, reason = ?, suggested_action = ?,
                        completed_at = COALESCE(completed_at, ?)
                    WHERE call_id = ?
                    """,
                    (failure.code, failure.reason, failure.suggested_action, now, call_id),
                )
                quarantine_import_in(
                    connection,
                    job_id=job_id,
                    stage_run_id=stage_run_id,
                    stage=stage,
                    error_code=failure.code,
                    reason=failure.reason,
                    suggested_action=failure.suggested_action,
                    attempt_count=attempt_count,
                    now=now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()


def model_details_for_job(
    connection: sqlite3.Connection, job_id: str
) -> tuple[tuple[DesktopModelCall, ...], DesktopQuarantinedDocument | None]:
    """Project the safe ledger entries used by task cards and failure menus."""
    rows = connection.execute(
        """
        SELECT call_id, stage_run_id, operation, COALESCE(lifecycle_status, status),
            attempt_count, error_code, reason, suggested_action, elapsed_seconds
        FROM model_calls
        WHERE job_id = ?
        ORDER BY created_at, rowid
        """,
        (job_id,),
    ).fetchall()
    calls = tuple(
        DesktopModelCall(
            call_id=str(row[0]),
            stage_run_id=str(row[1]),
            operation=str(row[2]),
            status=str(row[3]),
            attempt_count=int(row[4]),
            elapsed_seconds=float(row[8]),
            error_code=str(row[5]) if row[5] is not None else None,
            reason=str(row[6]) if row[6] is not None else None,
            suggested_action=str(row[7]) if row[7] is not None else None,
            attempts=_attempts_for_call(connection, str(row[0])),
        )
        for row in rows
    )
    quarantine_row = connection.execute(
        """
        SELECT stage_run_id, stage, error_code, reason, suggested_action, attempt_count
        FROM quarantined_documents
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    quarantine = (
        DesktopQuarantinedDocument(
            stage_run_id=str(quarantine_row[0]),
            stage=str(quarantine_row[1]),
            error_code=str(quarantine_row[2]),
            reason=str(quarantine_row[3]),
            suggested_action=str(quarantine_row[4]),
            attempt_count=int(quarantine_row[5]),
        )
        if quarantine_row is not None
        else None
    )
    return calls, quarantine


def _attempts_for_call(
    connection: sqlite3.Connection, call_id: str
) -> tuple[DesktopModelAttempt, ...]:
    rows = connection.execute(
        """
        SELECT attempt, COALESCE(lifecycle_status, status),
            error_code, reason, elapsed_seconds
        FROM model_attempts
        WHERE call_id = ?
        ORDER BY attempt
        """,
        (call_id,),
    ).fetchall()
    return tuple(
        DesktopModelAttempt(
            attempt=int(row[0]),
            status=str(row[1]),
            elapsed_seconds=float(row[4]),
            error_code=str(row[2]) if row[2] is not None else None,
            reason=str(row[3]) if row[3] is not None else None,
        )
        for row in rows
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
