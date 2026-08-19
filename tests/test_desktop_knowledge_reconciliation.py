"""Focused coverage for deterministic Desktop knowledge reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import openkb.desktop_knowledge_reconciliation_resolution as reconciliation_resolution
from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_generations import materialize_current_generation
from openkb.desktop_knowledge_pages import DesktopKnowledgePageError, DesktopKnowledgePageService
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_resolution import (
    DesktopKnowledgeReconciliationResolutionService,
)
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_incompatible_import_stays_in_review_without_advancing_generation(tmp_path: Path) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    first = tmp_path / "first.txt"
    first.write_text("# Concept: Retrieval\n\nFact: Paris", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text(
        "# Concept: Retrieval\n\nFact: Paris\n\nFact: Lyon",
        encoding="utf-8",
    )

    DesktopTextImportService(kb_dir).import_text(first)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    published_generation = reconciliation.current_generation_id()
    assert published_generation == 1

    DesktopTextImportService(kb_dir).import_text(second)

    conflicts = reconciliation.list_conflicts()
    assert reconciliation.current_generation_id() == published_generation
    assert len(conflicts) == 1
    assert conflicts[0].title == "Retrieval"
    assert conflicts[0].baseline_kind == "published_generation"
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        content = connection.execute(
            """
            SELECT content_markdown FROM knowledge_generation_items
            WHERE generation_id = ? AND normalized_title = 'retrieval'
            """,
            (published_generation,),
        ).fetchone()
    assert content == ("Fact: Paris",)


def test_compatible_addition_advances_the_published_generation(tmp_path: Path) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    first = tmp_path / "first.txt"
    first.write_text("# Concept: Retrieval\n\nDefinition: Finds source evidence.", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text(
        "# Concept: Retrieval\n\nDefinition: Finds source evidence.\n\n"
        "Purpose: Supports grounded answers.",
        encoding="utf-8",
    )

    DesktopTextImportService(kb_dir).import_text(first)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    assert reconciliation.current_generation_id() == 1

    DesktopTextImportService(kb_dir).import_text(second)

    assert reconciliation.current_generation_id() == 2
    assert reconciliation.list_conflicts() == ()
    evidence = DesktopEvidenceRetriever(kb_dir).retrieve("grounded").evidence
    assert all("knowledge_generation" not in item.channels for item in evidence)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            """
            SELECT DISTINCT provenance_state FROM knowledge_generation_items
            WHERE generation_id = 2
            """
        ).fetchall() == [("legacy_unmapped",)]
    materialize_current_generation(kb_dir)
    generated = next(
        path
        for path in (kb_dir / "knowledge-pages" / "generated").rglob("*.md")
        if path.name != "index.md"
    ).read_text(encoding="utf-8")
    assert "provenance: legacy_unmapped" in generated
    assert "source_document_id:" not in generated
    assert "verified:" not in generated


def test_d1_document_version_still_reconciles_when_it_reuses_canonical_ir(tmp_path: Path) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    first = tmp_path / "first.txt"
    first.write_text("# Concept: Retrieval\n\nKnown fact.", encoding="utf-8")
    second = tmp_path / "second.txt"
    second.write_text("# Concept: Retrieval\n\nKnown fact.   ", encoding="utf-8")

    DesktopTextImportService(kb_dir).import_text(first)
    duplicate = DesktopTextImportService(kb_dir).import_text(second)

    assert duplicate.job.deduplication is not None
    assert duplicate.job.deduplication.level == "D1"
    assert DesktopKnowledgeReconciliationService(kb_dir).current_generation_id() == 1
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_reconciliation_candidates"
        ).fetchone()
    assert count == (2,)


def test_incompatible_change_to_user_revision_is_isolated(tmp_path: Path) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="entity",
        title="Alice",
        content_markdown="# User-owned statement",
    )
    pages.publish(page.page_id)
    source = tmp_path / "incoming.txt"
    source.write_text("# Alice\n\nIncompatible source statement.", encoding="utf-8")

    DesktopTextImportService(kb_dir).import_text(source)

    conflicts = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].baseline_kind == "user_revision"
    assert conflicts[0].kind == "entity"
    current = pages.get_page(page.page_id)
    assert current.published_revision is not None
    assert current.published_revision.content_markdown == "# User-owned statement"


def test_working_draft_turns_compatible_incoming_knowledge_into_three_way_review(
    tmp_path: Path,
) -> None:
    """An unpublished human edit prevents even compatible additions from auto-publishing."""
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    pages = DesktopKnowledgePageService(kb_dir)
    published_body = "See [Configuration](configuration.md) for details."
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown=published_body,
    )
    pages.publish(page.page_id)
    draft_body = f"{published_body}\n\nUser draft note."
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown=draft_body,
    )
    source = tmp_path / "incoming.txt"
    source.write_text(
        "# Concept: Retrieval\n\nSee [Configuration](configuration.md) for details.\n\n"
        "Purpose: Adds grounded source routing.",
        encoding="utf-8",
    )

    DesktopTextImportService(kb_dir).import_text(source)

    conflicts = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.reconciliation_mode == "three_way"
    assert conflict.target_page_id == page.page_id
    assert conflict.baseline_content_markdown == published_body
    assert conflict.working_draft_content_markdown == draft_body
    assert conflict.content_markdown.endswith("Purpose: Adds grounded source routing.")
    assert DesktopKnowledgeReconciliationService(kb_dir).current_generation_id() is None


@pytest.mark.parametrize(
    ("decision", "manual_content", "expected_content", "updated_count", "kept_count"),
    [
        (
            "keep_draft",
            None,
            "[Published baseline](published-baseline.md)\n\nUser draft addition.",
            0,
            1,
        ),
        (
            "apply_incoming",
            None,
            "[Published baseline](published-baseline.md)\n\nUser draft addition.\n\n"
            "Incoming source addition.",
            1,
            0,
        ),
        (
            "replace_draft",
            None,
            "[Published baseline](published-baseline.md)\n\nIncoming source addition.",
            1,
            0,
        ),
        ("manual_merge", "Human merged result.", "Human merged result.", 1, 0),
    ],
)
def test_three_way_actions_update_only_the_working_draft(
    tmp_path: Path,
    decision: str,
    manual_content: str | None,
    expected_content: str,
    updated_count: int,
    kept_count: int,
) -> None:
    """Every three-way action remains a reversible draft choice until explicit Publish."""
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / decision).knowledge_base.kb_dir
    )
    pages = DesktopKnowledgePageService(kb_dir)
    published_body = "[Published baseline](published-baseline.md)"
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown=published_body,
    )
    published = pages.publish(page.page_id)
    draft_body = f"{published_body}\n\nUser draft addition."
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown=draft_body,
    )
    source = tmp_path / f"{decision}.txt"
    source.write_text(
        f"# Concept: Retrieval\n\n{published_body}\n\nIncoming source addition.",
        encoding="utf-8",
    )
    imported = DesktopTextImportService(kb_dir).import_text(source)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    conflict = reconciliation.list_conflicts()[0]

    staged = resolution.stage_decisions(
        (conflict.candidate_id,), decision, manual_merge_content=manual_content
    )
    staged_conflict = staged[0]
    assert staged_conflict.staged_decision == decision
    assert staged_conflict.staged_content_markdown == manual_content
    before_commit = pages.get_page(page.page_id)
    assert before_commit.published_revision == published.published_revision
    assert before_commit.working_draft is not None
    assert before_commit.working_draft.content_markdown == draft_body

    cleared = resolution.stage_decisions((conflict.candidate_id,), None)
    assert cleared[0].staged_decision is None
    resolution.stage_decisions(
        (conflict.candidate_id,), decision, manual_merge_content=manual_content
    )
    committed = resolution.commit_staged_decisions()

    assert committed.published_generation_id is None
    assert committed.published_count == 0
    assert committed.draft_updated_count == updated_count
    assert committed.kept_count == kept_count
    after_commit = pages.get_page(page.page_id)
    assert after_commit.published_revision == published.published_revision
    assert after_commit.working_draft is not None
    assert after_commit.working_draft.content_markdown == expected_content
    assert reconciliation.list_conflicts() == ()
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        candidate_row = connection.execute(
            """
            SELECT content_markdown, baseline_content_markdown,
                working_draft_content_markdown, staged_content_markdown,
                resolution_status
            FROM knowledge_reconciliation_candidates WHERE candidate_id = ?
            """,
            (conflict.candidate_id,),
        ).fetchone()
        resolution_row = connection.execute(
            """
            SELECT decision, target_page_id, published_generation_id
            FROM knowledge_reconciliation_resolution_records WHERE candidate_id = ?
            """,
            (conflict.candidate_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM source_documents WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_occurrences WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone()[0] > 0
    assert candidate_row is not None
    assert candidate_row[:4] == ("", None, None, None)
    assert candidate_row[4] == ("kept" if decision == "keep_draft" else "draft_updated")
    assert resolution_row == (decision, page.page_id, None)


def test_three_way_result_still_requires_publication_gate_and_explicit_publish(
    tmp_path: Path,
) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="# Published baseline",
    )
    published = pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="Unpublished user claim.",
    )
    source = tmp_path / "incoming.txt"
    source.write_text("# Concept: Retrieval\n\nIncoming unsourced claim.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(source)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "replace_draft")
    resolution.commit_staged_decisions()

    current = pages.get_page(page.page_id)
    assert current.published_revision == published.published_revision
    with pytest.raises(DesktopKnowledgePageError) as blocked:
        pages.publish(page.page_id)
    assert blocked.value.code == "knowledge_publication_blocked"


@pytest.mark.parametrize(
    "decision", ["apply_incoming", "replace_draft", "manual_merge"]
)
def test_replacing_a_source_backed_draft_discards_obsolete_source_bindings(
    tmp_path: Path, decision: str
) -> None:
    """A reconciliation replacement must leave a Draft that can be sourced and published."""
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    pages = DesktopKnowledgePageService(kb_dir)
    baseline_source = tmp_path / "baseline-source.txt"
    baseline_source.write_text("Published baseline fact.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(baseline_source)
    baseline_evidence = pages.search_sources("Published baseline fact")[0]
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="Published baseline fact.",
    )
    bound = pages.bind_source(
        page.page_id,
        "Published baseline fact.",
        baseline_evidence.evidence_id,
    )
    published = pages.publish(page.page_id)
    assert published.published_revision is not None
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown=published.published_revision.content_markdown,
    )
    assert bound.working_draft is not None
    copied_draft = pages.get_page(page.page_id).working_draft
    assert copied_draft is not None
    assert len(copied_draft.source_map) == 1

    incoming_source = tmp_path / "incoming.txt"
    incoming_source.write_text(
        "# Concept: Retrieval\n\nIncoming replacement fact.", encoding="utf-8"
    )
    DesktopTextImportService(kb_dir).import_text(incoming_source)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions(
        (conflict.candidate_id,),
        decision,
        manual_merge_content=(
            "Incoming replacement fact." if decision == "manual_merge" else None
        ),
    )
    resolution.commit_staged_decisions()

    replaced = pages.get_page(page.page_id)
    assert replaced.working_draft is not None
    assert replaced.working_draft.content_markdown == "Incoming replacement fact."
    assert replaced.working_draft.source_map == ()
    assert {item.code for item in replaced.publication_diagnostics} == {
        "knowledge_claim_source_missing"
    }

    incoming_evidence = pages.search_sources("Incoming replacement fact")[0]
    rebound = pages.bind_source(
        page.page_id,
        "Incoming replacement fact.",
        incoming_evidence.evidence_id,
    )
    assert rebound.publication_diagnostics == ()
    republished = pages.publish(page.page_id)
    assert republished.published_revision is not None
    assert republished.published_revision.revision_number == 2


def test_staged_three_way_choice_never_overwrites_a_later_user_edit(tmp_path: Path) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="# Published baseline",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="Draft selected during review.",
    )
    source = tmp_path / "incoming.txt"
    source.write_text("# Concept: Retrieval\n\nIncoming replacement.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(source)
    conflict = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "replace_draft")
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="Newer user edit.",
    )

    with pytest.raises(DesktopImportError) as changed:
        resolution.commit_staged_decisions()

    assert changed.value.code == "knowledge_reconciliation_working_draft_changed"
    current = pages.get_page(page.page_id)
    assert current.working_draft is not None
    assert current.working_draft.content_markdown == "Newer user edit."
    remaining = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()
    assert remaining[0].content_markdown == "Incoming replacement."


def test_d1_reimport_uses_the_same_three_way_boundary_when_a_draft_exists(
    tmp_path: Path,
) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    first = tmp_path / "first.txt"
    first.write_text("# Concept: Retrieval\n\nKnown source fact.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(first)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="# Current user revision",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="Unpublished user edit.",
    )
    second = tmp_path / "second.txt"
    second.write_text("# Concept: Retrieval\n\nKnown source fact.   ", encoding="utf-8")

    duplicate = DesktopTextImportService(kb_dir).import_text(second)

    assert duplicate.job.deduplication is not None
    assert duplicate.job.deduplication.level == "D1"
    conflicts = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].document_id == duplicate.document.document_id
    assert conflicts[0].reconciliation_mode == "three_way"
    assert conflicts[0].target_page_id == page.page_id


def test_d0_reuse_reconciles_stored_ir_against_a_newer_working_draft(tmp_path: Path) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    source = tmp_path / "same.txt"
    source.write_text("# Concept: Retrieval\n\nKnown source fact.", encoding="utf-8")
    original = DesktopTextImportService(kb_dir).import_text(source)
    pages = DesktopKnowledgePageService(kb_dir)
    page = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Retrieval",
        content_markdown="# Current user revision",
    )
    pages.publish(page.page_id)
    pages.save_draft(
        page_id=page.page_id,
        kind="concept",
        title="Retrieval",
        content_markdown="Unpublished user edit.",
    )

    duplicate = DesktopTextImportService(kb_dir).import_text(source)

    assert duplicate.job.deduplication is not None
    assert duplicate.job.deduplication.level == "D0"
    assert duplicate.document.document_id == original.document.document_id
    conflicts = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].document_id == original.document.document_id
    assert conflicts[0].reconciliation_mode == "three_way"
    assert conflicts[0].target_page_id == page.page_id


def test_staged_conflict_choices_publish_atomically_and_delete_review_copies(
    tmp_path: Path,
) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        "# Concept: Retrieval\n\nFact: Paris\n\n# Concept: Indexing\n\nMode: local",
        encoding="utf-8",
    )
    incoming = tmp_path / "incoming.txt"
    incoming.write_text(
        "# Concept: Retrieval\n\nFact: Lyon\n\n# Concept: Indexing\n\nMode: global",
        encoding="utf-8",
    )

    DesktopTextImportService(kb_dir).import_text(baseline)
    imported = DesktopTextImportService(kb_dir).import_text(incoming)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    published_generation = reconciliation.current_generation_id()
    conflicts = {item.title: item for item in reconciliation.list_conflicts()}

    assert set(conflicts) == {"Retrieval", "Indexing"}
    staged = resolution.stage_decisions(
        tuple(item.candidate_id for item in conflicts.values()), "keep_current"
    )
    assert {item.staged_decision for item in staged} == {"keep_current"}
    resolution.stage_decisions((conflicts["Retrieval"].candidate_id,), None)
    assert reconciliation.current_generation_id() == published_generation
    resolution.stage_decisions((conflicts["Retrieval"].candidate_id,), "publish_incoming")
    assert all(
        "knowledge_generation" not in item.channels
        for item in DesktopEvidenceRetriever(kb_dir).retrieve("Lyon").evidence
    )

    committed = resolution.commit_staged_decisions()

    assert committed.published_generation_id == (published_generation or 0) + 1
    assert committed.published_count == 1
    assert committed.kept_count == 1
    assert set(committed.resolved_candidate_ids) == {
        conflicts["Retrieval"].candidate_id,
        conflicts["Indexing"].candidate_id,
    }
    assert reconciliation.list_conflicts() == ()
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        generation_rows = connection.execute(
            """
            SELECT item_key, title, content_markdown
            FROM knowledge_generation_items
            WHERE generation_id = ? ORDER BY title
            """,
            (committed.published_generation_id,),
        ).fetchall()
        review_rows = connection.execute(
            """
            SELECT content_markdown, baseline_content_markdown, staged_decision, resolution_status
            FROM knowledge_reconciliation_candidates
            WHERE candidate_id IN (?, ?) ORDER BY candidate_id
            """,
            (conflicts["Retrieval"].candidate_id, conflicts["Indexing"].candidate_id),
        ).fetchall()
        resolution_rows = connection.execute(
            """
            SELECT decision, published_generation_id
            FROM knowledge_reconciliation_resolution_records
            WHERE candidate_id IN (?, ?) ORDER BY decision
            """,
            (conflicts["Retrieval"].candidate_id, conflicts["Indexing"].candidate_id),
        ).fetchall()
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_occurrences WHERE document_id = ?",
            (imported.document.document_id,),
        ).fetchone()

    assert [(row[1], row[2]) for row in generation_rows] == [
        ("Indexing", "Mode: local"),
        ("Retrieval", "Fact: Lyon"),
    ]
    retrieval_item_key = next(row[0] for row in generation_rows if row[1] == "Retrieval")
    projection = kb_dir / "knowledge-pages" / "generated" / "concept" / f"{retrieval_item_key}.md"
    assert "Fact: Lyon" in projection.read_text(encoding="utf-8")
    assert review_rows and all(
        row[0] == "" and row[1] is None and row[2] is None for row in review_rows
    )
    assert {row[3] for row in review_rows} == {"published", "kept"}
    assert resolution_rows == [
        ("keep_current", None),
        ("publish_incoming", committed.published_generation_id),
    ]
    assert evidence_count is not None and evidence_count[0] > 0


def test_projection_failure_leaves_a_staged_conflict_unpublished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("# Concept: Retrieval\n\nFact: Paris", encoding="utf-8")
    incoming = tmp_path / "incoming.txt"
    incoming.write_text("# Concept: Retrieval\n\nFact: Lyon", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(baseline)
    DesktopTextImportService(kb_dir).import_text(incoming)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    generation_id = reconciliation.current_generation_id()
    conflict = reconciliation.list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "publish_incoming")

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise OSError("projection unavailable")

    monkeypatch.setattr(
        reconciliation_resolution, "stage_generation_projection_in", fail_materialization
    )

    with pytest.raises(OSError, match="projection unavailable"):
        resolution.commit_staged_decisions()

    assert reconciliation.current_generation_id() == generation_id
    remaining = reconciliation.list_conflicts()
    assert len(remaining) == 1
    assert remaining[0].content_markdown == "Fact: Lyon"
    assert remaining[0].staged_decision == "publish_incoming"
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_reconciliation_resolution_records"
        ).fetchone() == (0,)


def test_committed_conflict_recovers_its_projection_after_activation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb_dir = Path(
        DesktopKnowledgeBaseRuntime().create(tmp_path / "knowledge").knowledge_base.kb_dir
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("# Concept: Retrieval\n\nFact: Paris", encoding="utf-8")
    incoming = tmp_path / "incoming.txt"
    incoming.write_text("# Concept: Retrieval\n\nFact: Lyon", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(baseline)
    DesktopTextImportService(kb_dir).import_text(incoming)
    reconciliation = DesktopKnowledgeReconciliationService(kb_dir)
    conflict = reconciliation.list_conflicts()[0]
    resolution = DesktopKnowledgeReconciliationResolutionService(kb_dir)
    resolution.stage_decisions((conflict.candidate_id,), "publish_incoming")

    def fail_activation(*_args: object, **_kwargs: object) -> None:
        raise OSError("projection activation unavailable")

    monkeypatch.setattr(
        reconciliation_resolution, "activate_generation_projection", fail_activation
    )

    committed = resolution.commit_staged_decisions()

    assert committed.published_generation_id == reconciliation.current_generation_id()
    previous_projection = tuple(
        path
        for path in (kb_dir / "knowledge-pages" / "generated").rglob("*.md")
        if path.name != "index.md"
    )
    assert len(previous_projection) == 1
    assert "Fact: Paris" in previous_projection[0].read_text(encoding="utf-8")
    assert "Fact: Lyon" not in previous_projection[0].read_text(encoding="utf-8")
    materialize_current_generation(kb_dir)
    projections = tuple(
        path
        for path in (kb_dir / "knowledge-pages" / "generated").rglob("*.md")
        if path.name != "index.md"
    )
    assert len(projections) == 1
    assert "Fact: Lyon" in projections[0].read_text(encoding="utf-8")
