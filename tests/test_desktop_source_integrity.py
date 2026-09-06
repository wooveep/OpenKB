"""Content-free source structure preservation audits."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

from openkb.documents.source_integrity import audit_source_integrity_in
from openkb.importing.service import DesktopTextImportService
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_markdown_structure_loss_is_reported_without_source_content(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "structured.md"
    source.write_text(
        "# Deployment\n\n```bash\ninstall-alpha\n```\n\n"
        "| Node | Role |\n| --- | --- |\n| A | primary |\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute(
            "UPDATE document_ir_blocks SET kind = 'paragraph' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        report = audit_source_integrity_in(connection, kb_dir=kb_dir).as_dict()

    assert report["status"] == "degraded"
    assert report["issues"]["documents_missing_expected_headings"] == 1
    assert report["issues"]["documents_missing_expected_code"] == 1
    assert report["issues"]["documents_missing_expected_tables"] == 1
    assert "install-alpha" not in str(report)


def test_docx_structure_loss_is_reported_from_the_raw_package(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "structured.docx"
    source.write_bytes(_structured_docx())
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute(
            "UPDATE document_ir_blocks SET kind = 'paragraph' WHERE document_id = ?",
            (imported.document.document_id,),
        )
        report = audit_source_integrity_in(connection, kb_dir=kb_dir).as_dict()

    assert report["issues"]["documents_missing_expected_headings"] == 1
    assert report["issues"]["documents_missing_expected_tables"] == 1


def test_locator_regression_range_and_evidence_mismatch_are_reported(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "locators.md"
    source.write_text("# First\n\nBody.\n\n## Second\n\nMore.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        connection.execute(
            "UPDATE document_ir_blocks SET locator_json = ? WHERE document_id = ? AND ordinal = 0",
            (json.dumps({"line_start": 10, "line_end": 2}), imported.document.document_id),
        )
        report = audit_source_integrity_in(connection, kb_dir=kb_dir).as_dict()

    assert report["issues"]["invalid_locator_ranges"] >= 1
    assert report["issues"]["documents_with_locator_regressions"] == 1
    assert report["issues"]["evidence_locator_mismatches"] >= 1


def _structured_docx() -> bytes:
    document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Guide</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Node</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>
</w:styles>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
    return output.getvalue()
