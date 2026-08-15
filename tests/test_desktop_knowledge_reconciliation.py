"""Focused coverage for deterministic Desktop knowledge reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import openkb.desktop_knowledge_reconciliation_resolution as reconciliation_resolution
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_generations import materialize_current_generation
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
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
    assert any("knowledge_generation" in item.channels for item in evidence)


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
    page = pages.save_page(
        page_id=None,
        kind="entity",
        title="Alice",
        content_markdown="User-owned statement.",
    )
    source = tmp_path / "incoming.txt"
    source.write_text("# Alice\n\nIncompatible source statement.", encoding="utf-8")

    DesktopTextImportService(kb_dir).import_text(source)

    conflicts = DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].baseline_kind == "user_revision"
    assert conflicts[0].kind == "entity"
    assert pages.get_page(page.page_id).content_markdown == "User-owned statement."


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
    assert not (kb_dir / "knowledge-pages" / "generated").exists()
    materialize_current_generation(kb_dir)
    projections = tuple((kb_dir / "knowledge-pages" / "generated").rglob("*.md"))
    assert len(projections) == 1
    assert "Fact: Lyon" in projections[0].read_text(encoding="utf-8")
