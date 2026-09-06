"""Cross-document review uses real immutable registry and page generation boundaries."""

import json
import sqlite3
from dataclasses import replace

import pytest
from test_desktop_corpus_synthesis_pipeline import (
    _candidate_fixture,
    _gateway,
    _page_response,
    _publish_candidate,
)

from openkb.importing.runner import DesktopTextImportService
from openkb.knowledge.analysis.reuse import analysis_evidence_for_document_in
from openkb.knowledge.corpus.knowledge_pipeline import CorpusKnowledgeSynthesisPipeline
from openkb.knowledge.corpus.review_store import CorpusReviewService
from openkb.workspace.paths import desktop_state_database_path


def _second_candidate(kb, tmp_path, analysis, *, title="Alpha", aliases=()):
    source = tmp_path / "second.md"
    source.write_text("# A second document\n\nAlpha now supports snapshots on a new platform.")
    document = DesktopTextImportService(kb).import_text(source).document
    with sqlite3.connect(desktop_state_database_path(kb)) as db:
        evidence = analysis_evidence_for_document_in(db, document.document_id)
    candidate = replace(
        analysis.entities[0],
        title=title,
        aliases=aliases,
        claims=(
            replace(
                analysis.entities[0].claims[0],
                text="Alpha supports a new platform.",
                source_evidence_ids=(evidence[0][0],),
            ),
        ),
    )
    new_analysis = replace(analysis, entities=(candidate,))
    _publish_candidate(kb, document.document_id, new_analysis, evidence, marker="second")
    return document.document_id, new_analysis, evidence


def test_conflicting_model_judgment_retains_page_until_bound_human_decision(tmp_path, monkeypatch):
    kb, _, analysis, *_ = _candidate_fixture(tmp_path, monkeypatch)
    pipeline = CorpusKnowledgeSynthesisPipeline(kb)
    first = pipeline.run_generation(gateway=_gateway(lambda req, _: _page_response(req)))
    _second_candidate(kb, tmp_path, analysis)
    calls = []

    def transport(request, _):
        calls.append(request.operation)
        payload = json.loads(request.content)
        if request.operation == "knowledge_claim_review":
            return json.dumps(
                {
                    "review_id": payload["review_id"],
                    "verdict": "conflicting",
                    "evidence_ids": [s["evidence_id"] for s in payload["evidence"]],
                }
            )
        return _page_response(request)

    pipeline.run_generation(gateway=_gateway(transport), force_generation=True)
    reviews = CorpusReviewService(kb)
    item = next(
        item for item in reviews.list_items() if item["reason"] == "claim_relationship_review"
    )
    assert item["status"] == "pending"
    assert item["decision"] == "conflicting"
    assert item["authority"] == "model"
    assert item["evidence"]
    assert "knowledge_claim_review" in calls
    with sqlite3.connect(desktop_state_database_path(kb)) as db:
        text = db.execute(
            "SELECT content_markdown FROM knowledge_generation_items WHERE generation_id = ?",
            (first.generation_id,),
        ).fetchone()[0]
        assert "new platform" not in text
    reviews.resolve(item["review_id"], "compatible")
    calls.clear()
    accepted = pipeline.run_generation(gateway=_gateway(transport), force_generation=True)
    assert accepted.status == "active"
    assert "knowledge_claim_review" not in calls
    assert reviews.list_items()[0]["authority"] == "human"
    with sqlite3.connect(desktop_state_database_path(kb)) as db:
        assert (
            "new platform"
            in db.execute(
                "SELECT content_markdown FROM knowledge_generation_items WHERE generation_id = ?",
                (accepted.generation_id,),
            ).fetchone()[0]
        )


def test_human_review_rejects_superseded_candidate_generation(tmp_path, monkeypatch):
    kb, _, analysis, *_ = _candidate_fixture(tmp_path, monkeypatch)
    document_id, second, evidence = _second_candidate(kb, tmp_path, analysis)
    CorpusKnowledgeSynthesisPipeline(kb).run_generation()
    reviews = CorpusReviewService(kb)
    item = reviews.list_items()[0]
    _publish_candidate(kb, document_id, second, evidence, marker="replacement")
    with pytest.raises(ValueError, match="superseded"):
        reviews.resolve(item["review_id"], "compatible")


def test_keep_separate_identity_decision_is_consumed_by_later_synthesis(tmp_path, monkeypatch):
    kb, _, analysis, *_ = _candidate_fixture(tmp_path, monkeypatch)
    pipeline = CorpusKnowledgeSynthesisPipeline(kb)
    pipeline.run_generation(gateway=_gateway(lambda req, _: _page_response(req)))
    _second_candidate(kb, tmp_path, analysis, title="Beta", aliases=("Alpha",))
    pipeline.run_generation()
    reviews = CorpusReviewService(kb)
    item = next(
        item
        for item in reviews.list_items()
        if item["reason"] == "semantic_identity_confirmation_required"
    )
    reviews.resolve(item["review_id"], "keep_separate")
    result = pipeline.run_generation(
        gateway=_gateway(lambda req, _: _page_response(req)), force_generation=True
    )
    assert result.status == "active"
    assert len(result.pages) == 2
    assert not any(item["status"] == "pending" for item in reviews.list_items())
