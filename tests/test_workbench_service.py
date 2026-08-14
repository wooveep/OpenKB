"""Behavior tests for the Desktop Workbench application-service seam."""

from __future__ import annotations

import json

import pytest

from openkb.workbench_service import (
    DesktopWorkbenchService,
    InspectKnowledgeBaseCommand,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseStateError,
)


def test_inspect_knowledge_base_returns_snapshot_and_event(kb_dir):
    """A workbench can inspect completed knowledge through one public seam."""
    (kb_dir / ".openkb" / "hashes.json").write_text(
        json.dumps(
            {
                "document-hash": {
                    "name": "notes.md",
                    "type": "md",
                    "pages": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    (kb_dir / "raw" / "notes.md").write_text("# Notes", encoding="utf-8")
    (kb_dir / "wiki" / "sources" / "notes.md").write_text("# Notes", encoding="utf-8")
    (kb_dir / "wiki" / "summaries" / "notes.md").write_text("# Summary", encoding="utf-8")
    (kb_dir / "wiki" / "reports" / "quality.md").write_text("# Report", encoding="utf-8")

    outcome = DesktopWorkbenchService().execute(InspectKnowledgeBaseCommand(kb_dir=kb_dir))

    assert outcome.snapshot.kb_dir == str(kb_dir)
    assert outcome.snapshot.inventory.document_count == 1
    assert outcome.snapshot.inventory.as_dict()["documents"] == [
        {
            "hash": "document-hash",
            "name": "notes.md",
            "type": "md",
            "display_type": "short",
            "pages": None,
        }
    ]
    assert outcome.snapshot.status.directories == {
        "sources": 1,
        "summaries": 1,
        "concepts": 0,
        "reports": 1,
    }
    assert outcome.snapshot.status.raw_count == 1
    assert outcome.snapshot.status.total_indexed == 1
    assert outcome.snapshot.status.last_compile is not None
    assert outcome.snapshot.status.last_lint is not None
    assert outcome.events[0].kind == "knowledge_base.inspected"
    assert outcome.events[0].data.kb_dir == str(kb_dir)
    assert outcome.events[0].data.document_count == 1


def test_inspect_knowledge_base_uses_a_typed_error_for_unknown_directory(tmp_path):
    """Desktop callers receive a stable domain error instead of a filesystem guess."""
    command = InspectKnowledgeBaseCommand(kb_dir=tmp_path / "missing")

    with pytest.raises(KnowledgeBaseNotFoundError) as error:
        DesktopWorkbenchService().execute(command)

    assert error.value.code == "knowledge_base_not_found"


def test_inspect_knowledge_base_rejects_invalid_registry_metadata(kb_dir):
    """Malformed persisted metadata cannot leak as loosely typed desktop state."""
    (kb_dir / ".openkb" / "hashes.json").write_text(
        json.dumps({"document-hash": {"name": [], "type": {}}}),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeBaseStateError) as error:
        DesktopWorkbenchService().execute(InspectKnowledgeBaseCommand(kb_dir=kb_dir))

    assert error.value.code == "knowledge_base_state_invalid"
