"""Focused PDF import coverage for the Desktop Document IR boundary."""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO

import pymupdf
import pytest
from PIL import Image as PillowImage
from PIL import ImageDraw, ImageFont

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_raw_assets import DesktopRawAssetService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_text_pdf_uses_fast_route_and_keeps_page_table_and_source_image(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "report.pdf"
    raw_bytes = _text_pdf_with_table_and_image()
    source.write_bytes(raw_bytes)
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    imported = DesktopTextImportService(kb_dir).import_text(source)

    assert imported.document.source_format == "pdf"
    raw_path = kb_dir / "raw" / f"{imported.document.raw_asset_sha256}.pdf"
    assert raw_path.read_bytes() == raw_bytes
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            "SELECT kind, text, locator_json FROM document_ir_blocks ORDER BY ordinal"
        ).fetchall()
        image_path, image_locator_json = connection.execute(
            "SELECT storage_path, locator_json FROM source_images"
        ).fetchone()

    locators = [(kind, text, json.loads(locator)) for kind, text, locator in rows]
    paragraph = next(locator for kind, _, locator in locators if kind == "paragraph")
    table = next(locator for kind, _, locator in locators if kind == "table")
    assert paragraph["page"] == 1
    assert paragraph["bbox"]
    assert paragraph["parser_route"] == "pymupdf_fast"
    assert table["page"] == 1
    assert table["bbox"]
    assert table["row_count"] == 3
    assert table["column_count"] == 2
    image_locator = json.loads(image_locator_json)
    assert image_locator["page"] == 1
    assert image_locator["bbox"]
    assert (kb_dir / image_path).read_bytes().startswith(b"\x89PNG")

    reader = DesktopRawAssetService(kb_dir).read_document(imported.document.document_id)
    assert "# Page 1" in reader.content
    assert "| Name | Value |" in reader.content
    assert len(reader.source_images) == 1
    assert not imported.model_calls


def test_scanned_pdf_uses_bundled_onnx_ocr_without_model_calls(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "scan.pdf"
    source.write_bytes(_scanned_pdf())
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    imported = DesktopTextImportService(kb_dir).import_text(source)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            "SELECT kind, text, locator_json FROM document_ir_blocks ORDER BY ordinal"
        ).fetchall()
    ocr_rows = [(kind, text, json.loads(locator)) for kind, text, locator in rows]
    table = next(locator for kind, _, locator in ocr_rows if kind == "table")
    assert "OpenKB" in " ".join(text for _, text, _ in ocr_rows)
    assert table["parser_route"] == "bundled_onnx_ocr"
    assert table["table_strategy"] == "onnx_ocr_cell_geometry"
    assert table["bbox"]
    assert table["row_count"] == 3
    assert table["column_count"] == 2
    assert not imported.model_calls


def test_corrupt_pdf_has_a_stable_import_failure(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not a PDF")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    with pytest.raises(DesktopImportError) as error:
        DesktopTextImportService(kb_dir).import_text(source)

    assert error.value.code == "invalid_pdf_document"


def _text_pdf_with_table_and_image() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(
        pymupdf.Rect(72, 72, 500, 170),
        "OpenKB Desktop retains PDF text as citable evidence. "
        "This report has enough readable source text to remain on the fast parser route. "
        "It also includes a table and a source image for the reader.",
        fontsize=11,
    )
    for x in (72, 200, 350):
        page.draw_line((x, 220), (x, 310))
    for y in (220, 250, 280, 310):
        page.draw_line((72, y), (350, y))
    for text, point in (
        ("Name", (80, 240)),
        ("Value", (210, 240)),
        ("OpenKB", (80, 270)),
        ("Desktop", (210, 270)),
        ("Parser", (80, 300)),
        ("Fast", (210, 300)),
    ):
        page.insert_text(point, text)
    image = BytesIO()
    PillowImage.new("RGB", (16, 16), "blue").save(image, format="PNG")
    page.insert_image(pymupdf.Rect(400, 220, 460, 280), stream=image.getvalue())
    result = document.tobytes()
    document.close()
    return result


def _scanned_pdf() -> bytes:
    image = PillowImage.new("RGB", (1200, 700), "white")
    drawing = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    for x in (100, 550, 1100):
        drawing.line((x, 100, x, 550), fill="black", width=5)
    for y in (100, 250, 400, 550):
        drawing.line((100, y, 1100, y), fill="black", width=5)
    for text, x, y in (
        ("Name", 140, 150),
        ("Value", 600, 150),
        ("OpenKB", 140, 300),
        ("Desktop", 600, 300),
        ("Parser", 140, 450),
        ("Enhanced", 600, 450),
    ):
        drawing.text((x, y), text, fill="black", font=font)
    image_bytes = BytesIO()
    image.save(image_bytes, format="PNG")
    document = pymupdf.open()
    page = document.new_page(width=1200, height=700)
    page.insert_image(page.rect, stream=image_bytes.getvalue())
    result = document.tobytes()
    document.close()
    return result
