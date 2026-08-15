"""Low-fidelity DOC/PPT coverage at the Desktop Document IR boundary."""

from __future__ import annotations

import json
import sqlite3

import pytest

import openkb.desktop_legacy_office_parsers as legacy_office
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_raw_assets import DesktopRawAssetService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


@pytest.mark.parametrize(("suffix", "source_format"), ((".doc", "doc"), (".ppt", "ppt")))
def test_legacy_office_import_keeps_only_text_and_metadata(
    tmp_path, monkeypatch, suffix, source_format
):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / f"legacy{suffix}"
    raw_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy binary fixture"
    source.write_bytes(raw_bytes)
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    monkeypatch.setattr(
        legacy_office,
        "_extract_with_tika",
        lambda source_path, content: {
            "content": "\nLegacy Office body\nSecond paragraph\n",
            "metadata": {"Author": "OpenKB", "Keywords": ["legacy", "office"]},
        },
    )

    imported = DesktopTextImportService(kb_dir).import_text(source)

    assert imported.document.source_format == source_format
    raw_path = kb_dir / "raw" / f"{imported.document.raw_asset_sha256}{suffix}"
    assert raw_path.read_bytes() == raw_bytes
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        kind, text, locator_json = connection.execute(
            "SELECT kind, text, locator_json FROM document_ir_blocks"
        ).fetchone()
        source_images = connection.execute("SELECT COUNT(*) FROM source_images").fetchone()[0]
    locator = json.loads(locator_json)
    assert kind == "paragraph"
    assert text == "Legacy Office body\nSecond paragraph"
    assert locator == {
        "parser_route": "tika_legacy",
        "fidelity": "low",
        "source_format": source_format,
        "metadata": {"Author": "OpenKB", "Keywords": ["legacy", "office"]},
    }
    assert source_images == 0
    reader = DesktopRawAssetService(kb_dir).read_document(imported.document.document_id)
    assert reader.content == text
    assert reader.source_images == ()


def test_legacy_office_empty_tika_result_is_isolated_with_conversion_advice(tmp_path, monkeypatch):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "empty.ppt"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy binary fixture")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    monkeypatch.setattr(
        legacy_office,
        "_extract_with_tika",
        lambda source_path, content: {"content": "", "metadata": {}},
    )

    with pytest.raises(DesktopImportError) as error:
        importer = DesktopTextImportService(kb_dir)
        importer.import_text(source)

    assert error.value.code == "legacy_office_parse_failed"
    assert error.value.suggested_action == "Convert it to PPTX and import it again."
    assert len(list((kb_dir / "raw").iterdir())) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_documents").fetchone() == (0,)

    history = importer.list_import_jobs()["jobs"]
    assert history[0]["job"]["status"] == "quarantined"
    assert history[0]["document"] is None
    assert history[0]["quarantine"] == {
        "stage_run_id": history[0]["stages"][2]["stage_run_id"],
        "stage": "document_ir",
        "error_code": "legacy_office_parse_failed",
        "reason": "OpenKB could not extract usable text from empty.ppt.",
        "suggested_action": "Convert it to PPTX and import it again.",
        "attempt_count": 1,
    }
