"""Protocol behavior tests for the packaged Desktop Python Engine."""

from __future__ import annotations

import io
import struct
import threading
import time

import pytest

from openkb.desktop_engine import (
    DesktopEngineServer,
    DesktopProtocolError,
    FrameReader,
    encode_frame,
)
from openkb.workbench_service import DesktopWorkbenchService


class FragmentedBytesIO(io.BytesIO):
    """A stream that returns short reads to model fragmented stdio frames."""

    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 3) if size >= 0 else 3)


class WaitForResponseBytesIO(FragmentedBytesIO):
    """Keep the simulated Shell connected until its asynchronous command replies."""

    def __init__(self, payload: bytes, response_written: threading.Event) -> None:
        super().__init__(payload)
        self._response_written = response_written

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        if not chunk:
            assert self._response_written.wait(timeout=1)
        return chunk


class InspectResponseOutput(io.BytesIO):
    """Signal the input stream once the asynchronous inspection has completed."""

    def __init__(self, response_written: threading.Event) -> None:
        super().__init__()
        self._response_written = response_written

    def write(self, payload: bytes) -> int:
        size = super().write(payload)
        if b'"id":"inspect"' in payload:
            self._response_written.set()
        return size


def _decode_frames(payload: bytes) -> list[dict[str, object]]:
    reader = FrameReader(io.BytesIO(payload))
    frames: list[dict[str, object]] = []
    while (frame := reader.read_frame()) is not None:
        frames.append(frame)
    return frames


def test_frame_reader_handles_fragmented_and_concatenated_frames():
    """The private protocol survives both short reads and multiple frames at once."""
    payload = encode_frame({"id": "first"}) + encode_frame({"id": "second"})
    reader = FrameReader(FragmentedBytesIO(payload))

    assert reader.read_frame() == {"id": "first"}
    assert reader.read_frame() == {"id": "second"}
    assert reader.read_frame() is None

    with pytest.raises(DesktopProtocolError, match="must contain an object"):
        FrameReader(io.BytesIO(struct.pack(">I", 2) + b"[]")).read_frame()


def test_engine_reports_handshake_health_events_command_and_cancel_round_trip(kb_dir):
    """Desktop Shell can establish a ready Engine and use the typed control path."""
    incoming = b"".join(
        (
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "handshake",
                    "method": "engine.handshake",
                    "params": {"protocol_version": 1},
                }
            ),
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "health",
                    "method": "engine.health",
                    "params": {},
                }
            ),
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "inspect",
                    "method": "workbench.inspect_knowledge_base",
                    "params": {"kb_dir": str(kb_dir)},
                }
            ),
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "cancel",
                    "method": "engine.cancel",
                    "params": {"request_id": "not-running"},
                }
            ),
        )
    )
    inspection_complete = threading.Event()
    output = InspectResponseOutput(inspection_complete)

    DesktopEngineServer(
        WaitForResponseBytesIO(incoming, inspection_complete), output, engine_version="test"
    ).serve()

    frames = _decode_frames(output.getvalue())
    responses = {frame["id"]: frame for frame in frames if "id" in frame}
    assert responses["handshake"]["result"] == {"protocol_version": 1, "engine_version": "test"}
    assert responses["health"]["result"] == {"status": "ready", "protocol_version": 1}
    assert responses["inspect"]["result"] == {
        "snapshot": {
            "kb_dir": str(kb_dir),
            "inventory": {
                "documents": [],
                "document_count": 0,
                "summaries": [],
                "concepts": [],
                "entities": [],
                "reports": [],
            },
            "status": {
                "directories": {"sources": 0, "summaries": 0, "concepts": 0, "reports": 0},
                "raw_count": 0,
                "total_indexed": 0,
                "last_compile": None,
                "last_lint": None,
            },
        },
        "events": [
            {
                "kind": "knowledge_base.inspected",
                "data": {"kb_dir": str(kb_dir), "document_count": 0},
            }
        ],
    }
    assert responses["cancel"]["result"] == {"cancelled": False, "request_id": "not-running"}
    assert any(
        frame.get("method") == "event"
        and isinstance(frame.get("params"), dict)
        and frame["params"].get("kind") == "engine.request_started"
        for frame in frames
    )


def test_engine_cancels_an_active_caller_owned_request(kb_dir):
    """Cancellation targets the same request ID that crossed the Desktop Bridge."""

    class SlowWorkbenchService(DesktopWorkbenchService):
        def execute(self, command):
            time.sleep(0.02)
            return super().execute(command)

    incoming = b"".join(
        (
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "handshake",
                    "method": "engine.handshake",
                    "params": {"protocol_version": 1},
                }
            ),
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "workbench-request",
                    "method": "workbench.inspect_knowledge_base",
                    "params": {"kb_dir": str(kb_dir)},
                }
            ),
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": "cancel",
                    "method": "engine.cancel",
                    "params": {"request_id": "workbench-request"},
                }
            ),
        )
    )
    output = io.BytesIO()

    DesktopEngineServer(
        FragmentedBytesIO(incoming), output, service=SlowWorkbenchService(), engine_version="test"
    ).serve()

    responses = {frame["id"]: frame for frame in _decode_frames(output.getvalue()) if "id" in frame}
    assert responses["cancel"]["result"] == {
        "cancelled": True,
        "request_id": "workbench-request",
    }
    assert responses["workbench-request"]["error"]["code"] == "request_cancelled"
