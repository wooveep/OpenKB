"""Closed Version Scope behavior at the public evidence-retrieval seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from openkb.desktop_answer_types import DesktopAnswerError
from openkb.desktop_document_version_catalog import (
    DocumentLineageDecision,
    DocumentVersionMemberDecision,
)
from openkb.desktop_document_versions import DesktopDocumentVersionService
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_version_scope import RetrievalRequest, VersionFilter
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _versioned_guide(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    old_path = tmp_path / "Guide_V10.2.md"
    current_path = tmp_path / "Guide_V10.3.md"
    old_path.write_text(
        "# Product Guide\n\nShared deployment fact.\n\nLegacy flux capacitor setting.",
        encoding="utf-8",
    )
    current_path.write_text(
        "# Product Guide\n\nShared deployment fact.\n\nCurrent deployment setting.",
        encoding="utf-8",
    )
    old = DesktopTextImportService(kb_dir).import_text(old_path).document
    current = DesktopTextImportService(kb_dir).import_text(current_path).document
    snapshot = DesktopDocumentVersionService(kb_dir).confirm_lineage(
        DocumentLineageDecision(
            display_name="Product Guide",
            version_scheme="numeric_dotted",
            aliases=("Guide",),
            members=(
                DocumentVersionMemberDecision(old.document_id, "V10.2"),
                DocumentVersionMemberDecision(
                    current.document_id,
                    "V10.3",
                    predecessor_document_id=old.document_id,
                ),
            ),
            current_document_id=current.document_id,
        )
    )
    lineage = next(item for item in snapshot.lineages if item.lineage_state == "confirmed")
    return kb_dir, old, current, lineage


def test_exact_and_latest_scope_filter_before_retrieval_ranking(
    tmp_path: Path, monkeypatch
) -> None:
    kb_dir, old, current, lineage = _versioned_guide(tmp_path, monkeypatch)
    retriever = DesktopEvidenceRetriever(kb_dir)

    exact = retriever.retrieve(
        RetrievalRequest(
            question="What is the legacy flux capacitor setting?",
            version_filter=VersionFilter(
                mode="exact",
                lineage_ids=(lineage.lineage_id,),
                version_labels=("V10.2",),
            ),
        )
    )
    latest = retriever.retrieve(
        RetrievalRequest(
            question="What is the legacy flux capacitor setting?",
            version_filter=VersionFilter(
                mode="latest",
                lineage_ids=(lineage.lineage_id,),
            ),
        )
    )

    assert exact.evidence
    assert {item.document_id for item in exact.evidence} == {old.document_id}
    assert {item.version_label for item in exact.evidence} == {"V10.2"}
    assert old.document_id in exact.retrieval_trace.version_scope_document_ids
    assert all(item.document_id == current.document_id for item in latest.evidence)
    assert old.document_id not in latest.retrieval_trace.version_scope_document_ids


def test_unavailable_exact_scope_fails_closed_before_retrieval_planning(
    tmp_path: Path, monkeypatch
) -> None:
    kb_dir, _old, _current, lineage = _versioned_guide(tmp_path, monkeypatch)

    def unexpected_planning(*_args, **_kwargs):
        raise AssertionError("retrieval planning must not run for an unavailable Version Scope")

    monkeypatch.setattr("openkb.desktop_retrieval.build_query_plan", unexpected_planning)

    with pytest.raises(DesktopAnswerError) as caught:
        DesktopEvidenceRetriever(kb_dir).retrieve(
            RetrievalRequest(
                question="Use Product Guide V99.0",
                version_filter=VersionFilter(
                    mode="exact",
                    lineage_ids=(lineage.lineage_id,),
                    version_labels=("V99.0",),
                ),
            )
        )

    assert caught.value.code == "desktop_version_scope_unavailable"
