"""Focused SQLite helpers for Knowledge Reanalysis runs and admission."""

from __future__ import annotations

import sqlite3

from openkb.desktop_import_artifacts import DesktopImportError


def available_documents_in(
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


def active_canonical_documents_in(
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


def refresh_run_in(connection: sqlite3.Connection, run_id: str, now: str) -> None:
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
        status, completed_at = "running", None
    elif failed and completed:
        status, completed_at = "partial_failure", now
    elif failed:
        status, completed_at = "failed", now
    else:
        status, completed_at = "completed", now
    connection.execute(
        "UPDATE knowledge_reanalysis_runs SET status = ?, completed_at = ? WHERE run_id = ?",
        (status, completed_at, run_id),
    )


def run_id_for_job_in(connection: sqlite3.Connection, job_id: str) -> str:
    row = connection.execute(
        "SELECT run_id FROM knowledge_reanalysis_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise DesktopImportError(
            "knowledge_reanalysis_job_not_found", "Knowledge Reanalysis job was not found."
        )
    return str(row[0])


def require_execution_update(cursor: sqlite3.Cursor) -> None:
    if cursor.rowcount != 1:
        raise DesktopImportError(
            "knowledge_reanalysis_interrupted",
            "Knowledge Reanalysis is no longer the active execution for this document.",
        )
