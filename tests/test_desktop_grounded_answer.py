"""Focused behavior checks for the Desktop vectorless grounded-answer baseline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openkb.desktop_answer_types import DesktopEvidencePack, DesktopRetrievalPlan
from openkb.desktop_grounded_answer import (
    DesktopGroundedAnswerService,
    generate_grounded_answer,
)
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import _source_image_matches_evidence
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def test_grounded_answer_persists_available_evidence_citations(tmp_path):
    """FTS/Structure Lexical evidence becomes an auditable completed citation."""
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
    assert {"fts", "structure_lexical"}.issubset(citation.channels)
    assert "page_tree" not in citation.channels
    assert "OpenKB" in answer.answer_text
    assert DesktopGroundedAnswerService(kb_dir).list() == (answer,)

    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT document_name, section, locator_json FROM grounded_answer_citations"
        ).fetchone()[0:2] == ("onboarding.txt", "Onboarding")


def test_grounded_answer_normalizes_legacy_page_tree_citations_on_read(tmp_path):
    """Persisted pre-rename citations remain readable under the canonical identity."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "legacy.txt"
    source.write_text("# Legacy\n\nThe archive keeps an audit trail.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    service = DesktopGroundedAnswerService(kb_dir)
    answer = service.answer("What keeps an audit trail?")
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE grounded_answer_citations SET channels_json = ? WHERE answer_id = ?",
            ('["fts", "page_tree"]', answer.answer_id),
        )
        connection.commit()

    restored = service.list()[0]

    assert restored.citations[0].channels == ("fts", "structure_lexical")


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


def test_grounded_answer_persists_an_interruption_when_the_answer_model_fails(tmp_path):
    """A terminal failure retains a retryable card instead of replacing it with a fallback."""
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
    assert "retrieval_plan_fallback" in answer.degradations
    assert answer.status == "interrupted"
    assert answer.interruption_code == "model_response_invalid"
    assert answer.answer_text == ""
    assert DesktopGroundedAnswerService(kb_dir).list() == (answer,)


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


def test_grounded_answer_persists_partial_text_when_the_model_stream_is_interrupted(tmp_path):
    """A terminal stream error keeps the text that was already visible to the user."""
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

    assert answer.status == "interrupted"
    assert answer.interruption_code == "model_timeout"
    assert answer.answer_text == "partial model text"
    assert deltas[0][1:] == ("partial model text", True, 1)
    assert len(deltas) == 1


def test_grounded_answer_retry_preserves_the_old_card_until_a_complete_replacement(tmp_path):
    """A failed retry cannot overwrite the interrupted text it was meant to repair."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "retryable-answer.txt"
    source.write_text("OpenKB keeps local evidence available.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class RetryTransport:
        def __init__(self) -> None:
            self.stream_attempts = 0

        def __call__(self, _request, _timeout_seconds):
            return '{"terms": ["evidence"]}'

        def stream(self, _request, _timeout_seconds, on_delta):
            self.stream_attempts += 1
            if self.stream_attempts < 3:
                on_delta(f"partial attempt {self.stream_attempts}")
                raise ValueError("invalid response")
            on_delta("complete replacement")
            return "complete replacement"

    service = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(RetryTransport()),
    )
    interrupted = service.answer("What does OpenKB keep?")
    failed_retry = service.retry(interrupted.answer_id)

    assert interrupted.status == "interrupted"
    assert interrupted.answer_text == "partial attempt 1"
    assert failed_retry == interrupted
    assert service.list() == (interrupted,)

    replacement = service.retry(interrupted.answer_id)

    assert replacement.answer_id == interrupted.answer_id
    assert replacement.created_at == interrupted.created_at
    assert replacement.status == "completed"
    assert replacement.answer_text == "complete replacement"
    assert DesktopGroundedAnswerService(kb_dir).list() == (replacement,)


def test_grounded_answer_stops_deterministic_stream_at_the_visible_partial_text(tmp_path):
    """Stopping after a local fallback delta persists an interrupted card, not a full answer."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "local-answer.txt"
    source.write_text("OpenKB keeps local evidence. " * 100, encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    cancelled = False
    deltas: list[str] = []

    def on_delta(_answer_id, delta, _replace, _attempt) -> None:
        nonlocal cancelled
        deltas.append(delta)
        cancelled = True

    answer = DesktopGroundedAnswerService(kb_dir).answer(
        "What evidence does OpenKB keep?",
        on_delta=on_delta,
        is_cancelled=lambda: cancelled,
    )

    assert answer.status == "interrupted"
    assert answer.interruption_code == "answer_cancelled"
    assert answer.answer_text == deltas[0]
    assert len(deltas) == 1


def test_grounded_answer_does_not_charge_when_provider_never_starts() -> None:
    class ExhaustedTransport:
        calls = 0

        def prepare_model_attempt(self, _is_cancelled, _remaining_seconds):
            return False

        def __call__(self, _request, _timeout_seconds):
            self.calls += 1
            return "unreachable"

    transport = ExhaustedTransport()
    pack = DesktopEvidencePack(
        retrieval_plan=DesktopRetrievalPlan("question", ("question",), "deterministic"),
        evidence=(),
    )

    generation = generate_grounded_answer(
        "question", pack, model_gateway=DesktopModelGateway(transport)
    )

    assert transport.calls == 0
    assert generation.answer_text is None
    assert generation.model_calls == 0
    assert generation.model_input_characters == 0
