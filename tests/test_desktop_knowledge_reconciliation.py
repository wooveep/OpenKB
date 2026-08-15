"""Focused coverage for deterministic Desktop knowledge reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
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
