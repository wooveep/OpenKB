"""Document worker that executes or resumes durable Desktop Import Jobs."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from portalocker import LockException

from openkb import __version__
from openkb import desktop_page_tree as page_tree_runtime
from openkb import desktop_page_tree_store as page_tree_store
from openkb.desktop_document_parsers import parse_structured_document
from openkb.desktop_document_usability import require_usable_document_ir
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
from openkb.desktop_import_checkpoint_validation import (
    matches_preflight_checkpoint,
    stage_completed,
)
from openkb.desktop_import_control import DesktopImportControl
from openkb.desktop_import_deduplication import DuplicateImportSignal, normalized_body_sha256
from openkb.desktop_import_failures import DIRECT_IMPORT_QUARANTINE_CODES
from openkb.desktop_import_model_call import run_import_model_call
from openkb.desktop_import_model_ledger import DesktopImportModelLedger
from openkb.desktop_import_quarantine import DesktopImportQuarantineStore
from openkb.desktop_import_recovery import DesktopImportRecoveryStore
from openkb.desktop_import_store import IMPORT_STAGES, DesktopImportStore, ImportJobState
from openkb.desktop_import_types import (
    DesktopImportedDocument,
    DesktopImportTask,
    DesktopRecoveryOverride,
    DesktopStageRun,
    DesktopTextImportResult,
)
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    knowledge_analysis_from_checkpoint,
    knowledge_analysis_provenance_from_checkpoint,
)
from openkb.desktop_knowledge_analysis_batches import (
    DesktopKnowledgeAnalysisBatchStore,
    run_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_reuse import (
    ReusableKnowledgeAnalysis,
    canonical_analysis_changes_in,
    canonical_analysis_document_id_in,
    canonical_analysis_evidence_map_in,
    load_reusable_knowledge_analysis,
)
from openkb.desktop_knowledge_graph import (
    record_graph_extraction_diagnostic,
    start_graph_extraction,
)
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_legacy_model_recovery import DesktopLegacyModelRecoveryService
from openkb.desktop_missing_sources import record_missing_source_candidates_in
from openkb.desktop_model_analysis_gate import (
    DesktopAnalysisCapabilityGate,
    DesktopImportAnalysisExecution,
)
from openkb.desktop_model_execution_profile import DesktopModelCapacityError
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
)
from openkb.desktop_okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.desktop_parser_runtime import begin_parser_warmup, require_parser_mode
from openkb.locks import kb_import_activity_lock

StageProgressCallback = Callable[[dict[str, object]], None]
_CONTROL_CODES = {"import_paused", "import_cancelled", "awaiting_model_configuration"}
logger = logging.getLogger(__name__)


class DesktopTextImportService:
    """Run one Desktop document Import Job while keeping stages resumable."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        on_stage_progress: StageProgressCallback | None = None,
        control: DesktopImportControl | None = None,
        model_gateway: DesktopModelGateway | None = None,
        require_model_analysis: bool = False,
        parser_mode: str = "auto",
    ) -> None:
        self._store = DesktopImportStore(kb_dir, on_stage_progress=on_stage_progress)
        self._model_ledger = DesktopImportModelLedger(kb_dir)
        self._document_versions = DesktopDocumentVersionService(kb_dir)
        self._knowledge_reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
        self._knowledge_analysis_batches = DesktopKnowledgeAnalysisBatchStore(kb_dir)
        self._quarantine = DesktopImportQuarantineStore(kb_dir)
        self._recovery = DesktopImportRecoveryStore(kb_dir, on_stage_progress=on_stage_progress)
        self._control = control or DesktopImportControl()
        self._model_gateway = model_gateway
        self._require_model_analysis = require_model_analysis
        self._parser_mode = require_parser_mode(parser_mode)

    def import_text(self, source_path: Path) -> DesktopTextImportResult:
        """Create and execute a brand-new supported document Import Job."""
        source = validate_text_source(source_path)
        with kb_import_activity_lock(self._store.state_dir):
            try:
                self._store.require_database()
                result = self._run(self._store.create_job(source))
            except DesktopImportError:
                raise
            except (OSError, sqlite3.Error, LockException) as error:
                raise DesktopImportError(
                    "desktop_import_failed", f"Could not import {source.name}: {error}"
                ) from error
        self._start_graph_extraction(result)
        return result

    def resume_text(self, job_id: str) -> DesktopTextImportResult:
        """Resume from the earliest pending stage without rerunning checkpoints."""
        with kb_import_activity_lock(self._store.state_dir):
            try:
                result = self._run(self._store.resume_job(job_id))
            except DesktopImportError:
                raise
            except (OSError, sqlite3.Error, LockException) as error:
                raise DesktopImportError(
                    "desktop_import_failed", f"Could not resume import {job_id}: {error}"
                ) from error
        self._start_graph_extraction(result)
        return result

    def recover_text(
        self, job_id: str, override: DesktopRecoveryOverride
    ) -> DesktopTextImportResult:
        """Resume a quarantined document at its failed stage using one run-only override."""
        with kb_import_activity_lock(self._store.state_dir):
            legacy_recovery = DesktopLegacyModelRecoveryService(self._store.kb_dir)
            assessment = legacy_recovery.assessment(job_id)
            selected_legacy_recovery = assessment is not None
            if assessment is not None:
                if override.legacy_recovery_choice is None:
                    raise DesktopImportError(
                        "legacy_model_recovery_choice_required",
                        "Choose a legacy Knowledge Analysis recovery path before continuing.",
                    )
                legacy_recovery.select(
                    job_id,
                    override.legacy_recovery_choice,
                    model_override=override.model,
                    context_capacity=override.context_capacity,
                )
            try:
                result = self._run(self._recovery.begin(job_id, override))
            except DesktopImportError:
                raise
            except (OSError, sqlite3.Error, LockException) as error:
                raise DesktopImportError(
                    "desktop_import_failed", f"Could not recover import {job_id}: {error}"
                ) from error
            finally:
                if selected_legacy_recovery:
                    legacy_recovery.record_resulting_plan(job_id)
        self._start_graph_extraction(result)
        return result

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

    def _start_graph_extraction(self, result: DesktopTextImportResult) -> None:
        """Make optional graph work independent from the completed Import Job result."""
        try:
            from openkb.desktop_catalog_store import start_catalog_rebuilds

            start_catalog_rebuilds(self._store.kb_dir)
        except (OSError, RuntimeError, sqlite3.Error):
            logger.warning("Could not start Knowledge Catalog rebuilds.")
        try:
            page_tree_store.start_page_tree_rebuilds(self._store.kb_dir)
        except RuntimeError:
            logger.warning("Could not start deterministic PageTree rebuilds.")
        try:
            start_graph_extraction(
                self._store.kb_dir,
                result.document.document_id,
                model_gateway=self._model_gateway,
            )
        except (OSError, RuntimeError):
            # A document remains available through the baseline even if a local
            # worker cannot be started.
            record_graph_extraction_diagnostic(self._store.kb_dir, result.document.document_id)
            logger.warning("Could not start local knowledge graph extraction.")

    def _run(self, state: ImportJobState) -> DesktopTextImportResult:
        stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
        active_stage = (
            "raw_asset" if stage_completed(stages, "raw_asset") else self._next_stage(state, stages)
        )
        terminal_state_committed = False
        analysis_gate = DesktopAnalysisCapabilityGate(self._store.kb_dir, None, False)
        parser_warmup = begin_parser_warmup(state.source)
        try:
            raw_bytes, text, source_format, asset_sha256, raw_path = self._raw_input(state, stages)
            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}

            if not stage_completed(stages, "document_ir"):
                active_stage = "document_ir"
                self._honor_control(state, active_stage)
                self._store.set_stage(state, active_stage, "running", 40)
                source_images: tuple[SourceImage, ...]
                if not source_format_uses_structured_ir(source_format):
                    blocks = build_document_ir(text)
                    source_images = ()
                else:
                    if parser_warmup is not None:
                        parser_warmup.wait()
                    parsed = parse_structured_document(
                        state.source,
                        raw_bytes,
                        parser_mode=self._parser_mode,
                    )
                    blocks = parsed.blocks
                    source_images = parsed.source_images
                    self._store.write_source_images(source_images)
                require_usable_document_ir(blocks)
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
                require_usable_document_ir(blocks)

            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
            normalized_body_hash = normalized_body_sha256(blocks)
            content_duplicate: DesktopImportedDocument | None = None
            if not stage_completed(stages, "evidence"):
                content_duplicate = self._store.find_available_document_by_normalized_body(
                    normalized_body_hash
                )
                active_stage = "evidence"
                self._honor_control(state, active_stage)
                self._store.set_stage(state, active_stage, "running", 60)
                evidence = build_evidence(blocks)
                self._store.set_stage(
                    state,
                    active_stage,
                    "skipped" if content_duplicate is not None else "completed",
                    75,
                    checkpoint=evidence_checkpoint(evidence),
                )
            else:
                evidence = evidence_from_checkpoint(self._checkpoint(state, "evidence"), blocks)
                if stages["evidence"].status == "skipped":
                    content_duplicate = self._store.find_available_document_by_normalized_body(
                        normalized_body_hash
                    )
                    if content_duplicate is None:
                        raise DesktopImportError(
                            "desktop_import_state_invalid",
                            "The reusable normalized document is no longer Available.",
                        )

            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
            active_stage = page_tree_runtime.PAGE_TREE_STAGE
            page_tree = page_tree_runtime.prepare_import_page_tree(
                store=self._store,
                state=state,
                stage_run_id=state.stage_ids[active_stage],
                stage_status=stages[active_stage].status,
                blocks=blocks,
                evidence=evidence,
                source_images=source_images,
                honor_control=lambda: self._honor_control(state, active_stage),
            )
            if content_duplicate is not None:
                document, deduplicated = self._store.complete_content_duplicate_job(
                    state=state,
                    source=state.source,
                    document_id=page_tree.document_version_id,
                    asset_sha256=asset_sha256,
                    raw_path=raw_path,
                    raw_size=len(raw_bytes),
                    source_format=source_format,
                    raw_media_type=source_media_type(source_format),
                    source_images=source_images,
                    normalized_body_sha256=normalized_body_hash,
                    canonical_document=content_duplicate,
                    before_commit=lambda connection, published, _deduplicated: (
                        page_tree_store.publish_or_queue_page_tree_in(
                            connection, published.document_id, page_tree
                        )
                    ),
                )
                terminal_state_committed = True
                self._record_document_version_candidates(document.document_id, blocks)
                self._record_existing_knowledge_reconciliation(document.document_id)
                return self._result(
                    state.job_id,
                    document.document_id,
                    deduplicated=deduplicated,
                )

            stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
            knowledge_analysis: DesktopKnowledgeAnalysis | None = None
            analysis_provenance_json: str | None = None
            if not stage_completed(stages, "model_analysis"):
                active_stage = "model_analysis"
                self._honor_control(state, active_stage)
                self._store.set_stage(state, active_stage, "running", 80)
                if self._model_gateway is None:
                    if state.recovery_run_id is not None:
                        raise DesktopImportError(
                            "recovery_model_not_configured",
                            "A configured model is required to resume model analysis.",
                        )
                    if self._require_model_analysis:
                        self._store.await_model_configuration(state, active_stage)
                        raise DesktopImportError(
                            "awaiting_model_configuration",
                            "The parsed document is waiting for a usable Analysis Model.",
                            suggested_action=(
                                "Correct Model Configuration, then explicitly continue this import."
                            ),
                        )
                    self._store.set_stage(
                        state,
                        active_stage,
                        "skipped",
                        85,
                        error_code="model_analysis_not_configured",
                    )
                else:
                    gateway = self._model_gateway
                    try:
                        execution = DesktopImportAnalysisExecution.resolve(
                            self._store.kb_dir,
                            gateway,
                            self._knowledge_analysis_batches.persisted_plan(state.job_id),
                        )
                        analysis_gate = execution.gate
                    except DesktopModelCapacityError as error:
                        self._store.await_model_configuration(state, active_stage)
                        raise DesktopImportError(
                            "awaiting_model_configuration",
                            str(error),
                            suggested_action=(
                                "Choose a compatible Analysis profile, run Model Capability "
                                "Check, then explicitly continue this import."
                            ),
                        ) from error
                    if not analysis_gate.verified:
                        self._store.await_model_configuration(state, active_stage)
                        raise DesktopImportError(
                            "awaiting_model_configuration",
                            "The parsed document is waiting for an explicit Model Capability "
                            "Check for its exact Analysis profile.",
                            suggested_action=(
                                "Run Model Capability Check, then explicitly continue this import."
                            ),
                        )

                    def honor_analysis_control() -> None:
                        self._honor_control(state, active_stage)
                        if analysis_gate is not None and not analysis_gate.verified:
                            raise DesktopImportError(
                                "awaiting_model_configuration",
                                "The Analysis profile changed or became unverified.",
                            )

                    run = run_knowledge_analysis(
                        store=self._knowledge_analysis_batches,
                        job_id=state.job_id,
                        stage_run_id=state.stage_ids[active_stage],
                        document_name=state.source.name,
                        evidence=evidence,
                        page_tree=page_tree.generation,
                        provider=execution.provider,
                        model=execution.model,
                        engine_version=__version__,
                        analyze=lambda request: run_import_model_call(
                            gateway=gateway,
                            ledger=self._model_ledger,
                            store=self._store,
                            state=state,
                            stage=active_stage,
                            request=request,
                            is_cancelled=lambda: self._control.action is not None,
                        ),
                        honor_control=honor_analysis_control,
                        on_batch_completed=lambda completed, total: self._store.emit_stage(
                            state,
                            active_stage,
                            "running",
                            80 + min(4, round((completed / total) * 4)),
                        ),
                        max_parallel_batches=getattr(gateway, "analysis_concurrency", 1),
                        capability_profile=execution.capability,
                        execution_profile=analysis_gate.profile,
                    )
                    knowledge_analysis = run.analysis
                    analysis_provenance_json = run.provenance_json
                    self._store.set_stage(
                        state,
                        active_stage,
                        "completed",
                        85,
                        checkpoint=run.checkpoint,
                    )
            else:
                analysis_checkpoint = self._checkpoint(state, "model_analysis")
                knowledge_analysis = knowledge_analysis_from_checkpoint(analysis_checkpoint)
                if knowledge_analysis is not None:
                    analysis_provenance_json = knowledge_analysis_provenance_from_checkpoint(
                        analysis_checkpoint
                    )

            active_stage = "search"
            self._honor_control(state, active_stage)
            self._store.set_stage(state, active_stage, "running", 90)
            staged_projection: Path | None = None

            def apply_import_derivatives(
                connection: sqlite3.Connection,
                published: DesktopImportedDocument,
                _deduplicated: bool,
            ) -> None:
                nonlocal staged_projection
                page_tree_store.publish_or_queue_page_tree_in(
                    connection, published.document_id, page_tree
                )
                if knowledge_analysis is not None and analysis_provenance_json is not None:
                    self._apply_knowledge_analysis_in(
                        connection,
                        published.document_id,
                        knowledge_analysis,
                        analysis_provenance_json,
                        evidence,
                    )
                    staged_projection = stage_okf_projection_in(connection, self._store.kb_dir)

            try:
                document, deduplicated = self._store.publish_document(
                    state=state,
                    source=state.source,
                    document_id=page_tree.document_version_id,
                    asset_sha256=asset_sha256,
                    raw_path=raw_path,
                    raw_size=len(raw_bytes),
                    source_format=source_format,
                    raw_media_type=source_media_type(source_format),
                    blocks=blocks,
                    evidence=evidence,
                    source_images=source_images,
                    normalized_body_sha256=normalized_body_hash,
                    before_commit=apply_import_derivatives,
                )
            except BaseException:
                if staged_projection is not None:
                    discard_okf_projection_staging(staged_projection)
                raise
            if staged_projection is not None:
                try:
                    activate_okf_projection(self._store.kb_dir, staged_projection)
                except Exception:
                    logger.exception("Could not activate Knowledge Analysis OKF projection.")
                finally:
                    discard_okf_projection_staging(staged_projection)
            terminal_state_committed = True
            self._store.emit_stage(
                state,
                "search",
                "completed",
                100,
                document_id=document.document_id,
            )
            self._record_document_version_candidates(document.document_id, blocks)
            if knowledge_analysis is None:
                self._record_knowledge_reconciliation(document.document_id, blocks)
            return self._result(state.job_id, document.document_id, deduplicated=deduplicated)
        except DuplicateImportSignal as duplicate:
            self._record_existing_knowledge_reconciliation(duplicate.document_id)
            return self._result(state.job_id, duplicate.document_id, deduplicated=True)
        except DesktopModelCancelledError as error:
            if self._control.action == "paused":
                self._honor_control(state, active_stage)
            self._store.pause_job(state, active_stage)
            self._recovery.mark_finished(state, "cancelled")
            raise DesktopImportError(
                "import_interrupted",
                "Import was interrupted while waiting for the model.",
                suggested_action="Continue the import to reuse completed checkpoints.",
            ) from error
        except DesktopModelCallError as error:
            analysis_gate.invalidate_result_failure(error)
            logger.warning(
                "import_model_analysis_quarantined job_id=%s document=%r stage=%s "
                "call_id=%s attempts=%s category=%s",
                state.job_id,
                state.source.name,
                active_stage,
                error.call_id,
                error.attempt_count,
                error.failure.code,
            )
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
            analysis_gate.invalidate_failure(error.code, reason=str(error))
            if error.code in DIRECT_IMPORT_QUARANTINE_CODES:
                self._quarantine.quarantine(
                    job_id=state.job_id,
                    stage_run_id=state.stage_ids[active_stage],
                    stage=active_stage,
                    error_code=error.code,
                    reason=str(error),
                    suggested_action=error.suggested_action
                    or "Convert the document to DOCX or PPTX and import it again.",
                    attempt_count=error.attempt_count,
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

    def _raw_input(
        self, state: ImportJobState, stages: Mapping[str, DesktopStageRun]
    ) -> tuple[bytes, str, str, str, str]:
        source_format = source_format_for_path(state.source)
        raw_suffix = state.source.suffix.lower()
        if stage_completed(stages, "raw_asset"):
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
        preflight_completed = stage_completed(stages, active_stage)
        if not preflight_completed:
            self._honor_control(state, active_stage)
        raw_bytes = state.source.read_bytes()
        if source_format_is_textual(source_format):
            text = decode_text(raw_bytes, state.source)
        else:
            text = ""
        asset_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if not preflight_completed or not matches_preflight_checkpoint(
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
            raise DuplicateImportSignal(duplicate.document_id)
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
            knowledge_analysis=task.knowledge_analysis,
            import_progress=task.import_progress,
            model_usage=task.model_usage,
            model_usage_aggregate=task.model_usage_aggregate,
            model_activity=task.model_activity,
            legacy_model_recovery=task.legacy_model_recovery,
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

    def _record_knowledge_reconciliation(
        self, document_id: str, blocks: tuple[DocumentIRBlock, ...]
    ) -> None:
        """Knowledge conflicts are review work, never an import failure."""
        try:
            self._knowledge_reconciliation.record_document_changes(document_id, blocks)
        except (DesktopImportError, OSError, sqlite3.Error, ValueError) as error:
            logger.warning("Could not reconcile imported knowledge for %s: %s", document_id, error)

    def _apply_knowledge_analysis_in(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        analysis: DesktopKnowledgeAnalysis,
        analysis_provenance_json: str,
        evidence: tuple[tuple[str, DocumentIRBlock], ...],
    ) -> None:
        """Atomically bind canonical Evidence and apply validated structured knowledge."""
        reusable = ReusableKnowledgeAnalysis(analysis, analysis_provenance_json, evidence)
        evidence_id_map = canonical_analysis_evidence_map_in(connection, document_id, reusable)
        changes = analysis.incoming_changes(
            evidence_id_map,
            analysis_provenance_json=analysis_provenance_json,
        )
        self._knowledge_reconciliation.record_analysis_changes_in(connection, document_id, changes)
        record_missing_source_candidates_in(
            connection,
            document_id=canonical_analysis_document_id_in(connection, document_id),
            claims=analysis.missing_source_claims(evidence_id_map),
            evidence=evidence,
            analysis_provenance_json=analysis_provenance_json,
        )

    def _record_existing_knowledge_reconciliation(self, document_id: str) -> None:
        """Replay canonical structured analysis, falling back only for legacy imports."""
        try:
            existing = load_reusable_knowledge_analysis(self._store.database_path, document_id)
            if existing is None:
                self._knowledge_reconciliation.record_existing_document_changes(document_id)
                return
            connection = sqlite3.connect(self._store.database_path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                changes = canonical_analysis_changes_in(connection, document_id, existing)
            finally:
                connection.close()
            self._knowledge_reconciliation.record_analysis_changes(document_id, changes)
        except (DesktopImportError, OSError, sqlite3.Error, ValueError) as error:
            logger.warning(
                "Could not reconcile reused imported knowledge for %s: %s", document_id, error
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
