"""Focused behavior checks for the first Desktop-native TXT import path."""

from __future__ import annotations

import sqlite3

import pytest

from openkb import desktop_import
from openkb.desktop_import import DesktopImportError, DesktopTextImportService
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


def test_failed_prepublication_stage_never_exposes_a_partial_document(tmp_path, monkeypatch):
    """A crash-like stage failure can leave raw recovery input but not Available Knowledge."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "broken.txt"
    source.write_text("Known source text.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def fail_document_ir(*_args, **_kwargs):
        raise DesktopImportError("simulated_document_ir_failure", "Simulated Document IR failure.")

    monkeypatch.setattr(desktop_import, "_build_document_ir", fail_document_ir)
    with pytest.raises(DesktopImportError, match="Simulated Document IR failure"):
        DesktopTextImportService(kb_dir).import_text(source)

    assert len(list((kb_dir / "raw").iterdir())) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_fts").fetchone() == (0,)
        assert connection.execute("SELECT status FROM import_jobs").fetchone() == ("failed",)
