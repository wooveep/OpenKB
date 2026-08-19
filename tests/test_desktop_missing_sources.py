"""Missing Source Candidate persistence, binding, and destructive dismissal."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openkb.desktop_import import DesktopImportError, DesktopTextImportService
from openkb.desktop_knowledge_analysis import KNOWLEDGE_ANALYSIS_SCHEMA_VERSION
from openkb.desktop_knowledge_pages import DesktopKnowledgePageService
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_missing_sources import DesktopMissingSourceService
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime


def _analysis_response(request_content: str) -> str:
    evidence_id = str(json.loads(request_content)["evidence"][0]["evidence_id"])
    return json.dumps(
        {
            "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
            "analysis_scope": "document",
            "document_description": "One valid claim and reviewable unsupported claims.",
            "concepts": [
                {
                    "title": "Supported knowledge",
                    "aliases": [],
                    "tags": [],
                    "claims": [
                        {
                            "text": "The source documents an available fact.",
                            "source_evidence_ids": [evidence_id],
                        }
                    ],
                },
                {
                    "title": "Needs binding",
                    "aliases": ["unresolved alias"],
                    "tags": ["review"],
                    "claims": [
                        {
                            "text": "A useful claim still needs a source.",
                            "source_evidence_ids": [],
                        },
                        {
                            "text": "A second claim references an unknown source.",
                            "source_evidence_ids": ["unknown-evidence"],
                        },
                    ],
                },
            ],
            "entities": [],
        }
    )


def _knowledge_base_with_missing_sources(
    tmp_path: Path, *, response=_analysis_response
) -> tuple[Path, str]:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "source.md"
    source.write_text("# Evidence\n\nOriginal available evidence.", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    def analyze(request, _timeout_seconds):
        return response(request.content)

    result = DesktopTextImportService(
        kb_dir,
        model_gateway=DesktopModelGateway(
            analyze, provider_name="review-provider", model_name="review-model"
        ),
    ).import_text(source)
    return kb_dir, result.document.document_id


def test_schema_valid_missing_claims_do_not_block_document_or_valid_knowledge(
    tmp_path: Path,
) -> None:
    kb_dir, document_id = _knowledge_base_with_missing_sources(tmp_path)
    missing = DesktopMissingSourceService(kb_dir).list_candidates()

    assert len(missing) == 2
    assert {candidate.reason for candidate in missing} == {
        "source_not_provided",
        "source_reference_unresolved",
    }
    assert all(candidate.document_id == document_id for candidate in missing)
    assert all(candidate.as_dict()["category"] == "missing_source" for candidate in missing)
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT availability FROM source_documents WHERE document_id = ?", (document_id,)
        ).fetchone() == ("available",)
        published = "\n".join(
            str(row[0])
            for row in connection.execute(
                """
                SELECT content_markdown FROM knowledge_generation_state AS state
                JOIN knowledge_generation_items AS items
                    ON items.generation_id = state.current_generation_id
                """
            ).fetchall()
        )
    assert "The source documents an available fact." in published
    assert "still needs a source" not in published
    assert "unknown source" not in published
    projection = "\n".join(
        path.read_text(encoding="utf-8") for path in (kb_dir / "knowledge-pages").rglob("*.md")
    )
    assert "still needs a source" not in projection
    assert "unknown source" not in projection


def test_binding_routes_through_generated_knowledge_and_working_draft(
    tmp_path: Path,
) -> None:
    kb_dir, _document_id = _knowledge_base_with_missing_sources(tmp_path)
    service = DesktopMissingSourceService(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    candidates = service.list_candidates()
    source = pages.search_sources("Original available evidence")[0]
    no_reference = next(item for item in candidates if item.reason == "source_not_provided")

    generated = service.bind(no_reference.candidate_id, source.evidence_id)

    assert generated.outcome == "generated"
    assert generated.remaining_count == 1
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT items.content_markdown, sources.evidence_id
            FROM knowledge_generation_state AS state
            JOIN knowledge_generation_items AS items
                ON items.generation_id = state.current_generation_id
            JOIN knowledge_generation_item_sources AS sources
                ON sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
            WHERE items.normalized_title = 'needs binding'
            """
        ).fetchone()
    assert row is not None
    assert "A useful claim still needs a source.[^src-" in str(row[0])
    assert row[1] == source.evidence_id

    remaining = service.list_candidates()[0]
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title="Needs binding",
        content_markdown=remaining.claim_text,
    )
    bound = service.bind(remaining.candidate_id, source.evidence_id)
    assert bound.outcome == "working_draft"
    page = pages.get_page(draft.page_id)
    assert page.working_draft is not None
    assert "[^src-" in page.working_draft.content_markdown
    assert page.publication_diagnostics == ()
    assert pages.publish(draft.page_id).published_revision is not None
    assert DesktopKnowledgeReconciliationService(kb_dir).list_conflicts() == ()


def test_binding_matching_user_revision_creates_a_draft_with_the_new_source(
    tmp_path: Path,
) -> None:
    kb_dir, _document_id = _knowledge_base_with_missing_sources(tmp_path)
    service = DesktopMissingSourceService(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = next(
        item for item in service.list_candidates() if item.reason == "source_not_provided"
    )
    original_source = pages.search_sources("Original available evidence")[0]
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title=candidate.title,
        content_markdown=candidate.claim_text,
    )
    pages.bind_source(draft.page_id, candidate.claim_text, original_source.evidence_id)
    published = pages.publish(draft.page_id)

    second_path = tmp_path / "second.md"
    second_path.write_text("# Other evidence\n\nIndependent supporting text.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(second_path)
    second_source = pages.search_sources("Independent supporting text")[0]

    result = service.bind(candidate.candidate_id, second_source.evidence_id)

    assert result.outcome == "working_draft"
    page = pages.get_page(draft.page_id)
    assert page.published_revision == published.published_revision
    assert page.working_draft is not None
    assert {item.evidence_id for item in page.working_draft.source_map} == {
        original_source.evidence_id,
        second_source.evidence_id,
    }
    assert page.publication_diagnostics == ()


def test_partial_user_revision_claim_routes_to_reconciliation(
    tmp_path: Path,
) -> None:
    kb_dir, _document_id = _knowledge_base_with_missing_sources(tmp_path)
    service = DesktopMissingSourceService(kb_dir)
    pages = DesktopKnowledgePageService(kb_dir)
    candidate = next(
        item for item in service.list_candidates() if item.reason == "source_not_provided"
    )
    original_source = pages.search_sources("Original available evidence")[0]
    published_claim = f"{candidate.claim_text} Extra context."
    draft = pages.save_draft(
        page_id=None,
        kind="concept",
        title=candidate.title,
        content_markdown=published_claim,
    )
    pages.bind_source(draft.page_id, published_claim, original_source.evidence_id)
    pages.publish(draft.page_id)

    second_path = tmp_path / "second.md"
    second_path.write_text("# Other evidence\n\nIndependent supporting text.", encoding="utf-8")
    DesktopTextImportService(kb_dir).import_text(second_path)
    second_source = pages.search_sources("Independent supporting text")[0]

    result = service.bind(candidate.candidate_id, second_source.evidence_id)

    assert result.outcome == "review_required"
    assert pages.get_page(draft.page_id).working_draft is None
    assert len(DesktopKnowledgeReconciliationService(kb_dir).list_conflicts()) == 1


def test_binding_rejects_reserved_source_markers_and_retains_candidate(
    tmp_path: Path,
) -> None:
    def marker_response(request_content: str) -> str:
        payload = json.loads(_analysis_response(request_content))
        payload["concepts"][1]["claims"][0]["text"] = (
            "The model inserted a bogus marker.[^src-bogus]"
        )
        return json.dumps(payload)

    kb_dir, _document_id = _knowledge_base_with_missing_sources(tmp_path, response=marker_response)
    service = DesktopMissingSourceService(kb_dir)
    candidate = next(
        item for item in service.list_candidates() if "bogus marker" in item.claim_text
    )
    source = DesktopKnowledgePageService(kb_dir).search_sources("Original available evidence")[0]

    with pytest.raises(DesktopImportError) as invalid:
        service.bind(candidate.candidate_id, source.evidence_id)

    assert invalid.value.code == "knowledge_source_claim_invalid"
    assert candidate.candidate_id in {item.candidate_id for item in service.list_candidates()}
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        generated = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT content_markdown FROM knowledge_generation_items"
            ).fetchall()
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_missing_source_resolution_records"
        ).fetchone() == (0,)
    assert "src-bogus" not in generated


def test_bulk_dismiss_deletes_candidate_bodies_and_keeps_minimal_records(
    tmp_path: Path,
) -> None:
    kb_dir, _document_id = _knowledge_base_with_missing_sources(tmp_path)
    service = DesktopMissingSourceService(kb_dir)
    candidate_ids = tuple(item.candidate_id for item in service.list_candidates())

    result = service.dismiss(candidate_ids)

    assert result.resolved_candidate_ids == candidate_ids
    assert result.remaining_count == 0
    assert service.list_candidates() == ()
    with sqlite3.connect(kb_dir / ".openkb" / "state.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT candidate_id, decision, evidence_id, outcome
            FROM knowledge_missing_source_resolution_records ORDER BY resolved_at, candidate_id
            """
        ).fetchall() == sorted(
            ((candidate_id, "dismissed", None, "dismissed") for candidate_id in candidate_ids)
        )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(knowledge_missing_source_resolution_records)"
            ).fetchall()
        }
        checkpoint = str(
            connection.execute(
                """
                SELECT runtime.checkpoint_json
                FROM stage_run_runtime AS runtime
                JOIN stage_runs AS stages ON stages.stage_run_id = runtime.stage_run_id
                WHERE stages.stage = 'model_analysis' AND stages.status = 'completed'
                """
            ).fetchone()[0]
        )
    assert "claim_text" not in columns
    assert "content_markdown" not in columns
    assert "The source documents an available fact." in checkpoint
    assert "A useful claim still needs a source." not in checkpoint
    assert "A second claim references an unknown source." not in checkpoint
    with pytest.raises(DesktopImportError) as deleted:
        service.bind(candidate_ids[0], "not-restorable")
    assert deleted.value.code == "missing_source_candidate_not_found"
