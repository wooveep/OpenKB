"""Focused behavior checks for the Desktop vectorless grounded-answer baseline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.desktop_grounded_answer import DesktopGroundedAnswerService
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import _source_image_matches_evidence
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_grounded_answer_persists_available_evidence_citations(tmp_path):
    """FTS/PageTree evidence becomes an auditable completed answer citation."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "onboarding.txt"
    source.write_text(
        "# Onboarding\n\nOpenKB makes the project overview searchable without embeddings.\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)

    answer = DesktopGroundedAnswerService(kb_dir).answer("How is the project overview searchable?")

    assert answer.retrieval_plan.source == "deterministic"
    assert answer.citations
    citation = answer.citations[0]
    assert citation.document_id == imported.document.document_id
    assert citation.document_name == "onboarding.txt"
    assert citation.section == "Onboarding"
    assert citation.locator
    assert {"fts", "page_tree"}.issubset(citation.channels)
    assert "OpenKB" in answer.answer_text
    assert DesktopGroundedAnswerService(kb_dir).list() == (answer,)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT document_name, section, locator_json FROM grounded_answer_citations"
        ).fetchone()[0:2] == ("onboarding.txt", "Onboarding")


def test_grounded_answer_excludes_unavailable_source_documents(tmp_path):
    """A document isolated after import never leaks into an Evidence Pack."""
    kb_dir = tmp_path / "desktop-kb"
    available = tmp_path / "available.txt"
    unavailable = tmp_path / "unavailable.txt"
    available.write_text("Meridian protocol is available to project members.", encoding="utf-8")
    unavailable.write_text(
        "Meridian protocol restricted secret must not be retrieved.", encoding="utf-8"
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    available_document = DesktopTextImportService(kb_dir).import_text(available).document
    unavailable_document = DesktopTextImportService(kb_dir).import_text(unavailable).document
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (unavailable_document.document_id,),
        )
        connection.commit()

    answer = DesktopGroundedAnswerService(kb_dir).answer("What is the Meridian protocol?")

    assert answer.citations
    assert {citation.document_id for citation in answer.citations} == {
        available_document.document_id
    }
    assert "restricted secret" not in answer.answer_text


def test_grounded_answer_persists_only_source_images_bound_to_its_citations(tmp_path):
    """Answer images retain a citation link and disappear with an unavailable source."""
    kb_dir = tmp_path / "desktop-kb"
    image = tmp_path / "evidence-pipeline.png"
    source = tmp_path / "evidence-pipeline.md"
    unrelated_image = tmp_path / "unrelated.png"
    unrelated_source = tmp_path / "unrelated.md"
    image_bytes = b"\x89PNG\r\n\x1a\nsource-image"
    image.write_bytes(image_bytes)
    unrelated_image.write_bytes(b"\x89PNG\r\n\x1a\nunrelated-image")
    source.write_text(
        "# Evidence pipeline\n\n"
        "The evidence pipeline diagram explains grounded answers.\n\n"
        "![Evidence pipeline diagram](evidence-pipeline.png)\n",
        encoding="utf-8",
    )
    unrelated_source.write_text(
        "# Separate notes\n\n![Satellite](unrelated.png)\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source).document
    DesktopTextImportService(kb_dir).import_text(unrelated_source)

    answer = DesktopGroundedAnswerService(kb_dir).answer(
        "What does the evidence pipeline diagram explain?"
    )

    assert len(answer.source_images) == 1
    source_image = answer.source_images[0]
    assert source_image.document_id == imported.document_id
    assert source_image.evidence_id in {citation.evidence_id for citation in answer.citations}
    assert source_image.locator["line_start"] == 5
    assert source_image.locator["source_image_id"] == source_image.source_image_id
    assert Path(source_image.file_path).read_bytes() == image_bytes

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT source_image_id, evidence_id FROM grounded_answer_source_images"
        ).fetchall() == [(source_image.source_image_id, source_image.evidence_id)]
        restored = DesktopGroundedAnswerService(kb_dir).list()[0]
        assert restored.source_images[0].locator["source_image_id"] == source_image.source_image_id
        connection.execute(
            "UPDATE source_documents SET availability = 'failed' WHERE document_id = ?",
            (imported.document_id,),
        )
        connection.commit()

    assert DesktopGroundedAnswerService(kb_dir).list()[0].source_images == ()


def test_source_image_matching_never_infers_a_link_from_page_or_slide_alone():
    """A matching container alone is not enough to show an unreferenced image."""
    assert not _source_image_matches_evidence("image-1", {"page": 2}, {"page": 2})
    assert not _source_image_matches_evidence("image-1", {"slide": 2}, {"slide": 2})
    assert _source_image_matches_evidence(
        "image-1", {"page": 2, "bbox": [0, 0, 10, 10]}, {"page": 2, "bbox": [5, 5, 15, 15]}
    )


def test_grounded_answer_falls_back_when_optional_model_calls_fail(tmp_path):
    """A bad planner or answer-model response cannot interrupt the evidence answer."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "fallback.txt"
    source.write_text("OpenKB keeps a local evidence baseline.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    answer = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(lambda *_args: None),
    ).answer("What baseline does OpenKB keep?")

    assert answer.citations
    assert answer.degradations == ("retrieval_plan_fallback", "answer_model_fallback")
    assert "Available source evidence" in answer.answer_text


def test_grounded_answer_streams_model_deltas_without_losing_baseline_terms(tmp_path):
    """A model plan augments deterministic retrieval and its answer is incremental."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "orbital-ledger.txt"
    source.write_text("The orbital ledger records every release window.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class StreamingTransport:
        def __call__(self, request, _timeout_seconds):
            assert request.operation == "retrieval_plan"
            return '{"terms": ["unrelated"]}'

        def stream(self, request, _timeout_seconds, on_delta):
            assert request.operation == "grounded_answer"
            on_delta("The orbital ")
            on_delta("ledger.")
            return "The orbital ledger."

    deltas: list[tuple[str, str, bool, int]] = []
    answer = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(StreamingTransport()),
    ).answer(
        "When does the orbital ledger record a release window?",
        on_delta=lambda answer_id, delta, replace, attempt: deltas.append(
            (answer_id, delta, replace, attempt)
        ),
    )

    assert "orbital" in answer.retrieval_plan.terms
    assert "unrelated" in answer.retrieval_plan.terms
    assert answer.citations
    assert answer.answer_text == "The orbital ledger."
    assert "".join(delta for _answer_id, delta, _replace, _attempt in deltas) == answer.answer_text
    assert len({answer_id for answer_id, _delta, _replace, _attempt in deltas}) == 1
    assert [(replace, attempt) for _answer_id, _delta, replace, attempt in deltas] == [
        (True, 1),
        (False, 1),
    ]


def test_grounded_answer_uses_knowledge_name_channel(tmp_path):
    """Canonical document and section names remain a useful logical wiki route."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "knowledge-brief.txt"
    source.write_text("Asterism.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    answer = DesktopGroundedAnswerService(kb_dir).answer("Where is the knowledge brief?")

    assert answer.citations
    assert "wiki" in answer.citations[0].channels


def test_grounded_answer_replaces_a_failed_stream_attempt(tmp_path):
    """A retry clears failed-attempt text before the next answer stream begins."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "retry.txt"
    source.write_text("OpenKB keeps an available local evidence baseline.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class RetryingStreamTransport:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, _request, _timeout_seconds):
            return '{"terms": ["evidence"]}'

        def stream(self, _request, _timeout_seconds, on_delta):
            self.attempts += 1
            if self.attempts == 1:
                on_delta("failed attempt")
                raise TimeoutError()
            on_delta("accepted answer")
            return "accepted answer"

    deltas: list[tuple[str, str, bool, int]] = []
    answer = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(RetryingStreamTransport()),
    ).answer(
        "What evidence does OpenKB keep?",
        on_delta=lambda answer_id, delta, replace, attempt: deltas.append(
            (answer_id, delta, replace, attempt)
        ),
    )

    assert answer.answer_text == "accepted answer"
    emitted = [(delta, replace, attempt) for _answer_id, delta, replace, attempt in deltas]
    assert emitted[0] == ("failed attempt", True, 1)
    next_attempt = [event for event in emitted if event[2] == 2]
    assert next_attempt
    assert next_attempt[0][1] is True
    assert next_attempt[-1][0] == "accepted answer"


def test_grounded_answer_replaces_partial_text_before_model_fallback(tmp_path):
    """A terminal stream error cannot append delayed text to the fallback answer."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "fallback-stream.txt"
    source.write_text("OpenKB keeps local evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class FailingStreamTransport:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, _request, _timeout_seconds):
            return '{"terms": ["evidence"]}'

        def stream(self, _request, _timeout_seconds, on_delta):
            self.attempts += 1
            if self.attempts == 1:
                on_delta("partial model text")
            raise TimeoutError()

    deltas: list[tuple[str, str, bool, int]] = []
    answer = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(FailingStreamTransport()),
    ).answer(
        "What does OpenKB keep?",
        on_delta=lambda answer_id, delta, replace, attempt: deltas.append(
            (answer_id, delta, replace, attempt)
        ),
    )

    assert answer.degradations == ("answer_model_fallback",)
    assert deltas[0][1:] == ("partial model text", True, 1)
    assert deltas[1][2:] == (True, 5)
    assert deltas[1][1].startswith("Available source evidence")
