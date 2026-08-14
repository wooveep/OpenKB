"""Read projections for Desktop import task state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.desktop_import_model_ledger import model_details_for_job
from openkb.desktop_import_types import (
    DesktopImportedDocument,
    DesktopImportJob,
    DesktopImportTask,
    DesktopStageRun,
)


def task_from_row(
    connection: sqlite3.Connection, row: tuple[object, ...], stage_order_sql: str
) -> DesktopImportTask:
    job_id = str(row[0])
    stages = stages_for_job(connection, job_id, stage_order_sql)
    model_calls, quarantine = model_details_for_job(connection, job_id)
    runtime_status = str(row[1])
    if runtime_status == "running":
        # A manual recovery leaves the old quarantine record in place until it
        # publishes atomically, but it should appear as active work while running.
        quarantine = None
    job = DesktopImportJob(
        job_id=job_id,
        source_name=Path(str(row[10])).name,
        status="quarantined" if quarantine is not None else runtime_status,
        progress=int(str(row[2])),
        document_id=str(row[3]) if row[3] is not None else None,
        deduplicated=any(
            stage.stage == "document_ir" and stage.status == "skipped" for stage in stages
        ),
    )
    document = document_from_row(row[4:10]) if row[4] is not None else None
    return DesktopImportTask(
        job=job,
        document=document,
        stages=stages,
        model_calls=model_calls,
        quarantine=quarantine,
    )


def stages_for_job(
    connection: sqlite3.Connection, job_id: str, stage_order_sql: str
) -> tuple[DesktopStageRun, ...]:
    rows = connection.execute(
        f"""
        SELECT stage_run_id, stage, status, progress, error_code
        FROM (
            SELECT stage_runs.stage_run_id, stage_runs.stage,
                COALESCE(stage_run_runtime.status, stage_runs.status) AS status,
                stage_runs.progress,
                COALESCE(stage_run_runtime.error_code, stage_runs.error_code) AS error_code
            FROM stage_runs
            LEFT JOIN stage_run_runtime ON stage_run_runtime.stage_run_id = stage_runs.stage_run_id
            WHERE stage_runs.job_id = ?
        )
        ORDER BY {stage_order_sql}
        """,
        (job_id,),
    ).fetchall()
    return tuple(
        DesktopStageRun(
            stage_run_id=str(row[0]),
            stage=str(row[1]),
            status=str(row[2]),
            progress=int(str(row[3])),
            error_code=str(row[4]) if row[4] is not None else None,
        )
        for row in rows
    )


def find_available_document_in(
    connection: sqlite3.Connection, asset_sha256: str
) -> DesktopImportedDocument | None:
    row = connection.execute(
        """
        SELECT document_id, display_name, source_format, asset_sha256, availability,
            (SELECT COUNT(*) FROM evidence_refs
             WHERE evidence_refs.document_id = source_documents.document_id)
        FROM source_documents
        WHERE asset_sha256 = ? AND availability = 'available'
        """,
        (asset_sha256,),
    ).fetchone()
    return document_from_row(row) if row is not None else None


def document_from_row(row: tuple[object, ...]) -> DesktopImportedDocument:
    return DesktopImportedDocument(
        document_id=str(row[0]),
        name=str(row[1]),
        source_format=str(row[2]),
        raw_asset_sha256=str(row[3]),
        availability=str(row[4]),
        evidence_count=int(str(row[5])),
    )
