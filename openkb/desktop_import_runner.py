"""Document worker that executes or resumes durable Desktop Import Jobs."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from portalocker import LockException

from openkb.desktop_document_parsers import analysis_text, parse_structured_document
from openkb.desktop_document_versions import DesktopDocumentVersionService
from openkb.desktop_import_artifacts import (
    DesktopImportError,
    DocumentIRBlock,
    SourceImage,
    build_document_ir,
    build_evidence,
    decode_text,
    document_ir_checkpoint,
    document_ir_from_checkpoint,
    evidence_checkpoint,
    evidence_from_checkpoint,
    source_format_for_path,
    source_format_is_textual,
    source_format_uses_structured_ir,
    source_images_from_checkpoint,
    source_media_type,
    validate_text_source,
)
from openkb.desktop_import_deduplication import normalized_body_sha256
from openkb.desktop_import_model_ledger import DesktopImportModelLedger
from openkb.desktop_import_quarantine import DesktopImportQuarantineStore
from openkb.desktop_import_recovery import DesktopImportRecoveryStore
from openkb.desktop_import_store import IMPORT_STAGES, DesktopImportStore, ImportJobState
from openkb.desktop_import_types import (
    DesktopImportTask,
    DesktopRecoveryOverride,
    DesktopStageRun,
    DesktopTextImportResult,
)
from openkb.desktop_model_gateway import (
    DesktopModelAttemptEvent,
    DesktopModelCallError,
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelResult,
)
from openkb.locks import kb_ingest_lock

StageProgressCallback = Callable[[dict[str, object]], None]
_CONTROL_CODES = {"import_paused", "import_cancelled"}
_DIRECT_QUARANTINE_CODES = {
    "legacy_office_parse_failed",
    "legacy_office_runtime_unavailable",
}
logger = logging.getLogger(__name__)


class DesktopImportControl:
    """In-memory worker signals; durable state changes occur at stage boundaries."""

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def request_pause(self) -> None:
        self._pause.set()

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def action(self) -> str | None:
        if self._cancel.is_set():
            return "cancelled"
        if self._pause.is_set():
            return "paused"
        return None


class DesktopTextImportService:
    """Run one Desktop document Import Job while keeping stages resumable."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        on_stage_progress: StageProgressCallback | None = None,
        control: DesktopImportControl | None = None,
        model_gateway: DesktopModelGateway | None = None,
    ) -> None:
        self._store = DesktopImportStore(kb_dir, on_stage_progress=on_stage_progress)
        self._model_ledger = DesktopImportModelLedger(kb_dir)
        self._document_versions = DesktopDocumentVersionService(kb_dir)
        self._quarantine = DesktopImportQuarantineStore(kb_dir)
        self._recovery = DesktopImportRecoveryStore(kb_dir, on_stage_progress=on_stage_progress)
        self._control = control or DesktopImportControl()
        self._model_gateway = model_gateway

    def import_text(self, source_path: Path) -> DesktopTextImportResult:
        """Create and execute a brand-new supported document Import Job."""
        source = validate_text_source(source_path)
        try:
            # A live worker owns the KB lock, so open-time recovery sees only a crashed owner.
            with kb_ingest_lock(self._store.state_dir):
                self._store.require_database()
                return self._run(self._store.create_job(source))
        except DesktopImportError:
            raise
        except (OSError, sqlite3.Error, LockException) as error:
            raise DesktopImportError(
                "desktop_import_failed", f"Could not import {source.name}: {error}"
            ) from error

    def resume_text(self, job_id: str) -> DesktopTextImportResult:
        """Resume from the earliest pending stage without rerunning checkpoints."""
        try:
            with kb_ingest_lock(self._store.state_dir):
                return self._run(self._store.resume_job(job_id))
        except DesktopImportError:
            raise
        except (OSError, sqlite3.Error, LockException) as error:
            raise DesktopImportError(
                "desktop_import_failed", f"Could not resume import {job_id}: {error}"
            ) from error

    def recover_text(
        self, job_id: str, override: DesktopRecoveryOverride
    ) -> DesktopTextImportResult:
        """Resume a quarantined document at its failed stage using one run-only override."""
        try:
            with kb_ingest_lock(self._store.state_dir):
                return self._run(self._recovery.begin(job_id, override))
        except DesktopImportError:
            raise
        except (OSError, sqlite3.Error, LockException) as error:
            raise DesktopImportError(
                "desktop_import_failed", f"Could not recover import {job_id}: {error}"
            ) from error

    def recoverable_job_ids(self) -> tuple[str, ...]:
        """Expose durable recovery work so the Engine can give each job its own control."""
        return self._store.resumable_job_ids()

    def list_import_jobs(self) -> dict[str, object]:
        return self._store.list_import_jobs()

    def task(self, job_id: str) -> DesktopImportTask:
        return self._store.task(job_id)

    def cancel_paused_job(self, job_id: str) -> None:
        """Cancel a job after its worker has already released the KB lock."""
        state = self._store.job_state(job_id)
        if state.status not in {"paused", "recoverable"}:
            raise DesktopImportError(
                "import_job_not_cancellable", f"Job {job_id} is {state.status}; not cancellable."
            )
        self._store.cancel_job(state, self._next_stage(state))

    def _run(self, state: ImportJobState) -> DesktopTextImportResult:
        stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
        active_stage = (
            "raw_asset" if self._completed(stages, "raw_asset") else self._next_stage(state, stages)
        )
        terminal_state_committed = False
        try:
            raw_bytes, text, source_format, asset_sha256, raw_path = self._raw_input(state, stages)
            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}

            if not self._completed(stages, "document_ir"):
                active_stage = "document_ir"
                self._honor_control(state, active_stage)
                self._store.set_stage(state, active_stage, "running", 40)
                source_images: tuple[SourceImage, ...]
                if not source_format_uses_structured_ir(source_format):
                    blocks = build_document_ir(text)
                    source_images = ()
                else:
                    parsed = parse_structured_document(state.source, raw_bytes)
                    blocks = parsed.blocks
                    source_images = parsed.source_images
                    self._store.write_source_images(source_images)
                self._store.set_stage(
                    state,
                    active_stage,
                    "completed",
                    55,
                    checkpoint=document_ir_checkpoint(blocks, source_images),
                )
            else:
                document_ir = self._checkpoint(state, "document_ir")
                blocks = document_ir_from_checkpoint(document_ir)
                source_images = source_images_from_checkpoint(document_ir)

            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
            normalized_body_hash = normalized_body_sha256(blocks)
            if not self._completed(stages, "evidence"):
                content_duplicate = self._store.find_available_document_by_normalized_body(
                    normalized_body_hash
                )
                if content_duplicate is not None:
                    document, deduplicated = self._store.complete_content_duplicate_job(
                        state=state,
                        source=state.source,
                        document_id=uuid.uuid4().hex,
                        asset_sha256=asset_sha256,
                        raw_path=raw_path,
                        raw_size=len(raw_bytes),
                        source_format=source_format,
                        raw_media_type=source_media_type(source_format),
                        source_images=source_images,
                        normalized_body_sha256=normalized_body_hash,
                        canonical_document=content_duplicate,
                    )
                    terminal_state_committed = True
                    self._record_document_version_candidates(document.document_id, blocks)
                    return self._result(
                        state.job_id,
                        document.document_id,
                        deduplicated=deduplicated,
                    )

                active_stage = "evidence"
                self._honor_control(state, active_stage)
                self._store.set_stage(state, active_stage, "running", 60)
                evidence = build_evidence(blocks)
                self._store.set_stage(
                    state,
                    active_stage,
                    "completed",
                    75,
                    checkpoint=evidence_checkpoint(evidence),
                )
            else:
                evidence = evidence_from_checkpoint(self._checkpoint(state, "evidence"), blocks)

            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
            if not self._completed(stages, "model_analysis"):
                active_stage = "model_analysis"
                self._honor_control(state, active_stage)
                self._store.set_stage(state, active_stage, "running", 80)
                if self._model_gateway is None:
                    if state.recovery_run_id is not None:
                        raise DesktopImportError(
                            "recovery_model_not_configured",
                            "A configured model is required to resume model analysis.",
                        )
                    self._store.set_stage(
                        state,
                        active_stage,
                        "skipped",
                        85,
                        error_code="model_analysis_not_configured",
                    )
                else:
                    result = self._analyze_document(
                        state,
                        active_stage,
                        text if source_format == "txt" else analysis_text(blocks),
                    )
                    self._store.set_stage(
                        state,
                        active_stage,
                        "completed",
                        85,
                        checkpoint={
                            "call_id": result.call_id,
                            "attempt_count": result.attempt_count,
                            "response_sha256": hashlib.sha256(
                                result.content.encode("utf-8")
                            ).hexdigest(),
                        },
                    )

            active_stage = "search"
            self._honor_control(state, active_stage)
            self._store.set_stage(state, active_stage, "running", 90)
            document, deduplicated = self._store.publish_document(
                state=state,
                source=state.source,
                document_id=uuid.uuid4().hex,
                asset_sha256=asset_sha256,
                raw_path=raw_path,
                raw_size=len(raw_bytes),
                source_format=source_format,
                raw_media_type=source_media_type(source_format),
                blocks=blocks,
                evidence=evidence,
                source_images=source_images,
                normalized_body_sha256=normalized_body_hash,
            )
            terminal_state_committed = True
            self._store.emit_stage(
                state,
                "search",
                "completed",
                100,
                document_id=document.document_id,
            )
            self._record_document_version_candidates(document.document_id, blocks)
            return self._result(state.job_id, document.document_id, deduplicated=deduplicated)
        except _DuplicateImport as duplicate:
            return self._result(state.job_id, duplicate.document_id, deduplicated=True)
        except DesktopModelCallError as error:
            self._model_ledger.quarantine(
                job_id=state.job_id,
                stage_run_id=state.stage_ids[active_stage],
                stage=active_stage,
                call_id=error.call_id,
                failure=error.failure,
                attempt_count=error.attempt_count,
            )
            self._recovery.mark_finished(state, "failed")
            self._store.emit_stage(
                state,
                active_stage,
                "failed",
                100,
                error_code=error.failure.code,
            )
            raise DesktopImportError("document_quarantined", error.failure.reason) from error
        except DesktopImportError as error:
            if error.code in _DIRECT_QUARANTINE_CODES:
                self._quarantine.quarantine(
                    job_id=state.job_id,
                    stage_run_id=state.stage_ids[active_stage],
                    stage=active_stage,
                    error_code=error.code,
                    reason=str(error),
                    suggested_action=error.suggested_action
                    or "Convert the document to DOCX or PPTX and import it again.",
                )
                self._recovery.mark_finished(state, "failed")
                self._store.emit_stage(
                    state,
                    active_stage,
                    "failed",
                    100,
                    error_code=error.code,
                )
                raise
            if error.code not in _CONTROL_CODES and not terminal_state_committed:
                self._store.fail_job(state, active_stage, error.code)
                self._recovery.mark_failed(state, active_stage, error.code)
            elif error.code == "import_cancelled":
                self._recovery.mark_finished(state, "cancelled")
            raise
        except (OSError, sqlite3.Error, LockException) as error:
            wrapped = DesktopImportError(
                "desktop_import_failed", f"Could not import {state.source.name}: {error}"
            )
            if not terminal_state_committed:
                self._store.fail_job(state, active_stage, wrapped.code)
                self._recovery.mark_failed(state, active_stage, wrapped.code)
            raise wrapped from error

    def _analyze_document(self, state: ImportJobState, stage: str, text: str) -> DesktopModelResult:
        if self._model_gateway is None:
            raise DesktopImportError(
                "desktop_import_state_invalid", "Model Gateway is unavailable."
            )

        def record_attempt(event: DesktopModelAttemptEvent) -> None:
            self._model_ledger.record_attempt(
                job_id=state.job_id,
                stage_run_id=state.stage_ids[stage],
                operation="document_analysis",
                event=event,
            )
            self._store.emit_stage(
                state,
                stage,
                "running",
                80,
                error_code=event.error_code,
            )

        return self._model_gateway.analyze(
            DesktopModelRequest("document_analysis", state.source.name, text),
            on_event=record_attempt,
        )

    def _raw_input(
        self, state: ImportJobState, stages: Mapping[str, DesktopStageRun]
    ) -> tuple[bytes, str, str, str, str]:
        source_format = source_format_for_path(state.source)
        raw_suffix = state.source.suffix.lower()
        if self._completed(stages, "raw_asset"):
            checkpoint = self._checkpoint(state, "raw_asset")
            if not isinstance(checkpoint, dict):
                raise DesktopImportError(
                    "import_checkpoint_invalid", "Invalid raw asset checkpoint."
                )
            raw_path = checkpoint.get("raw_path")
            expected_hash = checkpoint.get("asset_sha256")
            expected_size = checkpoint.get("raw_size")
            if (
                not isinstance(raw_path, str)
                or not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or any(character not in "0123456789abcdef" for character in expected_hash)
                or type(expected_size) is not int
                or expected_size < 0
                or raw_path != f"raw/{expected_hash}{raw_suffix}"
            ):
                raise DesktopImportError(
                    "import_checkpoint_invalid", "Invalid raw asset checkpoint."
                )
            raw_bytes = (self._store.kb_dir / raw_path).read_bytes()
            actual_hash = hashlib.sha256(raw_bytes).hexdigest()
            if actual_hash != expected_hash or len(raw_bytes) != expected_size:
                raise DesktopImportError(
                    "raw_asset_integrity_failed", "Saved raw asset fails checkpoint."
                )
            if source_format_is_textual(source_format):
                text = decode_text(raw_bytes, state.source)
            else:
                text = ""
            return raw_bytes, text, source_format, actual_hash, raw_path

        active_stage = "preflight"
        preflight_completed = self._completed(stages, active_stage)
        if not preflight_completed:
            self._honor_control(state, active_stage)
        raw_bytes = state.source.read_bytes()
        if source_format_is_textual(source_format):
            text = decode_text(raw_bytes, state.source)
        else:
            text = ""
        asset_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if not preflight_completed or not self._matches_preflight_checkpoint(
            self._checkpoint(state, active_stage), asset_sha256, len(raw_bytes)
        ):
            if preflight_completed:
                self._honor_control(state, active_stage)
            self._store.set_stage(state, active_stage, "running", 0)
            self._store.set_stage(
                state,
                active_stage,
                "completed",
                20,
                checkpoint={"asset_sha256": asset_sha256, "raw_size": len(raw_bytes)},
            )

        active_stage = "raw_asset"
        self._honor_control(state, active_stage)
        self._store.set_stage(state, active_stage, "running", 25)
        duplicate = self._store.find_available_document(asset_sha256)
        if duplicate is not None:
            self._store.set_stage(state, active_stage, "completed", 35)
            self._store.complete_duplicate_job(state, duplicate.document_id)
            raise _DuplicateImport(duplicate.document_id)
        raw_path = self._store.write_raw_asset(asset_sha256, raw_bytes, raw_suffix)
        self._store.set_stage(
            state,
            active_stage,
            "completed",
            35,
            checkpoint={
                "asset_sha256": asset_sha256,
                "raw_path": raw_path,
                "raw_size": len(raw_bytes),
            },
        )
        return raw_bytes, text, source_format, asset_sha256, raw_path

    def _result(
        self, job_id: str, document_id: str, *, deduplicated: bool
    ) -> DesktopTextImportResult:
        task = self._store.task(job_id)
        if task.document is None or task.document.document_id != document_id:
            raise DesktopImportError(
                "desktop_import_state_invalid", f"Job missing document: {job_id}."
            )
        return DesktopTextImportResult(
            document=task.document,
            job=task.job,
            stages=task.stages,
            model_calls=task.model_calls,
            quarantine=task.quarantine,
        )

    def _record_document_version_candidates(
        self, document_id: str, blocks: tuple[DocumentIRBlock, ...]
    ) -> None:
        """D3 suggestions never make an otherwise successful import fail."""
        try:
            self._document_versions.record_candidates(document_id, blocks)
        except (DesktopImportError, OSError, sqlite3.Error, ValueError) as error:
            logger.warning(
                "Could not record D3 Document Version Candidates for %s: %s", document_id, error
            )

    def _honor_control(self, state: ImportJobState, stage: str) -> None:
        action = self._control.action
        if action == "paused":
            self._store.pause_job(state, stage)
            raise DesktopImportError("import_paused", "Import was paused at a stage checkpoint.")
        if action == "cancelled":
            self._store.cancel_job(state, stage)
            raise DesktopImportError("import_cancelled", "Import cancelled at checkpoint.")

    def _checkpoint(self, state: ImportJobState, stage: str) -> object:
        checkpoint = self._store.checkpoint(state.stage_ids[stage])
        if checkpoint is None:
            raise DesktopImportError(
                "import_checkpoint_invalid", f"Completed stage {stage} has no usable checkpoint."
            )
        return checkpoint

    def _next_stage(
        self, state: ImportJobState, stages: Mapping[str, DesktopStageRun] | None = None
    ) -> str:
        values = stages or {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
        for stage in IMPORT_STAGES:
            if values[stage].status not in {"completed", "skipped"}:
                return stage
        return "search"

    @staticmethod
    def _completed(stages: Mapping[str, DesktopStageRun], stage: str) -> bool:
        return stages[stage].status in {"completed", "skipped"}

    @staticmethod
    def _matches_preflight_checkpoint(checkpoint: object, asset_sha256: str, raw_size: int) -> bool:
        return (
            isinstance(checkpoint, dict)
            and checkpoint.get("asset_sha256") == asset_sha256
            and type(checkpoint.get("raw_size")) is int
            and checkpoint["raw_size"] == raw_size
        )


class _DuplicateImport(Exception):
    """Internal signal used to reuse the existing document without more stages."""

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
