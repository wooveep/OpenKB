"""Behavior checks for SQLite-authoritative Desktop Concept and Entity pages."""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from pathlib import Path

import pytest

import openkb.desktop_knowledge_lifecycle as knowledge_lifecycle_module
import openkb.desktop_knowledge_pages as knowledge_pages_module
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_pages import (
    DesktopKnowledgePageError,
    DesktopKnowledgePageService,
)
from openkb.desktop_knowledge_source_retrieval import knowledge_source_rows_in
from openkb.desktop_model_result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _drop_page_tree_schema(connection: sqlite3.Connection) -> None:
    _drop_current_model_schema(connection)
    connection.execute("DROP TABLE grounded_answer_retrieval_traces")
    connection.execute("DROP TABLE conversation_answer_retrieval_traces")
    _drop_catalog_schema(connection)
    for table in (
        "model_capability_checks",
        "document_page_tree_provider_current",
        "document_page_tree_enrichment_current",
        "document_page_tree_enrichment_summaries",
        "document_page_tree_enrichment_tasks",
        "document_page_tree_enrichment_generations",
        "document_page_tree_current",
        "document_page_tree_node_images",
        "document_page_tree_node_evidence",
        "document_page_tree_nodes",
        "document_page_tree_rebuild_tasks",
        "document_page_tree_generations",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.execute("DROP INDEX import_jobs_document_completed_idx")
    connection.execute("DELETE FROM schema_migrations WHERE version IN (32, 33, 34, 35, 36, 37)")


def _drop_current_model_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP VIEW IF EXISTS current_knowledge_graph_edges")
    connection.execute("DROP VIEW IF EXISTS current_knowledge_graph_nodes")
    for table in (
        "knowledge_graph_attempt_issues",
        "knowledge_graph_attempts",
        "knowledge_graph_current",
        "knowledge_graph_result_edges",
        "knowledge_graph_result_nodes",
        "knowledge_graph_results",
        "knowledge_adoption_requests",
        "knowledge_origin_references",
        "model_capability_compatibility_audit",
        "model_operation_contract_events",
        "model_operation_retry_permits",
        "model_operation_contract_states",
        "knowledge_graph_extraction_tasks",
        "legacy_model_recovery_audit",
        "model_usage_records",
        "knowledge_reanalysis_merge_nodes",
        "knowledge_analysis_merge_nodes",
        "knowledge_reanalysis_plans",
        "knowledge_analysis_plans",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for table in ("model_calls", "model_attempts"):
        for column in ("lifecycle_status", "elapsed_seconds", "retry_after_seconds"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for table, column, _definition in MODEL_RESULT_OBSERVATION_COLUMNS:
        if connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone():
            connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for table, columns in (
        ("knowledge_graph_nodes", ("support_start", "support_end", "verification_state")),
        (
            "knowledge_graph_edges",
            ("relation_label", "support_start", "support_end", "verification_state"),
        ),
        ("document_page_tree_enrichment_tasks", ("retry_scope",)),
    ):
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in columns:
            if column in existing:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.execute("DELETE FROM schema_migrations WHERE version >= 38")


def _drop_catalog_schema(connection: sqlite3.Connection) -> None:
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'knowledge_catalog_%'"
    ).fetchall():
        connection.execute(f'DROP TRIGGER "{name}"')
    for table in (
        "knowledge_catalog_rebuild_tasks",
        "knowledge_catalog_state",
        "knowledge_catalog_links",
        "knowledge_catalog_node_sources",
        "knowledge_catalog_nodes",
        "knowledge_catalog_generations",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


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
    assert "authority: user_revision" in projection.read_text(encoding="utf-8")
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
        _drop_catalog_schema(connection)
        connection.execute("DROP TABLE knowledge_generation_item_sources")
        connection.execute("DROP TABLE knowledge_reconciliation_candidate_sources")
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN entity_subtype"
        )
        connection.execute("DROP TABLE knowledge_page_lifecycle_events")
        connection.execute("DROP TABLE knowledge_page_verifications")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_revision_sources")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_working_sources")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_ui_state")
        connection.execute("DROP TABLE IF EXISTS knowledge_page_working_drafts")
        _drop_page_tree_schema(connection)
        connection.execute("DROP TABLE knowledge_reanalysis_merges")
        connection.execute("DROP TABLE knowledge_reanalysis_batches")
        connection.execute("DROP TABLE knowledge_reanalysis_jobs")
        connection.execute("DROP TABLE knowledge_reanalysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 31")
        connection.execute("DROP TABLE knowledge_analysis_merges")
        connection.execute("DROP TABLE knowledge_analysis_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version = 30")
        connection.execute("DROP TABLE knowledge_missing_source_resolution_records")
        connection.execute("DROP TABLE knowledge_missing_source_candidates")
        connection.execute("ALTER TABLE knowledge_page_revisions DROP COLUMN provenance_state")
        connection.execute(
            "ALTER TABLE knowledge_generation_items DROP COLUMN analysis_provenance_json"
        )
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN analysis_provenance_json"
        )
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN aliases_json")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN tags_json")
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN aliases_json"
        )
        connection.execute("ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN tags_json")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN provenance_state")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN entity_subtype")
        connection.execute("ALTER TABLE knowledge_pages DROP COLUMN stale_after")
        connection.execute("ALTER TABLE knowledge_pages DROP COLUMN lifecycle_state")
        connection.execute(
            "DELETE FROM schema_migrations "
            "WHERE version IN (19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29)"
        )
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
    pages = DesktopKnowledgePageService(kb_dir)
    migrated = pages.get_page(page_id)

    assert migrated.publication_state == "published"
    assert migrated.published_revision is not None
    assert migrated.published_revision.content_markdown == "Legacy published content."
    assert migrated.published_revision.source_map == ()
    assert migrated.published_revision.provenance_state == "legacy_unmapped"
    assert migrated.verification.state == "unverified"
    assert migrated.verification.reason == "legacy_unmapped_not_verifiable"
    assert migrated.verification.can_verify is False
    with pytest.raises(DesktopKnowledgePageError) as verification_error:
        pages.verify(page_id)
    assert verification_error.value.code == "knowledge_verification_legacy_unmapped"
    assert migrated.working_draft is None
    pages.materialize_current_pages()
    projection = (kb_dir / migrated.materialized_path).read_text(encoding="utf-8")
    assert "provenance: legacy_unmapped" in projection
    assert "verified:" not in projection


def test_v20_source_map_migrates_as_source_backed_without_rewriting_the_revision(tmp_path):
    """A shipped claim map is preserved rather than mislabeled as legacy-unmapped."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "source.md"
    source.write_text("# Facts\n\nOriginal mapped evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = pages.search_sources("Original mapped evidence")[0]
    claim = "This claim already had a valid Source Map in schema v20."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Existing source map",
        content_markdown=claim,
    )
    pages.bind_source(draft.page_id, claim, candidate.evidence_id)
    before = pages.publish(draft.page_id)
    assert before.published_revision is not None
    before_sources = before.published_revision.source_map

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        _drop_catalog_schema(connection)
        connection.execute("DROP TABLE knowledge_generation_item_sources")
        connection.execute("DROP TABLE knowledge_reconciliation_candidate_sources")
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN entity_subtype"
        )
        connection.execute("DROP TABLE knowledge_page_lifecycle_events")
        connection.execute("DROP TABLE knowledge_page_verifications")
        connection.execute("ALTER TABLE knowledge_page_revisions DROP COLUMN provenance_state")
        connection.execute(
            "ALTER TABLE knowledge_generation_items DROP COLUMN analysis_provenance_json"
        )
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN analysis_provenance_json"
        )
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN aliases_json")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN tags_json")
        connection.execute(
            "ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN aliases_json"
        )
        connection.execute("ALTER TABLE knowledge_reconciliation_candidates DROP COLUMN tags_json")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN provenance_state")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN entity_subtype")
        connection.execute("ALTER TABLE knowledge_pages DROP COLUMN stale_after")
        connection.execute("ALTER TABLE knowledge_pages DROP COLUMN lifecycle_state")
        _drop_page_tree_schema(connection)
        connection.execute("DROP TABLE knowledge_reanalysis_merges")
        connection.execute("DROP TABLE knowledge_reanalysis_batches")
        connection.execute("DROP TABLE knowledge_reanalysis_jobs")
        connection.execute("DROP TABLE knowledge_reanalysis_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 31")
        connection.execute("DROP TABLE knowledge_analysis_merges")
        connection.execute("DROP TABLE knowledge_analysis_batches")
        connection.execute("DELETE FROM schema_migrations WHERE version = 30")
        connection.execute("DROP TABLE knowledge_missing_source_resolution_records")
        connection.execute("DROP TABLE knowledge_missing_source_candidates")
        connection.execute("DELETE FROM schema_migrations WHERE version = 29")
        connection.execute("DELETE FROM schema_migrations WHERE version = 28")
        connection.execute("DELETE FROM schema_migrations WHERE version = 27")
        connection.execute("DELETE FROM schema_migrations WHERE version = 26")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("DELETE FROM schema_migrations WHERE version = 22")
        connection.execute("DELETE FROM schema_migrations WHERE version = 23")
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.execute("DELETE FROM schema_migrations WHERE version = 24")
        connection.commit()

    DesktopKnowledgeBaseRuntime().open(kb_dir)
    migrated = DesktopKnowledgePageService(kb_dir).get_page(draft.page_id)

    assert migrated.published_revision is not None
    assert migrated.published_revision.provenance_state == "source_backed"
    assert migrated.published_revision.source_map == before_sources


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


def test_human_verification_binds_the_current_revision_and_is_not_inherited(tmp_path):
    """Verify is explicit, revision-bound, and never applies to a Working Draft."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "verification.md"
    source.write_text("# Policy\n\nThe recovery window is sixty seconds.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = pages.search_sources("recovery window")[0]
    claim = "Recovery has a bounded logical-call window."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Recovery verification",
        content_markdown=claim,
    )
    pages.bind_source(draft.page_id, claim, candidate.evidence_id)
    published = pages.publish(draft.page_id)

    assert published.verification.state == "unverified"
    assert published.verification.reason == "not_verified"
    assert published.verification.can_verify is True
    verified = pages.verify(draft.page_id)
    assert verified.verification.state == "human_reviewed"
    assert verified.verification.actor == "local_user"
    assert verified.verification.verified_at is not None
    assert verified.verification.revision_id is not None
    first_revision_id = verified.verification.revision_id

    pending = pages.save_draft(
        page_id=draft.page_id,
        kind="concept",
        title="Recovery verification",
        content_markdown=verified.published_revision.content_markdown,
    )
    assert pending.verification.state == "human_reviewed"
    assert pending.verification.reason == "working_draft_not_verifiable"
    assert pending.verification.can_verify is False
    with pytest.raises(DesktopKnowledgePageError) as draft_error:
        pages.verify(draft.page_id)
    assert draft_error.value.code == "knowledge_verification_requires_current_publication"

    republished = pages.publish(draft.page_id)
    assert republished.verification.state == "unverified"
    assert republished.verification.reason == "revision_changed"
    assert republished.verification.can_verify is True
    assert republished.verification.revision_id is None
    reverified = pages.verify(draft.page_id)
    assert reverified.verification.revision_id != first_revision_id
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = connection.execute(
            """
            SELECT actor, verification_kind, revision_id, invalidated_at
            FROM knowledge_page_verifications ORDER BY verified_at
            """
        ).fetchall()
    assert rows == [
        ("local_user", "human_reviewed", first_revision_id, None),
        ("local_user", "human_reviewed", reverified.verification.revision_id, None),
    ]


def test_verification_rechecks_the_publication_gate_and_trust_only_breaks_score_ties(tmp_path):
    """Unavailable evidence blocks Verify; review cannot outrank stronger relevance."""
    kb_dir = tmp_path / "desktop-kb"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n\nAlpha support for the routing policy.", encoding="utf-8")
    second.write_text("# Second\n\nBeta support for the routing policy.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    first_import = DesktopTextImportService(kb_dir).import_text(first)
    DesktopTextImportService(kb_dir).import_text(second)
    pages = DesktopKnowledgePageService(kb_dir)
    first_source = next(
        item for item in pages.search_sources("Alpha support") if "Alpha" in item.excerpt
    )
    second_source = next(
        item for item in pages.search_sources("Beta support") if "Beta" in item.excerpt
    )

    first_claim = "Priority routing policy uses the first source."
    first_page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Priority routing policy",
        content_markdown=first_claim,
    )
    pages.bind_source(first_page.page_id, first_claim, first_source.evidence_id)
    pages.publish(first_page.page_id)

    second_claim = "Routing policy uses an independently reviewed source."
    second_page = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Reviewed route",
        content_markdown=second_claim,
    )
    pages.bind_source(second_page.page_id, second_claim, second_source.evidence_id)
    pages.publish(second_page.page_id)
    pages.verify(second_page.page_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        ranked = knowledge_source_rows_in(connection, ("priority", "routing", "policy"), limit=8)
        tied = knowledge_source_rows_in(connection, ("uses",), limit=8)
    assert [str(row[0]) for row in ranked[:2]] == [
        first_source.evidence_id,
        second_source.evidence_id,
    ]
    assert [str(row[0]) for row in tied[:2]] == [
        second_source.evidence_id,
        first_source.evidence_id,
    ]

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (first_import.document.document_id,),
        )
        connection.commit()
    blocked = pages.get_page(first_page.page_id)
    assert blocked.verification.reason == "publication_gate_blocked"
    assert blocked.verification.can_verify is False
    with pytest.raises(DesktopKnowledgePageError) as gate_error:
        pages.verify(first_page.page_id)
    assert gate_error.value.code == "knowledge_verification_blocked"


def test_knowledge_source_limit_counts_unique_evidence_not_claim_mappings(tmp_path):
    kb_dir = tmp_path / "desktop-kb"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n\nPrimary supporting record.", encoding="utf-8")
    second.write_text("# Second\n\nIndependent supporting record.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(first)
    DesktopTextImportService(kb_dir).import_text(second)
    pages = DesktopKnowledgePageService(kb_dir)
    first_source = next(
        source for source in pages.search_sources("Primary") if "Primary" in source.excerpt
    )
    second_source = next(
        source for source in pages.search_sources("Independent") if "Independent" in source.excerpt
    )
    claims = tuple(f"Primary claim {index}." for index in range(13))
    crowded = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Zebra topic",
        content_markdown="\n\n".join(claims),
    )
    for claim in claims:
        pages.bind_source(crowded.page_id, claim, first_source.evidence_id)
    pages.publish(crowded.page_id)
    independent_claim = "Independent claim."
    independent = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Zebra independent",
        content_markdown=independent_claim,
    )
    pages.bind_source(independent.page_id, independent_claim, second_source.evidence_id)
    pages.publish(independent.page_id)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        rows = knowledge_source_rows_in(connection, ("zebra",), limit=12)

    assert {str(row[0]) for row in rows} == {
        first_source.evidence_id,
        second_source.evidence_id,
    }


def test_lifecycle_stales_deprecates_and_restores_without_changing_original_evidence(tmp_path):
    """Lifecycle changes invalidate review while Evidence remains independently available."""
    kb_dir = tmp_path / "desktop-kb"
    stale_source = tmp_path / "stale.md"
    fresh_source = tmp_path / "fresh.md"
    stale_source.write_text("# Policy\n\nStale-source routing evidence.", encoding="utf-8")
    fresh_source.write_text("# Policy\n\nFresh-source routing evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(stale_source)
    DesktopTextImportService(kb_dir).import_text(fresh_source)
    pages = DesktopKnowledgePageService(kb_dir)
    stale_evidence = next(
        source
        for source in pages.search_sources("Stale-source")
        if "Stale-source" in source.excerpt
    )
    fresh_evidence = next(
        source
        for source in pages.search_sources("Fresh-source")
        if "Fresh-source" in source.excerpt
    )

    stale_claim = "Routing policy follows the stale source."
    stale_draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Stale routing",
        content_markdown=stale_claim,
    )
    pages.bind_source(stale_draft.page_id, stale_claim, stale_evidence.evidence_id)
    pages.publish(stale_draft.page_id)
    pages.verify(stale_draft.page_id)

    fresh_claim = "Routing policy follows the fresh source."
    fresh_draft = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Fresh routing",
        content_markdown=fresh_claim,
    )
    pages.bind_source(fresh_draft.page_id, fresh_claim, fresh_evidence.evidence_id)
    pages.publish(fresh_draft.page_id)

    stale_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    stale = pages.set_stale_after(stale_draft.page_id, stale_at)
    assert stale.lifecycle_state == "stable"
    assert stale.stale_after == stale_at
    assert stale.is_stale is True
    assert stale.verification.state == "unverified"
    assert stale.verification.reason == "lifecycle_changed"
    assert stale.verification.can_verify is True

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        routed = knowledge_source_rows_in(connection, ("routing",), limit=8)
        invalidation = connection.execute(
            "SELECT invalidation_reason FROM knowledge_page_verifications"
        ).fetchone()
    assert [str(row[0]) for row in routed[:2]] == [
        fresh_evidence.evidence_id,
        stale_evidence.evidence_id,
    ]
    assert invalidation == ("lifecycle_changed",)

    deprecated = pages.deprecate(stale_draft.page_id)
    assert deprecated.page_id == stale_draft.page_id
    assert deprecated.lifecycle_state == "deprecated"
    assert deprecated.published_revision == stale.published_revision
    assert deprecated.verification.reason == "deprecated_not_verifiable"
    assert deprecated.verification.can_verify is False
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        routed = knowledge_source_rows_in(connection, ("routing",), limit=8)
    assert stale_evidence.evidence_id not in {str(row[0]) for row in routed}

    restored = pages.restore(stale_draft.page_id)
    assert restored.page_id == stale_draft.page_id
    assert restored.lifecycle_state == "stable"
    assert restored.published_revision == stale.published_revision
    cleared = pages.set_stale_after(stale_draft.page_id, None)
    assert cleared.stale_after is None
    assert cleared.is_stale is False
    original_pack = DesktopEvidenceRetriever(kb_dir).retrieve("Stale-source routing evidence")
    assert stale_evidence.evidence_id in {item.evidence_id for item in original_pack.evidence}


def test_permanent_delete_requires_deprecation_and_confirmation_but_keeps_source_corpus(tmp_path):
    """Confirmed page deletion removes Knowledge history, never imported source authority."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "source.md"
    source.write_text(
        "# Source\n\nOriginal evidence survives Knowledge deletion.", encoding="utf-8"
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = pages.search_sources("Original evidence survives")[0]
    claim = "Knowledge deletion preserves its original evidence."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Deletion lifecycle",
        content_markdown=claim,
    )
    pages.bind_source(draft.page_id, claim, candidate.evidence_id)
    published = pages.publish(draft.page_id)
    pages.verify(draft.page_id)
    pages.save_draft(
        page_id=draft.page_id,
        kind="concept",
        title="Deletion lifecycle",
        content_markdown=published.published_revision.content_markdown,
    )

    with pytest.raises(DesktopKnowledgePageError) as stable_error:
        pages.permanent_delete(draft.page_id, confirmation_page_id=draft.page_id)
    assert stable_error.value.code == "knowledge_page_deprecation_required"
    pages.deprecate(draft.page_id)
    with pytest.raises(DesktopKnowledgePageError) as confirmation_error:
        pages.permanent_delete(draft.page_id, confirmation_page_id="wrong-page")
    assert confirmation_error.value.code == "knowledge_page_delete_confirmation_invalid"

    pages.permanent_delete(draft.page_id, confirmation_page_id=draft.page_id)

    with pytest.raises(DesktopKnowledgePageError) as missing_error:
        pages.get_page(draft.page_id)
    assert missing_error.value.code == "knowledge_page_not_found"
    assert not (kb_dir / published.materialized_path).exists()
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_page_revisions WHERE page_id = ?", (draft.page_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_page_working_drafts WHERE page_id = ?",
            (draft.page_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM source_documents WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_refs WHERE evidence_id = ?", (candidate.evidence_id,)
        ).fetchone() == (1,)


def test_permanent_delete_reports_success_when_post_commit_staging_cleanup_is_deferred(
    tmp_path, monkeypatch
):
    """A locked staging file cannot turn an already committed deletion into failure."""
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Deferred cleanup",
        content_markdown="See [Configuration](configuration.md) for details.",
    )
    pages.publish(draft.page_id)
    pages.deprecate(draft.page_id)

    def fail_cleanup(_staged: Path) -> None:
        raise OSError("projection file is locked")

    monkeypatch.setattr(
        knowledge_lifecycle_module,
        "discard_knowledge_page_projection_staging",
        fail_cleanup,
    )

    pages.permanent_delete(draft.page_id, confirmation_page_id=draft.page_id)

    with pytest.raises(DesktopKnowledgePageError) as missing_error:
        pages.get_page(draft.page_id)
    assert missing_error.value.code == "knowledge_page_not_found"


def test_source_search_matches_a_noncanonical_available_d2_occurrence(tmp_path):
    """Document-name search chooses the matching occurrence before canonical deduplication."""
    kb_dir = tmp_path / "desktop-kb"
    alpha = tmp_path / "alpha-special.md"
    bravo = tmp_path / "bravo-target.md"
    shared = "The shared recovery fact remains canonical across document versions."
    alpha.write_text(f"# Shared\n\n{shared}", encoding="utf-8")
    bravo.write_text(f"# Shared\n\n{shared}\n\nBravo-only detail.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(alpha)
    DesktopTextImportService(kb_dir).import_text(bravo)

    matches = DesktopKnowledgePageService(kb_dir).search_sources("bravo-target")

    shared_match = next(item for item in matches if item.excerpt == shared)
    assert shared_match.document_name == "bravo-target.md"


def test_claim_supports_multiple_canonical_sources_without_counting_d2_twice(tmp_path):
    """Independent evidence survives revisions; repeated occurrences remain one source."""
    kb_dir = tmp_path / "desktop-kb"
    first = tmp_path / "first.md"
    repeated = tmp_path / "repeated.md"
    second = tmp_path / "second.md"
    shared = "Primary records show the worker completed recovery."
    independent = "Audit records independently confirm the recovery result."
    first.write_text(f"# Guide\n\n{shared}", encoding="utf-8")
    repeated.write_text(f"# Guide\n\n{shared}\n\nRepeated-only context.", encoding="utf-8")
    second.write_text(f"# Audit\n\n{independent}", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(first)
    DesktopTextImportService(kb_dir).import_text(repeated)
    DesktopTextImportService(kb_dir).import_text(second)
    pages = DesktopKnowledgePageService(kb_dir)
    shared_source = next(
        item for item in pages.search_sources("Primary records") if item.excerpt == shared
    )
    independent_source = next(
        item for item in pages.search_sources("Audit records") if item.excerpt == independent
    )
    claim = "The resilience policy is independently corroborated."
    second_claim = "The same recovery evidence also supports continuity."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Corroborated recovery",
        content_markdown=f"{claim}\n\n{second_claim}",
    )

    pages.bind_source(draft.page_id, claim, shared_source.evidence_id)
    pages.bind_source(draft.page_id, claim, shared_source.evidence_id)
    pages.bind_source(draft.page_id, claim, independent_source.evidence_id)
    bound = pages.bind_source(draft.page_id, second_claim, shared_source.evidence_id)

    assert bound.working_draft is not None
    first_source_ids = tuple(item.source_id for item in bound.working_draft.source_map)
    assert len(first_source_ids) == 3
    assert len(set(first_source_ids)) == 2
    shared_source_id = next(
        item.source_id
        for item in bound.working_draft.source_map
        if item.evidence_id == shared_source.evidence_id
    )
    independent_source_id = next(
        item.source_id
        for item in bound.working_draft.source_map
        if item.evidence_id == independent_source.evidence_id
    )
    assert bound.working_draft.content_markdown.count(f"[^{shared_source_id}]") == 2
    assert bound.working_draft.content_markdown.count(f"[^{independent_source_id}]") == 1
    published = pages.publish(draft.page_id)
    assert published.published_revision is not None
    assert published.published_revision.provenance_state == "source_backed"

    pages.save_draft(
        page_id=draft.page_id,
        kind="concept",
        title="Corroborated recovery",
        content_markdown=published.published_revision.content_markdown,
    )
    republished = pages.publish(draft.page_id)
    assert republished.published_revision is not None
    assert (
        tuple(item.source_id for item in republished.published_revision.source_map)
        == first_source_ids
    )
    projection = (kb_dir / republished.materialized_path).read_text(encoding="utf-8")
    assert projection.count(f"id: {shared_source_id}") == 1
    assert projection.count(f"[^{shared_source_id}]:") == 1

    routed = [
        item
        for item in DesktopEvidenceRetriever(kb_dir).retrieve("independently corroborated").evidence
        if "knowledge_source" in item.channels
    ]
    assert {item.evidence_id for item in routed} == {
        shared_source.evidence_id,
        independent_source.evidence_id,
    }
    assert sum(item.evidence_id == shared_source.evidence_id for item in routed) == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_occurrences WHERE evidence_id = ?",
            (shared_source.evidence_id,),
        ).fetchone() == (2,)


def test_source_map_resolves_an_available_occurrence_without_rewriting_history(tmp_path):
    """Availability changes choose a live Document Version and recover dynamically."""
    kb_dir = tmp_path / "desktop-kb"
    alpha = tmp_path / "alpha.md"
    bravo = tmp_path / "bravo.md"
    shared = "The canonical recovery evidence is shared across versions."
    alpha.write_text(f"# Guide\n\n{shared}", encoding="utf-8")
    bravo.write_text(f"# Guide\n\n{shared}\n\nBravo-only context.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    alpha_import = DesktopTextImportService(kb_dir).import_text(alpha)
    bravo_import = DesktopTextImportService(kb_dir).import_text(bravo)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = next(
        item for item in pages.search_sources("canonical recovery") if item.excerpt == shared
    )
    claim = "Recovery evidence remains available through a surviving version."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Available occurrence",
        content_markdown=claim,
    )
    pages.bind_source(draft.page_id, claim, candidate.evidence_id)
    published = pages.publish(draft.page_id)
    assert published.published_revision is not None
    source_id = published.published_revision.source_map[0].source_id
    assert published.published_revision.source_map[0].availability == "available"
    assert (
        published.published_revision.source_map[0].document_id == alpha_import.document.document_id
    )

    database_path = kb_dir / ".openkb" / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (alpha_import.document.document_id,),
        )
        connection.commit()
    fallback = pages.get_page(draft.page_id).published_revision
    assert fallback is not None
    assert fallback.source_map[0].availability == "available"
    assert fallback.source_map[0].document_id == bravo_import.document.document_id
    routed = next(
        item
        for item in DesktopEvidenceRetriever(kb_dir).retrieve("surviving version").evidence
        if item.evidence_id == candidate.evidence_id
    )
    assert routed.document_id == bravo_import.document.document_id
    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            "SELECT document_id FROM knowledge_page_revision_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        assert stored == (alpha_import.document.document_id,)
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (bravo_import.document.document_id,),
        )
        connection.commit()

    unavailable = pages.get_page(draft.page_id).published_revision
    assert unavailable is not None
    assert unavailable.source_map[0].availability == "unavailable"
    assert candidate.evidence_id not in {
        item.evidence_id
        for item in DesktopEvidenceRetriever(kb_dir).retrieve("surviving version").evidence
        if "knowledge_source" in item.channels
    }

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'available' WHERE document_id = ?",
            (bravo_import.document.document_id,),
        )
        connection.commit()
    restored = pages.get_page(draft.page_id).published_revision
    assert restored is not None
    assert restored.source_map[0].availability == "available"
    assert restored.source_map[0].document_id == bravo_import.document.document_id
    assert candidate.evidence_id in {
        item.evidence_id
        for item in DesktopEvidenceRetriever(kb_dir).retrieve("surviving version").evidence
        if "knowledge_source" in item.channels
    }


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
    assert unsourced.working_draft is not None
    assert unsourced.working_draft.provenance_state == "unsourced"
    assert DesktopEvidenceRetriever(kb_dir).retrieve("production timeout exactly").evidence == ()
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
    structural_page = pages.publish(polite_navigation.page_id)
    assert structural_page.published_revision is not None
    assert structural_page.published_revision.provenance_state == "structural"

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
