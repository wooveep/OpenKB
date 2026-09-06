"""Regression checks for draft-aware three-way Knowledge Reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationSource,
    knowledge_content_sha256,
)
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange
from openkb.desktop_knowledge_reconciliation_resolution import (
    DesktopKnowledgeReconciliationResolutionService,
)
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_model_result_migrations import MODEL_RESULT_OBSERVATION_COLUMNS
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


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
    imported = DesktopTextImportService(kb_dir).import_text(source)
    evidence_id = pages.search_sources("One source supports two facts")[0].evidence_id
    source_id = stable_source_id(evidence_id)
    first_claim = f"Claim one.[^{source_id}]"
    second_claim = f"Claim two.[^{source_id}]"
    incoming_content = f"{first_claim}\n\n{second_claim}"
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    conflicts = reconciliation.record_analysis_changes(
        imported.document.document_id,
        (
            IncomingKnowledgeChange(
                source_block_id=None,
                kind="concept",
                is_kind_explicit=True,
                title="Shared source",
                normalized_title="shared source",
                content_markdown=incoming_content,
                content_sha256=knowledge_content_sha256(incoming_content),
                sources=(
                    KnowledgeGenerationSource(source_id, evidence_id, "Claim one."),
                    KnowledgeGenerationSource(source_id, evidence_id, "Claim two."),
                ),
                analysis_provenance_json="{}",
            ),
        ),
    )
    conflict = conflicts[0]
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
