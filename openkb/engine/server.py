"""Private stdio runtime for the Desktop Python Engine.

The Engine owns OpenKB application work but never listens on a network port.
Desktop Shell starts this module as a child process and exchanges length-prefixed
JSON-RPC frames over stdio; stdout is reserved for those frames and diagnostics
belong on stderr.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import BinaryIO

from openkb import __version__
from openkb.answers.conversations import DesktopConversationError
from openkb.answers.grounded import DesktopGroundedAnswerService
from openkb.answers.types import DesktopAnswerError
from openkb.diagnostics.engine import (
    EngineRequestDiagnostics,
    initialize_engine_diagnostics,
    log_engine_runtime_failure,
    log_engine_stopped,
)
from openkb.documents.raw_assets import DesktopRawAssetService
from openkb.engine import imports as import_engine
from openkb.engine import methods as engine_methods
from openkb.engine.protocol import (
    PROTOCOL_VERSION,
    DesktopProtocolError,
    DesktopRequest,
    DesktopRequestError,
    FrameReader,
    FrameWriter,
    non_negative_int_param,
    optional_object_param,
    parse_request,
    required_path_list_param,
    required_string_param,
)
from openkb.engine.request_workers import DesktopRequestWorkers
from openkb.importing.service import (
    DesktopImportControl,
    DesktopImportError,
)
from openkb.importing.sources import inspect_import_sources
from openkb.importing.task_snapshots import DesktopImportTaskSnapshots
from openkb.importing.types import DesktopRecoveryOverride
from openkb.knowledge.pages.service import DesktopKnowledgePageError
from openkb.models.gateway import DesktopModelGateway
from openkb.models.transport import desktop_model_gateway_for
from openkb.parsers.legacy_office import shutdown_legacy_office_runtime
from openkb.workspace.runtime import (
    DesktopKnowledgeBaseError,
    DesktopKnowledgeBaseRuntime,
)
from openkb.workspace.transition import DesktopWorkspaceTransitionCoordinator


class DesktopEngineServer:
    """Serve one private Desktop Shell connection until stdin closes or shutdown arrives."""

    _CONTROL_METHODS = engine_methods.CONTROL_METHODS
    _WORKSPACE_METHODS = engine_methods.WORKSPACE_METHODS
    _INTERRUPTION_PRESERVING_METHODS = engine_methods.INTERRUPTION_PRESERVING_METHODS
    _NON_CANCELABLE_MUTATION_METHODS = engine_methods.NON_CANCELABLE_MUTATION_METHODS

    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        *,
        workspace: DesktopKnowledgeBaseRuntime | None = None,
        engine_version: str | None = None,
        model_gateway_factory: (
            Callable[[Path, DesktopRecoveryOverride | None], DesktopModelGateway | None] | None
        ) = None,
    ) -> None:
        self._reader = FrameReader(input_stream)
        self._writer = FrameWriter(output_stream)
        self._workspace = workspace or DesktopKnowledgeBaseRuntime()
        self._workspace_transition = DesktopWorkspaceTransitionCoordinator(self._workspace)
        self._workspace_requests_lock = threading.RLock()
        self._import_task_snapshots = DesktopImportTaskSnapshots()
        self._request_workers = DesktopRequestWorkers()
        self._engine_version = engine_version or __version__
        self._model_gateway_factory = model_gateway_factory or desktop_model_gateway_for
        self._handshake_complete = False
        self._shutdown = threading.Event()
        self._active_requests: dict[str, threading.Event] = {}
        self._non_cancelable_mutations: set[str] = set()
        self._import_controls: dict[str, DesktopImportControl] = {}
        self._active_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        self._knowledge_reanalysis_lease = 0
        self._page_tree_enrichment_lease = 0
        self._page_tree_enrichment_workers: set[Path] = set()
        self._page_tree_enrichment_reruns: set[Path] = set()
        self._page_tree_enrichment_retries: set[Path] = set()
        self._page_tree_enrichment_cancelled: set[tuple[Path, str]] = set()
        self._page_tree_enrichment_gateways: dict[Path, DesktopModelGateway] = {}
        from openkb.engine.corpus_synthesis import CorpusSynthesisWorkers

        self._corpus_synthesis_workers = CorpusSynthesisWorkers(self)
        self._knowledge_graph_extraction_lease = 0
        self._knowledge_graph_extraction_workers: set[Path] = set()
        self._knowledge_graph_extraction_reruns: set[Path] = set()
        self._knowledge_graph_extraction_cancelled: set[tuple[Path, str]] = set()
        self._knowledge_graph_extraction_gateways: dict[Path, DesktopModelGateway] = {}
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    def serve(self) -> None:
        """Consume input frames and return only when the Desktop Runtime ends."""
        while not self._shutdown.is_set():
            try:
                frame = self._reader.read_frame()
            except DesktopProtocolError as error:
                self._write_error(None, error.code, str(error))
                break
            if frame is None:
                break

            try:
                request = parse_request(frame)
            except DesktopRequestError as error:
                self._write_error(frame.get("id"), error.code, str(error))
                continue

            if request.method in self._CONTROL_METHODS:
                self._run_request(request, cancel_event=None)
            else:
                self._start_request(request)

        # EOF also means the owning Shell is gone. Do not let active work keep
        # running simply because it did not send an explicit shutdown request.
        self._shutdown.set()
        with self._active_lock:
            for cancel_event in self._active_requests.values():
                cancel_event.set()
            for control in self._import_controls.values():
                control.request_pause()
        self._join_workers()
        shutdown_legacy_office_runtime()

    def _start_request(self, request: DesktopRequest) -> None:
        _preload_frozen_ocr_for_pdf_import(request)
        request_key = str(request.request_id)
        with self._active_lock:
            if request_key in self._active_requests:
                self._write_error(
                    request.request_id,
                    "duplicate_request_id",
                    f"A Desktop Bridge request is already running with id {request.request_id!r}.",
                )
                return
            cancel_event = threading.Event()
            self._active_requests[request_key] = cancel_event

        self._request_workers.submit(self._run_request, request, cancel_event)

    def _run_request(self, request: DesktopRequest, cancel_event: threading.Event | None) -> None:
        diagnostics = EngineRequestDiagnostics.begin(request.request_id, request.method)
        self._emit_event("engine.request_started", {"request_id": request.request_id})
        completed_data: dict[str, object] = {"request_id": request.request_id, "ok": False}
        try:
            result = self._dispatch(request, cancel_event)
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and request.method not in self._NON_CANCELABLE_MUTATION_METHODS
                and request.method not in self._INTERRUPTION_PRESERVING_METHODS
            ):
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )
            self._write_result(request.request_id, result)
            completed_data["ok"] = True
        except DesktopRequestError as error:
            completed_data["error_code"] = error.code
            diagnostics.typed_failure(error)
            self._write_error(request.request_id, error.code, str(error))
        except (
            DesktopAnswerError,
            DesktopConversationError,
            DesktopKnowledgePageError,
            DesktopKnowledgeBaseError,
            DesktopImportError,
        ) as error:
            completed_data["error_code"] = error.code
            diagnostics.typed_failure(error)
            self._write_error(request.request_id, error.code, str(error))
        except Exception as error:  # Keep unexpected Engine failures behind a stable boundary.
            completed_data["error_code"] = "engine_request_failed"
            diagnostics.unexpected_failure(error)
            self._write_error(request.request_id, "engine_request_failed", str(error))
        finally:
            if completed_data["ok"]:
                diagnostics.completed()
            if cancel_event is not None:
                with self._active_lock:
                    request_key = str(request.request_id)
                    self._active_requests.pop(request_key, None)
                    self._non_cancelable_mutations.discard(request_key)
            self._emit_event("engine.request_completed", completed_data)

    def _dispatch(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> dict[str, object]:
        if request.method == "engine.handshake":
            requested_version = request.params.get("protocol_version")
            if requested_version != PROTOCOL_VERSION:
                raise DesktopRequestError(
                    "protocol_version_incompatible",
                    f"Desktop Bridge requires protocol version {PROTOCOL_VERSION}.",
                )
            self._handshake_complete = True
            return {
                "protocol_version": PROTOCOL_VERSION,
                "engine_version": self._engine_version,
            }

        if request.method == "engine.cancel":
            target = request.params.get("request_id")
            if isinstance(target, bool) or not isinstance(target, (str, int)):
                raise DesktopRequestError("invalid_params", "engine.cancel requires a request_id.")
            with self._active_lock:
                target_key = str(target)
                target_cancel_event = self._active_requests.get(target_key)
                cancelled = (
                    target_cancel_event is not None
                    and target_key not in self._non_cancelable_mutations
                )
            if cancelled and target_cancel_event is not None:
                target_cancel_event.set()
                self._emit_event("engine.request_cancelled", {"request_id": target})
            return {"cancelled": cancelled, "request_id": target}

        if request.method == "engine.shutdown":
            self._shutdown.set()
            with self._active_lock:
                for control in self._import_controls.values():
                    control.request_pause()
            return {"accepted": True}

        if not self._handshake_complete:
            raise DesktopRequestError(
                "handshake_required", "Desktop Bridge handshake must complete before this request."
            )

        if request.method == "engine.health":
            from openkb.parsers.runtime import inspect_parser_readiness

            return {
                "status": "ready",
                "protocol_version": PROTOCOL_VERSION,
                "parser_readiness": {
                    family: readiness.as_dict()
                    for family, readiness in inspect_parser_readiness().items()
                },
            }

        if (
            cancel_event is not None
            and cancel_event.is_set()
            and request.method not in self._INTERRUPTION_PRESERVING_METHODS
        ):
            raise DesktopRequestError("request_cancelled", "Desktop Bridge request was cancelled.")
        if request.method == "workbench.pause_import_job":
            return import_engine.pause_import_job(self, required_string_param(request, "job_id"))
        if request.method == "workbench.cancel_import_job":
            return import_engine.cancel_import_job(self, required_string_param(request, "job_id"))
        if request.method in self._WORKSPACE_METHODS:
            return self._dispatch_workspace_request(request, cancel_event)

        raise DesktopRequestError(
            "method_not_found", f"Unknown Desktop Bridge method: {request.method}"
        )

    def _dispatch_workspace_request(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> dict[str, object]:
        """Keep workspace binding stable while task projections stay readable."""
        if request.method == "workbench.import_jobs":
            active = self._workspace.active()
            if active is None:
                return {"jobs": []}
            return self._import_task_snapshots.read(Path(active.kb_dir))
        if request.method == "workbench.read_raw_document":
            active = self._workspace.active()
            if active is None:
                raise DesktopRequestError(
                    "no_active_knowledge_base",
                    "Open a Desktop Knowledge Base before reading an original document.",
                )
            return (
                DesktopRawAssetService(Path(active.kb_dir))
                .read_document(
                    required_string_param(request, "document_id"),
                    page=non_negative_int_param(request, "page", default=0),
                    focus_locator=optional_object_param(request, "focus_locator"),
                )
                .as_dict()
            )
        if request.method == "workbench.grounded_answers":
            active = self._workspace.active()
            if active is None:
                return {"answers": []}
            answers = DesktopGroundedAnswerService(Path(active.kb_dir)).list()
            return {"answers": [answer.as_dict() for answer in answers]}
        if (
            request.method.startswith("workbench.") and "conversation" in request.method
        ) or request.method == "workbench.select_answer_version":
            from openkb.engine.conversations import dispatch_conversation_request

            return dispatch_conversation_request(self, request, cancel_event)
        if request.method == "workbench.global_search":
            from openkb.engine.search import dispatch_global_search_request

            return dispatch_global_search_request(self, request)
        if request.method in engine_methods.KNOWLEDGE_PAGE_METHODS:
            from openkb.engine.knowledge_pages import dispatch_knowledge_page_request

            return dispatch_knowledge_page_request(self, request, cancel_event)
        if request.method in engine_methods.KNOWLEDGE_REANALYSIS_METHODS:
            from openkb.engine import knowledge_reanalysis as reanalysis

            return reanalysis.dispatch_knowledge_reanalysis_request(self, request, cancel_event)
        if request.method in {
            "workbench.document_version_candidates",
            "workbench.document_version_catalog",
            "workbench.confirm_document_lineage",
            "workbench.document_version_diffs",
            "workbench.resolve_document_version_candidate",
        }:
            from openkb.engine.document_versions import dispatch_document_version_request

            return dispatch_document_version_request(self, request, cancel_event)
        if "knowledge_reconciliation" in request.method:
            from openkb.engine.knowledge_reconciliation import (
                dispatch_knowledge_reconciliation_request,
            )

            return dispatch_knowledge_reconciliation_request(self, request, cancel_event)
        if request.method in engine_methods.ANSWER_METHODS:
            from openkb.engine.answers import dispatch_grounded_answer_request

            return dispatch_grounded_answer_request(self, request, cancel_event)
        if request.method in engine_methods.MODEL_SETTINGS_METHODS:
            from openkb.engine.model_settings import dispatch_model_settings_request

            return dispatch_model_settings_request(self, request, cancel_event)
        if request.method in {
            "workbench.cancel_page_tree_enrichment",
            "workbench.retry_page_tree_enrichment",
        }:
            from openkb.engine.page_tree_enrichment import (
                dispatch_page_tree_enrichment_control,
            )

            return dispatch_page_tree_enrichment_control(self, request)
        if request.method in {
            "workbench.cancel_knowledge_graph_extraction",
            "workbench.retry_knowledge_graph_extraction",
        }:
            from openkb.engine.knowledge_graph import (
                dispatch_knowledge_graph_control,
            )

            return dispatch_knowledge_graph_control(self, request)
        if request.method in {
            "workbench.import_text_document",
            "workbench.resume_import_job",
            "workbench.recover_import_job",
        }:
            return import_engine.dispatch_import_request(self, request, cancel_event)
        if request.method in {
            "workbench.create_knowledge_base",
            "workbench.open_knowledge_base",
        }:
            from openkb.engine.workspace_activation import (
                dispatch_knowledge_base_activation,
            )

            return dispatch_knowledge_base_activation(self, request, cancel_event)
        with self._workspace_requests_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )

            if request.method == "workbench.inspect_import_sources":
                return inspect_import_sources(
                    Path(path) for path in required_path_list_param(request, "source_paths")
                ).as_dict()
            active = self._workspace.active()
            return {"knowledge_base": active.as_dict() if active is not None else None}

    def _begin_workspace_mutation(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> None:
        """Make cancellation truthful before a workspace mutation can commit."""
        with self._active_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )
            self._non_cancelable_mutations.add(str(request.request_id))

    def _emit_event(self, kind: str, data: dict[str, object]) -> None:
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        self._writer.write_frame(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"sequence": sequence, "kind": kind, "data": data},
            }
        )

    def _write_result(self, request_id: str | int, result: dict[str, object]) -> None:
        self._writer.write_frame({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _write_error(self, request_id: object, code: str, message: str) -> None:
        self._writer.write_frame(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _join_workers(self) -> None:
        self._request_workers.close()
        while True:
            with self._workers_lock:
                workers = tuple(self._workers)
            if not workers:
                return
            for worker in workers:
                worker.join(timeout=1)


def main() -> int:
    """Run the packaged Engine child process."""
    if sys.argv[1:]:
        from openkb.evaluation.pageindex_acceptance import run_cli

        return run_cli(sys.argv[1:])
    if not initialize_engine_diagnostics(app_version=__version__):
        print("OPENKB_LOGGING_UNAVAILABLE", file=sys.stderr, flush=True)
    try:
        DesktopEngineServer(sys.stdin.buffer, sys.stdout.buffer).serve()
    except Exception as error:
        failure_event_id = log_engine_runtime_failure(error)
        print(
            f"OPENKB_ENGINE_RUNTIME_FAILED failure_event_id={failure_event_id}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    log_engine_stopped()
    return 0


def _preload_frozen_ocr_dependencies() -> None:
    """Load native OCR imports on the Engine main thread only when an import needs them."""
    if getattr(sys, "_MEIPASS", None) is None:
        return
    try:
        import_module("rapidocr_onnxruntime")
    except ImportError:
        return


def _preload_frozen_ocr_for_pdf_import(request: DesktopRequest) -> None:
    """Defer frozen OCR imports until the first PDF import, preserving safe native startup."""
    if request.method != "workbench.import_text_document":
        return
    source_path = request.params.get("source_path")
    if isinstance(source_path, str) and Path(source_path).suffix.lower() == ".pdf":
        _preload_frozen_ocr_dependencies()


if __name__ == "__main__":
    raise SystemExit(main())
