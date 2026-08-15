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
class DesktopDeduplication:
    """The durable D0–D2 reuse outcome for one completed import job."""

    level: str
    reason: str
    reused_document_id: str | None
    reused_evidence_count: int
    reusable_stages: tuple[str, ...]
    normalized_body_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "reason": self.reason,
            "reused_document_id": self.reused_document_id,
            "reused_evidence_count": self.reused_evidence_count,
            "reusable_stages": list(self.reusable_stages),
            "normalized_body_sha256": self.normalized_body_sha256,
        }


@dataclass(frozen=True)
class DesktopDocumentVersionCandidate:
    """One user-reviewable D3 suggestion; it never changes source identity itself."""

    candidate_id: str
    document_id: str
    document_name: str
    candidate_document_id: str
    candidate_document_name: str
    lexical_score: float
    character_score: float
    reason: str
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "candidate_document_id": self.candidate_document_id,
            "candidate_document_name": self.candidate_document_name,
            "lexical_score": self.lexical_score,
            "character_score": self.character_score,
            "reason": self.reason,
            "status": self.status,
        }


@dataclass(frozen=True)
class DesktopImportJob:
    """The persisted task-center record for one selected source."""

    job_id: str
    source_name: str
    status: str
    progress: int
    document_id: str | None
    deduplicated: bool
    deduplication: DesktopDeduplication | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "source_name": self.source_name,
            "status": self.status,
            "progress": self.progress,
            "document_id": self.document_id,
            "deduplicated": self.deduplicated,
            "deduplication": self.deduplication.as_dict() if self.deduplication else None,
        }


@dataclass(frozen=True)
class DesktopModelAttempt:
    """A safe, persisted physical provider attempt for one Model Call."""

    attempt: int
    status: str
    timeout_seconds: float
    remaining_seconds: float
    error_code: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "status": self.status,
            "timeout_seconds": self.timeout_seconds,
            "remaining_seconds": self.remaining_seconds,
            "error_code": self.error_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DesktopModelCall:
    """The safe task-center projection of a logical Model Call and its attempts."""

    call_id: str
    stage_run_id: str
    operation: str
    status: str
    attempt_count: int
    timeout_seconds: float
    next_timeout_seconds: float | None
    remaining_seconds: float
    error_code: str | None = None
    reason: str | None = None
    suggested_action: str | None = None
    attempts: tuple[DesktopModelAttempt, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "stage_run_id": self.stage_run_id,
            "operation": self.operation,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "timeout_seconds": self.timeout_seconds,
            "next_timeout_seconds": self.next_timeout_seconds,
            "remaining_seconds": self.remaining_seconds,
            "error_code": self.error_code,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class DesktopQuarantinedDocument:
    """The durable, safe failure record for an unpublished document."""

    stage_run_id: str
    stage: str
    error_code: str
    reason: str
    suggested_action: str
    attempt_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "stage_run_id": self.stage_run_id,
            "stage": self.stage,
            "error_code": self.error_code,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "attempt_count": self.attempt_count,
        }


@dataclass(frozen=True)
class DesktopRecoveryOverride:
    """One manual recovery's model settings, never a knowledge-base default."""

    model: str | None = None
    initial_timeout_seconds: float | None = None


@dataclass(frozen=True)
class DesktopTextImportResult:
    """The successful result of importing one Desktop document."""

    document: DesktopImportedDocument
    job: DesktopImportJob
    stages: tuple[DesktopStageRun, ...]
    model_calls: tuple[DesktopModelCall, ...] = ()
    quarantine: DesktopQuarantinedDocument | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "document": self.document.as_dict(),
            "job": self.job.as_dict(),
            "stages": [stage.as_dict() for stage in self.stages],
            "model_calls": [call.as_dict() for call in self.model_calls],
            "quarantine": self.quarantine.as_dict() if self.quarantine is not None else None,
        }


@dataclass(frozen=True)
class DesktopImportTask:
    """One persisted task-center record, including failed crash recovery work."""

    job: DesktopImportJob
    document: DesktopImportedDocument | None
    stages: tuple[DesktopStageRun, ...]
    model_calls: tuple[DesktopModelCall, ...] = ()
    quarantine: DesktopQuarantinedDocument | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "job": self.job.as_dict(),
            "document": self.document.as_dict() if self.document is not None else None,
            "stages": [stage.as_dict() for stage in self.stages],
            "model_calls": [call.as_dict() for call in self.model_calls],
            "quarantine": self.quarantine.as_dict() if self.quarantine is not None else None,
        }
