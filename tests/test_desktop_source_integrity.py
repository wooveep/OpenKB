"""Content-free source structure preservation audits."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_source_integrity import audit_source_integrity_in
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


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
