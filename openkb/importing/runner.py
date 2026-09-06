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
from openkb.config import ensure_preferred_knowledge_language
from openkb.diagnostics import imports as importlog
from openkb.documents.usability import require_usable_document_ir
from openkb.documents.versions import DesktopDocumentVersionService
from openkb.importing.artifacts import (
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
from openkb.importing.checkpoint_validation import (
    matches_preflight_checkpoint,
    stage_completed,
)
from openkb.importing.checkpoints import next_import_stage, require_import_checkpoint
from openkb.importing.control import DesktopImportControl
from openkb.importing.deduplication import DuplicateImportSignal, normalized_body_sha256
from openkb.importing.failures import DIRECT_IMPORT_QUARANTINE_CODES
from openkb.importing.knowledge import apply_import_knowledge_analysis
from openkb.importing.model_call import quarantine_import_model_call
from openkb.importing.model_dispatch import DesktopImportAnalysisDispatcher
from openkb.importing.model_ledger import DesktopImportModelLedger
from openkb.importing.quarantine import DesktopImportQuarantineStore
from openkb.importing.recovery import DesktopImportRecoveryStore
from openkb.importing.store import DesktopImportStore, ImportJobState
from openkb.importing.types import (
    DesktopImportedDocument,
    DesktopImportTask,
    DesktopRecoveryOverride,
    DesktopStageRun,
    DesktopTextImportResult,
)
from openkb.knowledge.analysis.batches import (
    DesktopKnowledgeAnalysisBatchStore,
    run_knowledge_analysis,
)
from openkb.knowledge.analysis.reuse import (
    load_reusable_knowledge_analysis,
)
from openkb.knowledge.analysis.service import (
    DesktopKnowledgeAnalysis,
    knowledge_analysis_from_checkpoint,
    knowledge_analysis_provenance_from_checkpoint,
)
from openkb.knowledge.graph.service import (
    record_graph_extraction_diagnostic,
    start_graph_extraction,
)
from openkb.knowledge.pages.okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
)
from openkb.knowledge.reconciliation.service import DesktopKnowledgeReconciliationService
from openkb.locks import kb_import_activity_lock
from openkb.models.analysis_gate import (
    DesktopAnalysisCapabilityGate,
    DesktopImportAnalysisExecution,
)
from openkb.models.execution_profile import DesktopModelCapacityError
from openkb.models.gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
)
from openkb.models.recovery import DesktopModelRecoveryService
from openkb.page_tree import store as page_tree_store
from openkb.page_tree import tree as page_tree_runtime
from openkb.parsers.document import parse_structured_document
from openkb.parsers.runtime import begin_parser_warmup, require_parser_mode

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
        stage_progress = importlog.ImportStageDiagnostics(on_stage_progress)
        self._store = DesktopImportStore(kb_dir, on_stage_progress=stage_progress)
        self._model_ledger = DesktopImportModelLedger(kb_dir)
        self._document_versions = DesktopDocumentVersionService(kb_dir)
        self._knowledge_reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
        self._knowledge_analysis_batches = DesktopKnowledgeAnalysisBatchStore(kb_dir)
        self._quarantine = DesktopImportQuarantineStore(kb_dir)
        self._recovery = DesktopImportRecoveryStore(kb_dir, on_stage_progress=stage_progress)
        self._control = control or DesktopImportControl()
        self._model_gateway = model_gateway
        self._require_model_analysis = require_model_analysis
        self._parser_mode = require_parser_mode(parser_mode)
        self._analysis_dispatch = DesktopImportAnalysisDispatcher(
            store=self._store,
            ledger=self._model_ledger,
            control=self._control,
        )

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
        if override.parser_mode is not None:
            require_parser_mode(override.parser_mode)
        with kb_import_activity_lock(self._store.state_dir):
            model_recovery = DesktopModelRecoveryService(self._store.kb_dir)
            assessment = model_recovery.assessment(job_id)
            selected_model_recovery = assessment is not None
            if assessment is not None:
                model_recovery.select_required(
                    job_id,
                    assessment,
                    override.legacy_recovery_choice,
                    model_override=override.model,
                    context_capacity=override.context_capacity,
                )
            try:
                state = self._recovery.begin(job_id, override)
                self._analysis_dispatch.begin_recovery(job_id)
                result = self._run(state)
            except DesktopImportError:
                raise
            except (OSError, sqlite3.Error, LockException) as error:
                raise DesktopImportError(
                    "desktop_import_failed", f"Could not recover import {job_id}: {error}"
                ) from error
            finally:
                self._analysis_dispatch.end_recovery()
                if selected_model_recovery:
                    model_recovery.record_resulting_plan(job_id)
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
        self._store.cancel_job(state, next_import_stage(self._store, state))

    def _start_graph_extraction(self, result: DesktopTextImportResult) -> None:
        """Make optional graph work independent from the completed Import Job result."""
        try:
            from openkb.retrieval.catalog_store import start_catalog_rebuilds

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
            # Keep optional graph work independent from the completed import result.
            record_graph_extraction_diagnostic(self._store.kb_dir, result.document.document_id)
            logger.warning("Could not start local knowledge graph extraction.")

    def _run(self, state: ImportJobState) -> DesktopTextImportResult:
        stages = {stage.stage: stage for stage in self._store.stage_runs(state.job_id)}
        active_stage = (
            "raw_asset"
            if stage_completed(stages, "raw_asset")
            else next_import_stage(self._store, state, stages)
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
                        parser_mode=self._recovery.parser_mode(state.job_id, self._parser_mode),
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
                document_ir = require_import_checkpoint(self._store, state, "document_ir")
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
                evidence = evidence_from_checkpoint(
                    require_import_checkpoint(self._store, state, "evidence"), blocks
                )
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
                        document_version_id=page_tree.document_version_id,
                        evidence=evidence,
                        page_tree=page_tree.generation,
                        provider=execution.provider,
                        model=execution.model,
                        engine_version=__version__,
                        analyze=lambda request: self._analysis_dispatch.run(
                            gateway=gateway,
                            state=state,
                            stage=active_stage,
                            request=request,
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
                        on_operation_validated=lambda request: self._analysis_dispatch.mark_ready(
                            gateway, request
                        ),
                        knowledge_language=ensure_preferred_knowledge_language(
                            self._store.kb_dir,
                            (block.text for _evidence_id, block in evidence),
                        ),
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
                analysis_checkpoint = require_import_checkpoint(
                    self._store, state, "model_analysis"
                )
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
                page_tree_store.publish_or_queue_page_tree_in(
                    connection, published.document_id, page_tree
                )

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
            if knowledge_analysis is not None and analysis_provenance_json is not None:
                try:
                    staged_projection = apply_import_knowledge_analysis(
                        self._store.kb_dir,
                        document_id=document.document_id,
                        analysis=knowledge_analysis,
                        analysis_provenance_json=analysis_provenance_json,
                        evidence=evidence,
                    )
                except Exception:
                    logger.exception(
                        "Could not publish derived Knowledge for Available document %s.",
                        document.document_id,
                    )
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
            analysis_gate.suspend_result_failure(self._model_gateway, error)
            importlog.log_model_analysis_quarantine(state, active_stage, error)
            public_code = quarantine_import_model_call(
                ledger=self._model_ledger,
                store=self._store,
                state=state,
                stage=active_stage,
                error=error,
            )
            self._recovery.mark_finished(state, "failed")
            self._store.emit_stage(
                state,
                active_stage,
                "failed",
                100,
                error_code=error.failure.code,
            )
            raise DesktopImportError(
                public_code,
                error.failure.reason,
                failure_event_id=error.failure_event_id,
                diagnostic_context=error.diagnostic_context,
            ) from error
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
                importlog.log_import_failure(state, active_stage, error, outcome="quarantined")
                raise
            if error.code not in _CONTROL_CODES and not terminal_state_committed:
                self._store.fail_job(state, active_stage, error.code)
                self._recovery.mark_failed(state, active_stage, error.code)
                importlog.log_import_failure(state, active_stage, error, outcome="failed")
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
                importlog.log_import_failure(
                    state, active_stage, wrapped, outcome="failed", include_traceback=True
                )
            raise wrapped from error

    def _raw_input(
        self, state: ImportJobState, stages: Mapping[str, DesktopStageRun]
    ) -> tuple[bytes, str, str, str, str]:
        source_format = source_format_for_path(state.source)
        raw_suffix = state.source.suffix.lower()
        if stage_completed(stages, "raw_asset"):
            checkpoint = require_import_checkpoint(self._store, state, "raw_asset")
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
            require_import_checkpoint(self._store, state, active_stage),
            asset_sha256,
            len(raw_bytes),
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

    def _record_existing_knowledge_reconciliation(self, document_id: str) -> None:
        """Replay canonical structured analysis, falling back only for legacy imports."""
        staged_projection: Path | None = None
        try:
            existing = load_reusable_knowledge_analysis(self._store.database_path, document_id)
            if existing is None:
                self._knowledge_reconciliation.record_existing_document_changes(document_id)
                return
            staged_projection = apply_import_knowledge_analysis(
                self._store.kb_dir,
                document_id=document_id,
                analysis=existing.analysis,
                analysis_provenance_json=existing.provenance_json,
                evidence=existing.evidence,
            )
            activate_okf_projection(self._store.kb_dir, staged_projection)
            return
        except (DesktopImportError, OSError, sqlite3.Error, ValueError) as error:
            logger.warning(
                "Could not reconcile reused imported knowledge for %s: %s", document_id, error
            )
        finally:
            if staged_projection is not None:
                discard_okf_projection_staging(staged_projection)

    def _honor_control(self, state: ImportJobState, stage: str) -> None:
        action = self._control.action
        if action == "paused":
            self._store.pause_job(state, stage)
            raise DesktopImportError("import_paused", "Import was paused at a stage checkpoint.")
        if action == "cancelled":
            self._store.cancel_job(state, stage)
            raise DesktopImportError("import_cancelled", "Import cancelled at checkpoint.")
