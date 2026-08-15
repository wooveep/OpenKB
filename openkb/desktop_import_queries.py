"""Read projections for Desktop import task state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_model_ledger import model_details_for_job
from openkb.desktop_import_types import (
    DesktopDeduplication,
    DesktopImportedDocument,
    DesktopImportJob,
    DesktopImportTask,
    DesktopStageRun,
)

_DEDUPLICATION_REASONS = {
    "D0": "raw_asset_sha256_match",
    "D1": "normalized_body_sha256_match",
    "D2": "evidence_sha256_match",
}
_REUSABLE_STAGES = frozenset(("document_ir", "evidence", "model_analysis", "search"))


def task_from_row(
    connection: sqlite3.Connection, row: tuple[object, ...], stage_order_sql: str
) -> DesktopImportTask:
    job_id = str(row[0])
    stages = stages_for_job(connection, job_id, stage_order_sql)
    model_calls, quarantine = model_details_for_job(connection, job_id)
    deduplication = _deduplication_for_job(connection, job_id)
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
        deduplicated=deduplication is not None
        or any(stage.stage == "document_ir" and stage.status == "skipped" for stage in stages),
        deduplication=deduplication,
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
        SELECT source_documents.document_id, display_name, source_format, asset_sha256,
            availability,
            (SELECT COUNT(*) FROM evidence_occurrences
             WHERE evidence_occurrences.document_id = COALESCE(
                 document_content_fingerprints.canonical_document_id,
                 source_documents.document_id
             ))
        FROM source_documents
        LEFT JOIN document_content_fingerprints
            ON document_content_fingerprints.document_id = source_documents.document_id
        WHERE asset_sha256 = ? AND availability = 'available'
        """,
        (asset_sha256,),
    ).fetchone()
    return document_from_row(row) if row is not None else None


def find_available_document_by_normalized_body_in(
    connection: sqlite3.Connection, normalized_body_sha256: str
) -> DesktopImportedDocument | None:
    """Return an available version that can lead the next exact-body reuse."""
    row = connection.execute(
        """
        SELECT source_documents.document_id, display_name, source_format, asset_sha256,
            availability,
            (SELECT COUNT(*) FROM evidence_occurrences
             WHERE evidence_occurrences.document_id = COALESCE(
                 document_content_fingerprints.canonical_document_id,
                 source_documents.document_id
             ))
        FROM source_documents
        JOIN document_content_fingerprints
            ON document_content_fingerprints.document_id = source_documents.document_id
        WHERE source_documents.availability = 'available'
            AND document_content_fingerprints.normalized_body_sha256 = ?
        ORDER BY document_content_fingerprints.canonical_document_id IS NOT NULL,
            source_documents.created_at, source_documents.document_id
        LIMIT 1
        """,
        (normalized_body_sha256,),
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


def _deduplication_for_job(
    connection: sqlite3.Connection, job_id: str
) -> DesktopDeduplication | None:
    row = connection.execute(
        """
        SELECT level, reason, reused_document_id, reused_evidence_count,
            reusable_stages_json, normalized_body_sha256
        FROM import_deduplications
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    level = row[0]
    reason = row[1]
    reused_document_id = row[2]
    reused_evidence_count = row[3]
    try:
        stages = json.loads(str(row[4]))
    except json.JSONDecodeError as error:
        raise _invalid_deduplication() from error
    if (
        not isinstance(level, str)
        or level not in _DEDUPLICATION_REASONS
        or not isinstance(reason, str)
        or reason != _DEDUPLICATION_REASONS[level]
        or isinstance(reused_evidence_count, bool)
        or not isinstance(reused_evidence_count, int)
        or reused_evidence_count < 0
        or not isinstance(stages, list)
        or not all(isinstance(stage, str) and stage in _REUSABLE_STAGES for stage in stages)
        or len(set(stages)) != len(stages)
    ):
        raise _invalid_deduplication()
    if level in {"D0", "D1"}:
        if not isinstance(reused_document_id, str) or not reused_document_id:
            raise _invalid_deduplication()
    elif reused_document_id is not None:
        raise _invalid_deduplication()
    normalized_body_sha256 = row[5]
    if level == "D1":
        if not _is_sha256(normalized_body_sha256):
            raise _invalid_deduplication()
    elif normalized_body_sha256 is not None:
        raise _invalid_deduplication()
    return DesktopDeduplication(
        level=level,
        reason=reason,
        reused_document_id=reused_document_id,
        reused_evidence_count=reused_evidence_count,
        reusable_stages=tuple(stages),
        normalized_body_sha256=normalized_body_sha256,
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _invalid_deduplication() -> DesktopImportError:
    return DesktopImportError(
        "desktop_import_state_invalid", "Import deduplication state is invalid."
    )
