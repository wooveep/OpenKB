"""Protocol behavior tests for the packaged Desktop Python Engine."""

from __future__ import annotations

import hashlib
import io
import struct
import threading
import time

import pytest

from openkb.desktop_engine import (
    DesktopEngineServer,
    DesktopProtocolError,
    DesktopRequest,
    FrameReader,
    encode_frame,
)
from openkb.desktop_import import DesktopImportControl, DesktopImportError, DesktopTextImportService
from openkb.desktop_import_store import DesktopImportStore
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


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


class RequestResponseOutput(io.BytesIO):
    """Signal once a chosen asynchronous Desktop Bridge request has completed."""

    def __init__(self, response_written: threading.Event, request_id: str) -> None:
        super().__init__()
        self._response_written = response_written
        self._request_marker = f'"id":"{request_id}"'.encode()

    def write(self, payload: bytes) -> int:
        size = super().write(payload)
        if self._request_marker in payload:
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


def test_engine_reports_handshake_health_events_command_and_cancel_round_trip():
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
                    "id": "active",
                    "method": "workbench.active_knowledge_base",
                    "params": {},
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
    active_complete = threading.Event()
    output = RequestResponseOutput(active_complete, "active")

    DesktopEngineServer(
        WaitForResponseBytesIO(incoming, active_complete), output, engine_version="test"
    ).serve()

    frames = _decode_frames(output.getvalue())
    responses = {frame["id"]: frame for frame in frames if "id" in frame}
    assert responses["handshake"]["result"] == {"protocol_version": 1, "engine_version": "test"}
    assert responses["health"]["result"] == {"status": "ready", "protocol_version": 1}
    assert responses["active"]["result"] == {"knowledge_base": None}
    assert responses["cancel"]["result"] == {"cancelled": False, "request_id": "not-running"}
    assert any(
        frame.get("method") == "event"
        and isinstance(frame.get("params"), dict)
        and frame["params"].get("kind") == "engine.request_started"
        for frame in frames
    )


def test_engine_cancels_an_active_caller_owned_request():
    """Cancellation targets the same request ID that crossed the Desktop Bridge."""

    class SlowWorkspace(DesktopKnowledgeBaseRuntime):
        def active(self):
            time.sleep(0.02)
            return super().active()

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
                    "method": "workbench.active_knowledge_base",
                    "params": {},
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
        FragmentedBytesIO(incoming), output, workspace=SlowWorkspace(), engine_version="test"
    ).serve()

    responses = {frame["id"]: frame for frame in _decode_frames(output.getvalue()) if "id" in frame}
    assert responses["cancel"]["result"] == {
        "cancelled": True,
        "request_id": "workbench-request",
    }
    assert responses["workbench-request"]["error"]["code"] == "request_cancelled"


def test_engine_does_not_report_a_started_knowledge_base_activation_as_cancelled(tmp_path):
    """An activation that can mutate the active binding remains truthful through its reply."""
    started = threading.Event()
    release = threading.Event()

    class BlockingWorkspace(DesktopKnowledgeBaseRuntime):
        def create(self, kb_dir, *, name=None):
            started.set()
            assert release.wait(timeout=1)
            return super().create(kb_dir, name=name)

    output = io.BytesIO()
    server = DesktopEngineServer(io.BytesIO(), output, workspace=BlockingWorkspace())
    server._handshake_complete = True
    request = DesktopRequest(
        request_id="create",
        method="workbench.create_knowledge_base",
        params={"kb_dir": str(tmp_path / "desktop-kb"), "name": "Desktop KB"},
    )

    server._start_request(request)
    assert started.wait(timeout=1)
    cancelled = server._dispatch(
        DesktopRequest(
            request_id="cancel",
            method="engine.cancel",
            params={"request_id": "create"},
        ),
        cancel_event=None,
    )
    assert cancelled == {"cancelled": False, "request_id": "create"}

    release.set()
    server._join_workers()
    responses = {frame["id"]: frame for frame in _decode_frames(output.getvalue()) if "id" in frame}
    assert "result" in responses["create"]


def test_engine_creates_and_activates_a_sqlite_desktop_knowledge_base(tmp_path):
    """The private Bridge exposes the new workspace format, not a legacy command path."""
    desktop_kb = tmp_path / "desktop-kb"
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
                    "id": "create",
                    "method": "workbench.create_knowledge_base",
                    "params": {"kb_dir": str(desktop_kb), "name": "Desktop KB"},
                }
            ),
        )
    )
    created = threading.Event()
    output = RequestResponseOutput(created, "create")

    DesktopEngineServer(WaitForResponseBytesIO(incoming, created), output).serve()

    responses = {frame["id"]: frame for frame in _decode_frames(output.getvalue()) if "id" in frame}
    assert responses["create"]["result"] == {
        "knowledge_base": {
            "kb_dir": str(desktop_kb),
            "name": "Desktop KB",
            "schema_version": 21,
            "last_checkpoint_at": None,
        },
        "events": [
            {
                "kind": "knowledge_base.activated",
                "data": {
                    "kb_dir": str(desktop_kb),
                    "name": "Desktop KB",
                    "previous_kb_dir": None,
                    "checkpointed": False,
                },
            }
        ],
    }
    assert (desktop_kb / ".openkb" / "state.sqlite3").is_file()


def test_engine_inspects_batch_sources_before_importing(tmp_path):
    """The Bridge previews a selected directory without creating Import Jobs."""
    source_directory = tmp_path / "sources"
    source_directory.mkdir()
    (source_directory / "note.txt").write_text("Ready.", encoding="utf-8")
    (source_directory / "diagram.pdf").write_bytes(b"PDF will be parsed when imported")
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    server._handshake_complete = True

    result = server._dispatch(
        DesktopRequest(
            request_id="inspect-import-sources",
            method="workbench.inspect_import_sources",
            params={"source_paths": [str(source_directory)]},
        ),
        cancel_event=None,
    )

    assert [source["name"] for source in result["supported"]] == ["diagram.pdf", "note.txt"]
    assert result["unsupported"] == []


def test_engine_reads_a_verified_raw_document(tmp_path):
    """The Desktop Bridge exposes originals only through the integrity-checked reader."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text("Original reader text.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
    imported = DesktopTextImportService(desktop_kb).import_text(source)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    result = server._dispatch(
        DesktopRequest(
            request_id="read-raw-document",
            method="workbench.read_raw_document",
            params={"document_id": imported.document.document_id},
        ),
        cancel_event=None,
    )

    assert result == {
        "document_id": imported.document.document_id,
        "name": "guide.txt",
        "source_format": "txt",
        "asset_sha256": imported.document.raw_asset_sha256,
        "byte_size": len(b"Original reader text."),
        "content": "Original reader text.",
        "page": 0,
        "has_more": False,
        "source_images": [],
    }


def test_open_quarantines_documents_with_a_missing_raw_original(tmp_path):
    """A new Desktop session cannot surface a document whose raw asset disappeared."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text("Original reader text.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(desktop_kb)
    imported = DesktopTextImportService(desktop_kb).import_text(source)
    (desktop_kb / "raw" / f"{imported.document.raw_asset_sha256}.txt").unlink()
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    server._handshake_complete = True

    server._dispatch(
        DesktopRequest(
            request_id="open",
            method="workbench.open_knowledge_base",
            params={"kb_dir": str(desktop_kb)},
        ),
        cancel_event=None,
    )
    jobs = server._dispatch(
        DesktopRequest(
            request_id="jobs",
            method="workbench.import_jobs",
            params={},
        ),
        cancel_event=None,
    )

    assert jobs["jobs"][0]["document"]["availability"] == "failed"


def test_completed_batch_document_remains_visible_while_the_next_job_runs(tmp_path):
    """A later import must not hide an earlier Available Knowledge document."""
    desktop_kb = tmp_path / "desktop-kb"
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("First document.", encoding="utf-8")
    second.write_text("Second document.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
    second_started = threading.Event()
    release_second = threading.Event()

    def analyze(request, _timeout_seconds):
        if request.document_name == "second.txt":
            second_started.set()
            assert release_second.wait(timeout=1)
        return "Analysis complete."

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True
    server._dispatch(
        DesktopRequest(
            request_id="first",
            method="workbench.import_text_document",
            params={"source_path": str(first)},
        ),
        cancel_event=None,
    )

    worker = threading.Thread(
        target=server._dispatch,
        args=(
            DesktopRequest(
                request_id="second",
                method="workbench.import_text_document",
                params={"source_path": str(second)},
            ),
            None,
        ),
    )
    worker.start()
    assert second_started.wait(timeout=1)

    task_result: dict[str, object] = {}
    task_ready = threading.Event()

    def read_tasks() -> None:
        task_result.update(
            server._dispatch(
                DesktopRequest(
                    request_id="tasks",
                    method="workbench.import_jobs",
                    params={},
                ),
                cancel_event=None,
            )
        )
        task_ready.set()

    reader = threading.Thread(target=read_tasks)
    reader.start()
    assert task_ready.wait(timeout=1)
    jobs = task_result["jobs"]
    assert isinstance(jobs, list)
    assert any(job["document"]["name"] == "first.txt" for job in jobs if job["document"])
    assert any(job["job"]["status"] == "running" for job in jobs)

    release_second.set()
    worker.join(timeout=1)
    reader.join(timeout=1)
    assert not worker.is_alive()


def test_engine_imports_txt_and_emits_durable_stage_progress(tmp_path):
    """The Bridge exposes the Desktop-native import task instead of a legacy CLI command."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "notes.txt"
    source.write_text("One searchable note.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
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
                    "id": "import",
                    "method": "workbench.import_text_document",
                    "params": {"source_path": str(source)},
                }
            ),
        )
    )
    completed = threading.Event()
    output = RequestResponseOutput(completed, "import")

    DesktopEngineServer(
        WaitForResponseBytesIO(incoming, completed), output, workspace=workspace
    ).serve()

    frames = _decode_frames(output.getvalue())
    responses = {frame["id"]: frame for frame in frames if "id" in frame}
    result = responses["import"]["result"]
    assert result["document"]["availability"] == "available"
    assert result["job"]["status"] == "completed"
    assert any(
        frame.get("method") == "event"
        and isinstance(frame.get("params"), dict)
        and frame["params"].get("kind") == "import.stage_progress"
        and frame["params"].get("data", {}).get("request_id") == "import"
        and frame["params"].get("data", {}).get("stage") == "search"
        for frame in frames
    )


def test_engine_reads_persisted_import_tasks_for_the_active_knowledge_base(tmp_path):
    """A reopened workbench can project durable task-center state through the Bridge."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "notes.txt"
    source.write_text("One durable import task.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
    imported = DesktopTextImportService(desktop_kb).import_text(source)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    history = server._dispatch(
        DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
        cancel_event=None,
    )

    assert history["jobs"][0]["job"]["job_id"] == imported.job.job_id
    assert history["jobs"][0]["document"]["availability"] == "available"


def test_engine_uses_the_configured_model_gateway_for_imports(tmp_path):
    """The production import path reaches retry/quarantine behavior when configured."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "slow.txt"
    source.write_text("A model timeout must quarantine this document.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)

    def timeout_transport(_request, _timeout_seconds):
        raise TimeoutError()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(timeout_transport),
    )
    server._handshake_complete = True
    request = DesktopRequest(
        request_id="import",
        method="workbench.import_text_document",
        params={"source_path": str(source)},
    )

    with pytest.raises(DesktopImportError) as error:
        server._dispatch(request, cancel_event=None)

    assert error.value.code == "document_quarantined"
    history = server._dispatch(
        DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
        cancel_event=None,
    )
    assert history["jobs"][0]["job"]["status"] == "quarantined"
    assert history["jobs"][0]["quarantine"]["error_code"] == "model_timeout"


def test_engine_recovers_a_quarantined_import_with_a_run_scoped_override(tmp_path):
    """The Bridge forwards a recovery override only to the selected recovery run."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "recover.txt"
    source.write_text("Recover only this document.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
    overrides = []

    def model_gateway_factory(_kb_dir, override=None):
        overrides.append(override)
        if override is None:
            return DesktopModelGateway(lambda *_args: (_ for _ in ()).throw(TimeoutError()))
        return DesktopModelGateway(lambda *_args: "Recovered")

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=model_gateway_factory,
    )
    server._handshake_complete = True
    with pytest.raises(DesktopImportError, match="response timeout"):
        server._dispatch(
            DesktopRequest(
                request_id="import",
                method="workbench.import_text_document",
                params={"source_path": str(source)},
            ),
            cancel_event=None,
        )
    job_id = server._dispatch(
        DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
        cancel_event=None,
    )["jobs"][0]["job"]["job_id"]

    recovered = server._dispatch(
        DesktopRequest(
            request_id="recover",
            method="workbench.recover_import_job",
            params={
                "job_id": job_id,
                "recovery_override": {
                    "model": "test/recovery-model",
                    "initialTimeoutSeconds": 30,
                },
            },
        ),
        cancel_event=None,
    )

    assert recovered["job"]["status"] == "completed"
    assert recovered["quarantine"] is None
    assert overrides[0] is None
    assert overrides[1].model == "test/recovery-model"
    assert overrides[1].initial_timeout_seconds == 30


def test_open_starts_recovery_from_the_latest_verified_checkpoint(tmp_path):
    """Opening a KB restarts recoverable work without waiting for a user action."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "recover.txt"
    source.write_text("# Recover\n\nResume from the raw checkpoint.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(desktop_kb)
    source_bytes = source.read_bytes()
    asset_sha256 = hashlib.sha256(source_bytes).hexdigest()
    store = DesktopImportStore(desktop_kb)
    state = store.create_job(source)
    store.set_stage(
        state,
        "preflight",
        "completed",
        20,
        checkpoint={"asset_sha256": asset_sha256, "raw_size": len(source_bytes)},
    )
    raw_path = store.write_raw_asset(asset_sha256, source_bytes)
    store.set_stage(
        state,
        "raw_asset",
        "completed",
        35,
        checkpoint={
            "asset_sha256": asset_sha256,
            "raw_path": raw_path,
            "raw_size": len(source_bytes),
        },
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    server._handshake_complete = True

    server._dispatch(
        DesktopRequest(
            request_id="open",
            method="workbench.open_knowledge_base",
            params={"kb_dir": str(desktop_kb)},
        ),
        cancel_event=None,
    )

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if DesktopTextImportService(desktop_kb).task(state.job_id).job.status == "completed":
            break
        time.sleep(0.01)
    assert DesktopTextImportService(desktop_kb).task(state.job_id).job.status == "completed"


def test_engine_signals_an_active_import_without_waiting_for_workspace_lock():
    """Pause and cancel travel through the task control, not generic request cancellation."""
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    server._handshake_complete = True
    pause_control = DesktopImportControl()
    cancel_control = DesktopImportControl()
    server._import_controls["pause-job"] = pause_control
    server._import_controls["cancel-job"] = cancel_control

    paused = server._dispatch(
        DesktopRequest(
            request_id="pause",
            method="workbench.pause_import_job",
            params={"job_id": "pause-job"},
        ),
        cancel_event=None,
    )
    cancelled = server._dispatch(
        DesktopRequest(
            request_id="cancel",
            method="workbench.cancel_import_job",
            params={"job_id": "cancel-job"},
        ),
        cancel_event=None,
    )

    assert paused == {"job_id": "pause-job", "accepted": True}
    assert cancelled == {"job_id": "cancel-job", "accepted": True}
    assert pause_control.action == "paused"
    assert cancel_control.action == "cancelled"


def test_engine_streams_and_returns_a_grounded_answer(tmp_path):
    """The typed private protocol returns completed citations after answer deltas."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "answer.txt"
    source.write_text("# Evidence\n\nThe project uses a local evidence baseline.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    output = io.BytesIO()
    server = DesktopEngineServer(io.BytesIO(), output, workspace=workspace)
    server._handshake_complete = True

    server._run_request(
        DesktopRequest(
            request_id="answer",
            method="workbench.ask_grounded",
            params={"question": "What baseline does the project use?"},
        ),
        cancel_event=threading.Event(),
    )

    frames = _decode_frames(output.getvalue())
    response = next(frame for frame in frames if frame.get("id") == "answer")
    assert response["result"]["citations"][0]["document_name"] == "answer.txt"
    delta_event = next(
        frame
        for frame in frames
        if frame.get("method") == "event"
        and frame["params"]["kind"] == "answer.delta"
        and frame["params"]["data"]["request_id"] == "answer"
    )
    assert delta_event["params"]["data"]["replace"] is True
    assert delta_event["params"]["data"]["attempt"] == 1


def test_engine_autosaves_then_explicitly_publishes_user_knowledge_pages(tmp_path):
    """The public workbench boundary keeps drafts separate from published reader state."""
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    saved = server._dispatch(
        DesktopRequest(
            request_id="save-page",
            method="workbench.save_knowledge_page",
            params={
                "page_id": None,
                "kind": "concept",
                "title": "Evidence",
                "content_markdown": "# User-owned knowledge",
            },
        ),
        cancel_event=None,
    )
    assert saved["publication_state"] == "draft"
    assert saved["published_revision"] is None
    assert saved["working_draft"]["content_markdown"] == "# User-owned knowledge"
    assert not (kb_dir / str(saved["materialized_path"])).exists()

    published = server._dispatch(
        DesktopRequest(
            request_id="publish-page",
            method="workbench.publish_knowledge_page",
            params={"page_id": str(saved["page_id"])},
        ),
        cancel_event=None,
    )
    revised = server._dispatch(
        DesktopRequest(
            request_id="revise-page",
            method="workbench.save_knowledge_page",
            params={
                "page_id": saved["page_id"],
                "kind": "concept",
                "title": "Evidence",
                "content_markdown": "# Unpublished revision",
            },
        ),
        cancel_event=None,
    )
    listed = server._dispatch(
        DesktopRequest(
            request_id="list-pages",
            method="workbench.knowledge_pages",
            params={},
        ),
        cancel_event=None,
    )
    read = server._dispatch(
        DesktopRequest(
            request_id="read-page",
            method="workbench.knowledge_page",
            params={"page_id": str(saved["page_id"])},
        ),
        cancel_event=None,
    )

    assert listed["pages"] == [
        {
            "page_id": saved["page_id"],
            "kind": "concept",
            "title": "Evidence",
            "publication_state": "unpublished_changes",
            "published_revision_number": 1,
            "updated_at": revised["updated_at"],
        }
    ]
    assert listed["selected_page_id"] == saved["page_id"]
    assert read["published_revision"]["content_markdown"] == "# User-owned knowledge"
    assert read["working_draft"]["content_markdown"] == "# Unpublished revision"

    projection = kb_dir / str(published["materialized_path"])
    projection.unlink()
    reopened = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    reopened._handshake_complete = True
    reopened._dispatch(
        DesktopRequest(
            request_id="open-page-kb",
            method="workbench.open_knowledge_base",
            params={"kb_dir": str(kb_dir)},
        ),
        cancel_event=None,
    )
    assert "# User-owned knowledge" in projection.read_text(encoding="utf-8")
    restored = reopened._dispatch(
        DesktopRequest(
            request_id="restored-pages",
            method="workbench.knowledge_pages",
            params={},
        ),
        cancel_event=None,
    )
    assert restored["selected_page_id"] == saved["page_id"]


def test_engine_binds_one_knowledge_claim_to_available_original_evidence(tmp_path):
    """Source search, binding, and publication stay behind the workbench protocol."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.md"
    source.write_text("# Runtime\n\nThe sidecar starts once per session.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    claim = "OpenKB starts its local engine once."
    draft = server._dispatch(
        DesktopRequest(
            request_id="draft-source-page",
            method="workbench.save_knowledge_page",
            params={
                "page_id": None,
                "kind": "concept",
                "title": "Engine lifecycle",
                "content_markdown": claim,
            },
        ),
        cancel_event=None,
    )

    searched = server._dispatch(
        DesktopRequest(
            request_id="search-page-source",
            method="workbench.search_knowledge_sources",
            params={"query": "sidecar session"},
        ),
        cancel_event=None,
    )
    candidate = next(item for item in searched["sources"] if "starts once" in item["excerpt"])
    bound = server._dispatch(
        DesktopRequest(
            request_id="bind-page-source",
            method="workbench.bind_knowledge_page_source",
            params={
                "page_id": draft["page_id"],
                "claim_text": claim,
                "evidence_id": candidate["evidence_id"],
            },
        ),
        cancel_event=None,
    )
    published = server._dispatch(
        DesktopRequest(
            request_id="publish-source-page",
            method="workbench.publish_knowledge_page",
            params={"page_id": draft["page_id"]},
        ),
        cancel_event=None,
    )

    assert len(bound["working_draft"]["source_map"]) == 1
    assert bound["publication_diagnostics"] == []
    assert (
        published["published_revision"]["source_map"][0]["evidence_id"]
        == (candidate["evidence_id"])
    )


def test_engine_lists_isolated_knowledge_reconciliation_conflicts(tmp_path):
    """The review queue exposes conflicts without publishing the incoming change."""
    kb_dir = tmp_path / "desktop-kb"
    first = tmp_path / "first.txt"
    first.write_text("# Concept: Evidence\n\nStable statement.", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text("# Concept: Evidence\n\nConflicting statement.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(first)
    DesktopTextImportService(kb_dir).import_text(second)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    queue = server._dispatch(
        DesktopRequest(
            request_id="knowledge-conflicts",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    )

    assert len(queue["conflicts"]) == 1
    assert queue["conflicts"][0]["title"] == "Evidence"
    assert queue["conflicts"][0]["baseline_kind"] == "published_generation"

    staged = server._dispatch(
        DesktopRequest(
            request_id="stage-knowledge-conflict",
            method="workbench.stage_knowledge_reconciliation_decisions",
            params={
                "candidate_ids": [queue["conflicts"][0]["candidate_id"]],
                "decision": "publish_incoming",
            },
        ),
        cancel_event=None,
    )
    assert staged["conflicts"][0]["staged_decision"] == "publish_incoming"
    committed = server._dispatch(
        DesktopRequest(
            request_id="commit-knowledge-conflict",
            method="workbench.commit_knowledge_reconciliation_decisions",
            params={},
        ),
        cancel_event=None,
    )
    assert committed["published_count"] == 1
    assert server._dispatch(
        DesktopRequest(
            request_id="knowledge-conflicts-after-commit",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    ) == {"conflicts": []}


def test_engine_returns_a_persisted_interrupted_answer_after_user_stop(tmp_path):
    """Stopping an in-flight answer is a durable answer state, not a bridge error."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "answer.txt"
    source.write_text("The project uses a local evidence baseline.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    output = io.BytesIO()
    stop = threading.Event()

    class StoppingTransport:
        def __call__(self, _request, _timeout_seconds):
            return '{"terms": ["evidence"]}'

        def stream(self, _request, _timeout_seconds, on_delta):
            on_delta("partial answer")
            stop.set()
            return "late answer"

    server = DesktopEngineServer(
        io.BytesIO(),
        output,
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(StoppingTransport()),
    )
    server._handshake_complete = True

    server._run_request(
        DesktopRequest(
            request_id="answer",
            method="workbench.ask_grounded",
            params={"question": "What baseline does the project use?"},
        ),
        cancel_event=stop,
    )

    response = next(
        frame for frame in _decode_frames(output.getvalue()) if frame.get("id") == "answer"
    )
    result = response["result"]
    assert result["status"] == "interrupted"
    assert result["interruption_code"] == "answer_cancelled"
    assert result["answer_text"] == "partial answer"
