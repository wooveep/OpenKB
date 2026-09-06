"""Explicit reparsing replaces every derived checkpoint, while preserving source bytes."""

import json
import sqlite3

import pytest

from openkb.engine.protocol import DesktopRequest, DesktopRequestError, recovery_override_param
from openkb.importing import runner
from openkb.importing.recovery import DesktopImportRecoveryStore
from openkb.importing.service import (
    DesktopImportError,
    DesktopRecoveryOverride,
    DesktopTextImportService,
)
from openkb.knowledge.analysis.service import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.models.gateway import DesktopModelGateway
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime


def test_recovery_reparses_after_model_failure_from_verified_raw_bytes(tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    source = tmp_path / "source.md"
    source.write_text("# Evidence\n\nOriginal reading.")
    DesktopKnowledgeBaseRuntime().create(kb)
    parse = runner.parse_structured_document
    modes = []

    def parser(path, content, *, parser_mode):
        modes.append(parser_mode)
        if parser_mode == "enhanced":
            content = content.replace(b"Original reading", b"Enhanced reading")
        return parse(path, content, parser_mode=parser_mode)

    monkeypatch.setattr(runner, "parse_structured_document", parser)

    def unavailable(*args):
        raise TimeoutError()

    with pytest.raises(DesktopImportError):
        DesktopTextImportService(kb, model_gateway=DesktopModelGateway(unavailable)).import_text(
            source
        )
    task = DesktopTextImportService(kb).list_import_jobs()["jobs"][0]
    job_id = task["job"]["job_id"]
    assert task["quarantine"]["stage"] == "model_analysis"
    source.unlink()
    analysis = json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "A reading.",
            "document_summary": [],
            "candidates": [],
        }
    )
    recovered = DesktopTextImportService(
        kb, model_gateway=DesktopModelGateway(lambda *a: analysis)
    ).recover_text(job_id, DesktopRecoveryOverride(parser_mode="enhanced"))
    assert recovered.job.status == "completed"
    assert modes == ["auto", "enhanced"]
    with sqlite3.connect(kb / ".openkb/state.sqlite3") as db:
        blocks = db.execute("SELECT text FROM document_ir_blocks").fetchall()
        assert any("Enhanced reading" in row[0] for row in blocks)
        assert not any("Original reading" in row[0] for row in blocks)
        assert db.execute("SELECT parser_mode FROM recovery_runs").fetchone() == ("enhanced",)
    # A fresh service retains the choice if another stage pauses or the process restarts.
    assert DesktopImportRecoveryStore(kb).parser_mode(job_id, "auto") == "enhanced"


@pytest.mark.parametrize("mode", [True, [], {}, "unsupported", 1])
def test_recovery_boundary_rejects_invalid_parser_modes(mode):
    with pytest.raises(DesktopRequestError):
        recovery_override_param(
            DesktopRequest(
                "retry",
                "workbench.recover_import_job",
                {
                    "recovery_override": {"parserMode": mode},
                },
            )
        )


@pytest.mark.parametrize("key", ["parserMode", "parser_mode"])
def test_recovery_boundary_accepts_both_wire_spellings(key):
    override = recovery_override_param(
        DesktopRequest(
            "retry",
            "workbench.recover_import_job",
            {
                "recovery_override": {key: "enhanced"},
            },
        )
    )
    assert override.parser_mode == "enhanced"
