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
from openkb.desktop_knowledge_analysis_batches import knowledge_analysis_progress_in
from openkb.desktop_model_recovery import model_recovery_assessment_in
from openkb.desktop_model_usage import (
    current_model_activity_in,
    model_usage_aggregate_in,
    model_usage_records_in,
)
from openkb.desktop_parser_runtime import ParserFamily, parser_runtime_snapshot

_DEDUPLICATION_REASONS = {
    "D0": "raw_asset_sha256_match",
    "D1": "normalized_body_sha256_match",
    "D2": "evidence_sha256_match",
}
_REUSABLE_STAGES = frozenset(
    ("document_ir", "evidence", "deterministic_page_tree", "model_analysis", "search")
)


def task_from_row(
    connection: sqlite3.Connection, row: tuple[object, ...], stage_order_sql: str
) -> DesktopImportTask:
    job_id = str(row[0])
    stages = stages_for_job(connection, job_id, stage_order_sql)
    model_calls, quarantine = model_details_for_job(connection, job_id)
    knowledge_analysis = knowledge_analysis_progress_in(connection, job_id, model_calls)
    model_usage = model_usage_records_in(connection, job_id)
    model_usage_aggregate = model_usage_aggregate_in(connection, job_id)
    model_activity = current_model_activity_in(connection, job_id)
    model_recovery = model_recovery_assessment_in(connection, job_id)
    deduplication = _deduplication_for_job(connection, job_id)
    runtime_status = str(row[1])
    if runtime_status == "running":
        # A manual recovery leaves the old quarantine record in place until it
        # publishes atomically, but it should appear as active work while running.
        quarantine = None
    public_status = runtime_status
    if runtime_status == "paused" and any(
        stage.error_code == "awaiting_model_configuration" for stage in stages
    ):
        public_status = "awaiting_model_configuration"
    job = DesktopImportJob(
        job_id=job_id,
        source_name=Path(str(row[10])).name,
        status="quarantined" if quarantine is not None else public_status,
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
        knowledge_analysis=knowledge_analysis,
        import_progress=_import_progress_in(
            connection,
            job_id,
            stages,
            knowledge_analysis,
            Path(str(row[10])),
        ),
        model_usage=model_usage,
        model_usage_aggregate=model_usage_aggregate,
        model_activity=model_activity,
        legacy_model_recovery=(model_recovery.as_dict() if model_recovery is not None else None),
    )


def _import_progress_in(
    connection: sqlite3.Connection,
    job_id: str,
    stages: tuple[DesktopStageRun, ...],
    knowledge_analysis,
    source: Path,
) -> tuple[dict[str, object], ...]:
    """Project user-visible steps only from durable stage and batch checkpoints."""
    by_name = {stage.stage: stage for stage in stages}

    def stage_step(key: str, source: str) -> dict[str, object]:
        stage = by_name[source]
        return {
            "stage": key,
            "status": stage.status,
            "source_stage_run_id": stage.stage_run_id,
            "error_code": stage.error_code,
        }

    document_ir = by_name["document_ir"]
    parser: dict[str, object] = {
        "stage": "parser_initialization",
        "status": (
            "completed" if document_ir.status in {"completed", "skipped"} else document_ir.status
        ),
        "source_stage_run_id": document_ir.stage_run_id,
        "error_code": document_ir.error_code,
        "runtime_kind": "parser",
        **_parser_progress_metadata_in(connection, job_id, source, document_ir),
    }
    model_stage = by_name["model_analysis"]
    plan_exists = (
        connection.execute(
            "SELECT 1 FROM knowledge_analysis_plans WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        is not None
    )
    plan_status = (
        "completed"
        if plan_exists
        else "skipped"
        if model_stage.status == "skipped"
        else "failed"
        if model_stage.status == "failed"
        else "running"
        if model_stage.status == "running"
        else "pending"
    )
    total = knowledge_analysis.total if knowledge_analysis is not None else 0
    completed = knowledge_analysis.completed if knowledge_analysis is not None else 0
    failed = knowledge_analysis.failed if knowledge_analysis is not None else 0
    active = knowledge_analysis.active if knowledge_analysis is not None else 0
    if model_stage.status == "skipped":
        batch_status = "skipped"
    elif failed:
        batch_status = "failed"
    elif total and completed == total:
        batch_status = "completed"
    elif active or completed:
        batch_status = "running"
    elif model_stage.status == "completed" and total == 0:
        batch_status = "completed"
    else:
        batch_status = "pending"
    merge_row = connection.execute(
        "SELECT status, error_code FROM knowledge_analysis_merges WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    merge_status = (
        str(merge_row[0])
        if merge_row is not None
        else "skipped"
        if model_stage.status == "skipped"
        else "completed"
        if model_stage.status == "completed"
        else "pending"
    )
    analysis_stage_id = model_stage.stage_run_id
    return (
        stage_step("preflight", "preflight"),
        stage_step("raw_asset", "raw_asset"),
        parser,
        stage_step("document_ir", "document_ir"),
        stage_step("evidence", "evidence"),
        {
            "stage": "knowledge_analysis_plan",
            "status": plan_status,
            "source_stage_run_id": analysis_stage_id,
            "error_code": model_stage.error_code,
            "runtime_kind": "model",
        },
        {
            "stage": "knowledge_analysis_batches",
            "status": batch_status,
            "source_stage_run_id": analysis_stage_id,
            "error_code": model_stage.error_code,
            "completed": completed,
            "total": total,
            "runtime_kind": "model",
        },
        {
            "stage": "knowledge_analysis_merge",
            "status": merge_status,
            "source_stage_run_id": analysis_stage_id,
            "error_code": str(merge_row[1]) if merge_row and merge_row[1] else None,
            "runtime_kind": "model",
        },
        stage_step("publication", "search"),
    )


def _parser_progress_metadata_in(
    connection: sqlite3.Connection,
    job_id: str,
    source: Path,
    document_ir: DesktopStageRun,
) -> dict[str, object]:
    family, planned_route = _parser_identity(source)
    route = _completed_parser_route_in(connection, job_id) or planned_route
    resource_state, runtime_state = _parser_runtime_state(
        family,
        route,
        document_ir.status,
    )
    return {
        "parser_family": family,
        "parser_route": route,
        "parser_resource_state": resource_state,
        "parser_runtime_state": runtime_state,
    }


def _parser_identity(source: Path) -> tuple[str, str]:
    suffix = source.suffix.casefold()
    if suffix in {".txt", ".md", ".markdown"}:
        return "text", "plain_text"
    if suffix in {".doc", ".ppt"}:
        return "legacy_office", "tika_legacy"
    if suffix == ".pdf":
        return "pdf", "auto"
    return "native_office", "direct_structured"


def _completed_parser_route_in(connection: sqlite3.Connection, job_id: str) -> str | None:
    row = connection.execute(
        """
        SELECT runtime.checkpoint_json
        FROM stage_run_runtime AS runtime
        JOIN stage_runs AS stages ON stages.stage_run_id = runtime.stage_run_id
        WHERE runtime.job_id = ? AND stages.stage = 'document_ir'
        """,
        (job_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        checkpoint = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return None
    blocks = checkpoint.get("blocks") if isinstance(checkpoint, dict) else checkpoint
    if not isinstance(blocks, list):
        return None
    routes = {
        route
        for item in blocks
        if isinstance(item, dict)
        and isinstance(locator := item.get("locator"), dict)
        and isinstance(route := locator.get("parser_route"), str)
        and route
    }
    for preferred in ("bundled_onnx_ocr", "tika_legacy", "pymupdf_fast"):
        if preferred in routes:
            return preferred
    return None


def _parser_runtime_state(
    family: str,
    route: str,
    stage_status: str,
) -> tuple[str, str]:
    if stage_status in {"completed", "skipped"}:
        return "resources_ready", "ready"
    if stage_status == "failed":
        return "unavailable", "unavailable"
    if family == "text":
        return "resources_ready", "ready"
    snapshot_family: ParserFamily | None = (
        "legacy_office"
        if family == "legacy_office"
        else "pdf_ocr"
        if family == "pdf" and route == "bundled_onnx_ocr"
        else "native_office"
        if family == "native_office"
        else None
    )
    if snapshot_family is None:
        return "resources_ready", "not_loaded"
    readiness = parser_runtime_snapshot()[snapshot_family]
    return readiness.resource_state, readiness.runtime_state


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
