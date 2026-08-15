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
from openkb.desktop_answer_types import DesktopAnswerError
from openkb.desktop_grounded_answer import DesktopGroundedAnswerService
from openkb.desktop_import import (
    DesktopImportControl,
    DesktopImportError,
    DesktopTextImportService,
)
from openkb.desktop_import_sources import inspect_import_sources
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_knowledge_generations import materialize_current_generation
from openkb.desktop_knowledge_pages import DesktopKnowledgePageError, DesktopKnowledgePageService
from openkb.desktop_legacy_office_parsers import shutdown_legacy_office_runtime
from openkb.desktop_model_gateway import MODEL_CALL_DEADLINE_SECONDS, DesktopModelGateway
from openkb.desktop_model_transport import desktop_model_gateway_for
from openkb.desktop_raw_assets import DesktopRawAssetService
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseError,
    DesktopKnowledgeBaseRuntime,
)

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

    _CONTROL_METHODS = {
        "engine.handshake",
        "engine.health",
        "engine.cancel",
        "engine.shutdown",
    }
    _WORKSPACE_METHODS = {
        "workbench.create_knowledge_base",
        "workbench.open_knowledge_base",
        "workbench.active_knowledge_base",
        "workbench.inspect_import_sources",
        "workbench.import_text_document",
        "workbench.resume_import_job",
        "workbench.recover_import_job",
        "workbench.import_jobs",
        "workbench.read_raw_document",
        "workbench.ask_grounded",
        "workbench.retry_interrupted_answer",
        "workbench.grounded_answers",
        "workbench.knowledge_pages",
        "workbench.knowledge_page",
        "workbench.save_knowledge_page",
        "workbench.document_version_candidates",
        "workbench.resolve_document_version_candidate",
        "workbench.knowledge_reconciliation_conflicts",
        "workbench.stage_knowledge_reconciliation_decisions",
        "workbench.commit_knowledge_reconciliation_decisions",
        "workbench.model_settings",
        "workbench.save_model_settings",
        "workbench.export_diagnostic_bundle",
    }
    _INTERRUPTION_PRESERVING_METHODS = {
        "workbench.ask_grounded",
        "workbench.retry_interrupted_answer",
    }
    _NON_CANCELABLE_MUTATION_METHODS = {
        "workbench.create_knowledge_base",
        "workbench.open_knowledge_base",
        "workbench.import_text_document",
        "workbench.read_raw_document",
        "workbench.save_knowledge_page",
        "workbench.resolve_document_version_candidate",
        "workbench.stage_knowledge_reconciliation_decisions",
        "workbench.commit_knowledge_reconciliation_decisions",
        "workbench.save_model_settings",
        "workbench.export_diagnostic_bundle",
    }

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
        self._workspace_requests_lock = threading.Lock()
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
        with self._active_lock:
            for cancel_event in self._active_requests.values():
                cancel_event.set()
        self._join_workers()
        shutdown_legacy_office_runtime()

    def _start_request(self, request: DesktopRequest) -> None:
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
            self._write_error(request.request_id, error.code, str(error))
        except (
            DesktopAnswerError,
            DesktopKnowledgePageError,
            DesktopKnowledgeBaseError,
            DesktopImportError,
        ) as error:
            completed_data["error_code"] = error.code
            self._write_error(request.request_id, error.code, str(error))
        except Exception as error:  # Keep unexpected Engine failures behind a stable boundary.
            completed_data["error_code"] = "engine_request_failed"
            self._write_error(request.request_id, "engine_request_failed", str(error))
        finally:
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
            return {"accepted": True}

        if not self._handshake_complete:
            raise DesktopRequestError(
                "handshake_required", "Desktop Bridge handshake must complete before this request."
            )

        if request.method == "engine.health":
            return {"status": "ready", "protocol_version": PROTOCOL_VERSION}

        if (
            cancel_event is not None
            and cancel_event.is_set()
            and request.method not in self._INTERRUPTION_PRESERVING_METHODS
        ):
            raise DesktopRequestError("request_cancelled", "Desktop Bridge request was cancelled.")
        if request.method == "workbench.pause_import_job":
            return self._pause_import_job(_required_string_param(request, "job_id"))
        if request.method == "workbench.cancel_import_job":
            return self._cancel_import_job(_required_string_param(request, "job_id"))
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
        if request.method in {
            "workbench.knowledge_pages",
            "workbench.knowledge_page",
            "workbench.save_knowledge_page",
        }:
            from openkb.desktop_engine_knowledge_pages import dispatch_knowledge_page_request

            return dispatch_knowledge_page_request(self, request, cancel_event)
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
            return self._dispatch_grounded_answer_request(request, cancel_event)
        if request.method in {
            "workbench.model_settings",
            "workbench.save_model_settings",
            "workbench.export_diagnostic_bundle",
        }:
            from openkb.desktop_engine_model_settings import dispatch_model_settings_request

            return dispatch_model_settings_request(self, request, cancel_event)
        if request.method in {
            "workbench.import_text_document",
            "workbench.resume_import_job",
            "workbench.recover_import_job",
        }:
            return self._dispatch_import_request(request, cancel_event)
        with self._workspace_requests_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )

            if request.method == "workbench.create_knowledge_base":
                kb_dir = _required_path_param(request, "kb_dir")
                name = request.params.get("name")
                if name is not None and not isinstance(name, str):
                    raise DesktopRequestError(
                        "invalid_params", "workbench.create_knowledge_base name must be a string."
                    )
                self._begin_workspace_mutation(request, cancel_event)
                return self._workspace.create(Path(kb_dir), name=name).as_dict()

            if request.method == "workbench.open_knowledge_base":
                kb_dir = _required_path_param(request, "kb_dir")
                self._begin_workspace_mutation(request, cancel_event)
                activation = self._workspace.open(Path(kb_dir))
                active_kb_dir = Path(activation.knowledge_base.kb_dir)
                DesktopRawAssetService(active_kb_dir).verify_available_documents()
                DesktopKnowledgePageService(active_kb_dir).materialize_current_pages()
                materialize_current_generation(active_kb_dir)
                self._start_recoverable_imports(active_kb_dir)
                return activation.as_dict()

            if request.method == "workbench.inspect_import_sources":
                return inspect_import_sources(
                    Path(path) for path in _required_path_list_param(request, "source_paths")
                ).as_dict()
            active = self._workspace.active()
            return {"knowledge_base": active.as_dict() if active is not None else None}

    def _dispatch_grounded_answer_request(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> dict[str, object]:
        from openkb.desktop_engine_answers import dispatch_grounded_answer_request

        return dispatch_grounded_answer_request(self, request, cancel_event)

    def _dispatch_import_request(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> dict[str, object]:
        """Run one Import Job without allowing the active KB binding to change."""
        with self._workspace_requests_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )
            active = self._workspace.active()
            if active is None:
                raise DesktopRequestError(
                    "no_active_knowledge_base",
                    "Open a Desktop Knowledge Base before importing a document.",
                )
            self._begin_workspace_mutation(request, cancel_event)
            kb_dir = Path(active.kb_dir)

            if request.method == "workbench.import_text_document":
                return self._run_import(
                    kb_dir,
                    request_id=str(request.request_id),
                    source_path=Path(_required_path_param(request, "source_path")),
                )
            if request.method == "workbench.resume_import_job":
                return self._run_import(
                    kb_dir,
                    request_id=str(request.request_id),
                    job_id=_required_string_param(request, "job_id"),
                )
            return self._run_import(
                kb_dir,
                request_id=str(request.request_id),
                job_id=_required_string_param(request, "job_id"),
                recovery_override=_recovery_override_param(request),
            )

    def _run_import(
        self,
        kb_dir: Path,
        *,
        request_id: str | None,
        source_path: Path | None = None,
        job_id: str | None = None,
        recovery_override: DesktopRecoveryOverride | None = None,
    ) -> dict[str, object]:
        """Run one job while its durable state, not this worker, remains authoritative."""
        control = DesktopImportControl()
        model_gateway = self._model_gateway_factory(kb_dir, recovery_override)
        importer = DesktopTextImportService(
            kb_dir,
            control=control,
            on_stage_progress=lambda data: self._record_import_stage(request_id, control, data),
            model_gateway=model_gateway,
        )
        try:
            if source_path is not None:
                return importer.import_text(source_path).as_dict()
            if job_id is not None:
                if recovery_override is not None:
                    return importer.recover_text(job_id, recovery_override).as_dict()
                return importer.resume_text(job_id).as_dict()
            raise DesktopRequestError("invalid_params", "An import source or job is required.")
        finally:
            self._release_import_control(control)

    def _record_import_stage(
        self,
        request_id: str | None,
        control: DesktopImportControl,
        data: dict[str, object],
    ) -> None:
        job_id = data.get("job_id")
        if isinstance(job_id, str):
            with self._active_lock:
                self._import_controls[job_id] = control
        self._emit_event("import.stage_progress", {"request_id": request_id, **data})

    def _release_import_control(self, control: DesktopImportControl) -> None:
        with self._active_lock:
            for job_id, active_control in tuple(self._import_controls.items()):
                if active_control is control:
                    del self._import_controls[job_id]

    def _pause_import_job(self, job_id: str) -> dict[str, object]:
        with self._active_lock:
            control = self._import_controls.get(job_id)
        if control is None:
            raise DesktopRequestError(
                "import_job_not_running", "Import job is not running in this Desktop Runtime."
            )
        control.request_pause()
        return {"job_id": job_id, "accepted": True}

    def _cancel_import_job(self, job_id: str) -> dict[str, object]:
        with self._active_lock:
            control = self._import_controls.get(job_id)
        if control is not None:
            control.request_cancel()
            return {"job_id": job_id, "accepted": True}

        active = self._workspace.active()
        if active is None:
            raise DesktopRequestError(
                "no_active_knowledge_base", "Open a Desktop Knowledge Base before cancelling."
            )
        DesktopTextImportService(Path(active.kb_dir)).cancel_paused_job(job_id)
        return {"job_id": job_id, "accepted": True}

    def _start_recoverable_imports(self, kb_dir: Path) -> None:
        job_ids = DesktopTextImportService(kb_dir).recoverable_job_ids()
        if not job_ids:
            return
        threading.Thread(
            target=self._resume_recoverable_imports,
            args=(kb_dir, job_ids),
            daemon=True,
            name="openkb-engine-import-recovery",
        ).start()

    def _resume_recoverable_imports(self, kb_dir: Path, job_ids: tuple[str, ...]) -> None:
        for job_id in job_ids:
            if self._shutdown.is_set():
                return
            try:
                self._run_import(kb_dir, request_id=None, job_id=job_id)
            except (DesktopImportError, DesktopRequestError) as error:
                logger.warning("Could not resume Desktop import job %s: %s", job_id, error)

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
    timeout_value = (
        value["initial_timeout_seconds"]
        if "initial_timeout_seconds" in value
        else value.get("initialTimeoutSeconds")
    )
    if timeout_value is not None and (
        isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float))
    ):
        raise DesktopRequestError(
            "invalid_params", "Recovery response timeout must be a number of seconds."
        )
    timeout = float(timeout_value) if timeout_value is not None else None
    if timeout is not None and not 0 < timeout <= MODEL_CALL_DEADLINE_SECONDS:
        raise DesktopRequestError(
            "invalid_params", "Recovery response timeout must be between 1 and 60 seconds."
        )
    return DesktopRecoveryOverride(
        model=model_value.strip() if isinstance(model_value, str) else None,
        initial_timeout_seconds=timeout,
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
    _preload_frozen_ocr_dependencies()
    try:
        DesktopEngineServer(sys.stdin.buffer, sys.stdout.buffer).serve()
    except Exception as error:
        print(f"OpenKB Desktop Engine failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


def _preload_frozen_ocr_dependencies() -> None:
    """Load native OCR imports on the Engine main thread before worker dispatch."""
    if getattr(sys, "_MEIPASS", None) is None:
        return
    try:
        import_module("rapidocr_onnxruntime")
    except ImportError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
