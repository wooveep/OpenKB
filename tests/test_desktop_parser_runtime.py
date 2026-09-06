"""Parser readiness, warm-up, and DocumentIR usability contracts."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock


def _block(
    text: str = "OpenKB keeps source-grounded knowledge.",
    *,
    block_id: str = "block-1",
    ordinal: int = 0,
    line_start: int = 1,
    line_end: int = 1,
) -> DocumentIRBlock:
    return DocumentIRBlock(
        block_id=block_id,
        ordinal=ordinal,
        kind="paragraph",
        text=text,
        heading_path=("Overview",),
        line_start=line_start,
        line_end=line_end,
    )


def test_parser_readiness_inspection_never_initializes_heavy_runtimes(monkeypatch) -> None:
    from openkb.parsers import runtime as runtime

    initialized: list[str] = []
    monkeypatch.setattr(runtime, "_legacy_resources_available", lambda: True)
    monkeypatch.setattr(runtime, "_ocr_resources_available", lambda: True)
    monkeypatch.setattr(
        runtime,
        "_initialize_family",
        lambda family, source: initialized.append(family),
    )

    readiness = runtime.inspect_parser_readiness()

    assert initialized == []
    assert readiness["legacy_office"].resource_state == "resources_ready"
    assert readiness["legacy_office"].runtime_state == "not_loaded"
    assert readiness["pdf_ocr"].resource_state == "resources_ready"
    assert readiness["native_office"].runtime_state == "ready"
    assert "java" not in readiness["legacy_office"].diagnostic.casefold()


def test_legacy_parser_warmup_transitions_and_reuses_one_runtime(monkeypatch, tmp_path) -> None:
    from openkb.parsers import runtime as runtime

    runtime.reset_parser_runtime_for_tests()
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"legacy")
    entered = threading.Event()
    release = threading.Event()
    initializations: list[str] = []

    def initialize(family: str, _source: Path | None) -> None:
        initializations.append(family)
        entered.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(runtime, "_legacy_resources_available", lambda: True)
    monkeypatch.setattr(runtime, "_initialize_family", initialize)

    first = runtime.begin_parser_warmup(source)
    assert first is not None
    assert entered.wait(timeout=1)
    assert runtime.parser_runtime_snapshot()["legacy_office"].runtime_state == "initializing"
    release.set()
    first.wait()
    assert runtime.parser_runtime_snapshot()["legacy_office"].runtime_state == "ready"

    second = runtime.begin_parser_warmup(source)
    assert second is not None
    second.wait()
    assert initializations == ["legacy_office"]


@pytest.mark.parametrize(
    ("blocks", "code"),
    (
        ((), "document_ir_empty"),
        ((_block("\ufffd\ufffd\ufffd\ufffd"),), "document_ir_garbled"),
        ((_block(line_start=0, line_end=0),), "document_ir_unlocated"),
        (
            (
                _block(block_id="duplicate", ordinal=0),
                _block(block_id="duplicate", ordinal=1),
            ),
            "document_ir_invalid",
        ),
    ),
)
def test_document_ir_usability_gate_rejects_before_model_work(blocks, code) -> None:
    from openkb.documents.usability import require_usable_document_ir

    with pytest.raises(DesktopImportError) as captured:
        require_usable_document_ir(blocks)

    assert captured.value.code == code
    assert captured.value.suggested_action


def test_document_ir_usability_gate_accepts_short_located_content() -> None:
    from openkb.documents.usability import assess_document_ir, require_usable_document_ir

    blocks = (_block("有效内容"),)

    report = assess_document_ir(blocks)
    require_usable_document_ir(blocks)

    assert report.usable
    assert report.text_characters == 4
    assert report.located_blocks == 1


def test_import_usability_gate_runs_before_evidence_or_model_calls(monkeypatch, tmp_path) -> None:
    from openkb.importing import runner as desktop_import_runner
    from openkb.importing.artifacts import ParsedDocument
    from openkb.importing.service import DesktopTextImportService
    from openkb.models.gateway import DesktopModelGateway
    from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime

    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "broken.docx"
    source.write_bytes(b"not-used-by-the-fake-parser")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    model_calls: list[str] = []
    monkeypatch.setattr(
        desktop_import_runner,
        "parse_structured_document",
        lambda *_args, **_kwargs: ParsedDocument((_block(line_start=0, line_end=0),), ()),
    )

    def model_transport(request, _connect_timeout_seconds):
        model_calls.append(request.operation)
        return "must not run"

    importer = DesktopTextImportService(kb_dir, model_gateway=DesktopModelGateway(model_transport))
    with pytest.raises(DesktopImportError) as captured:
        importer.import_text(source)

    assert captured.value.code == "document_ir_unlocated"
    assert model_calls == []
    task = importer.list_import_jobs()["jobs"][0]
    assert task["quarantine"]["error_code"] == "document_ir_unlocated"


def test_engine_health_reports_parser_readiness(monkeypatch) -> None:
    from openkb.engine.protocol import DesktopRequest
    from openkb.engine.server import DesktopEngineServer
    from openkb.parsers import runtime as runtime

    monkeypatch.setattr(runtime, "_legacy_resources_available", lambda: True)
    monkeypatch.setattr(runtime, "_ocr_resources_available", lambda: True)
    server = DesktopEngineServer(io.BytesIO(), io.BytesIO())
    server._handshake_complete = True

    health = server._dispatch(DesktopRequest("health", "engine.health", {}), None)

    assert health["status"] == "ready"
    assert health["parser_readiness"]["legacy_office"]["runtime_state"] in {
        "not_loaded",
        "ready",
    }
    assert health["parser_readiness"]["pdf_ocr"]["resource_state"] == "resources_ready"


def test_modern_office_auto_mode_uses_enhanced_recovery_only_when_direct_ir_is_insufficient(
    monkeypatch, tmp_path
) -> None:
    from openkb.importing.artifacts import ParsedDocument
    from openkb.parsers import document as parsers
    from openkb.parsers import office_ocr as desktop_office_ocr

    source = tmp_path / "image-heavy.docx"
    direct = ParsedDocument((_block("短文"),), ())
    enhanced = ParsedDocument((_block("Recovered selectable document content."),), ())
    calls: list[str] = []
    monkeypatch.setattr(parsers, "_parse_docx", lambda *_args: direct)
    monkeypatch.setattr(
        desktop_office_ocr,
        "enhance_office_document",
        lambda parsed, *, source_format: calls.append(source_format) or enhanced,
    )

    assert parsers.parse_structured_document(source, b"docx", parser_mode="fast") == direct
    assert parsers.parse_structured_document(source, b"docx", parser_mode="auto") == enhanced
    assert calls == ["docx"]
