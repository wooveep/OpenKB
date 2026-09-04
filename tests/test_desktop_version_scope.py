"""Version Scope resolution and D2 canonical Evidence occurrence projection."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from openkb.desktop_document_version_catalog import (
    DocumentLineageDecision,
    DocumentVersionMemberDecision,
)
from openkb.desktop_document_versions import DesktopDocumentVersionService
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_scoped_evidence import ScopedEvidenceView
from openkb.desktop_version_scope import resolve_version_scope
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def _confirmed_pair(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    old_source = tmp_path / "Guide_V10.2.md"
    new_source = tmp_path / "Guide_V10.3.md"
    old_source.write_text(
        "# Product Guide\n\nShared deployment fact.\n\nOld-only setting.", encoding="utf-8"
    )
    new_source.write_text(
        "# Product Guide\n\nShared deployment fact.\n\nCurrent setting.", encoding="utf-8"
    )
    old = DesktopTextImportService(kb_dir).import_text(old_source).document
    new = DesktopTextImportService(kb_dir).import_text(new_source).document
    service = DesktopDocumentVersionService(kb_dir)
    snapshot = service.confirm_lineage(
        DocumentLineageDecision(
            display_name="Product Guide",
            version_scheme="numeric_dotted",
            aliases=("Guide",),
            members=(
                DocumentVersionMemberDecision(old.document_id, "V10.2"),
                DocumentVersionMemberDecision(
                    new.document_id, "V10.3", predecessor_document_id=old.document_id
                ),
            ),
            current_document_id=new.document_id,
        )
    )
    lineage = next(item for item in snapshot.lineages if item.lineage_state == "confirmed")
    return kb_dir, old, new, snapshot, lineage


def test_scope_resolver_handles_latest_exact_compare_all_and_follow_up(
    tmp_path: Path, monkeypatch
) -> None:
    _kb_dir, old, new, catalog, lineage = _confirmed_pair(tmp_path, monkeypatch)

    latest = resolve_version_scope(
        "How do I deploy Product Guide?",
        conversation_scope=None,
        ui_filter=None,
        catalog=catalog,
    )
    exact = resolve_version_scope(
        "Use Product Guide V10.2",
        conversation_scope=None,
        ui_filter=None,
        catalog=catalog,
    )
    compare = resolve_version_scope(
        "Compare Product Guide V10.2 with V10.3",
        conversation_scope=None,
        ui_filter=None,
        catalog=catalog,
    )
    all_versions = resolve_version_scope(
        "Show all versions of Product Guide",
        conversation_scope=None,
        ui_filter=None,
        catalog=catalog,
    )
    previous = resolve_version_scope(
        "What about the previous version?",
        conversation_scope=latest,
        ui_filter=None,
        catalog=catalog,
    )
    unavailable = resolve_version_scope(
        "Use Product Guide V10.4",
        conversation_scope=None,
        ui_filter=None,
        catalog=catalog,
    )

    assert latest.status == "resolved"
    assert latest.allowed_document_ids == frozenset((new.document_id,))
    assert exact.status == "resolved"
    assert exact.allowed_document_ids == frozenset((old.document_id,))
    assert compare.status == "resolved"
    assert compare.allowed_document_ids == frozenset((old.document_id, new.document_id))
    assert all_versions.allowed_document_ids == frozenset((old.document_id, new.document_id))
    assert previous.allowed_document_ids == frozenset((old.document_id,))
    assert unavailable.status == "unavailable"
    assert unavailable.allowed_document_ids == frozenset()
    assert unavailable.available_labels == ("V10.2", "V10.3")
    assert latest.lineage_ids == (lineage.lineage_id,)


def test_short_explicit_version_follow_up_inherits_the_previous_lineage(
    tmp_path: Path, monkeypatch
) -> None:
    _kb_dir, old, _new, catalog, lineage = _confirmed_pair(tmp_path, monkeypatch)
    competing_members = tuple(
        replace(member, document_id=f"other-{member.document_id}") for member in lineage.members
    )
    competing = replace(
        lineage,
        lineage_id="other-lineage",
        display_name="Other Guide",
        normalized_name="other guide",
        aliases=("Other Guide",),
        current_document_id=competing_members[-1].document_id,
        members=competing_members,
    )
    ambiguous_catalog = replace(catalog, lineages=(lineage, competing))
    previous = resolve_version_scope(
        "Product Guide",
        conversation_scope=None,
        ui_filter=None,
        catalog=ambiguous_catalog,
    )

    follow_up = resolve_version_scope(
        "What about V10.2?",
        conversation_scope=previous,
        ui_filter=None,
        catalog=ambiguous_catalog,
    )

    assert follow_up.status == "resolved"
    assert follow_up.lineage_ids == (lineage.lineage_id,)
    assert follow_up.allowed_document_ids == frozenset((old.document_id,))


def test_latest_degrades_only_along_a_confirmed_predecessor(tmp_path: Path, monkeypatch) -> None:
    _kb_dir, old, new, catalog, lineage = _confirmed_pair(tmp_path, monkeypatch)
    unavailable_members = tuple(
        replace(member, availability="missing") if member.document_id == new.document_id else member
        for member in lineage.members
    )
    degraded_catalog = replace(
        catalog,
        lineages=tuple(
            replace(value, members=unavailable_members)
            if value.lineage_id == lineage.lineage_id
            else value
            for value in catalog.lineages
        ),
    )

    scope = resolve_version_scope(
        "Use the latest Product Guide",
        conversation_scope=None,
        ui_filter=None,
        catalog=degraded_catalog,
    )

    assert scope.status == "degraded"
    assert scope.allowed_document_ids == frozenset((old.document_id,))
    assert scope.degradation_reason == "current_unavailable_confirmed_predecessor"


def test_named_latest_scope_keeps_independent_documents_as_supporting_material(
    tmp_path: Path, monkeypatch
) -> None:
    kb_dir, old, current, _catalog, _lineage = _confirmed_pair(tmp_path, monkeypatch)
    independent_source = tmp_path / "Operations Notes.md"
    independent_source.write_text(
        "# Operations Notes\n\nIndependent deployment prerequisites.",
        encoding="utf-8",
    )
    independent = DesktopTextImportService(kb_dir).import_text(independent_source).document
    catalog = DesktopDocumentVersionService(kb_dir).catalog_snapshot()

    scope = resolve_version_scope(
        "How do I deploy the latest Product Guide?",
        conversation_scope=None,
        ui_filter=None,
        catalog=catalog,
    )

    assert scope.allowed_document_ids == frozenset((current.document_id, independent.document_id))
    assert old.document_id not in scope.allowed_document_ids


def test_scoped_view_projects_shared_d2_evidence_to_the_selected_version(
    tmp_path: Path, monkeypatch
) -> None:
    kb_dir, old, new, catalog, _lineage = _confirmed_pair(tmp_path, monkeypatch)
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence_id = str(
            connection.execute(
                "SELECT evidence_id FROM evidence_refs WHERE text = 'Shared deployment fact.'"
            ).fetchone()[0]
        )
        exact_scope = resolve_version_scope(
            "Product Guide V10.2",
            conversation_scope=None,
            ui_filter=None,
            catalog=catalog,
        )
        latest_scope = resolve_version_scope(
            "latest Product Guide",
            conversation_scope=None,
            ui_filter=None,
            catalog=catalog,
        )
        compare_scope = resolve_version_scope(
            "compare Product Guide V10.2 and V10.3",
            conversation_scope=None,
            ui_filter=None,
            catalog=catalog,
        )

        exact = ScopedEvidenceView(exact_scope).preferred_occurrence_in(connection, evidence_id)
        latest = ScopedEvidenceView(latest_scope).preferred_occurrence_in(connection, evidence_id)
        compared = ScopedEvidenceView(compare_scope).occurrences_for_evidence_in(
            connection, evidence_id
        )

    assert exact is not None and exact.document_id == old.document_id
    assert exact.version_label == "V10.2"
    assert latest is not None and latest.document_id == new.document_id
    assert latest.version_label == "V10.3"
    assert [(item.document_id, item.side) for item in compared] == [
        (old.document_id, "left"),
        (new.document_id, "right"),
    ]
