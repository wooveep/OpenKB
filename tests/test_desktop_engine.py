"""Protocol behavior tests for the packaged Desktop Python Engine."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest

from openkb import desktop_workspace as desktop_workspace_module
from openkb.desktop_engine import (
    DesktopEngineServer,
    DesktopProtocolError,
    DesktopRequest,
    DesktopRequestError,
    FrameReader,
    encode_frame,
)
from openkb.desktop_engine_logging import EngineRequestDiagnostics
from openkb.desktop_import import DesktopImportControl, DesktopImportError, DesktopTextImportService
from openkb.desktop_import_store import DesktopImportStore
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_logging import TRACE_LEVEL
from openkb.desktop_model_gateway import DesktopModelGateway, DesktopModelResult
from openkb.desktop_model_terminal import DesktopTerminalModelEvent
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _empty_knowledge_analysis() -> str:
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "No durable knowledge candidates.",
            "concepts": [],
            "entities": [],
        }
    )


def _seed_generated_workspace_item(
    kb_dir: Path,
    tmp_path: Path,
    *,
    title: str = "Generated Knowledge",
    item_key: str = "generated-item",
) -> tuple[int, str, str]:
    source = tmp_path / f"{item_key}.md"
    source.write_text(f"# {title}\n\n{title} is evidence-bound.", encoding="utf-8")
    imported = DesktopTextImportService(kb_dir).import_text(source)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ? LIMIT 1",
                (imported.document.document_id,),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            "INSERT INTO knowledge_generations (parent_generation_id, created_at) "
            "VALUES (NULL, '2026-08-28T00:00:00+00:00')"
        )
        generation_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO knowledge_generation_state (singleton, current_generation_id) "
            "VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "current_generation_id = excluded.current_generation_id",
            (generation_id,),
        )
        markdown = f"# {title}\n\n{title} is evidence-bound.[^src-generated]"
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, aliases_json, tags_json
            ) VALUES (?, ?, 'concept', ?, ?, ?, ?, ?, '2026-08-28T00:00:00+00:00',
                'source_backed', '[]', '[]')
            """,
            (
                generation_id,
                item_key,
                title,
                title.casefold(),
                markdown,
                hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                imported.document.document_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_item_sources (
                generation_id, item_key, source_id, evidence_id, claim_text
            ) VALUES (?, ?, 'src-generated', ?, ?)
            """,
            (generation_id, item_key, evidence_id, f"{title} is evidence-bound."),
        )
        connection.commit()
    return generation_id, item_key, evidence_id


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


def test_import_job_polling_uses_one_deduplicated_trace_without_filling_info(caplog) -> None:
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    server._handshake_complete = True

    with caplog.at_level(TRACE_LEVEL, logger="openkb.desktop_engine_logging"):
        server._run_request(
            DesktopRequest(
                request_id="poll",
                method="workbench.import_jobs",
                params={},
            ),
            cancel_event=None,
        )

    records = [record for record in caplog.records if record.msg == "engine_poll_observed"]
    assert [record.levelno for record in records] == [TRACE_LEVEL]
    assert records[0].openkb_fields["method"] == "workbench.import_jobs"


def test_engine_boundary_references_failure_owner_through_domain_wrappers(caplog) -> None:
    owner = RuntimeError("provider failed")
    owner.failure_event_id = "failure-owner-1"  # type: ignore[attr-defined]
    try:
        raise owner
    except RuntimeError as cause:
        try:
            raise ValueError("capability check failed") from cause
        except ValueError as wrapped:
            with caplog.at_level(TRACE_LEVEL, logger="openkb.desktop_engine_logging"):
                EngineRequestDiagnostics.begin("request-1", "workbench.check_model").typed_failure(
                    wrapped
                )

    records = [record for record in caplog.records if record.msg == "failure_propagated"]
    assert len(records) == 1
    assert records[0].openkb_fields["failure_event_id"] == "failure-owner-1"
    assert all(record.msg != "engine_request_failed" for record in caplog.records)


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
    assert responses["health"]["result"]["status"] == "ready"
    assert responses["health"]["result"]["protocol_version"] == 1
    assert "native_office" in responses["health"]["result"]["parser_readiness"]
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


def test_engine_eof_stops_background_runtime_work():
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())

    server.serve()

    assert server._shutdown.is_set()


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
            "schema_version": desktop_workspace_module._MIGRATIONS[-1][0],
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
        return _empty_knowledge_analysis()

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
        WaitForResponseBytesIO(incoming, completed),
        output,
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(
            lambda *_args: _empty_knowledge_analysis()
        ),
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


def test_engine_keeps_model_summary_status_separate_from_failure_lifecycle(tmp_path):
    """Detailed provider failure never escapes through the four-state status field."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "notes.txt"
    source.write_text("One durable import task.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
    imported = DesktopTextImportService(
        desktop_kb,
        model_gateway=DesktopModelGateway(lambda *_args: _empty_knowledge_analysis()),
    ).import_text(source)
    with sqlite3.connect(desktop_kb / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            """
            UPDATE model_calls
            SET status = 'failed', lifecycle_status = 'provider_failure'
            WHERE job_id = ?
            """,
            (imported.job.job_id,),
        )
        connection.execute(
            """
            UPDATE model_attempts
            SET status = 'failed', lifecycle_status = 'provider_failure'
            WHERE call_id IN (SELECT call_id FROM model_calls WHERE job_id = ?)
            """,
            (imported.job.job_id,),
        )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    history = server._dispatch(
        DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
        cancel_event=None,
    )

    call = history["jobs"][0]["model_calls"][0]
    assert call["status"] == "failed"
    assert call["lifecycle_status"] == "provider_failure"
    assert call["attempts"][0]["status"] == "failed"
    assert call["attempts"][0]["lifecycle_status"] == "provider_failure"


def test_engine_preserves_nullable_historical_model_lifecycle_without_rewriting_it(tmp_path):
    """A pre-v38 ledger migrates and future lifecycle values stay non-destructive."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "notes.txt"
    source.write_text("One historical import task.", encoding="utf-8")
    desktop_kb.mkdir()
    (desktop_kb / "raw").mkdir()
    state_dir = desktop_kb / ".openkb"
    state_dir.mkdir()
    database_path = desktop_kb / ".openkb" / "state.sqlite3"
    with desktop_workspace_module._connect(database_path) as connection:
        for version, statements in desktop_workspace_module._MIGRATIONS:
            if version >= 38:
                break
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2026-01-01T00:00:00+00:00"),
            )
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (("format", "openkb-desktop"), ("knowledge_base_name", "Historical KB")),
        )
        connection.execute(
            """
            INSERT INTO import_jobs (
                job_id, source_path, document_id, status, progress, error_code,
                created_at, completed_at
            ) VALUES ('historical-job', ?, NULL, 'failed', 100, 'provider_failure', ?, ?)
            """,
            (str(source), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
        )
        connection.execute(
            """
            INSERT INTO import_job_runtime (
                job_id, status, lease_owner, lease_expires_at, updated_at
            ) VALUES ('historical-job', 'failed', NULL, NULL, ?)
            """,
            ("2026-01-01T00:00:01+00:00",),
        )
        stages = (
            "preflight",
            "raw_asset",
            "document_ir",
            "evidence",
            "deterministic_page_tree",
            "model_analysis",
            "search",
        )
        for stage in stages:
            status = "failed" if stage == "model_analysis" else "completed"
            error_code = "provider_failure" if status == "failed" else None
            connection.execute(
                """
                INSERT INTO stage_runs (
                    stage_run_id, job_id, stage, status, progress, error_code,
                    started_at, completed_at
                ) VALUES (?, 'historical-job', ?, ?, 100, ?, ?, ?)
                """,
                (
                    f"historical-{stage}",
                    stage,
                    status,
                    error_code,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:01+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO stage_run_runtime (
                    stage_run_id, job_id, status, checkpoint_json, error_code, updated_at
                ) VALUES (?, 'historical-job', ?, NULL, ?, ?)
                """,
                (
                    f"historical-{stage}",
                    status,
                    error_code,
                    "2026-01-01T00:00:01+00:00",
                ),
            )
        connection.execute(
            """
            INSERT INTO model_calls (
                call_id, job_id, stage_run_id, operation, status, attempt_count,
                timeout_seconds, next_timeout_seconds, remaining_seconds,
                error_code, reason, suggested_action, created_at, completed_at
            ) VALUES (
                'historical-call', 'historical-job', 'historical-model_analysis',
                'knowledge_analysis', 'failed', 1, 60, NULL, 0,
                'provider_failure', 'Historical provider failure.', NULL, ?, ?
            )
            """,
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
        )
        connection.execute(
            """
            INSERT INTO model_attempts (
                call_id, attempt, status, timeout_seconds, remaining_seconds,
                error_code, reason, created_at, completed_at
            ) VALUES (
                'historical-call', 1, 'failed', 60, 0, 'provider_failure',
                'Historical provider failure.', ?, ?
            )
            """,
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
        )
        connection.commit()
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (37,)
        assert "lifecycle_status" not in {
            row[1] for row in connection.execute("PRAGMA table_info(model_calls)")
        }

    migrated_workspace = DesktopKnowledgeBaseRuntime()
    migrated_workspace.open(desktop_kb)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=migrated_workspace)
    server._handshake_complete = True

    for status in ("running", "retry_wait", "completed", "failed"):
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE model_calls SET status = ?, lifecycle_status = NULL WHERE job_id = ?",
                (status, "historical-job"),
            )
            connection.execute(
                """
                UPDATE model_attempts
                SET status = ?, lifecycle_status = NULL
                WHERE call_id IN (SELECT call_id FROM model_calls WHERE job_id = ?)
                """,
                (status, "historical-job"),
            )

        history = server._dispatch(
            DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
            cancel_event=None,
        )

        call = history["jobs"][0]["model_calls"][0]
        assert call["status"] == status
        assert call["lifecycle_status"] is None
        assert call["attempts"][0]["status"] == status
        assert call["attempts"][0]["lifecycle_status"] is None

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE model_calls SET status = 'future_summary_state', "
            "lifecycle_status = 'completed' WHERE job_id = ?",
            ("historical-job",),
        )
        connection.execute(
            """
            UPDATE model_attempts
            SET status = 'future_summary_state', lifecycle_status = 'queued'
            WHERE call_id IN (SELECT call_id FROM model_calls WHERE job_id = ?)
            """,
            ("historical-job",),
        )

    independent = server._dispatch(
        DesktopRequest(
            request_id="independent-statuses", method="workbench.import_jobs", params={}
        ),
        cancel_event=None,
    )["jobs"][0]["model_calls"][0]
    assert independent["status"] == "failed"
    assert independent["lifecycle_status"] == "completed"
    assert independent["attempts"][0]["status"] == "failed"
    assert independent["attempts"][0]["lifecycle_status"] == "queued"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE model_calls SET lifecycle_status = 'future_provider_state' WHERE job_id = ?",
            ("historical-job",),
        )
        connection.execute(
            """
            UPDATE model_attempts
            SET lifecycle_status = 'future_provider_state'
            WHERE call_id IN (SELECT call_id FROM model_calls WHERE job_id = ?)
            """,
            ("historical-job",),
        )

    history = server._dispatch(
        DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
        cancel_event=None,
    )

    call = history["jobs"][0]["model_calls"][0]
    assert call["status"] == "failed"
    assert call["lifecycle_status"] is None
    assert call["attempts"][0]["status"] == "failed"
    assert call["attempts"][0]["lifecycle_status"] is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT lifecycle_status FROM model_calls WHERE job_id = ?",
            ("historical-job",),
        ).fetchone() == ("future_provider_state",)
        assert connection.execute(
            """
            SELECT lifecycle_status FROM model_attempts
            WHERE call_id IN (SELECT call_id FROM model_calls WHERE job_id = ?)
            """,
            ("historical-job",),
        ).fetchone() == ("future_provider_state",)


def test_engine_classifies_provider_timeout_as_a_network_failure(tmp_path):
    """An explicit network timeout is a provider failure, never a response deadline."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "slow.txt"
    source.write_text("A network timeout must quarantine this document.", encoding="utf-8")
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
    assert history["jobs"][0]["quarantine"]["error_code"] == "model_network_transient"


def test_engine_pauses_new_import_when_model_is_not_configured(tmp_path):
    """Parsed evidence waits for configuration without becoming a failed document."""
    desktop_kb = tmp_path / "desktop-kb"
    source = tmp_path / "unconfigured.txt"
    source.write_text("This document needs Knowledge Analysis.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(desktop_kb)
    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: None,
    )
    server._handshake_complete = True

    with pytest.raises(DesktopImportError) as error:
        server._dispatch(
            DesktopRequest(
                request_id="import",
                method="workbench.import_text_document",
                params={"source_path": str(source)},
            ),
            cancel_event=None,
        )

    assert error.value.code == "awaiting_model_configuration"
    history = server._dispatch(
        DesktopRequest(request_id="history", method="workbench.import_jobs", params={}),
        cancel_event=None,
    )
    assert history["jobs"][0]["job"]["status"] == "awaiting_model_configuration"
    assert history["jobs"][0]["quarantine"] is None
    assert history["jobs"][0]["document"] is None


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
        return DesktopModelGateway(lambda *_args: _empty_knowledge_analysis())

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        workspace=workspace,
        model_gateway_factory=model_gateway_factory,
    )
    server._handshake_complete = True
    with pytest.raises(DesktopImportError, match="connection to the model provider"):
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
                    "context_capacity": 32_768,
                },
            },
        ),
        cancel_event=None,
    )

    assert recovered["job"]["status"] == "completed"
    assert recovered["quarantine"] is None
    assert overrides[0] is None
    assert overrides[1].model == "test/recovery-model"
    assert overrides[1].context_capacity == 32_768


def test_open_leaves_recoverable_import_waiting_for_explicit_resume(tmp_path):
    """Opening a KB preserves checkpoints without automatically spending model tokens."""
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
    analysis_calls = 0
    model_called = threading.Event()

    def analyze(_request, _timeout_seconds):
        nonlocal analysis_calls
        if _request.operation == "knowledge_analysis":
            analysis_calls += 1
            model_called.set()
        return _empty_knowledge_analysis()

    server = DesktopEngineServer(
        io.BytesIO(),
        io.BytesIO(),
        model_gateway_factory=lambda _kb_dir, _override: DesktopModelGateway(analyze),
    )
    server._handshake_complete = True

    server._dispatch(
        DesktopRequest(
            request_id="open",
            method="workbench.open_knowledge_base",
            params={"kb_dir": str(desktop_kb)},
        ),
        cancel_event=None,
    )

    assert not model_called.wait(0.2)
    task = DesktopTextImportService(desktop_kb).task(state.job_id)
    assert task.job.status == "recoverable"
    assert task.stages[0].status == "completed"
    assert task.stages[1].status == "completed"
    assert analysis_calls == 0


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


def test_engine_emits_sanitized_interactive_answer_model_lifecycle(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "answer.txt"
    source.write_text("OpenKB keeps local evidence available.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    output = io.BytesIO()

    class ObservedInteractiveGateway:
        def for_lane(self, lane):
            assert lane == "interactive"
            return self

        def analyze(self, request, *, on_event, is_cancelled):
            assert request.operation == "retrieval_plan"
            assert is_cancelled is not None
            for status in (
                "queued",
                "connecting",
                "awaiting_model_result",
                "validating",
                "completed",
            ):
                on_event(
                    DesktopTerminalModelEvent(
                        "retrieval-call",
                        1,
                        status,
                        4,
                        operation="retrieval_plan",
                        model_role="analysis",
                        provider_name="custom",
                        model_name="analysis-model",
                        execution_lane="interactive",
                    )
                )
            return DesktopModelResult("retrieval-call", '{"terms":["evidence"]}', 1)

        def stream(self, request, *, on_event, on_delta, on_reset, is_cancelled):
            assert request.operation == "grounded_answer"
            assert is_cancelled is not None
            del on_reset
            for status in ("queued", "connecting", "awaiting_model_result"):
                on_event(
                    DesktopTerminalModelEvent(
                        "answer-call",
                        1,
                        status,
                        7,
                        operation="grounded_answer",
                        model_role="answer",
                        provider_name="custom",
                        model_name="answer-model",
                        execution_lane="interactive",
                    )
                )
            on_delta(1, "private streamed answer")
            for status in ("model_output_activity", "validating", "completed"):
                on_event(
                    DesktopTerminalModelEvent(
                        "answer-call",
                        1,
                        status,
                        9,
                        operation="grounded_answer",
                        model_role="answer",
                        provider_name="custom",
                        model_name="answer-model",
                        execution_lane="interactive",
                    )
                )
            return DesktopModelResult("answer-call", "private streamed answer", 1)

    server = DesktopEngineServer(
        io.BytesIO(),
        output,
        workspace=workspace,
        model_gateway_factory=lambda _kb_dir, _override: ObservedInteractiveGateway(),
    )
    server._handshake_complete = True
    server._run_request(
        DesktopRequest(
            request_id="answer-lifecycle",
            method="workbench.ask_grounded",
            params={"question": "What does OpenKB keep?"},
        ),
        cancel_event=threading.Event(),
    )

    frames = _decode_frames(output.getvalue())
    lifecycle = [
        frame["params"]["data"]
        for frame in frames
        if frame.get("method") == "event" and frame["params"]["kind"] == "model.call_lifecycle"
    ]
    assert {event["operation"] for event in lifecycle} == {
        "retrieval_plan",
        "grounded_answer",
    }
    answer_activity = next(
        event for event in lifecycle if event["status"] == "model_output_activity"
    )
    assert answer_activity["request_id"] == "answer-lifecycle"
    assert answer_activity["model_role"] == "answer"
    assert answer_activity["model_name"] == "answer-model"
    assert answer_activity["call_id"] == "answer-call"
    assert answer_activity["attempt_id"] == "answer-call:1"
    assert answer_activity["execution_lane"] == "interactive"
    assert answer_activity["long_wait_threshold_seconds"] == 300.0
    assert "private streamed answer" not in repr(lifecycle)


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
            "lifecycle_state": "stable",
            "stale_after": None,
            "is_stale": False,
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


def test_engine_knowledge_workspace_lists_current_generated_and_user_authorities(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "workspace-source.md"
    source.write_text("# OpenKB\n\nOpenKB keeps knowledge evidence-bound.", encoding="utf-8")
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_occurrences WHERE document_id = ? LIMIT 1",
                (imported.document.document_id,),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            "INSERT INTO knowledge_generations (parent_generation_id, created_at) "
            "VALUES (NULL, '2026-08-28T00:00:00+00:00')"
        )
        generation_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO knowledge_generation_state (singleton, current_generation_id) "
            "VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "current_generation_id = excluded.current_generation_id",
            (generation_id,),
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype, aliases_json, tags_json,
                analysis_provenance_json
            ) VALUES (?, 'openkb-item', 'concept', 'OpenKB', 'openkb', ?, ?, ?, ?,
                'source_backed', NULL, '["OKB"]', '["knowledge"]', ?)
            """,
            (
                generation_id,
                "# OpenKB\n\nEvidence-bound knowledge.[^src-1]",
                hashlib.sha256(b"generated").hexdigest(),
                imported.document.document_id,
                "2026-08-28T00:00:00+00:00",
                json.dumps(
                    {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "prompt_digest": "prompt-digest",
                        "engine_version": "0.1.0",
                        "schema_version": "openkb.knowledge-analysis.v1",
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_item_sources (
                generation_id, item_key, source_id, evidence_id, claim_text
            ) VALUES (?, 'openkb-item', 'src-1', ?, 'Evidence-bound knowledge.')
            """,
            (generation_id, evidence_id),
        )
        connection.commit()
    DesktopKnowledgePageService(kb_dir).save_draft(
        page_id=None,
        kind="entity",
        title="OpenKB Team",
        content_markdown="# OpenKB Team",
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    result = server._dispatch(
        DesktopRequest(
            request_id="knowledge-workspace",
            method="workbench.knowledge_workspace",
            params={"query": "OpenKB"},
        ),
        cancel_event=None,
    )
    legacy = server._dispatch(
        DesktopRequest(
            request_id="legacy-knowledge-pages",
            method="workbench.knowledge_pages",
            params={},
        ),
        cancel_event=None,
    )
    generated = next(item for item in result["items"] if item["authority"] == "generated")
    detail = server._dispatch(
        DesktopRequest(
            request_id="generated-detail",
            method="workbench.knowledge_workspace_item",
            params={
                "authority": "generated",
                "generation_id": generation_id,
                "item_key": "openkb-item",
            },
        ),
        cancel_event=None,
    )

    assert result["current_generation_id"] == generation_id
    assert {(item["authority"], item["title"]) for item in result["items"]} == {
        ("generated", "OpenKB"),
        ("user", "OpenKB Team"),
    }
    assert generated["current"] is True
    assert legacy["pages"] == [DesktopKnowledgePageService(kb_dir).list_pages()[0].as_dict()]
    assert detail["authority"] == "generated"
    assert detail["content_markdown"].startswith("# OpenKB")
    assert detail["aliases"] == ["OKB"]
    assert detail["tags"] == ["knowledge"]
    assert detail["source_map"][0]["evidence_id"] == evidence_id
    assert detail["editable"] is False


def test_engine_adopts_generated_knowledge_as_an_idempotent_working_draft(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    request = DesktopRequest(
        request_id="adopt-generated-item",
        method="workbench.adopt_knowledge_item",
        params={
            "generation_id": generation_id,
            "item_key": item_key,
            "adoption_request_id": "adoption-1",
        },
    )

    adopted = server._dispatch(request, cancel_event=None)
    repeated = server._dispatch(request, cancel_event=None)
    with pytest.raises(DesktopRequestError) as request_conflict:
        server._dispatch(
            DesktopRequest(
                request_id="adopt-generated-item-conflict",
                method="workbench.adopt_knowledge_item",
                params={
                    "generation_id": generation_id,
                    "item_key": "different-generated-item",
                    "adoption_request_id": "adoption-1",
                },
            ),
            cancel_event=None,
        )
    same_origin = server._dispatch(
        DesktopRequest(
            request_id="adopt-generated-item-again",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-2",
            },
        ),
        cancel_event=None,
    )

    assert adopted["status"] == "adopted"
    assert repeated == adopted
    assert request_conflict.value.code == "knowledge_adoption_request_conflict"
    assert same_origin["status"] == "already_adopted"
    assert same_origin["page_id"] == adopted["page_id"]
    page = DesktopKnowledgePageService(kb_dir).get_page(str(adopted["page_id"]))
    assert page.publication_state == "draft"
    assert page.published_revision is None
    assert page.working_draft is not None
    assert page.working_draft.source_map[0].evidence_id == evidence_id
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT generation_id, item_key, page_id FROM knowledge_origin_references"
        ).fetchall() == [(generation_id, item_key, adopted["page_id"])]
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_page_working_drafts").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT content_markdown FROM knowledge_generation_items "
                "WHERE generation_id = ? AND item_key = ?",
                (generation_id, item_key),
            )
            .fetchone()[0]
            .startswith("# Generated Knowledge")
        )

    DesktopKnowledgePageService(kb_dir).permanent_delete(
        str(adopted["page_id"]),
        confirmation_page_id=str(adopted["page_id"]),
    )
    readopted = server._dispatch(
        DesktopRequest(
            request_id="readopt-generated-item",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-after-delete",
            },
        ),
        cancel_event=None,
    )
    assert readopted["status"] == "adopted"
    assert readopted["page_id"] != adopted["page_id"]


def test_engine_adoption_preserves_multiple_claims_bound_to_one_source(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    content = (
        "# Generated Knowledge\n\n"
        "Generated Knowledge is evidence-bound.[^src-generated]\n\n"
        "Generated Knowledge stays local.[^src-generated]"
    )
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE knowledge_generation_items SET content_markdown = ?, content_sha256 = ? "
            "WHERE generation_id = ? AND item_key = ?",
            (
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                generation_id,
                item_key,
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_generation_item_sources (
                generation_id, item_key, source_id, evidence_id, claim_text
            ) VALUES (?, ?, 'src-generated', ?, 'Generated Knowledge stays local.')
            """,
            (generation_id, item_key, evidence_id),
        )
        connection.commit()
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    adopted = server._dispatch(
        DesktopRequest(
            request_id="adopt-multiple-claims",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-multiple-claims",
            },
        ),
        cancel_event=None,
    )

    page = DesktopKnowledgePageService(kb_dir).get_page(str(adopted["page_id"]))
    assert page.working_draft is not None
    assert {source.claim_text for source in page.working_draft.source_map} == {
        "Generated Knowledge is evidence-bound.",
        "Generated Knowledge stays local.",
    }
    published = DesktopKnowledgePageService(kb_dir).publish(str(adopted["page_id"]))
    assert published.publication_diagnostics == ()


def test_engine_adoption_collision_requires_reconciliation_without_overwrite(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, _evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    existing = DesktopKnowledgePageService(kb_dir).save_draft(
        page_id=None,
        kind="concept",
        title="Generated Knowledge",
        content_markdown="# Human Working Draft\n\nKeep this content.",
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    result = server._dispatch(
        DesktopRequest(
            request_id="adopt-collision",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-collision",
            },
        ),
        cancel_event=None,
    )

    assert result["status"] == "reconciliation_required"
    assert result["page_id"] is None
    assert result["candidates"] == [
        {
            "page_id": existing.page_id,
            "title": "Generated Knowledge",
            "publication_state": "draft",
            "match": "exact",
            "confidence": 1.0,
        }
    ]
    repeated_origin = server._dispatch(
        DesktopRequest(
            request_id="adopt-collision-again",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-collision-again",
            },
        ),
        cancel_event=None,
    )
    assert repeated_origin["status"] == "reconciliation_required"
    assert repeated_origin["candidates"] == result["candidates"]
    current = DesktopKnowledgePageService(kb_dir).get_page(existing.page_id)
    assert current.working_draft is not None
    assert current.working_draft.content_markdown.endswith("Keep this content.")
    conflicts = server._dispatch(
        DesktopRequest(
            request_id="adoption-reconciliation-queue",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    )["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["target_page_id"] == existing.page_id
    assert conflicts[0]["content_markdown"].startswith("# Generated Knowledge")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT page_id FROM knowledge_origin_references").fetchone() == (
            existing.page_id,
        )

    DesktopKnowledgePageService(kb_dir).permanent_delete(
        existing.page_id,
        confirmation_page_id=existing.page_id,
    )
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_origin_references WHERE page_id = ?",
            (existing.page_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_adoption_requests "
            "WHERE generation_id = ? AND item_key = ?",
            (generation_id, item_key),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_reconciliation_candidates WHERE target_page_id = ?",
            (existing.page_id,),
        ).fetchone() == (0,)
    readopted = server._dispatch(
        DesktopRequest(
            request_id="adopt-collision-after-delete",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-collision",
            },
        ),
        cancel_event=None,
    )
    assert readopted["status"] == "adopted"
    assert readopted["page_id"] != existing.page_id


def test_engine_adoption_collision_survives_an_unavailable_source_binding(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, _evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    existing = DesktopKnowledgePageService(kb_dir).save_draft(
        page_id=None,
        kind="concept",
        title="Generated Knowledge",
        content_markdown="# Human Working Draft\n\nKeep this content.",
    )
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        document_id = str(
            connection.execute(
                "SELECT source_document_id FROM knowledge_generation_items "
                "WHERE generation_id = ? AND item_key = ?",
                (generation_id, item_key),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (document_id,),
        )
        connection.commit()
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    adopted = server._dispatch(
        DesktopRequest(
            request_id="adopt-unavailable-collision",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-unavailable-collision",
            },
        ),
        cancel_event=None,
    )
    queue = server._dispatch(
        DesktopRequest(
            request_id="unavailable-adoption-review-queue",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    )["conflicts"]

    assert adopted["status"] == "reconciliation_required"
    assert adopted["candidates"][0]["page_id"] == existing.page_id
    assert len(queue) == 1
    assert queue[0]["target_page_id"] == existing.page_id
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_reconciliation_candidate_sources"
        ).fetchone() == (0,)

    server._dispatch(
        DesktopRequest(
            request_id="stage-unavailable-adoption",
            method="workbench.stage_knowledge_reconciliation_decisions",
            params={
                "candidate_ids": [queue[0]["candidate_id"]],
                "decision": "replace_draft",
            },
        ),
        cancel_event=None,
    )
    committed = server._dispatch(
        DesktopRequest(
            request_id="commit-unavailable-adoption",
            method="workbench.commit_knowledge_reconciliation_decisions",
            params={},
        ),
        cancel_event=None,
    )

    assert committed["draft_updated_count"] == 1
    current = DesktopKnowledgePageService(kb_dir).get_page(existing.page_id)
    assert current.working_draft is not None
    assert current.working_draft.content_markdown.startswith("# Generated Knowledge")
    assert current.working_draft.source_map == ()


def test_engine_high_confidence_adoption_queues_reconciliation_without_choice(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, _evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    existing = DesktopKnowledgePageService(kb_dir).save_draft(
        page_id=None,
        kind="concept",
        title="Generated Knowledg",
        content_markdown="# Human Working Draft\n\nKeep this content.",
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    result = server._dispatch(
        DesktopRequest(
            request_id="adopt-high-confidence",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-high-confidence",
            },
        ),
        cancel_event=None,
    )

    assert result["status"] == "reconciliation_required"
    assert result["page_id"] is None
    assert result["candidates"][0]["page_id"] == existing.page_id
    assert result["candidates"][0]["match"] == "possible"
    assert result["candidates"][0]["confidence"] >= 0.9
    current = DesktopKnowledgePageService(kb_dir).get_page(existing.page_id)
    assert current.working_draft is not None
    assert current.working_draft.content_markdown.endswith("Keep this content.")


def test_engine_ambiguous_adoption_can_explicitly_create_a_separate_page(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, _evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    existing = DesktopKnowledgePageService(kb_dir).save_draft(
        page_id=None,
        kind="concept",
        title="Generated Knowledge Guide",
        content_markdown="# Human Guide\n\nKeep this separate.",
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    choice = server._dispatch(
        DesktopRequest(
            request_id="adopt-ambiguous",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-ambiguous",
            },
        ),
        cancel_event=None,
    )
    with pytest.raises(DesktopRequestError) as semantic_conflict:
        server._dispatch(
            DesktopRequest(
                request_id="adopt-ambiguous-conflicting-replay",
                method="workbench.adopt_knowledge_item",
                params={
                    "generation_id": generation_id,
                    "item_key": item_key,
                    "adoption_request_id": "adoption-ambiguous",
                    "adoption_decision": "create_new",
                },
            ),
            cancel_event=None,
        )
    created = server._dispatch(
        DesktopRequest(
            request_id="adopt-ambiguous-new-page",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-ambiguous-new-page",
                "adoption_decision": "create_new",
            },
        ),
        cancel_event=None,
    )

    assert choice["status"] == "choice_required"
    assert semantic_conflict.value.code == "knowledge_adoption_request_conflict"
    assert choice["candidates"][0]["page_id"] == existing.page_id
    assert created["status"] == "adopted"
    assert created["page_id"] != existing.page_id
    original = DesktopKnowledgePageService(kb_dir).get_page(existing.page_id)
    assert original.working_draft is not None
    assert original.working_draft.content_markdown.endswith("Keep this separate.")


def test_engine_ambiguous_adoption_can_queue_reconciliation_with_selected_page(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    generation_id, item_key, _evidence_id = _seed_generated_workspace_item(kb_dir, tmp_path)
    existing = DesktopKnowledgePageService(kb_dir).save_draft(
        page_id=None,
        kind="concept",
        title="Generated Knowledge Guide",
        content_markdown="# Human Guide\n\nReview this before merging.",
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    choice = server._dispatch(
        DesktopRequest(
            request_id="adopt-select-existing",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-select-existing",
            },
        ),
        cancel_event=None,
    )
    queued = server._dispatch(
        DesktopRequest(
            request_id="adopt-selected-existing",
            method="workbench.adopt_knowledge_item",
            params={
                "generation_id": generation_id,
                "item_key": item_key,
                "adoption_request_id": "adoption-selected-existing",
                "adoption_decision": "use_existing",
                "candidate_page_id": existing.page_id,
            },
        ),
        cancel_event=None,
    )

    assert choice["status"] == "choice_required"
    assert queued["status"] == "reconciliation_required"
    conflicts = server._dispatch(
        DesktopRequest(
            request_id="selected-existing-review-queue",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    )["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["target_page_id"] == existing.page_id
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT page_id FROM knowledge_origin_references").fetchone() == (
            existing.page_id,
        )
        assert connection.execute(
            "SELECT decision, candidate_page_id FROM knowledge_adoption_requests "
            "WHERE request_id = 'adoption-selected-existing'"
        ).fetchone() == ("use_existing", existing.page_id)


def test_engine_generated_history_is_explicit_and_default_workspace_is_current(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    first_generation, _, _ = _seed_generated_workspace_item(
        kb_dir, tmp_path, title="Historical Concept", item_key="historical-item"
    )
    current_generation, _, _ = _seed_generated_workspace_item(
        kb_dir, tmp_path, title="Current Concept", item_key="current-item"
    )
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    current = server._dispatch(
        DesktopRequest(
            request_id="current-workspace",
            method="workbench.knowledge_workspace",
            params={},
        ),
        cancel_event=None,
    )
    history = server._dispatch(
        DesktopRequest(
            request_id="workspace-history",
            method="workbench.knowledge_workspace_history",
            params={},
        ),
        cancel_event=None,
    )
    historical = server._dispatch(
        DesktopRequest(
            request_id="historical-generation",
            method="workbench.knowledge_workspace_history",
            params={"generation_id": first_generation},
        ),
        cancel_event=None,
    )

    assert current["current_generation_id"] == current_generation
    assert [item["title"] for item in current["items"]] == ["Current Concept"]
    generations = {item["generation_id"]: item for item in history["generations"]}
    assert generations[first_generation]["current"] is False
    assert generations[current_generation]["current"] is True
    assert generations[first_generation]["item_count"] == 1
    assert [item["title"] for item in historical["items"]] == ["Historical Concept"]
    assert historical["current"] is False


def test_engine_exports_a_typed_knowledge_bundle_to_a_selected_directory(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    destination = tmp_path / "exports"
    destination.mkdir()
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    saved = server._dispatch(
        DesktopRequest(
            request_id="save-export-page",
            method="workbench.save_knowledge_page",
            params={
                "page_id": None,
                "kind": "concept",
                "title": "Exported Knowledge",
                "content_markdown": "Please see [Configuration](configuration.md).",
            },
        ),
        cancel_event=None,
    )
    server._dispatch(
        DesktopRequest(
            request_id="publish-export-page",
            method="workbench.publish_knowledge_page",
            params={"page_id": saved["page_id"]},
        ),
        cancel_event=None,
    )

    exported = server._dispatch(
        DesktopRequest(
            request_id="export-knowledge",
            method="workbench.export_knowledge_bundle",
            params={"destination": str(destination), "mode": "knowledge_projection"},
        ),
        cancel_event=None,
    )

    assert exported["mode"] == "knowledge_projection"
    assert exported["raw_asset_count"] == 0
    assert exported["source_image_count"] == 0
    assert "source-manifest.json" in exported["files"]
    assert Path(exported["path"]).parent == destination


def test_engine_exports_an_explicit_portable_wiki_snapshot(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    destination = tmp_path / "exports"
    destination.mkdir()
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    exported = server._dispatch(
        DesktopRequest(
            request_id="export-portable-wiki",
            method="workbench.export_knowledge_bundle",
            params={"destination": str(destination), "mode": "portable_wiki"},
        ),
        cancel_event=None,
    )

    assert exported["mode"] == "portable_wiki"
    assert exported["raw_asset_count"] == 0
    assert "index.md" in exported["files"]
    assert "wiki-manifest.json" in exported["files"]


def test_engine_exposes_knowledge_lifecycle_and_confirmed_permanent_delete(tmp_path):
    """Lifecycle mutations stay typed behind the workbench boundary."""
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True
    saved = server._dispatch(
        DesktopRequest(
            request_id="save-lifecycle-page",
            method="workbench.save_knowledge_page",
            params={
                "page_id": None,
                "kind": "concept",
                "title": "Lifecycle",
                "content_markdown": "# Lifecycle",
            },
        ),
        cancel_event=None,
    )
    server._dispatch(
        DesktopRequest(
            request_id="publish-lifecycle-page",
            method="workbench.publish_knowledge_page",
            params={"page_id": saved["page_id"]},
        ),
        cancel_event=None,
    )

    stale = server._dispatch(
        DesktopRequest(
            request_id="stale-lifecycle-page",
            method="workbench.set_knowledge_page_stale_after",
            params={
                "page_id": saved["page_id"],
                "stale_after": "2026-01-01T00:00:00+00:00",
            },
        ),
        cancel_event=None,
    )
    deprecated = server._dispatch(
        DesktopRequest(
            request_id="deprecate-lifecycle-page",
            method="workbench.deprecate_knowledge_page",
            params={"page_id": saved["page_id"]},
        ),
        cancel_event=None,
    )
    restored = server._dispatch(
        DesktopRequest(
            request_id="restore-lifecycle-page",
            method="workbench.restore_knowledge_page",
            params={"page_id": saved["page_id"]},
        ),
        cancel_event=None,
    )
    server._dispatch(
        DesktopRequest(
            request_id="deprecate-lifecycle-page-again",
            method="workbench.deprecate_knowledge_page",
            params={"page_id": saved["page_id"]},
        ),
        cancel_event=None,
    )
    deleted = server._dispatch(
        DesktopRequest(
            request_id="delete-lifecycle-page",
            method="workbench.permanently_delete_knowledge_page",
            params={
                "page_id": saved["page_id"],
                "confirmation_page_id": saved["page_id"],
            },
        ),
        cancel_event=None,
    )

    assert stale["is_stale"] is True
    assert deprecated["lifecycle_state"] == "deprecated"
    assert restored["lifecycle_state"] == "stable"
    assert deleted == {"page_id": saved["page_id"], "deleted": True}


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
    verified = server._dispatch(
        DesktopRequest(
            request_id="verify-source-page",
            method="workbench.verify_knowledge_page",
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
    assert verified["verification"] == {
        "state": "human_reviewed",
        "can_verify": False,
        "reason": None,
        "actor": "local_user",
        "verified_at": verified["verification"]["verified_at"],
        "revision_id": verified["verification"]["revision_id"],
    }


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
    assert committed["draft_updated_count"] == 0
    assert server._dispatch(
        DesktopRequest(
            request_id="knowledge-conflicts-after-commit",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    ) == {"conflicts": []}


def test_engine_stages_a_manual_three_way_merge_as_a_working_draft(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Evidence",
        content_markdown="# Published revision",
    )
    published = pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Evidence",
        content_markdown="Working Draft content.",
    )
    source = tmp_path / "incoming.txt"
    source.write_text("# Concept: Evidence\n\nIncoming content.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(source)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    queue = server._dispatch(
        DesktopRequest(
            request_id="three-way-conflicts",
            method="workbench.knowledge_reconciliation_conflicts",
            params={},
        ),
        cancel_event=None,
    )
    conflict = queue["conflicts"][0]
    assert conflict["reconciliation_mode"] == "three_way"
    assert conflict["working_draft_content_markdown"] == "Working Draft content."
    staged = server._dispatch(
        DesktopRequest(
            request_id="stage-manual-merge",
            method="workbench.stage_knowledge_reconciliation_decisions",
            params={
                "candidate_ids": [conflict["candidate_id"]],
                "decision": "manual_merge",
                "manual_merge_content": "Human merged Draft.",
            },
        ),
        cancel_event=None,
    )
    assert staged["conflicts"][0]["staged_content_markdown"] == "Human merged Draft."
    committed = server._dispatch(
        DesktopRequest(
            request_id="commit-manual-merge",
            method="workbench.commit_knowledge_reconciliation_decisions",
            params={},
        ),
        cancel_event=None,
    )

    assert committed["draft_updated_count"] == 1
    current = pages.get_page(page.page_id)
    assert current.published_revision == published.published_revision
    assert current.working_draft is not None
    assert current.working_draft.content_markdown == "Human merged Draft."


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

        def stream_until_terminal(self, request, _connect_timeout_seconds, on_delta):
            if request.operation == "retrieval_plan":
                return '{"terms": ["evidence"]}'
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
