"""Private stdio runtime for the Desktop Python Engine.

The Engine owns OpenKB application work but never listens on a network port.
Desktop Shell starts this module as a child process and exchanges length-prefixed
JSON-RPC frames over stdio; stdout is reserved for those frames and diagnostics
belong on stderr.
"""

from __future__ import annotations

import json
import logging
import struct
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import BinaryIO

from openkb import __version__
from openkb import desktop_engine_imports as import_engine
from openkb import desktop_engine_methods as engine_methods
from openkb.desktop_answer_types import DesktopAnswerError
from openkb.desktop_conversations import DesktopConversationError
from openkb.desktop_grounded_answer import DesktopGroundedAnswerService
from openkb.desktop_import import (
    DesktopImportControl,
    DesktopImportError,
    DesktopTextImportService,
)
from openkb.desktop_import_sources import inspect_import_sources
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_knowledge_pages import DesktopKnowledgePageError
from openkb.desktop_legacy_office_parsers import shutdown_legacy_office_runtime
from openkb.desktop_logging import configure_desktop_engine_logging
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_transport import desktop_model_gateway_for
from openkb.desktop_raw_assets import DesktopRawAssetService
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseError,
    DesktopKnowledgeBaseRuntime,
)
from openkb.desktop_workspace_transition import DesktopWorkspaceTransitionCoordinator

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


class DesktopProtocolError(ValueError):
    """A malformed private-protocol frame with a stable bridge error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DesktopRequestError(RuntimeError):
    """A typed application error returned to Desktop Shell."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FrameReader:
    """Read one length-prefixed JSON object at a time from a binary stream."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def read_frame(self) -> dict[str, object] | None:
        """Read a frame, allowing clean EOF only before a new frame begins."""
        prefix = self._read_exact(4, allow_clean_eof=True)
        if prefix is None:
            return None
        size = struct.unpack(">I", prefix)[0]
        if size > MAX_FRAME_BYTES:
            raise DesktopProtocolError(
                "frame_too_large", f"Desktop Bridge frame exceeds {MAX_FRAME_BYTES} bytes."
            )
        payload = self._read_exact(size)
        assert payload is not None
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopProtocolError(
                "invalid_frame", f"Invalid Desktop Bridge JSON frame: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise DesktopProtocolError(
                "invalid_frame", "Desktop Bridge frame must contain an object."
            )
        return dict(decoded)

    def _read_exact(self, size: int, *, allow_clean_eof: bool = False) -> bytes | None:
        buffer = bytearray()
        while len(buffer) < size:
            chunk = self._stream.read(size - len(buffer))
            if not chunk:
                if allow_clean_eof and not buffer:
                    return None
                raise DesktopProtocolError(
                    "truncated_frame", "Desktop Bridge frame ended unexpectedly."
                )
            buffer.extend(chunk)
        return bytes(buffer)


class FrameWriter:
    """Serialize concurrent Engine responses and events onto stdout safely."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write_frame(self, payload: dict[str, object]) -> None:
        with self._lock:
            self._stream.write(encode_frame(payload))
            self._stream.flush()


def encode_frame(payload: dict[str, object]) -> bytes:
    """Encode one private-protocol object with its big-endian byte length."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise DesktopProtocolError(
            "frame_too_large", f"Desktop Bridge frame exceeds {MAX_FRAME_BYTES} bytes."
        )
    return struct.pack(">I", len(body)) + body


@dataclass(frozen=True)
class DesktopRequest:
    """A validated JSON-RPC request sent by Desktop Shell."""

    request_id: str | int
    method: str
    params: dict[str, object]


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
                request = _parse_request(frame)
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

        worker = threading.Thread(
            target=self._run_request,
            args=(request, cancel_event),
            daemon=True,
            name=f"openkb-engine-{request_key}",
        )
        with self._workers_lock:
            self._workers.add(worker)
        worker.start()

    def _run_request(self, request: DesktopRequest, cancel_event: threading.Event | None) -> None:
        logger.info(
            "engine_request_started request_id=%s method=%s",
            request.request_id,
            request.method,
        )
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
            logger.warning(
                "engine_request_failed request_id=%s method=%s error_code=%s detail=%r",
                request.request_id,
                request.method,
                error.code,
                str(error),
            )
            self._write_error(request.request_id, error.code, str(error))
        except (
            DesktopAnswerError,
            DesktopConversationError,
            DesktopKnowledgePageError,
            DesktopKnowledgeBaseError,
            DesktopImportError,
        ) as error:
            completed_data["error_code"] = error.code
            logger.warning(
                "engine_request_failed request_id=%s method=%s error_code=%s detail=%r",
                request.request_id,
                request.method,
                error.code,
                str(error),
            )
            self._write_error(request.request_id, error.code, str(error))
        except Exception as error:  # Keep unexpected Engine failures behind a stable boundary.
            completed_data["error_code"] = "engine_request_failed"
            logger.exception(
                "engine_request_failed request_id=%s method=%s error_code=engine_request_failed",
                request.request_id,
                request.method,
            )
            self._write_error(request.request_id, "engine_request_failed", str(error))
        finally:
            logger.info(
                "engine_request_completed request_id=%s method=%s ok=%s error_code=%s",
                request.request_id,
                request.method,
                completed_data["ok"],
                completed_data.get("error_code"),
            )
            if cancel_event is not None:
                with self._active_lock:
                    request_key = str(request.request_id)
                    self._active_requests.pop(request_key, None)
                    self._non_cancelable_mutations.discard(request_key)
            self._emit_event("engine.request_completed", completed_data)
            current = threading.current_thread()
            with self._workers_lock:
                self._workers.discard(current)

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
            from openkb.desktop_parser_runtime import inspect_parser_readiness

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
            return import_engine.pause_import_job(self, _required_string_param(request, "job_id"))
        if request.method == "workbench.cancel_import_job":
            return import_engine.cancel_import_job(self, _required_string_param(request, "job_id"))
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
            return DesktopTextImportService(Path(active.kb_dir)).list_import_jobs()
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
                    _required_string_param(request, "document_id"),
                    page=_non_negative_int_param(request, "page", default=0),
                    focus_locator=_optional_object_param(request, "focus_locator"),
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
            from openkb.desktop_engine_conversations import dispatch_conversation_request

            return dispatch_conversation_request(self, request, cancel_event)
        if request.method == "workbench.global_search":
            from openkb.desktop_engine_search import dispatch_global_search_request

            return dispatch_global_search_request(self, request)
        if request.method in engine_methods.KNOWLEDGE_PAGE_METHODS:
            from openkb.desktop_engine_knowledge_pages import dispatch_knowledge_page_request

            return dispatch_knowledge_page_request(self, request, cancel_event)
        if request.method in engine_methods.KNOWLEDGE_REANALYSIS_METHODS:
            from openkb import desktop_engine_knowledge_reanalysis as reanalysis

            return reanalysis.dispatch_knowledge_reanalysis_request(self, request, cancel_event)
        if request.method in {
            "workbench.document_version_candidates",
            "workbench.resolve_document_version_candidate",
        }:
            from openkb.desktop_engine_document_versions import dispatch_document_version_request

            return dispatch_document_version_request(self, request, cancel_event)
        if "knowledge_reconciliation" in request.method:
            from openkb.desktop_engine_knowledge_reconciliation import (
                dispatch_knowledge_reconciliation_request,
            )

            return dispatch_knowledge_reconciliation_request(self, request, cancel_event)
        if request.method in self._INTERRUPTION_PRESERVING_METHODS:
            from openkb.desktop_engine_answers import dispatch_grounded_answer_request

            return dispatch_grounded_answer_request(self, request, cancel_event)
        if request.method in engine_methods.MODEL_SETTINGS_METHODS:
            from openkb.desktop_engine_model_settings import dispatch_model_settings_request

            return dispatch_model_settings_request(self, request, cancel_event)
        if request.method in {
            "workbench.cancel_page_tree_enrichment",
            "workbench.retry_page_tree_enrichment",
        }:
            from openkb.desktop_engine_page_tree_enrichment import (
                dispatch_page_tree_enrichment_control,
            )

            return dispatch_page_tree_enrichment_control(self, request)
        if request.method in {
            "workbench.cancel_knowledge_graph_extraction",
            "workbench.retry_knowledge_graph_extraction",
        }:
            from openkb.desktop_engine_knowledge_graph import (
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
            from openkb.desktop_engine_workspace_activation import (
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
                    Path(path) for path in _required_path_list_param(request, "source_paths")
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
        while True:
            with self._workers_lock:
                workers = tuple(self._workers)
            if not workers:
                return
            for worker in workers:
                worker.join(timeout=1)


def _required_path_param(request: DesktopRequest, key: str) -> str:
    value = request.params.get(key)
    if not isinstance(value, str) or not value:
        raise DesktopRequestError("invalid_params", f"{request.method} requires a non-empty {key}.")
    return value


def _required_path_list_param(request: DesktopRequest, key: str) -> tuple[str, ...]:
    value = request.params.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(path, str) or not path for path in value)
    ):
        raise DesktopRequestError(
            "invalid_params", f"{request.method} requires a non-empty list of {key}."
        )
    return tuple(value)


def _required_string_param(request: DesktopRequest, key: str) -> str:
    value = request.params.get(key)
    if not isinstance(value, str) or not value:
        raise DesktopRequestError("invalid_params", f"{request.method} requires a non-empty {key}.")
    return value


def _non_negative_int_param(request: DesktopRequest, key: str, *, default: int) -> int:
    value = request.params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} requires a non-negative integer {key}."
        )
    return value


def _optional_object_param(request: DesktopRequest, key: str) -> dict[str, object] | None:
    """Return an optional non-empty object parameter for stable source focus."""
    value = request.params.get(key)
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} {key} must be a non-empty object."
        )
    return dict(value)


def _recovery_override_param(request: DesktopRequest) -> DesktopRecoveryOverride:
    value = request.params.get("recovery_override", {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DesktopRequestError(
            "invalid_params", "workbench.recover_import_job recovery_override must be an object."
        )
    model_value = value.get("model")
    if model_value is not None and (not isinstance(model_value, str) or not model_value.strip()):
        raise DesktopRequestError(
            "invalid_params", "Recovery model must be a non-empty string when provided."
        )
    context_value = value.get("context_capacity", value.get("contextCapacity"))
    if context_value is not None and (
        isinstance(context_value, bool)
        or not isinstance(context_value, int)
        or context_value < 4_096
    ):
        raise DesktopRequestError(
            "invalid_params", "Recovery context capacity must be at least 4096 tokens."
        )
    choice_value = value.get(
        "legacy_recovery_choice",
        value.get("legacyRecoveryChoice"),
    )
    if choice_value is not None and choice_value not in {
        "continue_compatible",
        "restart_current_plan",
    }:
        raise DesktopRequestError("invalid_params", "Choose a supported legacy recovery path.")
    return DesktopRecoveryOverride(
        model=model_value.strip() if isinstance(model_value, str) else None,
        context_capacity=context_value,
        legacy_recovery_choice=choice_value,
    )


def _parse_request(frame: dict[str, object]) -> DesktopRequest:
    request_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params", {})
    if frame.get("jsonrpc") != "2.0":
        raise DesktopRequestError(
            "invalid_request", "Desktop Bridge requests must use JSON-RPC 2.0."
        )
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise DesktopRequestError(
            "invalid_request", "Desktop Bridge request id must be a string or integer."
        )
    if not isinstance(method, str) or not method:
        raise DesktopRequestError(
            "invalid_request", "Desktop Bridge request method must be a string."
        )
    if not isinstance(params, dict):
        raise DesktopRequestError(
            "invalid_params", "Desktop Bridge request params must be an object."
        )
    return DesktopRequest(request_id=request_id, method=method, params=dict(params))


def main() -> int:
    """Run the packaged Engine child process."""
    if sys.argv[1:]:
        from openkb.desktop_pageindex_acceptance import run_cli

        return run_cli(sys.argv[1:])
    configure_desktop_engine_logging()
    logger.info("OpenKB Desktop Engine started.")
    try:
        DesktopEngineServer(sys.stdin.buffer, sys.stdout.buffer).serve()
    except Exception as error:
        logger.exception("OpenKB Desktop Engine failed.")
        print(f"OpenKB Desktop Engine failed: {error}", file=sys.stderr, flush=True)
        return 1
    logger.info("OpenKB Desktop Engine stopped.")
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
