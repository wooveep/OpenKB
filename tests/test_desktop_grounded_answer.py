"""Focused behavior checks for the Desktop vectorless grounded-answer baseline."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_answer_budget import answer_output_reserve_for_context
from openkb.desktop_answer_types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopKnowledgeGuidance,
    DesktopRetrievalPlan,
)
from openkb.desktop_grounded_answer import (
    DesktopGroundedAnswerService,
    _answer_prompt,
    generate_grounded_answer,
)
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_model_gateway import (
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelProviderResponse,
    DesktopModelResult,
)
from openkb.desktop_model_roles import DesktopRoleModelGateway
from openkb.desktop_model_settings import DesktopModelSettings
from openkb.desktop_retrieval import _source_image_matches_evidence
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _query_plan_response(
    request,
    *,
    terms: tuple[str, ...] = ("evidence",),
    facets: tuple[tuple[str, str], ...] = (("Requested answer", "Answer the question."),),
) -> str:
    payload = json.loads(request.content)
    seed_ids = [item["evidence_id"] for item in payload["seed_observations"]]
    return json.dumps(
        {
            "retrieval_plan": {"terms": list(terms)},
            "question_facet_plan": {
                "goal": "Answer the supplied question from evidence.",
                "facets": [
                    {"label": label, "description": description, "importance": "required"}
                    for label, description in facets
                ],
            },
            "initial_answer_coverage": [
                {
                    "facet_ordinal": ordinal,
                    "state": "covered" if seed_ids else "missing",
                    "evidence_ids": seed_ids[:1],
                }
                for ordinal, _facet in enumerate(facets)
            ],
        }
    )


def test_answer_prompt_exposes_guidance_structure_without_guidance_facts() -> None:
    prompt = _answer_prompt(
        "How do I deploy Alpha?",
        (
            DesktopEvidenceRef(
                "evidence-1",
                "document-1",
                "manual.md",
                "Install",
                {},
                "Run the evidence-backed installer.",
                ("fts",),
            ),
        ),
        (
            DesktopKnowledgeGuidance(
                route="generated/procedure/alpha",
                kind="procedure",
                authority="published_generation",
                title="Alpha deployment",
                content_markdown=(
                    "## Prerequisites\n\nUNSUPPORTED_GUIDANCE_FACT\n\n## Steps\n\n"
                    "1. Another unsupported fact."
                ),
                source_evidence_ids=("evidence-1",),
            ),
        ),
    )

    assert "Route: generated/procedure/alpha" in prompt
    assert "Outline: Prerequisites > Steps" in prompt
    assert "UNSUPPORTED_GUIDANCE_FACT" not in prompt
    assert "Another unsupported fact" not in prompt
    assert "Run the evidence-backed installer" in prompt


def test_citation_guard_removes_uncited_list_claims_without_semantic_classification() -> None:
    from openkb.desktop_grounded_answer import _citation_guarded_answer

    answer = """# Install cluster

- Prepare both nodes.

## Validation

- Stop nginx and verify VIP failover. [2]
- Confirm storage is healthy.

## Deployment notes

- Optional scope note without a citation.
"""

    guarded = _citation_guarded_answer(answer, evidence_count=2)

    assert "Stop nginx and verify VIP failover. [2]" in guarded
    assert "Confirm storage is healthy." not in guarded
    assert "Optional scope note without a citation." not in guarded


def test_answer_output_budget_expands_only_for_large_context_models() -> None:
    assert answer_output_reserve_for_context(4_096) == 2_048
    assert answer_output_reserve_for_context(16_384) == 2_048
    assert answer_output_reserve_for_context(32_768) == 4_096
    assert answer_output_reserve_for_context(1_000_000) == 32_768


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
    assert "query_planning_failed" in answer.degradations
    assert answer.status == "interrupted"
    assert answer.interruption_code == "model_response_invalid"
    assert answer.answer_text == ""
    assert DesktopGroundedAnswerService(kb_dir).list() == (answer,)


@pytest.mark.parametrize(
    ("observations", "expected_code"),
    (
        (DesktopModelOutputObservations(finish_reason="stop"), "empty_final_result"),
        (
            DesktopModelOutputObservations(
                finish_reason="stop",
                reasoning_observed=True,
                reasoning_chunk_count=1,
                reasoning_character_count=91,
            ),
            "reasoning_only_result",
        ),
        (
            DesktopModelOutputObservations(
                finish_reason="length",
                reasoning_observed=True,
                reasoning_chunk_count=2,
                reasoning_character_count=182,
                output_limit_reached=True,
            ),
            "reasoning_output_exhausted",
        ),
    ),
)
def test_answer_result_failures_are_explicit_interrupted_cards_without_retry(
    tmp_path, observations, expected_code
) -> None:
    kb_dir = tmp_path / expected_code
    source = tmp_path / f"{expected_code}.txt"
    source.write_text("OpenKB keeps grounded evidence available.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)
    grounded_calls = 0

    class ResultFailureTransport:
        def __call__(self, request, _timeout_seconds):
            return _query_plan_response(request)

        def stream_until_terminal(self, request, _timeout_seconds, _on_delta):
            nonlocal grounded_calls
            if request.operation == "query_planning":
                return _query_plan_response(request)
            grounded_calls += 1
            return DesktopModelProviderResponse("", observations=observations)

    answer = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(ResultFailureTransport()),
    ).answer("What does OpenKB keep available?")

    assert grounded_calls == 1
    assert answer.status == "interrupted"
    assert answer.interruption_code == expected_code
    assert answer.answer_text == ""
    assert answer.citations
    assert DesktopGroundedAnswerService(kb_dir).list() == (answer,)


def test_grounded_answer_reserves_default_reasoning_before_final_output() -> None:
    requests = []

    class CapturingAnswerGateway:
        def stream(self, request, **kwargs):
            requests.append(request)
            kwargs["on_delta"](1, "Grounded answer [1].")
            return DesktopModelResult("answer-call", "Grounded answer [1].", 1)

    terminal = CapturingAnswerGateway()
    gateway = DesktopRoleModelGateway(
        settings=DesktopModelSettings(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_base_url="https://api.deepseek.com",
            api_key="test-key",
            max_concurrent_model_calls=1,
        ),
        default_gateway=terminal,  # type: ignore[arg-type]
        analysis_gateway=terminal,  # type: ignore[arg-type]
        answer_gateway=terminal,  # type: ignore[arg-type]
    )
    pack = DesktopEvidencePack(
        retrieval_plan=DesktopRetrievalPlan("question", ("question",), "deterministic"),
        evidence=(
            DesktopEvidenceRef(
                "evidence-1",
                "document-1",
                "manual.md",
                "Answer",
                {},
                "Grounded evidence.",
                ("fts",),
            ),
        ),
    )

    generation = generate_grounded_answer("question", pack, model_gateway=gateway)

    assert generation.answer_text == "Grounded answer [1]."
    assert len(requests) == 1
    assert requests[0].generation_parameters == {"max_tokens": 40_960}


def test_reasoning_only_answer_is_replaced_only_after_an_explicit_successful_retry(
    tmp_path,
) -> None:
    kb_dir = tmp_path / "reasoning-retry"
    source = tmp_path / "reasoning-retry.txt"
    source.write_text("OpenKB keeps retryable grounded evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class ReasoningThenFinalTransport:
        def __init__(self) -> None:
            self.grounded_calls = 0

        def __call__(self, request, _timeout_seconds):
            return _query_plan_response(request)

        def stream_until_terminal(self, request, _timeout_seconds, on_delta):
            if request.operation == "query_planning":
                return _query_plan_response(request)
            self.grounded_calls += 1
            if self.grounded_calls == 1:
                return DesktopModelProviderResponse(
                    "",
                    observations=DesktopModelOutputObservations(
                        finish_reason="stop",
                        reasoning_observed=True,
                        reasoning_chunk_count=1,
                        reasoning_character_count=73,
                    ),
                )
            on_delta("Recovered cited answer [1].")
            return "Recovered cited answer [1]."

    transport = ReasoningThenFinalTransport()
    service = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(transport),
    )
    interrupted = service.answer("What evidence is retryable?")

    assert interrupted.status == "interrupted"
    assert interrupted.interruption_code == "reasoning_only_result"
    assert service.list() == (interrupted,)

    replacement = service.retry(interrupted.answer_id)

    assert transport.grounded_calls == 2
    assert replacement.answer_id == interrupted.answer_id
    assert replacement.created_at == interrupted.created_at
    assert replacement.status == "completed"
    assert replacement.answer_text == "Recovered cited answer [1]."
    assert service.list() == (replacement,)


def test_grounded_answer_streams_model_deltas_without_losing_baseline_terms(tmp_path):
    """A model plan augments deterministic retrieval and its answer is incremental."""
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "orbital-ledger.txt"
    source.write_text("The orbital ledger records every release window.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class StreamingTransport:
        def __call__(self, request, _timeout_seconds):
            assert request.operation == "query_planning"
            return _query_plan_response(request, terms=("unrelated",))

        def stream_until_terminal(self, request, _connect_timeout_seconds, on_delta):
            if request.operation == "query_planning":
                return _query_plan_response(request, terms=("unrelated",))
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


def test_grounded_answer_receives_source_backed_navigation_blueprint(tmp_path) -> None:
    kb_dir = tmp_path / "desktop-kb"
    source = tmp_path / "install-alpha.md"
    source.write_text(
        "# Alpha 安装\n\n先检查主机容量，再安装 Alpha，最后运行健康检查。\n",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    DesktopTextImportService(kb_dir).import_text(source)

    class BlueprintTransport:
        answer_prompt = ""

        def __call__(self, request, _timeout_seconds):
            if request.operation == "query_planning":
                return _query_plan_response(
                    request,
                    terms=("Alpha", "安装", "健康检查"),
                    facets=(
                        ("环境准备", "安装前需确认的条件。"),
                        ("执行安装", "证据支持的安装动作。"),
                        ("结果确认", "证据支持的完成状态。"),
                    ),
                )
            if request.operation == "knowledge_navigation_step":
                prompt = json.loads(request.content)
                evidence_ids = [item["evidence_id"] for item in prompt["evidence"]]
                return json.dumps(
                    {
                        "schema_version": "openkb.knowledge-navigation-step.v2",
                        "snapshot_id": prompt["snapshot_id"],
                        "coverage": [
                            {
                                "facet_id": facet["facet_id"],
                                "state": "covered",
                                "evidence_ids": evidence_ids[:1],
                            }
                            for facet in prompt["objective"]["facets"]
                        ],
                        "actions": [],
                        "decision": "stop",
                    }
                )
            raise AssertionError(request.operation)

        def stream_until_terminal(self, request, _connect_timeout_seconds, on_delta):
            assert request.operation == "grounded_answer"
            self.answer_prompt = request.content
            on_delta("按证据执行并验证。[1]")
            return "按证据执行并验证。[1]"

    transport = BlueprintTransport()
    answer = DesktopGroundedAnswerService(
        kb_dir,
        model_gateway=DesktopModelGateway(transport),
    ).answer("Alpha 如何安装")

    assert answer.retrieval_trace.navigation_stop_reason == "covered"
    assert "Answer Blueprint (navigation only" in transport.answer_prompt
    assert "环境准备 (required) — covered" in transport.answer_prompt
    assert "执行安装 (required) — covered" in transport.answer_prompt
    assert "Original Evidence is the only factual authority" in transport.answer_prompt


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

        def __call__(self, request, _timeout_seconds):
            return _query_plan_response(request)

        def stream_until_terminal(self, request, _connect_timeout_seconds, on_delta):
            if request.operation == "query_planning":
                return _query_plan_response(request)
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

        def __call__(self, request, _timeout_seconds):
            return _query_plan_response(request)

        def stream_until_terminal(self, request, _connect_timeout_seconds, on_delta):
            if request.operation == "query_planning":
                return _query_plan_response(request)
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
    assert answer.interruption_code == "model_network_transient"
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

        def __call__(self, request, _timeout_seconds):
            return _query_plan_response(request)

        def stream_until_terminal(self, request, _connect_timeout_seconds, on_delta):
            if request.operation == "query_planning":
                return _query_plan_response(request)
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
