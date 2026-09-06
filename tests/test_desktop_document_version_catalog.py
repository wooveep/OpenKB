"""Service-level behavior for confirmed Document Lineages and catalog revisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openkb.documents.version_catalog import (
    DocumentLineageDecision,
    DocumentVersionMemberDecision,
    _decode_lineages,
)
from openkb.documents.versions import DesktopDocumentVersionService
from openkb.importing.artifacts import DesktopImportError
from openkb.importing.runner import DesktopTextImportService
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def _versions(tmp_path: Path, monkeypatch) -> tuple[DesktopDocumentVersionService, tuple[str, ...]]:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document_ids = []
    for label, sentence in (
        ("V10.1", "The first workflow uses alpha."),
        ("V10.2", "The second workflow uses beta."),
        ("V10.10", "The current workflow uses gamma."),
    ):
        source = tmp_path / f"Guide_{label}.md"
        source.write_text(f"# Product Guide {label}\n\n{sentence}", encoding="utf-8")
        document_ids.append(
            DesktopTextImportService(kb_dir).import_text(source).document.document_id
        )
    return DesktopDocumentVersionService(kb_dir), tuple(document_ids)


def _decision(document_ids: tuple[str, ...], **overrides) -> DocumentLineageDecision:
    first, second, third = document_ids
    values = {
        "display_name": "Product Guide",
        "version_scheme": "numeric_dotted",
        "members": (
            DocumentVersionMemberDecision(first, "V10.1"),
            DocumentVersionMemberDecision(second, "V10.2", predecessor_document_id=first),
            DocumentVersionMemberDecision(third, "V10.10", predecessor_document_id=second),
        ),
        "current_document_id": third,
        "aliases": ("Guide",),
    }
    values.update(overrides)
    return DocumentLineageDecision(**values)


def test_batch_confirmation_orders_numeric_segments_and_sets_one_current(
    tmp_path: Path, monkeypatch
) -> None:
    service, document_ids = _versions(tmp_path, monkeypatch)

    snapshot = service.confirm_lineage(_decision(document_ids))

    confirmed = [lineage for lineage in snapshot.lineages if lineage.lineage_state == "confirmed"]
    assert len(confirmed) == 1
    lineage = confirmed[0]
    assert lineage.current_document_id == document_ids[-1]
    assert [member.version_label for member in lineage.members] == [
        "V10.1",
        "V10.2",
        "V10.10",
    ]
    assert set(lineage.aliases) == {
        "Guide",
        "Product Guide",
        "Guide_V10.1.md",
        "Guide_V10.2.md",
        "Guide_V10.10.md",
    }
    diffs = service.list_diffs(lineage.lineage_id)
    assert [(item.from_document_id, item.to_document_id) for item in diffs] == [
        (document_ids[0], document_ids[1]),
        (document_ids[1], document_ids[2]),
    ]
    assert all(item.status == "ready" and item.items for item in diffs)
    assert service.catalog_snapshot() == snapshot


def test_duplicate_label_delta_current_and_stale_review_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    service, document_ids = _versions(tmp_path, monkeypatch)
    initial = service.catalog_snapshot()
    revisions = tuple(
        (lineage.lineage_id, lineage.metadata_revision)
        for lineage in initial.lineages
        if any(member.document_id in document_ids for member in lineage.members)
    )

    duplicate_members = list(_decision(document_ids).members)
    duplicate_members[-1] = DocumentVersionMemberDecision(
        document_ids[-1], "V10.2", predecessor_document_id=document_ids[1]
    )
    with pytest.raises(DesktopImportError) as duplicate:
        service.confirm_lineage(_decision(document_ids, members=tuple(duplicate_members)))
    assert duplicate.value.code == "document_version_label_conflict"

    delta_members = list(_decision(document_ids).members)
    delta_members[-1] = DocumentVersionMemberDecision(
        document_ids[-1],
        "V10.10",
        predecessor_document_id=document_ids[1],
        snapshot_kind="delta",
    )
    with pytest.raises(DesktopImportError) as delta:
        service.confirm_lineage(_decision(document_ids, members=tuple(delta_members)))
    assert delta.value.code == "invalid_document_lineage_current"

    confirmed = service.confirm_lineage(
        _decision(document_ids, expected_metadata_revisions=revisions)
    )
    with pytest.raises(DesktopImportError) as conflict:
        service.confirm_lineage(_decision(document_ids, expected_metadata_revisions=revisions))
    assert conflict.value.code == "document_lineage_revision_conflict"
    assert service.catalog_snapshot() == confirmed


def test_catalog_snapshot_decoder_rejects_non_array_aliases() -> None:
    payload = [
        {
            "lineage_id": "lineage-1",
            "display_name": "Product Guide",
            "normalized_name": "product guide",
            "lineage_state": "singleton",
            "version_scheme": "opaque",
            "current_document_id": "document-1",
            "metadata_revision": 1,
            "aliases": "Guide",
            "members": [
                {
                    "document_id": "document-1",
                    "document_name": "Guide.md",
                    "availability": "available",
                    "version_label": None,
                    "normalized_version_label": None,
                    "version_key_json": None,
                    "branch_label": None,
                    "predecessor_document_id": None,
                    "snapshot_kind": "full_snapshot",
                    "metadata_origin": "migration",
                    "confirmed_at": None,
                }
            ],
        }
    ]

    with pytest.raises(ValueError, match="snapshot is invalid"):
        _decode_lineages(json.dumps(payload))
