"""Domain-neutral behavior at the Knowledge Candidate Pipeline seam."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_candidate_registry import DesktopKnowledgeCandidateRegistry
from openkb.desktop_import_runner import DesktopTextImportService
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_merge import deterministic_merge_knowledge
from openkb.desktop_knowledge_analysis_reuse import analysis_evidence_for_document_in
from openkb.desktop_knowledge_candidate_pipeline import DesktopKnowledgeCandidatePipeline
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_knowledge_analysis_accepts_role_free_claims_and_dynamic_summary_units() -> None:
    analysis = parse_knowledge_analysis(
        json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "A source-backed account of photosynthesis.",
                "document_summary": [
                    {
                        "label": "Energy conversion",
                        "text": "The document explains conversion of light energy.",
                        "source_evidence_ids": ["evidence-light"],
                    }
                ],
                "candidates": [
                    {
                        "kind": "concept",
                        "title": "Photosynthesis",
                        "aliases": [],
                        "identity_labels": ["plant biology"],
                        "admission": "admit",
                        "claims": [
                            {
                                "text": "Photosynthesis stores captured light as chemical energy.",
                                "source_evidence_ids": ["evidence-light"],
                                "applicability": [
                                    {
                                        "dimension": "organism group",
                                        "value": "oxygenic phototrophs",
                                        "source_evidence_ids": ["evidence-light"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        known_evidence_ids=frozenset({"evidence-light"}),
    )

    assert analysis.candidates[0].kind == "concept"
    assert analysis.candidates[0].admission == "admit"
    assert analysis.candidates[0].identity_labels == ("plant biology",)
    assert analysis.candidates[0].claims[0].applicability[0].dimension == "organism group"
    assert analysis.document_summary[0].label == "Energy conversion"
    serialized = analysis.as_dict()
    assert "role" not in json.dumps(serialized)
    assert "subtype" not in json.dumps(serialized)


def test_exact_claim_and_open_applicability_merge_aggregates_all_evidence() -> None:
    def analysis(evidence_id: str):
        return parse_knowledge_analysis(
            json.dumps(
                {
                    "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                    "analysis_scope": "document",
                    "document_description": "A plant biology source.",
                    "document_summary": [],
                    "candidates": [
                        {
                            "kind": "concept",
                            "title": "Photosynthesis",
                            "aliases": [],
                            "identity_labels": [],
                            "admission": "admit",
                            "claims": [
                                {
                                    "text": "Photosynthesis stores light as chemical energy.",
                                    "source_evidence_ids": [evidence_id],
                                    "applicability": [
                                        {
                                            "dimension": "organism group",
                                            "value": "oxygenic phototrophs",
                                            "source_evidence_ids": [evidence_id],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ),
            known_evidence_ids=frozenset({evidence_id}),
        )

    merged = deterministic_merge_knowledge((analysis("evidence-one"), analysis("evidence-two")))

    [claim] = merged.candidates[0].claims
    assert claim.source_evidence_ids == ("evidence-one", "evidence-two")
    assert claim.applicability[0].source_evidence_ids == (
        "evidence-one",
        "evidence-two",
    )


def test_candidate_pipeline_does_not_assign_semantics_from_url_or_path_shape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "source.md"
    source.write_text(
        "# Resource\n\nhttps://example.test/archive is the canonical archive identity.",
        encoding="utf-8",
    )
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    document = DesktopTextImportService(kb_dir).import_text(source).document
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        evidence = analysis_evidence_for_document_in(connection, document.document_id)
    evidence_id = evidence[-1][0]
    analysis = parse_knowledge_analysis(
        json.dumps(
            {
                "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
                "analysis_scope": "document",
                "document_description": "An archive resource.",
                "document_summary": [],
                "candidates": [
                    {
                        "kind": "entity",
                        "title": "https://example.test/archive",
                        "aliases": [],
                        "identity_labels": ["canonical archive"],
                        "admission": "admit",
                        "claims": [
                            {
                                "text": "The URL names the canonical archive identity.",
                                "source_evidence_ids": [evidence_id],
                                "applicability": [],
                            }
                        ],
                    }
                ],
            }
        ),
        known_evidence_ids=frozenset({evidence_id}),
    )

    outcome = DesktopKnowledgeCandidatePipeline(kb_dir).run_document(
        document_id=document.document_id,
        analysis=analysis,
        analysis_provenance_json='{"checkpoint_digest":"domain-neutral"}',
        evidence=evidence,
    )

    assert outcome.status == "ready"
    assert outcome.generation is not None
    assert outcome.generation.admitted_count == 1
    assert DesktopKnowledgeCandidateRegistry(kb_dir).inspect(document.document_id) == outcome
