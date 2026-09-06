"""Public Engine coverage for Document Version Catalog review and diffs."""

from __future__ import annotations

import io

import pytest

from openkb.engine.protocol import DesktopRequest, DesktopRequestError
from openkb.engine.server import DesktopEngineServer
from openkb.importing.runner import DesktopTextImportService
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def test_engine_exposes_catalog_confirmation_and_deterministic_diffs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    old_path = tmp_path / "Guide_V10.2.md"
    new_path = tmp_path / "Guide_V10.3.md"
    old_path.write_text("# Guide\n\nOld setting.", encoding="utf-8")
    new_path.write_text("# Guide\n\nNew setting.", encoding="utf-8")
    old = DesktopTextImportService(kb_dir).import_text(old_path).document
    new = DesktopTextImportService(kb_dir).import_text(new_path).document
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    initial = server._dispatch(
        DesktopRequest("catalog", "workbench.document_version_catalog", {}), None
    )
    revisions = {
        lineage["lineage_id"]: lineage["metadata_revision"] for lineage in initial["lineages"]
    }
    confirmed = server._dispatch(
        DesktopRequest(
            "confirm",
            "workbench.confirm_document_lineage",
            {
                "decision": {
                    "display_name": "Guide",
                    "version_scheme": "numeric_dotted",
                    "aliases": ["Product Guide"],
                    "members": [
                        {
                            "document_id": old.document_id,
                            "version_label": "V10.2",
                            "branch_label": "main",
                            "predecessor_document_id": None,
                            "snapshot_kind": "full_snapshot",
                            "metadata_origin": "user",
                        },
                        {
                            "document_id": new.document_id,
                            "version_label": "V10.3",
                            "branch_label": "main",
                            "predecessor_document_id": old.document_id,
                            "snapshot_kind": "full_snapshot",
                            "metadata_origin": "user",
                        },
                    ],
                    "current_document_id": new.document_id,
                    "lineage_id": None,
                    "expected_metadata_revisions": [
                        {
                            "lineage_id": lineage_id,
                            "metadata_revision": revision,
                        }
                        for lineage_id, revision in revisions.items()
                    ],
                }
            },
        ),
        None,
    )
    lineage = next(item for item in confirmed["lineages"] if item["lineage_state"] == "confirmed")
    diffs = server._dispatch(
        DesktopRequest(
            "diffs",
            "workbench.document_version_diffs",
            {"lineage_id": lineage["lineage_id"]},
        ),
        None,
    )

    assert confirmed["revision_id"] != initial["revision_id"]
    assert lineage["current_document_id"] == new.document_id
    assert [member["version_label"] for member in lineage["members"]] == ["V10.2", "V10.3"]
    assert len(diffs["diffs"]) == 1
    assert diffs["diffs"][0]["status"] == "ready"
    assert diffs["diffs"][0]["items"]
    changed = next(
        item for item in diffs["diffs"][0]["items"] if item["content_change_kind"] == "modified"
    )
    assert changed["old_locator"] == {"line_start": 3, "line_end": 3}
    assert changed["new_locator"] == {"line_start": 3, "line_end": 3}


def test_engine_rejects_unknown_lineage_decision_fields(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    with pytest.raises(DesktopRequestError) as caught:
        server._dispatch(
            DesktopRequest(
                "confirm",
                "workbench.confirm_document_lineage",
                {"decision": {"display_name": "Guide", "unexpected": True}},
            ),
            None,
        )

    assert caught.value.code == "invalid_params"


def test_engine_rejects_unknown_version_filter_fields(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    workspace = DesktopKnowledgeBaseRuntime()
    workspace.create(kb_dir)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO(), workspace=workspace)
    server._handshake_complete = True

    with pytest.raises(DesktopRequestError) as caught:
        server._dispatch(
            DesktopRequest(
                "ask",
                "workbench.ask_grounded",
                {
                    "question": "Which version?",
                    "version_filter": {"mode": "latest", "unexpected": True},
                },
            ),
            None,
        )

    assert caught.value.code == "invalid_params"
