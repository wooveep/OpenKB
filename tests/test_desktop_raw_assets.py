"""Focused integrity and reader checks for Desktop Raw Assets."""

from __future__ import annotations

import json
import sqlite3

import pytest

import openkb.documents.raw_assets as desktop_raw_assets
from openkb.documents.raw_assets import DesktopRawAssetService
from openkb.importing.service import DesktopImportError, DesktopTextImportService
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def test_raw_reader_returns_verified_original_and_persists_its_lifecycle(tmp_path):
    """The reader serves the one complete Raw Asset after checking its identity."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    original = "# Original guide\n\nOnly raw stores the complete document.\n"
    source.write_text(original, encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    imported = DesktopTextImportService(kb_dir).import_text(source)
    raw_document = DesktopRawAssetService(kb_dir).read_document(imported.document.document_id)

    assert raw_document.document_id == imported.document.document_id
    assert raw_document.name == "guide.txt"
    assert raw_document.content == original
    assert raw_document.page == 0
    assert raw_document.has_more is False
    assert raw_document.asset_sha256 == imported.document.raw_asset_sha256
    assert raw_document.byte_size == len(original.encode("utf-8"))
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT asset_sha256, byte_size, lifecycle_status, integrity_error_code,
                verified_at
            FROM raw_assets
            JOIN raw_asset_integrity USING (asset_sha256)
            """
        ).fetchone()
        assert row[:4] == (
            imported.document.raw_asset_sha256,
            len(original.encode("utf-8")),
            "available",
            None,
        )
        assert row[4] is not None


def test_raw_reader_pages_a_large_original_below_the_engine_frame_limit(tmp_path, monkeypatch):
    """The reader does not serialize an arbitrarily large original in one Bridge frame."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text("abcdefghi", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    monkeypatch.setattr(desktop_raw_assets, "RAW_DOCUMENT_PAGE_BYTES", 4)
    reader = DesktopRawAssetService(kb_dir)

    first = reader.read_document(imported.document.document_id)
    second = reader.read_document(imported.document.document_id, page=1)
    third = reader.read_document(imported.document.document_id, page=2)

    assert (first.content, first.has_more) == ("abcd", True)
    assert (second.content, second.has_more) == ("efgh", True)
    assert (third.content, third.has_more) == ("i", False)


def test_raw_reader_focus_locator_opens_the_matching_structured_source_page(tmp_path, monkeypatch):
    """A source-image ID selects its figure rather than an earlier matching paragraph."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.md"
    image = tmp_path / "diagram.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsource-image")
    source.write_text(
        "# Guide\n\n" + ("Earlier source text. " * 80) + "![Target image](diagram.png)\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        source_image_id, locator_json = connection.execute(
            "SELECT source_image_id, locator_json FROM source_images"
        ).fetchone()
        locator = json.loads(locator_json)
        assert "source_image_id" not in locator
        locator["source_image_id"] = source_image_id
    monkeypatch.setattr(desktop_raw_assets, "RAW_DOCUMENT_PAGE_BYTES", 128)

    focused = DesktopRawAssetService(kb_dir).read_document(
        imported.document.document_id, focus_locator=locator
    )

    assert focused.page > 0
    assert "[Image: Target image]" in focused.content


def test_raw_reader_focus_locator_opens_the_matching_text_line(tmp_path, monkeypatch):
    """A TXT citation line opens the raw-reader page that contains that line."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_text(("filler\n" * 64) + "\nTarget source line\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    monkeypatch.setattr(desktop_raw_assets, "RAW_DOCUMENT_PAGE_BYTES", 128)

    focused = DesktopRawAssetService(kb_dir).read_document(
        imported.document.document_id, focus_locator={"line_start": 66, "line_end": 66}
    )

    assert focused.page > 0
    assert "Target source line" in focused.content


@pytest.mark.parametrize(
    ("replacement", "expected_error"),
    [
        (None, "raw_asset_missing"),
        (b"short", "raw_asset_size_mismatch"),
        (b"X" * len(b"Original text."), "raw_asset_hash_mismatch"),
    ],
)
def test_unusable_raw_asset_quarantines_its_document(tmp_path, replacement, expected_error):
    """Missing, resized, and altered originals cannot remain available knowledge."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.txt"
    source.write_bytes(b"Original text.")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    raw_path = kb_dir / "raw" / f"{imported.document.raw_asset_sha256}.txt"
    if replacement is None:
        raw_path.unlink()
    else:
        raw_path.write_bytes(replacement)

    reader = DesktopRawAssetService(kb_dir)
    with pytest.raises(DesktopImportError) as error:
        reader.read_document(imported.document.document_id)

    assert error.value.code == expected_error
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT lifecycle_status, integrity_error_code FROM raw_asset_integrity"
        ).fetchone() == ("quarantined", expected_error)
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == ("failed",)

    with pytest.raises(DesktopImportError) as quarantined:
        reader.read_document(imported.document.document_id)
    assert quarantined.value.code == "raw_asset_quarantined"
