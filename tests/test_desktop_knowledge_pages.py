"""Behavior checks for SQLite-authoritative Desktop Concept and Entity pages."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

import openkb.desktop_knowledge_pages as knowledge_pages_module
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_working_draft_does_not_replace_published_revision_until_explicit_publish(tmp_path):
    """Autosave is durable, while readers keep seeing the last explicit publication."""
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)

    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Eventual Consistency",
        content_markdown="The **first** working draft.",
    )
    assert draft.publication_state == "draft"
    assert draft.published_revision is None
    assert draft.working_draft is not None
    assert not (kb_dir / draft.materialized_path).exists()

    published = pages.publish(draft.page_id)
    revised = pages.save_draft(
        page_id=draft.page_id,
        kind="concept",
        title="Eventual Consistency",
        content_markdown="The **second** working draft.",
    )

    projection = kb_dir / published.materialized_path
    assert revised.publication_state == "unpublished_changes"
    assert revised.published_revision is not None
    assert revised.published_revision.revision_number == 1
    assert revised.published_revision.content_markdown == "The **first** working draft."
    assert revised.working_draft is not None
    assert revised.working_draft.content_markdown == "The **second** working draft."
    assert 'authority: "user_revision"' in projection.read_text(encoding="utf-8")
    assert "The **first** working draft." in projection.read_text(encoding="utf-8")

    projection.unlink()
    reopened = DesktopKnowledgePageService(kb_dir)
    reopened.materialize_current_pages()

    assert reopened.get_page(draft.page_id) == revised
    assert "The **first** working draft." in projection.read_text(encoding="utf-8")

    republished = reopened.publish(draft.page_id)

    assert republished.publication_state == "published"
    assert republished.working_draft is None
    assert republished.published_revision is not None
    assert republished.published_revision.revision_number == 2
    assert "The **second** working draft." in projection.read_text(encoding="utf-8")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_page_revisions WHERE page_id = ?", (draft.page_id,)
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_page_working_drafts WHERE page_id = ?",
            (draft.page_id,),
        ).fetchone() == (0,)


def test_projection_staging_failure_preserves_published_revision_and_working_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pre-commit projection never consumes the editable draft."""
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    created = pages.save_draft(
        page_id=None,
        kind="entity",
        title="OpenKB",
        content_markdown="Published content.",
    )
    pages.publish(created.page_id)
    pages.save_draft(
        page_id=created.page_id,
        kind="entity",
        title="OpenKB",
        content_markdown="Unpublished content.",
    )

    def fail_projection(*_args: object, **_kwargs: object) -> Path:
        raise OSError("projection unavailable")

    monkeypatch.setattr(knowledge_pages_module, "stage_knowledge_page_projection", fail_projection)

    with pytest.raises(OSError, match="projection unavailable"):
        pages.publish(created.page_id)

    preserved = pages.get_page(created.page_id)
    assert preserved.published_revision is not None
    assert preserved.published_revision.content_markdown == "Published content."
    assert preserved.working_draft is not None
    assert preserved.working_draft.content_markdown == "Unpublished content."


def test_v18_page_migrates_as_published_without_inventing_a_working_draft(tmp_path):
    """Existing user content remains the current publication after schema upgrade."""
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    page_id = uuid.uuid4().hex
    revision_id = uuid.uuid4().hex
    now = "2026-08-19T00:00:00+00:00"
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE IF EXISTS knowledge_page_ui_state")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_working_drafts")
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        connection.execute(
            """
            INSERT INTO knowledge_pages (
                page_id, kind, title, normalized_title, materialized_path,
                current_revision_id, created_at, updated_at
            ) VALUES (?, 'concept', 'Legacy Page', 'legacy page', ?, ?, ?, ?)
            """,
            (page_id, f"knowledge-pages/concept/{page_id}.md", revision_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO knowledge_page_revisions (
                revision_id, page_id, revision_number, title, content_markdown, created_at
            ) VALUES (?, ?, 1, 'Legacy Page', 'Legacy published content.', ?)
            """,
            (revision_id, page_id, now),
        )
        connection.commit()

    DesktopKnowledgeBaseRuntime().open(kb_dir)
    migrated = DesktopKnowledgePageService(kb_dir).get_page(page_id)

    assert migrated.publication_state == "published"
    assert migrated.published_revision is not None
    assert migrated.published_revision.content_markdown == "Legacy published content."
    assert migrated.working_draft is None


def test_later_document_import_does_not_overwrite_a_submitted_user_revision(tmp_path):
    """Document ingestion has no write path into the user-revision authority tables."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "new-source.txt"
    source.write_text("Imported document content.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="entity",
        title="OpenKB",
        content_markdown="This is the user-owned description.",
    )
    pages.publish(page.page_id)

    DesktopTextImportService(kb_dir).import_text(source)

    current = pages.get_page(page.page_id)
    assert current.published_revision is not None
    assert current.published_revision.revision_number == 1
    assert current.published_revision.content_markdown == "This is the user-owned description."
