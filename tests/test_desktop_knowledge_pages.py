"""Behavior checks for SQLite-authoritative Desktop Concept and Entity pages."""

from __future__ import annotations

import sqlite3

from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_user_revision_is_authoritative_and_re_materializes_markdown_after_reopen(tmp_path):
    """A saved page survives restart from SQLite even when its derived file is absent."""
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)

    created = pages.save_page(
        page_id=None,
        kind="concept",
        title="Eventual Consistency",
        content_markdown="The **first** user revision.",
    )
    revised = pages.save_page(
        page_id=created.page_id,
        kind="concept",
        title="Eventual Consistency",
        content_markdown="The **current** user revision.",
    )

    projection = kb_dir / revised.materialized_path
    assert revised.revision_number == 2
    assert 'authority: "user_revision"' in projection.read_text(encoding="utf-8")
    assert "The **current** user revision." in projection.read_text(encoding="utf-8")

    projection.unlink()
    reopened = DesktopKnowledgePageService(kb_dir)
    reopened.materialize_current_pages()

    assert reopened.get_page(created.page_id) == revised
    assert "The **current** user revision." in projection.read_text(encoding="utf-8")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_page_revisions WHERE page_id = ?", (created.page_id,)
        ).fetchone() == (2,)


def test_later_document_import_does_not_overwrite_a_submitted_user_revision(tmp_path):
    """Document ingestion has no write path into the user-revision authority tables."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "new-source.txt"
    source.write_text("Imported document content.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_page(
        page_id=None,
        kind="entity",
        title="OpenKB",
        content_markdown="This is the user-owned description.",
    )

    DesktopTextImportService(kb_dir).import_text(source)

    current = pages.get_page(page.page_id)
    assert current.revision_number == 1
    assert current.content_markdown == "This is the user-owned description."
