"""Private stdio runtime for the Desktop Python Engine.

The Engine owns OpenKB application work but never listens on a network port.
Desktop Shell starts this module as a child process and exchanges length-prefixed
JSON-RPC frames over stdio; stdout is reserved for those frames and diagnostics
belong on stderr.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from openkb import __version__
from openkb.desktop_workspace import (
    DesktopKnowledgeBaseError,
    DesktopKnowledgeBaseRuntime,
)
from openkb.workbench_service import (
    DesktopWorkbenchError,
    DesktopWorkbenchService,
    InspectKnowledgeBaseCommand,
)

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024


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
    }
    _ACTIVATION_METHODS = {
        "workbench.create_knowledge_base",
        "workbench.open_knowledge_base",
    }

    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        *,
        service: DesktopWorkbenchService | None = None,
        workspace: DesktopKnowledgeBaseRuntime | None = None,
        engine_version: str | None = None,
    ) -> None:
        self._reader = FrameReader(input_stream)
        self._writer = FrameWriter(output_stream)
        self._service = service or DesktopWorkbenchService()
        self._workspace = workspace or DesktopKnowledgeBaseRuntime()
        self._workspace_requests_lock = threading.Lock()
        self._engine_version = engine_version or __version__
        self._handshake_complete = False
        self._shutdown = threading.Event()
        self._active_requests: dict[str, threading.Event] = {}
        self._activation_in_progress: set[str] = set()
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
                and request.method not in self._ACTIVATION_METHODS
            ):
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )
            self._write_result(request.request_id, result)
            completed_data["ok"] = True
        except DesktopRequestError as error:
            completed_data["error_code"] = error.code
            self._write_error(request.request_id, error.code, str(error))
        except (DesktopWorkbenchError, DesktopKnowledgeBaseError) as error:
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
                    self._activation_in_progress.discard(request_key)
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
                    and target_key not in self._activation_in_progress
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

        if cancel_event is not None and cancel_event.is_set():
            raise DesktopRequestError("request_cancelled", "Desktop Bridge request was cancelled.")

        if request.method in self._WORKSPACE_METHODS:
            return self._dispatch_workspace_request(request, cancel_event)

        if request.method == "workbench.inspect_knowledge_base":
            kb_dir = request.params.get("kb_dir")
            if not isinstance(kb_dir, str) or not kb_dir:
                raise DesktopRequestError(
                    "invalid_params", "workbench.inspect_knowledge_base requires a kb_dir."
                )
            outcome = self._service.execute(InspectKnowledgeBaseCommand(kb_dir=Path(kb_dir)))
            return {
                "snapshot": {
                    "kb_dir": outcome.snapshot.kb_dir,
                    "inventory": outcome.snapshot.inventory.as_dict(),
                    "status": outcome.snapshot.status.as_dict(),
                },
                "events": [
                    {"kind": event.kind, "data": event.data.as_dict()} for event in outcome.events
                ],
            }

        raise DesktopRequestError(
            "method_not_found", f"Unknown Desktop Bridge method: {request.method}"
        )

    def _dispatch_workspace_request(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> dict[str, object]:
        """Serialize workspace binding so cancellation cannot leave the UI stale."""
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
                self._begin_workspace_activation(request, cancel_event)
                return self._workspace.create(Path(kb_dir), name=name).as_dict()

            if request.method == "workbench.open_knowledge_base":
                kb_dir = _required_path_param(request, "kb_dir")
                self._begin_workspace_activation(request, cancel_event)
                return self._workspace.open(Path(kb_dir)).as_dict()

            active = self._workspace.active()
            return {"knowledge_base": active.as_dict() if active is not None else None}

    def _begin_workspace_activation(
        self, request: DesktopRequest, cancel_event: threading.Event | None
    ) -> None:
        """Make cancellation truthful before an activation can mutate the active binding."""
        with self._active_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise DesktopRequestError(
                    "request_cancelled", "Desktop Bridge request was cancelled."
                )
            self._activation_in_progress.add(str(request.request_id))

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
    try:
        DesktopEngineServer(sys.stdin.buffer, sys.stdout.buffer).serve()
    except Exception as error:
        print(f"OpenKB Desktop Engine failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
