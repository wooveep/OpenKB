"""Behavior checks for SQLite-authoritative Desktop Concept and Entity pages."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

import openkb.desktop_knowledge_pages as knowledge_pages_module
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_pages import (
    DesktopKnowledgePageError,
    DesktopKnowledgePageService,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever
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
        content_markdown="# First working draft",
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
        content_markdown="# Second working draft",
    )

    projection = kb_dir / published.materialized_path
    assert revised.publication_state == "unpublished_changes"
    assert revised.published_revision is not None
    assert revised.published_revision.revision_number == 1
    assert revised.published_revision.content_markdown == "# First working draft"
    assert revised.working_draft is not None
    assert revised.working_draft.content_markdown == "# Second working draft"
    assert 'authority: "user_revision"' in projection.read_text(encoding="utf-8")
    assert "# First working draft" in projection.read_text(encoding="utf-8")

    projection.unlink()
    reopened = DesktopKnowledgePageService(kb_dir)
    reopened.materialize_current_pages()

    assert reopened.get_page(draft.page_id) == revised
    assert "# First working draft" in projection.read_text(encoding="utf-8")

    republished = reopened.publish(draft.page_id)

    assert republished.publication_state == "published"
    assert republished.working_draft is None
    assert republished.published_revision is not None
    assert republished.published_revision.revision_number == 2
    assert "# Second working draft" in projection.read_text(encoding="utf-8")
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
        content_markdown="# Published content",
    )
    pages.publish(created.page_id)
    pages.save_draft(
        page_id=created.page_id,
        kind="entity",
        title="OpenKB",
        content_markdown="# Unpublished content",
    )

    def fail_projection(*_args: object, **_kwargs: object) -> Path:
        raise OSError("projection unavailable")

    monkeypatch.setattr(knowledge_pages_module, "stage_knowledge_page_projection", fail_projection)

    with pytest.raises(OSError, match="projection unavailable"):
        pages.publish(created.page_id)

    preserved = pages.get_page(created.page_id)
    assert preserved.published_revision is not None
    assert preserved.published_revision.content_markdown == "# Published content"
    assert preserved.working_draft is not None
    assert preserved.working_draft.content_markdown == "# Unpublished content"


def test_v18_page_migrates_as_published_without_inventing_a_working_draft(tmp_path):
    """Existing user content remains the current publication after schema upgrade."""
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    page_id = uuid.uuid4().hex
    revision_id = uuid.uuid4().hex
    now = "2026-08-19T00:00:00+00:00"
    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE IF EXISTS knowledge_page_revision_sources")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_working_sources")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_ui_state")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_working_drafts")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (19, 20)")
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
    assert migrated.published_revision.source_map == ()
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
        content_markdown="# User-owned description",
    )
    pages.publish(page.page_id)

    DesktopTextImportService(kb_dir).import_text(source)

    current = pages.get_page(page.page_id)
    assert current.published_revision is not None
    assert current.published_revision.revision_number == 1
    assert current.published_revision.content_markdown == "# User-owned description"


def test_one_source_backed_claim_routes_only_to_its_available_original_evidence(tmp_path):
    """Knowledge wording may route retrieval, but only original Evidence enters the pack."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "operations.md"
    source.write_text(
        "# Recovery policy\n\nThe worker makes four total attempts before isolation.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    claim = "OpenKB retries a failed analysis three times after the initial request."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Analysis recovery",
        content_markdown=f"# Analysis recovery\n\n{claim}",
    )

    candidates = pages.search_sources("operations Recovery policy")
    candidate = next(item for item in candidates if "four total attempts" in item.excerpt)
    assert candidate.document_id == imported.document.document_id
    assert candidate.document_name == "operations.md"
    assert candidate.section == "Recovery policy"

    bound = pages.bind_source(draft.page_id, claim, candidate.evidence_id)
    assert bound.working_draft is not None
    assert len(bound.working_draft.source_map) == 1
    source_entry = bound.working_draft.source_map[0]
    assert source_entry.source_id.startswith("src-")
    assert source_entry.evidence_id == candidate.evidence_id
    assert f"{claim}[^{source_entry.source_id}]" in bound.working_draft.content_markdown
    assert bound.publication_diagnostics == ()

    published = pages.publish(draft.page_id)
    assert published.published_revision is not None
    assert published.published_revision.source_map == (source_entry,)

    pack = DesktopEvidenceRetriever(kb_dir).retrieve("Which analysis retries happen?")
    routed = next(item for item in pack.evidence if item.evidence_id == source_entry.evidence_id)
    assert "knowledge_source" in routed.channels
    assert routed.excerpt == "The worker makes four total attempts before isolation."
    assert claim not in routed.excerpt

    edited = pages.save_draft(
        page_id=draft.page_id,
        kind="concept",
        title="Analysis recovery",
        content_markdown=(
            f"# Analysis recovery\n\nOpenKB never retries analysis.[^{source_entry.source_id}]"
        ),
    )
    assert {item.code for item in edited.publication_diagnostics} == {
        "knowledge_source_claim_mismatch"
    }
    with pytest.raises(DesktopKnowledgePageError) as mismatch_error:
        pages.publish(draft.page_id)
    assert mismatch_error.value.code == "knowledge_publication_blocked"

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (imported.document.document_id,),
        )
    unavailable_pack = DesktopEvidenceRetriever(kb_dir).retrieve("Which analysis retries happen?")
    assert source_entry.evidence_id not in {item.evidence_id for item in unavailable_pack.evidence}


def test_source_search_matches_a_noncanonical_available_d2_occurrence(tmp_path):
    """Document-name search chooses the matching occurrence before canonical deduplication."""
    kb_dir = tmp_path / "desktop-kb"
    alpha = tmp_path / "alpha-special.md"
    bravo = tmp_path / "bravo-target.md"
    shared = "The shared recovery fact remains canonical across document versions."
    alpha.write_text(f"# Alpha\n\n{shared}", encoding="utf-8")
    bravo.write_text(f"# Bravo\n\n{shared}\n\nBravo-only detail.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(alpha)
    DesktopTextImportService(kb_dir).import_text(bravo)

    matches = DesktopKnowledgePageService(kb_dir).search_sources("bravo-target")

    shared_match = next(item for item in matches if item.excerpt == shared)
    assert shared_match.document_name == "bravo-target.md"


def test_publication_gate_preserves_draft_with_missing_or_unresolved_source_marker(tmp_path):
    """A broken binding remains editable and can never replace the current publication."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "source.md"
    source.write_text("# Facts\n\nThe source-backed value is 42.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = pages.search_sources("source-backed")[0]
    claim = "The answer is forty-two."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Answer",
        content_markdown=claim,
    )
    bound = pages.bind_source(draft.page_id, claim, candidate.evidence_id)
    source_id = bound.working_draft.source_map[0].source_id
    pages.publish(bound.page_id)

    partial = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Partial selection",
        content_markdown="The production timeout is exactly 37 seconds.",
    )
    with pytest.raises(DesktopKnowledgePageError) as partial_error:
        pages.bind_source(partial.page_id, "timeout", candidate.evidence_id)
    assert partial_error.value.code == "knowledge_claim_selection_invalid"

    misplaced = pages.save_draft(
        page_id=bound.page_id,
        kind="concept",
        title="Answer",
        content_markdown=f"# {claim}[^{source_id}]",
    )
    assert {item.code for item in misplaced.publication_diagnostics} == {
        "knowledge_source_claim_mismatch"
    }
    drifted = pages.save_draft(
        page_id=bound.page_id,
        kind="concept",
        title="Answer",
        content_markdown=f"{claim} This additional claim is unsupported.[^{source_id}]",
    )
    assert {item.code for item in drifted.publication_diagnostics} == {
        "knowledge_source_claim_mismatch"
    }

    revised = pages.save_draft(
        page_id=bound.page_id,
        kind="concept",
        title="Answer",
        content_markdown=f"{claim}\n\n[^src-does-not-exist]",
    )
    assert {item.code for item in revised.publication_diagnostics} == {
        "knowledge_claim_source_missing",
        "knowledge_source_marker_missing",
        "knowledge_source_unresolved",
    }

    with pytest.raises(DesktopKnowledgePageError) as exc_info:
        pages.publish(bound.page_id)
    assert exc_info.value.code == "knowledge_publication_blocked"

    preserved = pages.get_page(bound.page_id)
    assert preserved.publication_state == "unpublished_changes"
    assert preserved.published_revision is not None
    assert preserved.published_revision.content_markdown != revised.working_draft.content_markdown
    assert preserved.working_draft == revised.working_draft

    structural = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Navigation",
        content_markdown=(
            "# Navigation\n\n"
            "See [Configuration](configuration.md) for details.\n\n"
            "Continue to the next section."
        ),
    )
    assert pages.publish(structural.page_id).publication_state == "published"

    unsourced = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Unbound fact",
        content_markdown="The production timeout is exactly 37 seconds.",
    )
    assert {item.code for item in unsourced.publication_diagnostics} == {
        "knowledge_claim_source_missing"
    }
    with pytest.raises(DesktopKnowledgePageError) as missing_error:
        pages.publish(unsourced.page_id)
    assert missing_error.value.code == "knowledge_publication_blocked"

    factual_link = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Linked fact",
        content_markdown=(
            "See [the production timeout is exactly 37 seconds](details.md).\n\n"
            "[OpenKB retries failed analyses](details.md).\n\n"
            "Please see [analysis retries automatically](details.md).\n\n"
            "请参见[OpenKB采用SQLite](details.md)。"
        ),
    )
    assert {item.code for item in factual_link.publication_diagnostics} == {
        "knowledge_claim_source_missing"
    }

    polite_navigation = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Polite navigation",
        content_markdown=(
            "# Navigation\n\n"
            "Please see [Configuration](configuration.md).\n\n"
            "请参见[配置](configuration.md)。"
        ),
    )
    assert polite_navigation.publication_diagnostics == ()
    assert pages.publish(polite_navigation.page_id).publication_state == "published"

    with pytest.raises(DesktopKnowledgePageError) as query_error:
        pages.search_sources(" ".join(f"term{index}" for index in range(400)))
    assert query_error.value.code == "knowledge_source_query_invalid"


def test_source_map_and_markdown_roll_back_together_when_publication_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "facts.txt"
    source.write_text("Original evidence for the claim.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = pages.search_sources("Original evidence")[0]
    claim = "This is a source-backed claim."
    draft = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Atomic source map",
        content_markdown=claim,
    )
    bound = pages.bind_source(draft.page_id, claim, candidate.evidence_id)

    def fail_projection(*_args: object, **_kwargs: object) -> Path:
        raise OSError("projection unavailable")

    monkeypatch.setattr(knowledge_pages_module, "stage_knowledge_page_projection", fail_projection)
    with pytest.raises(OSError, match="projection unavailable"):
        pages.publish(draft.page_id)

    preserved = pages.get_page(draft.page_id)
    assert preserved.published_revision is None
    assert preserved.working_draft == bound.working_draft
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_page_revision_sources"
        ).fetchone() == (0,)
