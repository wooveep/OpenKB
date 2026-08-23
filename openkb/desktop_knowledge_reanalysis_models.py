"""Typed read projections for Knowledge Reanalysis."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, cast

from openkb.desktop_import_artifacts import DesktopImportError

AnalysisState = Literal["current", "analysis_outdated", "missing"]
ReanalysisStatus = Literal["pending", "running", "completed", "failed"]
ReanalysisPhase = Literal["pending", "batches", "merge", "reconciliation", "completed", "failed"]
ReanalysisRunStatus = Literal["pending", "running", "completed", "partial_failure", "failed"]
ReanalysisMode = Literal["single", "bulk"]


@dataclass(frozen=True)
class DesktopDocumentAnalysisStatus:
    document_id: str
    document_name: str
    state: AnalysisState
    schema_version: str | None
    provider: str | None
    model: str | None
    prompt_digest: str | None
    engine_version: str | None
    analyzed_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "document_name": self.document_name,
            "state": self.state,
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model": self.model,
            "prompt_digest": self.prompt_digest,
            "engine_version": self.engine_version,
            "analyzed_at": self.analyzed_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgeReanalysisJob:
    job_id: str
    run_id: str
    document_id: str
    document_name: str
    status: ReanalysisStatus
    phase: ReanalysisPhase
    progress: int
    provider: str
    model: str
    error_code: str | None
    reason: str | None
    batch_total: int
    batch_completed: int
    current_batch: int | None
    attempt_count: int | None
    created_at: str
    completed_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "status": self.status,
            "phase": self.phase,
            "progress": self.progress,
            "provider": self.provider,
            "model": self.model,
            "error_code": self.error_code,
            "reason": self.reason,
            "batch_total": self.batch_total,
            "batch_completed": self.batch_completed,
            "current_batch": self.current_batch,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgeReanalysisRun:
    run_id: str
    mode: ReanalysisMode
    status: ReanalysisRunStatus
    jobs: tuple[DesktopKnowledgeReanalysisJob, ...]
    created_at: str
    completed_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "total": len(self.jobs),
            "completed": sum(job.status == "completed" for job in self.jobs),
            "failed": sum(job.status == "failed" for job in self.jobs),
            "jobs": [job.as_dict() for job in self.jobs],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


def knowledge_reanalysis_runs_in(
    connection: sqlite3.Connection,
) -> tuple[DesktopKnowledgeReanalysisRun, ...]:
    rows = connection.execute(
        """
        SELECT run_id, mode, status, created_at, completed_at
        FROM knowledge_reanalysis_runs ORDER BY created_at DESC LIMIT 20
        """
    ).fetchall()
    return tuple(_run_from_row(connection, row) for row in rows)


def require_knowledge_reanalysis_run_in(
    connection: sqlite3.Connection, run_id: str
) -> DesktopKnowledgeReanalysisRun:
    row = connection.execute(
        """
        SELECT run_id, mode, status, created_at, completed_at
        FROM knowledge_reanalysis_runs WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise DesktopImportError(
            "knowledge_reanalysis_run_not_found", "Knowledge Reanalysis run was not found."
        )
    return _run_from_row(connection, row)


def _run_from_row(
    connection: sqlite3.Connection, row: tuple[object, ...]
) -> DesktopKnowledgeReanalysisRun:
    jobs = connection.execute(
        """
        SELECT jobs.job_id, jobs.run_id, jobs.document_id, documents.display_name,
            jobs.status, jobs.phase, jobs.progress, jobs.provider, jobs.model,
            jobs.error_code, jobs.reason,
            (SELECT COUNT(*) FROM knowledge_reanalysis_batches AS batches
                WHERE batches.job_id = jobs.job_id),
            (SELECT COUNT(*) FROM knowledge_reanalysis_batches AS batches
                WHERE batches.job_id = jobs.job_id AND batches.status = 'completed'),
            (SELECT MIN(batch_ordinal) + 1 FROM knowledge_reanalysis_batches AS batches
                WHERE batches.job_id = jobs.job_id
                    AND batches.status IN ('pending', 'running', 'failed')),
            jobs.attempt_count, jobs.created_at, jobs.completed_at
        FROM knowledge_reanalysis_jobs AS jobs
        JOIN source_documents AS documents ON documents.document_id = jobs.document_id
        WHERE jobs.run_id = ? ORDER BY jobs.created_at, jobs.rowid
        """,
        (str(row[0]),),
    ).fetchall()
    return DesktopKnowledgeReanalysisRun(
        run_id=str(row[0]),
        mode=cast(ReanalysisMode, str(row[1])),
        status=cast(ReanalysisRunStatus, str(row[2])),
        jobs=tuple(_job_from_row(job) for job in jobs),
        created_at=str(row[3]),
        completed_at=str(row[4]) if row[4] is not None else None,
    )


def _job_from_row(row: tuple[object, ...]) -> DesktopKnowledgeReanalysisJob:
    return DesktopKnowledgeReanalysisJob(
        job_id=str(row[0]),
        run_id=str(row[1]),
        document_id=str(row[2]),
        document_name=str(row[3]),
        status=cast(ReanalysisStatus, str(row[4])),
        phase=cast(ReanalysisPhase, str(row[5])),
        progress=int(str(row[6])),
        provider=str(row[7]),
        model=str(row[8]),
        error_code=str(row[9]) if row[9] is not None else None,
        reason=str(row[10]) if row[10] is not None else None,
        batch_total=int(str(row[11])),
        batch_completed=int(str(row[12])),
        current_batch=int(str(row[13])) if row[13] is not None else None,
        attempt_count=int(str(row[14])) if row[14] is not None else None,
        created_at=str(row[15]),
        completed_at=str(row[16]) if row[16] is not None else None,
    )
