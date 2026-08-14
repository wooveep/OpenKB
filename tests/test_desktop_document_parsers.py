"""Focused Markdown/DOCX import coverage for the Desktop Document IR boundary."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_raw_assets import DesktopRawAssetService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_markdown_import_retains_structured_ir_and_local_source_image(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.md"
    image = tmp_path / "diagram.png"
    table_image = tmp_path / "table-diagram.png"
    image_bytes = b"\x89PNG\r\n\x1a\nsource-image"
    table_image_bytes = b"\x89PNG\r\n\x1a\ntable-source-image"
    image.write_bytes(image_bytes)
    table_image.write_bytes(table_image_bytes)
    source.write_text(
        "### Guide\n\nParagraph.\n\n- first\n- second\n\n```python\nprint('ok')\n```\n\n"
        "| Name | Value |\n| --- | --- |\n| OpenKB | ![Table diagram](table-diagram.png) |\n\n"
        "![System diagram](diagram.png)\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    imported = DesktopTextImportService(kb_dir).import_text(source)

    assert imported.document.source_format == "markdown"
    raw_path = kb_dir / "raw" / f"{imported.document.raw_asset_sha256}.md"
    assert raw_path.read_bytes() == source.read_bytes()
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        kinds = [
            row[0]
            for row in connection.execute("SELECT kind FROM document_ir_blocks ORDER BY ordinal")
        ]
        heading_locator = json.loads(
            connection.execute(
                "SELECT locator_json FROM document_ir_blocks WHERE kind = 'heading'"
            ).fetchone()[0]
        )
        figure_locators = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT locator_json FROM document_ir_blocks WHERE kind = 'figure' ORDER BY ordinal"
            )
        ]
        image_rows = connection.execute(
            "SELECT display_name, storage_path FROM source_images ORDER BY ordinal"
        ).fetchall()
    assert {"heading", "paragraph", "list", "code", "table", "figure"}.issubset(kinds)
    assert heading_locator["heading_level"] == 3
    assert all(locator["source_image_id"] for locator in figure_locators)
    assert [(kb_dir / row[1]).read_bytes() for row in image_rows] == [
        table_image_bytes,
        image_bytes,
    ]

    reader = DesktopRawAssetService(kb_dir).read_document(imported.document.document_id)
    assert "### Guide" in reader.content
    assert "```python" in reader.content
    assert "| Name | Value |" in reader.content
    assert [(item.name, item.alt_text) for item in reader.source_images] == [
        ("table-diagram.png", "Table diagram"),
        ("diagram.png", "System diagram"),
    ]


def test_docx_import_keeps_body_order_coordinates_and_embedded_source_image(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "guide.docx"
    image_bytes = b"\x89PNG\r\n\x1a\nembedded-image"
    source.write_bytes(_minimal_docx(image_bytes))
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    imported = DesktopTextImportService(kb_dir).import_text(source)

    assert imported.document.source_format == "docx"
    raw_path = kb_dir / "raw" / f"{imported.document.raw_asset_sha256}.docx"
    assert raw_path.read_bytes() == source.read_bytes()
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            "SELECT kind, text, locator_json FROM document_ir_blocks ORDER BY ordinal"
        ).fetchall()
        image_row = connection.execute(
            "SELECT display_name, storage_path FROM source_images"
        ).fetchone()
    assert [row[0] for row in rows] == ["heading", "paragraph", "table", "figure"]
    assert json.loads(rows[0][2])["paragraph"] == 1
    assert json.loads(rows[0][2])["heading_level"] == 3
    assert json.loads(rows[2][2])["table"] == 1
    assert json.loads(rows[3][2])["source_image_id"]
    assert image_row[0] == "diagram.png"
    assert (kb_dir / image_row[1]).read_bytes() == image_bytes

    reader = DesktopRawAssetService(kb_dir).read_document(imported.document.document_id)
    assert "### Overview" in reader.content
    assert "| Name | Value |" in reader.content
    assert len(reader.source_images) == 1
    assert reader.source_images[0].name == "diagram.png"


def _minimal_docx(image_bytes: bytes) -> bytes:
    document = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"
 xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"
 xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val=\"Heading3\"/></w:pPr><w:r><w:t>Overview</w:t></w:r></w:p>
    <w:p><w:r><w:t>Body paragraph.</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>OpenKB</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>local</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:drawing><a:blip r:embed=\"rIdImage1\"/></w:drawing></w:r></w:p>
  </w:body>
</w:document>"""
    relationships = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">
  <Relationship Id=\"rIdImage1\" Target=\"media/diagram.png\" />
</Relationships>"""
    styles = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<w:styles xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:style w:type=\"paragraph\" w:styleId=\"Heading3\"><w:name w:val=\"Heading 3\"/></w:style>
</w:styles>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/media/diagram.png", image_bytes)
    return output.getvalue()
