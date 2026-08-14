"""Focused behavior checks for the first Desktop-native TXT import path."""

from __future__ import annotations

import sqlite3
import threading
from hashlib import sha256

import pytest

from openkb import desktop_import_runner
from openkb.desktop_import import DesktopImportControl, DesktopImportError, DesktopTextImportService
from openkb.desktop_import_store import DesktopImportStore
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_txt_import_publishes_raw_ir_evidence_and_fts_in_one_available_document(tmp_path):
    """A successful TXT import produces every retrieval baseline artifact exactly once."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text(
        "# Getting started\n\nOpenKB keeps local knowledge searchable.\n", encoding="utf-8"
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    events: list[dict[str, object]] = []

    result = DesktopTextImportService(kb_dir, on_stage_progress=events.append).import_text(source)

    assert result.document.name == "guide.txt"
    assert result.document.availability == "available"
    assert result.document.evidence_count == 2
    assert result.job.status == "completed"
    assert [stage.status for stage in result.stages] == ["completed"] * 5
    assert [(event["stage"], event["status"]) for event in events] == [
        ("preflight", "running"),
        ("preflight", "completed"),
        ("raw_asset", "running"),
        ("raw_asset", "completed"),
        ("document_ir", "running"),
        ("document_ir", "completed"),
        ("evidence", "running"),
        ("evidence", "completed"),
        ("search", "running"),
        ("search", "completed"),
    ]

    raw_files = list((kb_dir / "raw").iterdir())
    assert [path.name for path in raw_files] == [f"{result.document.raw_asset_sha256}.txt"]
    assert raw_files[0].read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_assets").fetchone() == (1,)
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (result.document.document_id,),
        ).fetchone() == ("available",)
        assert connection.execute("SELECT COUNT(*) FROM document_ir_blocks").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_refs").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_fts WHERE evidence_fts MATCH 'searchable'"
        ).fetchone() == (1,)


def test_duplicate_txt_reuses_the_single_available_raw_asset(tmp_path):
    """D0-identical input is immediately available without another source document."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "same.txt"
    source.write_text("Same content.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    importer = DesktopTextImportService(kb_dir)

    first = importer.import_text(source)
    second = importer.import_text(source)

    assert second.job.deduplicated is True
    assert second.document.document_id == first.document.document_id
    assert [stage.status for stage in second.stages] == [
        "completed",
        "completed",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert len(list((kb_dir / "raw").iterdir())) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (1,)

    history = importer.list_import_jobs()["jobs"]
    assert history[0]["job"]["deduplicated"] is True


def test_failed_prepublication_stage_never_exposes_a_partial_document(tmp_path, monkeypatch):
    """A crash-like stage failure can leave raw recovery input but not Available Knowledge."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "broken.txt"
    source.write_text("Known source text.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def fail_document_ir(*_args, **_kwargs):
        raise DesktopImportError("simulated_document_ir_failure", "Simulated Document IR failure.")

    monkeypatch.setattr(desktop_import_runner, "build_document_ir", fail_document_ir)
    with pytest.raises(DesktopImportError, match="Simulated Document IR failure"):
        DesktopTextImportService(kb_dir).import_text(source)

    assert len(list((kb_dir / "raw").iterdir())) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_fts").fetchone() == (0,)
        assert connection.execute("SELECT status FROM import_jobs").fetchone() == ("failed",)


def test_open_recovers_an_interrupted_job_without_exposing_a_partial_document(tmp_path):
    """Restarting exposes a pre-publication crash as durable recoverable work."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "interrupted.txt"
    source.write_text("Recoverable raw source.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    store.set_stage(
        state,
        "preflight",
        "completed",
        20,
        checkpoint={"asset_sha256": "a" * 64, "raw_size": len(source.read_bytes())},
    )
    raw_path = store.write_raw_asset("a" * 64, source.read_bytes())
    store.set_stage(
        state,
        "raw_asset",
        "completed",
        35,
        checkpoint={
            "asset_sha256": "a" * 64,
            "raw_path": raw_path,
            "raw_size": len(source.read_bytes()),
        },
    )

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    history = store.list_import_jobs()["jobs"]
    assert history[0]["job"]["status"] == "recoverable"
    assert history[0]["document"] is None
    assert [stage["status"] for stage in history[0]["stages"]] == [
        "completed",
        "completed",
        "pending",
        "pending",
        "pending",
    ]
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)


def test_resume_revalidates_preflight_when_the_source_changed_before_raw_asset(tmp_path):
    """A stale preflight checkpoint cannot certify a later version of a source file."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "changed.txt"
    original = b"Original source."
    replacement = b"Replacement source."
    source.write_bytes(original)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    store.set_stage(
        state,
        "preflight",
        "completed",
        20,
        checkpoint={"asset_sha256": sha256(original).hexdigest(), "raw_size": len(original)},
    )
    store.pause_job(state, "raw_asset")
    source.write_bytes(replacement)

    result = DesktopTextImportService(kb_dir).resume_text(state.job_id)

    assert result.document.raw_asset_sha256 == sha256(replacement).hexdigest()
    assert (kb_dir / "raw" / f"{sha256(replacement).hexdigest()}.txt").read_bytes() == replacement


def test_open_marks_an_expired_running_lease_as_recoverable(tmp_path):
    """A restart labels an expired worker lease distinctly from an abrupt handoff."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "expired.txt"
    source.write_text("Lease recovery.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopImportStore(kb_dir)
    state = store.create_job(source)
    store.set_stage(state, "preflight", "running", 0)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE import_job_runtime SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
        )

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    task = DesktopTextImportService(kb_dir).task(state.job_id)
    assert task.job.status == "recoverable"
    assert task.stages[0].status == "paused"
    assert task.stages[0].error_code == "import_lease_expired"


def test_open_waits_for_a_live_import_instead_of_recovering_it(tmp_path, monkeypatch):
    """A second Engine cannot misclassify a lock-owning import as a crash."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "live.txt"
    source.write_text("# Live import\n\nStill working.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document_ir_started = threading.Event()
    release_import = threading.Event()
    open_finished = threading.Event()
    outcomes: list[object] = []
    original_build_document_ir = desktop_import_runner.build_document_ir

    def wait_before_building_document_ir(*args, **kwargs):
        document_ir_started.set()
        assert release_import.wait(timeout=2)
        return original_build_document_ir(*args, **kwargs)

    monkeypatch.setattr(
        desktop_import_runner, "build_document_ir", wait_before_building_document_ir
    )

    def import_in_background() -> None:
        try:
            outcomes.append(DesktopTextImportService(kb_dir).import_text(source))
        except BaseException as error:  # Captured to make the thread assertion deterministic.
            outcomes.append(error)

    def open_in_background() -> None:
        try:
            outcomes.append(DesktopKnowledgeBaseRuntime().open(kb_dir))
        except BaseException as error:  # Captured to make the thread assertion deterministic.
            outcomes.append(error)
        finally:
            open_finished.set()

    importer_thread = threading.Thread(target=import_in_background)
    importer_thread.start()
    assert document_ir_started.wait(timeout=2)
    opener_thread = threading.Thread(target=open_in_background)
    opener_thread.start()
    assert not open_finished.wait(timeout=0.1)
    release_import.set()
    importer_thread.join(timeout=2)
    opener_thread.join(timeout=2)

    assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
    history = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"]
    assert [task["job"]["status"] for task in history] == ["completed"]


def test_paused_import_resumes_from_verified_document_ir_checkpoint(tmp_path, monkeypatch):
    """A resumed job does not rerun the completed Document IR stage."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "resume.txt"
    source.write_text("# Resume\n\nKeep this completed stage.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    control = DesktopImportControl()
    calls = 0
    original_build_document_ir = desktop_import_runner.build_document_ir

    def pause_after_document_ir(*args, **kwargs):
        nonlocal calls
        calls += 1
        blocks = original_build_document_ir(*args, **kwargs)
        control.request_pause()
        return blocks

    monkeypatch.setattr(desktop_import_runner, "build_document_ir", pause_after_document_ir)
    with pytest.raises(DesktopImportError) as paused:
        DesktopTextImportService(kb_dir, control=control).import_text(source)
    assert paused.value.code == "import_paused"

    paused_task = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]
    assert paused_task["job"]["status"] == "paused"
    assert [stage["status"] for stage in paused_task["stages"]] == [
        "completed",
        "completed",
        "completed",
        "paused",
        "pending",
    ]

    resumed = DesktopTextImportService(kb_dir).resume_text(paused_task["job"]["job_id"])

    assert resumed.job.status == "completed"
    assert calls == 1
    assert [stage.status for stage in resumed.stages] == ["completed"] * 5


def test_cancelled_paused_import_never_becomes_available_knowledge(tmp_path, monkeypatch):
    """A user can cancel after pause without publishing pre-publication artifacts."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "cancel.txt"
    source.write_text("# Cancel\n\nLeave this unavailable.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    control = DesktopImportControl()
    original_build_document_ir = desktop_import_runner.build_document_ir

    def pause_after_document_ir(*args, **kwargs):
        blocks = original_build_document_ir(*args, **kwargs)
        control.request_pause()
        return blocks

    monkeypatch.setattr(desktop_import_runner, "build_document_ir", pause_after_document_ir)
    with pytest.raises(DesktopImportError, match="paused"):
        DesktopTextImportService(kb_dir, control=control).import_text(source)

    job_id = DesktopTextImportService(kb_dir).list_import_jobs()["jobs"][0]["job"]["job_id"]
    DesktopTextImportService(kb_dir).cancel_paused_job(job_id)

    cancelled = DesktopTextImportService(kb_dir).task(job_id)
    assert cancelled.job.status == "cancelled"
    assert cancelled.document is None
    assert cancelled.stages[3].status == "cancelled"
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
