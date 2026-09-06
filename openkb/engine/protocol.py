"""Private Bridge framing and request validation, independent of Engine dispatch."""

from __future__ import annotations

import json
import struct
import threading
from dataclasses import dataclass
from typing import BinaryIO

from openkb.importing.types import DesktopRecoveryOverride

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


def required_path_list_param(request: DesktopRequest, key: str) -> tuple[str, ...]:
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


def required_string_param(request: DesktopRequest, key: str) -> str:
    value = request.params.get(key)
    if not isinstance(value, str) or not value:
        raise DesktopRequestError("invalid_params", f"{request.method} requires a non-empty {key}.")
    return value


def non_negative_int_param(request: DesktopRequest, key: str, *, default: int) -> int:
    value = request.params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} requires a non-negative integer {key}."
        )
    return value


def optional_object_param(request: DesktopRequest, key: str) -> dict[str, object] | None:
    """Return an optional non-empty object parameter for stable source focus."""
    value = request.params.get(key)
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise DesktopRequestError(
            "invalid_params", f"{request.method} {key} must be a non-empty object."
        )
    return dict(value)


def recovery_override_param(request: DesktopRequest) -> DesktopRecoveryOverride:
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
    reasoning_value = value.get("reasoning")
    if reasoning_value is not None and reasoning_value not in {
        "off",
        "low",
        "medium",
        "high",
    }:
        raise DesktopRequestError(
            "invalid_params", "Choose a supported one-time Recovery reasoning level."
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
    check_and_recover = value.get(
        "check_and_recover",
        value.get("checkAndRecover", False),
    )
    if type(check_and_recover) is not bool:
        raise DesktopRequestError("invalid_params", "check_and_recover must be a boolean.")
    parser_mode = value.get("parser_mode", value.get("parserMode"))
    if parser_mode is not None and (
        not isinstance(parser_mode, str) or parser_mode not in {"auto", "fast", "enhanced"}
    ):
        raise DesktopRequestError("invalid_params", "parser_mode must be auto, fast, or enhanced.")
    return DesktopRecoveryOverride(
        model=model_value.strip() if isinstance(model_value, str) else None,
        context_capacity=context_value,
        reasoning=reasoning_value,
        legacy_recovery_choice=choice_value,
        check_and_recover=check_and_recover,
        parser_mode=parser_mode,
    )


def parse_request(frame: dict[str, object]) -> DesktopRequest:
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
