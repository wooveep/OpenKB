"""Regression checks for draft-aware three-way Knowledge Reconciliation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_resolution import (
    DesktopKnowledgeReconciliationResolutionService,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path
from openkb.desktop_workspace_migrations import (
    KNOWLEDGE_RECONCILIATION_MIGRATION_STATEMENTS,
    KNOWLEDGE_RECONCILIATION_RESOLUTION_MIGRATION_STATEMENTS,
)


def _drop_page_tree_schema(connection: sqlite3.Connection) -> None:
    for table in (
        "document_page_tree_current",
        "document_page_tree_node_images",
        "document_page_tree_node_evidence",
        "document_page_tree_nodes",
        "document_page_tree_rebuild_tasks",
        "document_page_tree_generations",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DROP INDEX import_jobs_document_completed_idx")
    connection.execute("DELETE FROM schema_migrations WHERE version = 32")


def _knowledge_base(tmp_path: Path) -> Path:
    return Path(DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir)


def _two_way_conflict(kb_dir: Path, tmp_path: Path) -> str:
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("# Concept: Retrieval\n\nMode: local", encoding="utf-8")
    incoming = tmp_path / "incoming.txt"
    incoming.write_text("# Concept: Retrieval\n\nMode: global", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(baseline)
    DesktopTextImportService(kb_dir).import_text(incoming)
    return DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0].candidate_id


def test_two_way_commit_rejects_a_working_draft_created_after_staging(
    tmp_path: Path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    candidate_id = _two_way_conflict(kb_dir, tmp_path)
    original_generation = reconciliation.current_generation_id()
    resolution.stage_decisions((candidate_id,), "publish_incoming")

    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="# Published user page",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="# Unpublished user edit",
    )

    visible = reconciliation.list_conflicts()[0]
    assert visible.reconciliation_mode == "three_way"
    with pytest.raises(DesktopImportError) as changed:
        resolution.commit_staged_decisions()

    assert changed.value.code == "knowledge_reconciliation_working_draft_changed"
    assert reconciliation.current_generation_id() == original_generation


def test_keep_current_commit_refreshes_the_okf_resolution_log(tmp_path: Path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    candidate_id = _two_way_conflict(kb_dir, tmp_path)
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((candidate_id,), "keep_current")

    resolution.commit_staged_decisions()

    change_log = (kb_dir / "knowledge-pages/log.md").read_text(encoding="utf-8")
    assert candidate_id in change_log
    assert "keep_current" in change_log


def test_v24_migration_reclassifies_a_generation_candidate_with_a_draft(
    tmp_path: Path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    candidate_id = _two_way_conflict(kb_dir, tmp_path)
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((candidate_id,), "publish_incoming")
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="# Published user page",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="# Unpublished user edit",
    )
    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        legacy_candidate = connection.execute(
            """
            SELECT candidate_id, document_id, source_block_id, kind, title,
                normalized_title, content_markdown, content_sha256, classification,
                status, baseline_kind, baseline_id, baseline_title,
                baseline_content_markdown, observed_generation_id, created_at,
                staged_decision, resolution_status, resolved_at
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        assert legacy_candidate is not None
        connection.execute("DROP TABLE knowledge_reconciliation_resolution_records")
        connection.execute("DROP TABLE knowledge_reconciliation_candidates")
        for statement in KNOWLEDGE_RECONCILIATION_MIGRATION_STATEMENTS[4:]:
            connection.execute(statement)
        for statement in KNOWLEDGE_RECONCILIATION_RESOLUTION_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO knowledge_reconciliation_candidates (
                candidate_id, document_id, source_block_id, kind, title,
                normalized_title, content_markdown, content_sha256, classification,
                status, baseline_kind, baseline_id, baseline_title,
                baseline_content_markdown, observed_generation_id, created_at,
                staged_decision, resolution_status, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            legacy_candidate,
        )
        resolved_candidate = list(legacy_candidate)
        resolved_candidate[0] = f"resolved-{candidate_id}"
        resolved_candidate[6] = ""
        resolved_candidate[13] = None
        resolved_candidate[16] = None
        resolved_candidate[17] = "kept"
        resolved_candidate[18] = "2026-08-19T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO knowledge_reconciliation_candidates (
                candidate_id, document_id, source_block_id, kind, title,
                normalized_title, content_markdown, content_sha256, classification,
                status, baseline_kind, baseline_id, baseline_title,
                baseline_content_markdown, observed_generation_id, created_at,
                staged_decision, resolution_status, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            resolved_candidate,
        )
        auto_candidate = list(legacy_candidate)
        auto_candidate[0] = f"auto-{candidate_id}"
        auto_candidate[6] = ""
        auto_candidate[8] = "duplicate"
        auto_candidate[9] = "auto_reconciled"
        auto_candidate[13] = None
        auto_candidate[16] = None
        auto_candidate[17] = None
        auto_candidate[18] = None
        connection.execute(
            """
            INSERT INTO knowledge_reconciliation_candidates (
                candidate_id, document_id, source_block_id, kind, title,
                normalized_title, content_markdown, content_sha256, classification,
                status, baseline_kind, baseline_id, baseline_title,
                baseline_content_markdown, observed_generation_id, created_at,
                staged_decision, resolution_status, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            auto_candidate,
        )
        connection.execute("DROP TABLE knowledge_generation_item_sources")
        connection.execute("DROP TABLE knowledge_reconciliation_candidate_sources")
        connection.execute(
            "ALTER TABLE knowledge_generation_items DROP COLUMN analysis_provenance_json"
        )
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN aliases_json")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN tags_json")
        connection.execute("ALTER TABLE knowledge_generation_items DROP COLUMN entity_subtype")
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
        connection.execute("DELETE FROM schema_migrations WHERE version = 25")
        connection.execute("DELETE FROM schema_migrations WHERE version = 24")

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            """
            SELECT reconciliation_mode, target_page_id, staged_decision
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        resolved = connection.execute(
            """
            SELECT reconciliation_mode, target_page_id, working_draft_title,
                working_draft_content_markdown, working_draft_content_sha256,
                working_draft_updated_at, staged_content_markdown
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (f"resolved-{candidate_id}",),
        ).fetchone()
        auto_reconciled = connection.execute(
            """
            SELECT reconciliation_mode, target_page_id, working_draft_title,
                working_draft_content_markdown, working_draft_content_sha256,
                working_draft_updated_at, staged_content_markdown
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (f"auto-{candidate_id}",),
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrated == ("three_way", page.page_id, None)
    assert resolved == ("two_way", None, None, None, None, None, None)
    assert auto_reconciled == ("two_way", None, None, None, None, None, None)


def test_draft_only_page_blocks_incoming_from_automatic_publication(
    tmp_path: Path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="User-authored unpublished claim.",
    )
    incoming = tmp_path / "incoming-draft-only.txt"
    incoming.write_text("# Concept: Retrieval\n\nIncoming document claim.", encoding="utf-8")

    DesktopTextImportService(kb_dir).import_text(incoming)

    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    conflicts = reconciliation.list_conflicts()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.baseline_kind == "unpublished_page"
    assert conflict.baseline_content_markdown == ""
    assert conflict.reconciliation_mode == "three_way"
    assert conflict.target_page_id == page.page_id
    assert reconciliation.current_generation_id() is None
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "apply_incoming")

    resolution.commit_staged_decisions()

    updated = pages.get_page(page.page_id)
    assert updated.published_revision is None
    assert updated.working_draft is not None
    assert updated.working_draft.content_markdown == (
        "User-authored unpublished claim.\n\nIncoming document claim."
    )
    assert reconciliation.current_generation_id() is None


def test_auto_reconciled_duplicate_never_retains_a_working_draft_snapshot(
    tmp_path: Path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    published_body = "[Published baseline](published-baseline.md)"
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown=published_body,
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="[Private draft](private-draft.md)",
    )
    incoming = tmp_path / "duplicate.txt"
    incoming.write_text(f"# Concept: Retrieval\n\n{published_body}", encoding="utf-8")
    imported = DesktopTextImportService(kb_dir).import_text(incoming)

    assert DesktopKnowledgeReconciliationService(kb_dir).list_conflicts() == ()
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        stored = connection.execute(
            """
            SELECT status, reconciliation_mode, target_page_id,
                working_draft_title, working_draft_content_markdown,
                working_draft_content_sha256, working_draft_updated_at
            FROM knowledge_reconciliation_candidates
            WHERE document_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (imported.document.document_id,),
        ).fetchone()
    assert stored == ("auto_reconciled", "two_way", None, None, None, None, None)


@pytest.mark.parametrize(("trailing", "deduplication_level"), [("", "D0"), ("   ", "D1")])
def test_source_markers_do_not_create_false_deduplicated_reimport_conflicts(
    tmp_path: Path, trailing: str, deduplication_level: str
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    original = tmp_path / "original.txt"
    original.write_text("# Concept: Retrieval\n\nKnown fact.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(original)
    pages = DesktopKnowledgePageService(kb_dir)
    evidence = pages.search_sources("Known fact")[0]
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="Known fact.",
    )
    bound = pages.bind_source(page.page_id, "Known fact.", evidence.evidence_id)
    published = pages.publish(page.page_id)
    assert published.published_revision is not None
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown=published.published_revision.content_markdown,
    )
    assert bound.working_draft is not None
    duplicate_source = tmp_path / "duplicate.txt"
    duplicate_source.write_text(f"# Concept: Retrieval\n\nKnown fact.{trailing}", encoding="utf-8")

    duplicate = DesktopTextImportService(kb_dir).import_text(duplicate_source)

    assert duplicate.job.deduplication is not None
    assert duplicate.job.deduplication.level == deduplication_level
    assert DesktopKnowledgeReconciliationService(kb_dir).list_conflicts() == ()


def test_incoming_title_matches_an_unpublished_draft_rename(tmp_path: Path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Old Name",
        content_markdown="# Published page",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="entity",
        title="New Name",
        content_markdown="# Renamed Working Draft",
    )
    incoming = tmp_path / "renamed.txt"
    incoming.write_text("# New Name\n\nIncoming renamed-page claim.", encoding="utf-8")

    DesktopTextImportService(kb_dir).import_text(incoming)

    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    conflicts = reconciliation.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].kind == "entity"
    assert conflicts[0].reconciliation_mode == "three_way"
    assert conflicts[0].target_page_id == page.page_id
    assert reconciliation.current_generation_id() is None


def test_apply_incoming_applies_non_overlapping_baseline_delta_to_draft(
    tmp_path: Path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown=("[Mode local](mode-local.md)\n\n[Owner platform](owner-platform.md)"),
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown=("[Mode local](mode-local.md)\n\n[Owner user](owner-user.md)"),
    )
    incoming = tmp_path / "incoming.txt"
    incoming.write_text(
        "# Concept: Retrieval\n\n[Mode global](mode-global.md)\n\n"
        "[Owner platform](owner-platform.md)",
        encoding="utf-8",
    )
    DesktopTextImportService(kb_dir).import_text(incoming)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "apply_incoming")

    resolution.commit_staged_decisions()

    result = pages.get_page(page.page_id).working_draft
    assert result is not None
    assert result.content_markdown == (
        "[Mode global](mode-global.md)\n\n[Owner user](owner-user.md)"
    )


def test_apply_incoming_requires_manual_merge_for_overlapping_changes(
    tmp_path: Path,
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="[Mode local](mode-local.md)",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="[Mode user](mode-user.md)",
    )
    incoming = tmp_path / "incoming.txt"
    incoming.write_text("# Concept: Retrieval\n\n[Mode global](mode-global.md)", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(incoming)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "apply_incoming")

    with pytest.raises(DesktopImportError) as merge_required:
        resolution.commit_staged_decisions()

    assert merge_required.value.code == "knowledge_reconciliation_manual_merge_required"
    preserved = pages.get_page(page.page_id).working_draft
    assert preserved is not None
    assert preserved.content_markdown == "[Mode user](mode-user.md)"


@pytest.mark.parametrize("with_published_baseline", [False, True])
def test_apply_incoming_rejects_conflicting_same_anchor_insertions(
    tmp_path: Path, with_published_baseline: bool
) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    baseline = "[Definition](definition.md)" if with_published_baseline else ""
    draft_content = "\n\n".join(value for value in (baseline, "Mode: local") if value)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown=(baseline or draft_content),
    )
    if with_published_baseline:
        pages.publish(page.page_id)
        pages.save_draft(
            page_id=page.page_id,
            kind="concept",
            title="Retrieval",
            content_markdown=draft_content,
        )
    incoming_content = "\n\n".join(value for value in (baseline, "Mode: global") if value)
    incoming = tmp_path / "same-anchor.txt"
    incoming.write_text(f"# Concept: Retrieval\n\n{incoming_content}", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(incoming)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "apply_incoming")

    with pytest.raises(DesktopImportError) as merge_required:
        resolution.commit_staged_decisions()

    assert merge_required.value.code == "knowledge_reconciliation_manual_merge_required"
    preserved = pages.get_page(page.page_id).working_draft
    assert preserved is not None
    assert preserved.content_markdown == draft_content


def test_manual_merge_keeps_only_the_exact_claim_source_mapping(tmp_path: Path) -> None:
    kb_dir = _knowledge_base(tmp_path)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Shared source",
        content_markdown="[Overview](overview.md)",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Shared source",
        content_markdown="[Overview](overview.md)",
    )
    source = tmp_path / "shared-source.txt"
    source.write_text("One source supports two facts.", encoding="utf-8")

    def analyze(request, _timeout_seconds):
        evidence_id = str(json.loads(request.content)["evidence"][0]["evidence_id"])
        return json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "Two claims share one source.",
                "concepts": [
                    {
                        "title": "Shared source",
                        "aliases": [],
                        "tags": [],
                        "claims": [
                            {
                                "text": "Claim one.",
                                "source_evidence_ids": [evidence_id],
                            },
                            {
                                "text": "Claim two.",
                                "source_evidence_ids": [evidence_id],
                            },
                        ],
                    }
                ],
                "entities": [],
            }
        )

    DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(analyze)).import_text(source)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    first_claim = conflict.content_markdown.split("\n\n")[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions(
        (conflict.candidate_id,), "manual_merge", manual_merge_content=first_claim
    )

    resolution.commit_staged_decisions()

    resolved = pages.get_page(page.page_id)
    assert resolved.working_draft is not None
    assert resolved.working_draft.content_markdown == first_claim
    assert [source.claim_text for source in resolved.working_draft.source_map] == ["Claim one."]
    assert resolved.publication_diagnostics == ()
    pages.publish(page.page_id)
