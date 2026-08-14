"""Wire-safe values shared by Desktop import runtime and Bridge layers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesktopStageRun:
    """One durable import stage, suitable for the task center and Bridge events."""

    stage_run_id: str
    stage: str
    status: str
    progress: int
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "stage_run_id": self.stage_run_id,
            "stage": self.stage,
            "status": self.status,
            "progress": self.progress,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class DesktopImportedDocument:
    """The Available Knowledge record exposed once the final transaction commits."""

    document_id: str
    name: str
    source_format: str
    raw_asset_sha256: str
    evidence_count: int
    availability: str

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "name": self.name,
            "source_format": self.source_format,
            "raw_asset_sha256": self.raw_asset_sha256,
            "evidence_count": self.evidence_count,
            "availability": self.availability,
        }


@dataclass(frozen=True)
class DesktopImportJob:
    """The persisted task-center record for one selected source."""

    job_id: str
    status: str
    progress: int
    document_id: str | None
    deduplicated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "document_id": self.document_id,
            "deduplicated": self.deduplicated,
        }


@dataclass(frozen=True)
class DesktopTextImportResult:
    """The successful result of importing one TXT file."""

    document: DesktopImportedDocument
    job: DesktopImportJob
    stages: tuple[DesktopStageRun, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "document": self.document.as_dict(),
            "job": self.job.as_dict(),
            "stages": [stage.as_dict() for stage in self.stages],
        }


@dataclass(frozen=True)
class DesktopImportTask:
    """One persisted task-center record, including failed crash recovery work."""

    job: DesktopImportJob
    document: DesktopImportedDocument | None
    stages: tuple[DesktopStageRun, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "job": self.job.as_dict(),
            "document": self.document.as_dict() if self.document is not None else None,
            "stages": [stage.as_dict() for stage in self.stages],
        }
