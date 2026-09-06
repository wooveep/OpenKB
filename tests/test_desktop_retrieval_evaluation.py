"""Acceptance checks for the fixed Desktop retrieval-ablation gate."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from openkb.answers.grounded import prepare_grounded_evidence_pack
from openkb.answers.types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopKnowledgeGuidance,
    DesktopRetrievalPlan,
)
from openkb.evaluation.navigator_gate import navigator_evaluation_gate
from openkb.evaluation.retrieval import (
    DesktopRetrievalEvaluationSuite,
    DesktopRetrievalEvaluator,
    _case_result,
    _page_tree_gate,
)
from openkb.evaluation.retrieval_types import (
    EVALUATION_CATEGORIES,
    DesktopEvaluationAnswer,
    DesktopEvaluationEvidenceSelector,
    DesktopEvaluationModelCost,
    DesktopOriginalAgentObservation,
    DesktopRetrievalEvaluationCase,
    DesktopRetrievalEvaluationCaseResult,
    DesktopRetrievalEvaluationMetrics,
    evaluation_corpus_identity,
)
from openkb.importing.service import DesktopTextImportService
from openkb.knowledge.graph.feature_flags import local_graph_default_enabled
from openkb.knowledge.pages.generations import (
    KnowledgeGenerationChange,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_generation_changes_in,
)
from openkb.models.gateway import DesktopModelCancelledError, DesktopModelGateway
from openkb.page_tree.selection import PageTreeSelectionResult
from openkb.retrieval.catalog_store import rebuild_pending_catalog
from openkb.retrieval.service import DesktopEvidenceRetriever
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_evidence_recall_at_six_ignores_routed_answer_context_beyond_six() -> None:
    evidence = tuple(
        DesktopEvidenceRef(
            evidence_id=f"evidence-{ordinal}",
            document_id="document",
            document_name="guide.md",
            section=f"Section {ordinal}",
            locator={},
            excerpt=f"Detail {ordinal}",
            channels=("document_page_tree",),
        )
        for ordinal in range(1, 8)
    )
    pack = DesktopEvidencePack(
        DesktopRetrievalPlan("question", ("question",), "deterministic"),
        evidence,
    )
    case = DesktopRetrievalEvaluationCase(
        "routed-context",
        "multi_hop",
        "question",
        (),
        ("expected",),
        long_document=True,
    )

    result = _case_result(
        case,
        0,
        "baseline",
        ("evidence-7",),
        pack,
        DesktopEvaluationAnswer("expected"),
        0.0,
        0.0,
        0.0,
        DesktopEvaluationModelCost(),
    )

    assert result.evidence_recall_at_k == 0.0
    assert result.citation_precision == pytest.approx(1 / 7)
    assert result.answer_faithfulness == 1.0


def test_case_result_scores_actual_answer_citations_and_reviewed_unsupported_claims() -> None:
    expected = DesktopEvidenceRef(
        evidence_id="expected",
        document_id="document",
        document_name="guide.md",
        section="Expected",
        locator={},
        excerpt="The supported fact.",
        channels=("fts",),
    )
    distractor = DesktopEvidenceRef(
        evidence_id="distractor",
        document_id="document",
        document_name="guide.md",
        section="Distractor",
        locator={},
        excerpt="An unrelated fact.",
        channels=("fts",),
    )
    observation = DesktopOriginalAgentObservation(
        critical_evidence=(),
        answer_points=("supported fact",),
        unsupported_claim_markers=("invented default",),
        citation_precision=1.0,
        unsupported_claim_count=0,
        latency_ms=1_000.0,
        model_calls=3,
    )
    case = DesktopRetrievalEvaluationCase(
        "actual-citations",
        "local_fact",
        "question",
        (),
        ("supported fact",),
        original_observation=observation,
    )
    pack = DesktopEvidencePack(
        DesktopRetrievalPlan("question", ("question",), "deterministic"),
        (expected, distractor),
    )

    result = _case_result(
        case,
        1,
        "navigator",
        (expected.evidence_id,),
        pack,
        DesktopEvaluationAnswer(
            "The supported fact, plus an invented default.",
            cited_evidence_ids=(expected.evidence_id,),
        ),
        1.0,
        1.0,
        0.0,
        DesktopEvaluationModelCost(),
        original_evidence_ids=(expected.evidence_id,),
    )

    assert result.cited_evidence_ids == (expected.evidence_id,)
    assert result.citation_precision == 1.0
    assert result.original_evidence_recall_at_k == 1.0
    assert result.original_answer_point_coverage == 1.0
    assert result.unsupported_claim_count == 1


def test_case_result_detects_a_new_uncited_or_unsupported_claim_without_a_marker() -> None:
    evidence = DesktopEvidenceRef(
        evidence_id="expected",
        document_id="document",
        document_name="guide.md",
        section="Expected",
        locator={},
        excerpt="The relay forwards supported deliveries.",
        channels=("fts",),
    )
    observation = DesktopOriginalAgentObservation(
        critical_evidence=(DesktopEvaluationEvidenceSelector("guide.md", "relay forwards"),),
        answer_points=("relay forwards",),
        unsupported_claim_markers=(),
        citation_precision=1.0,
        unsupported_claim_count=0,
        latency_ms=1_000.0,
        model_calls=3,
    )
    case = DesktopRetrievalEvaluationCase(
        "novel-claim",
        "local_fact",
        "question",
        (),
        ("relay forwards",),
        original_observation=observation,
    )
    pack = DesktopEvidencePack(
        DesktopRetrievalPlan("question", ("question",), "deterministic"),
        (evidence,),
    )

    result = _case_result(
        case,
        1,
        "navigator",
        (evidence.evidence_id,),
        pack,
        DesktopEvaluationAnswer(
            "The relay forwards supported deliveries [1]. The moon controls routing [1].",
            cited_evidence_ids=(evidence.evidence_id,),
        ),
        1.0,
        1.0,
        0.0,
        DesktopEvaluationModelCost(),
        original_evidence_ids=(evidence.evidence_id,),
    )

    assert result.unsupported_claim_count >= 1


def test_case_result_rejects_a_cited_claim_with_a_conflicting_numeric_value() -> None:
    evidence = DesktopEvidenceRef(
        evidence_id="expected",
        document_id="document",
        document_name="guide.md",
        section="Network",
        locator={},
        excerpt="Configure the management service to listen on port 443.",
        channels=("fts",),
    )
    observation = DesktopOriginalAgentObservation(
        critical_evidence=(DesktopEvaluationEvidenceSelector("guide.md", "port 443"),),
        answer_points=("management service",),
        unsupported_claim_markers=(),
        citation_precision=1.0,
        unsupported_claim_count=0,
        latency_ms=1_000.0,
        model_calls=3,
    )
    case = DesktopRetrievalEvaluationCase(
        "conflicting-value",
        "local_fact",
        "question",
        (),
        ("management service",),
        original_observation=observation,
    )
    pack = DesktopEvidencePack(
        DesktopRetrievalPlan("question", ("question",), "deterministic"),
        (evidence,),
    )

    result = _case_result(
        case,
        1,
        "navigator",
        (evidence.evidence_id,),
        pack,
        DesktopEvaluationAnswer(
            "Configure the management service to listen on port 999 [1].",
            cited_evidence_ids=(evidence.evidence_id,),
        ),
        1.0,
        1.0,
        0.0,
        DesktopEvaluationModelCost(),
        original_evidence_ids=(evidence.evidence_id,),
    )

    assert result.unsupported_claim_count == 1


@pytest.mark.parametrize(
    ("source_text", "answer_text"),
    (
        ("Enable swap before installation.", "Disable swap before installation [1]."),
        ("Do not enable swap before installation.", "Enable swap before installation [1]."),
    ),
)
def test_case_result_rejects_a_cited_claim_with_the_opposite_operation(
    source_text: str, answer_text: str
) -> None:
    evidence = DesktopEvidenceRef(
        evidence_id="expected",
        document_id="document",
        document_name="guide.md",
        section="Installation",
        locator={},
        excerpt=source_text,
        channels=("fts",),
    )
    case = DesktopRetrievalEvaluationCase(
        "opposite-operation",
        "local_fact",
        "How should swap be configured?",
        (),
        ("swap",),
    )
    pack = DesktopEvidencePack(
        DesktopRetrievalPlan("question", ("swap",), "deterministic"),
        (evidence,),
    )

    result = _case_result(
        case,
        1,
        "navigator",
        (evidence.evidence_id,),
        pack,
        DesktopEvaluationAnswer(
            answer_text,
            cited_evidence_ids=(evidence.evidence_id,),
        ),
        1.0,
        1.0,
        0.0,
        DesktopEvaluationModelCost(),
    )

    assert result.unsupported_claim_count == 1


def test_original_citation_precision_uses_original_critical_evidence() -> None:
    expected = DesktopEvidenceRef(
        evidence_id="new-expected",
        document_id="document",
        document_name="guide.md",
        section="New",
        locator={},
        excerpt="New expected detail.",
        channels=("fts",),
    )
    original = replace(
        expected,
        evidence_id="original-critical",
        section="Original",
        excerpt="Original critical detail.",
    )
    observation = DesktopOriginalAgentObservation(
        critical_evidence=(DesktopEvaluationEvidenceSelector("guide.md", "Original critical"),),
        answer_points=("expected detail",),
        unsupported_claim_markers=(),
        citation_precision=1.0,
        unsupported_claim_count=0,
        latency_ms=1_000.0,
        model_calls=3,
    )
    case = DesktopRetrievalEvaluationCase(
        "original-precision",
        "local_fact",
        "question",
        (),
        ("expected detail",),
        original_observation=observation,
    )
    pack = DesktopEvidencePack(
        DesktopRetrievalPlan("question", ("question",), "deterministic"),
        (expected, original),
    )

    result = _case_result(
        case,
        1,
        "navigator",
        (expected.evidence_id,),
        pack,
        DesktopEvaluationAnswer(
            "New expected detail.",
            cited_evidence_ids=(expected.evidence_id,),
        ),
        1.0,
        1.0,
        0.0,
        DesktopEvaluationModelCost(),
        original_evidence_ids=(original.evidence_id,),
    )

    assert result.citation_precision == 1.0
    assert result.original_citation_precision == 0.0


def test_schema_two_suite_requires_a_frozen_original_observation(tmp_path) -> None:
    suite_path = tmp_path / "suite.json"
    payload = _minimal_suite_payload()
    payload["schema_version"] = 2
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="original_observation"):
        DesktopRetrievalEvaluationSuite.from_json(suite_path)


def test_schema_three_suite_requires_pinned_original_and_navigator_profiles(tmp_path) -> None:
    suite_path = tmp_path / "suite.json"
    payload = _minimal_suite_payload()
    payload["schema_version"] = 3
    payload["navigator_model_profile_digest"] = "3" * 64
    payload["minimum_navigator_repetitions"] = 3
    for case in payload["cases"]:
        assert isinstance(case, dict)
        absent = bool(case.get("expect_absent_answer"))
        case["original_observation"] = {
            "critical_evidence": [] if absent else case["expected_evidence"],
            "answer_points": [] if absent else case["expected_answer_terms"],
            "unsupported_claim_markers": [],
            "citation_precision": 1.0,
            "unsupported_claim_count": 0,
            "latency_ms": 1_000.0,
            "model_calls": 3,
            "absent_answer_correct": absent,
        }
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="original_commit_sha"):
        DesktopRetrievalEvaluationSuite.from_json(suite_path)

    for case in payload["cases"]:
        observation = case["original_observation"]
        assert isinstance(observation, dict)
        observation.update(
            {
                "original_commit_sha": "1" * 40,
                "model_profile_digest": "2" * 64,
                "sample_count": 3,
                "latency_variance_ms": 25.0,
            }
        )
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    suite = DesktopRetrievalEvaluationSuite.from_json(suite_path)

    assert suite.schema_version == 3
    assert suite.minimum_navigator_repetitions == 3
    assert suite.navigator_model_profile_digest == "3" * 64


def test_corpus_identity_includes_original_only_critical_evidence(tmp_path, monkeypatch) -> None:
    expected = DesktopEvaluationEvidenceSelector("expected.txt", "expected fact")
    original_only = DesktopEvaluationEvidenceSelector("original-only.txt", "critical fact")
    observation = DesktopOriginalAgentObservation(
        critical_evidence=(original_only,),
        answer_points=("critical fact",),
        unsupported_claim_markers=(),
        citation_precision=1.0,
        unsupported_claim_count=0,
        latency_ms=1.0,
        model_calls=1,
    )
    suite = DesktopRetrievalEvaluationSuite(
        snapshot_id="snapshot",
        cases=(
            DesktopRetrievalEvaluationCase(
                "identity",
                "local_fact",
                "question",
                (expected,),
                ("expected fact",),
                original_observation=observation,
            ),
        ),
        digest="suite-digest",
        schema_version=2,
    )
    monkeypatch.setattr(
        DesktopRetrievalEvaluationSuite,
        "from_json",
        classmethod(lambda _cls, _path: suite),
    )
    suite_path = tmp_path / "suite.json"
    suite_path.write_text("{}", encoding="utf-8")
    (tmp_path / "expected.txt").write_text("expected fact", encoding="utf-8")
    (tmp_path / "original-only.txt").write_text("critical fact", encoding="utf-8")

    _digest, names = evaluation_corpus_identity(suite_path)

    assert names == ("expected.txt", "original-only.txt")


def test_fixed_suite_compares_all_vectorless_variants_with_unknown_graph_semantics(
    tmp_path, monkeypatch
):
    """A snapshot suite reports every metric without inventing unavailable graph meaning."""
    monkeypatch.setattr(
        "openkb.importing.runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "openkb.retrieval.catalog_store.start_catalog_rebuilds", lambda *_args, **_kwargs: None
    )
    kb_dir = tmp_path / "desktop-kb"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    assert not local_graph_default_enabled(kb_dir)
    sources = {
        "delivery-system.txt": (
            "# controlplane raptor\n\n"
            "## Relay forwards\n\nThe Relay forwards deliveries.\n\n"
            "## Gate validates\n\nThe Gate validates deliveries.\n\n"
            + "\n\n".join(
                f"## Background {ordinal}\n\nBackground detail {ordinal}." for ordinal in range(42)
            )
        ),
        "policy-a.txt": "The archive policy permits encrypted storage.\n",
        "policy-b.txt": "The archive policy rejects plaintext storage.\n",
        "archive.txt": "The Archive keeps records for audit.\n",
        "ledger.txt": "The Ledger carries audit history.\n",
    }
    importer = DesktopTextImportService(kb_dir)
    documents = []
    for name, content in sources.items():
        source = tmp_path / name
        source.write_text(content, encoding="utf-8")
        documents.append(importer.import_text(source).document)

    assert rebuild_pending_catalog(kb_dir)

    answer_requests = []

    def answer_response(request, _timeout_seconds):
        if request.operation == "query_planning":
            payload = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in payload["seed_observations"]]
            return json.dumps(
                {
                    "retrieval_plan": {"terms": payload["question"].casefold().split()},
                    "question_facet_plan": {
                        "goal": "Answer the question from available Evidence.",
                        "facets": [
                            {
                                "label": "Evidence for the question",
                                "description": (
                                    "The available source facts relevant to the question."
                                ),
                                "importance": "required",
                            }
                        ],
                    },
                    "initial_answer_coverage": [
                        {
                            "facet_ordinal": 0,
                            "state": "covered" if evidence_ids else "missing",
                            "evidence_ids": evidence_ids[:1],
                        }
                    ],
                }
            )
        if request.operation == "page_tree_selection":
            payload = json.loads(request.content)
            question = payload["question"].casefold()
            selections = []
            for tree in payload["trees"]:
                node_ids = []
                for node in tree["nodes"]:
                    title = node["title"].casefold()
                    if node["kind"] != "paragraph":
                        continue
                    if "controlplane" in question and ("relay" in title or "gate" in title):
                        node_ids.append(node["node_id"])
                    elif "relay" in question and "relay" in title:
                        node_ids.append(node["node_id"])
                if node_ids:
                    selections.append({"document_id": tree["document_id"], "node_ids": node_ids})
            return json.dumps({"selections": selections})
        if request.operation == "knowledge_navigation_step":
            payload = json.loads(request.content)
            evidence_ids = [item["evidence_id"] for item in payload["evidence"]]
            return json.dumps(
                {
                    "schema_version": "openkb.knowledge-navigation-step.v2",
                    "snapshot_id": payload["snapshot_id"],
                    "coverage": [
                        {
                            "facet_id": facet["facet_id"],
                            "state": "covered" if evidence_ids else "missing",
                            "evidence_ids": evidence_ids[:1],
                        }
                        for facet in payload["objective"]["facets"]
                    ],
                    "actions": [],
                    "decision": "stop",
                }
            )
        assert request.operation == "grounded_answer"
        answer_requests.append(request)
        evidence_material = request.content.split("Evidence:\n", 1)[1]
        if not evidence_material.strip():
            return "No available source evidence was found for this question."
        lines = evidence_material.splitlines()
        claims = []
        for index, line in enumerate(lines):
            if line.startswith("[") and "]" in line and index + 1 < len(lines):
                ordinal = line.split("]", 1)[0][1:]
                claims.append(f"{lines[index + 1]} [{ordinal}].")
        return " ".join(claims)

    suite_path = tmp_path / "fixed-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "snapshot_id": "desktop-vectorless-fixture-v1",
                "max_graph_latency_ms": 1_000,
                "max_graph_model_calls": 20,
                "cases": [
                    {
                        "case_id": "local-relay",
                        "category": "local_fact",
                        "question": "relay forward",
                        "expected_evidence": [
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "The Relay forwards deliveries.",
                            },
                        ],
                        "expected_answer_terms": ["Relay", "forwards"],
                        "original_observation": {
                            "critical_evidence": [
                                {
                                    "document_name": "delivery-system.txt",
                                    "text_contains": "The Relay forwards deliveries.",
                                }
                            ],
                            "answer_points": ["Relay", "forwards"],
                            "unsupported_claim_markers": ["fabricated-original-claim"],
                            "citation_precision": 0.0,
                            "unsupported_claim_count": 0,
                            "latency_ms": 1_000_000,
                            "model_calls": 100,
                        },
                    },
                    {
                        "case_id": "control-path",
                        "category": "multi_hop",
                        "question": "controlplane raptor",
                        "expected_evidence": [
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "The Relay forwards deliveries.",
                            },
                            {
                                "document_name": "delivery-system.txt",
                                "text_contains": "The Gate validates deliveries.",
                            },
                        ],
                        "expected_answer_terms": ["Relay", "Gate"],
                        "long_document": True,
                        "original_observation": {
                            "critical_evidence": [
                                {
                                    "document_name": "delivery-system.txt",
                                    "text_contains": "The Relay forwards deliveries.",
                                },
                                {
                                    "document_name": "delivery-system.txt",
                                    "text_contains": "The Gate validates deliveries.",
                                },
                            ],
                            "answer_points": ["Relay", "Gate"],
                            "unsupported_claim_markers": ["fabricated-original-claim"],
                            "citation_precision": 0.0,
                            "unsupported_claim_count": 0,
                            "latency_ms": 1_000_000,
                            "model_calls": 100,
                        },
                    },
                    {
                        "case_id": "policy-conflict",
                        "category": "cross_document_conflict",
                        "question": "policyconflict zeta",
                        "expected_evidence": [
                            {"document_name": "policy-a.txt", "text_contains": "permits encrypted"},
                            {"document_name": "policy-b.txt", "text_contains": "rejects plaintext"},
                        ],
                        "expected_answer_terms": ["permits", "rejects"],
                        "original_observation": {
                            "critical_evidence": [
                                {
                                    "document_name": "policy-a.txt",
                                    "text_contains": "permits encrypted",
                                },
                                {
                                    "document_name": "policy-b.txt",
                                    "text_contains": "rejects plaintext",
                                },
                            ],
                            "answer_points": ["permits", "rejects"],
                            "unsupported_claim_markers": ["fabricated-original-claim"],
                            "citation_precision": 0.0,
                            "unsupported_claim_count": 0,
                            "latency_ms": 1_000_000,
                            "model_calls": 100,
                        },
                    },
                    {
                        "case_id": "retention-theme",
                        "category": "global_theme",
                        "question": "retentiontheme omega",
                        "expected_evidence": [
                            {"document_name": "archive.txt", "text_contains": "keeps records"},
                            {"document_name": "ledger.txt", "text_contains": "carries audit"},
                        ],
                        "expected_answer_terms": ["Archive", "Ledger"],
                        "original_observation": {
                            "critical_evidence": [
                                {
                                    "document_name": "archive.txt",
                                    "text_contains": "keeps records",
                                },
                                {
                                    "document_name": "ledger.txt",
                                    "text_contains": "carries audit",
                                },
                            ],
                            "answer_points": ["Archive", "Ledger"],
                            "unsupported_claim_markers": ["fabricated-original-claim"],
                            "citation_precision": 0.0,
                            "unsupported_claim_count": 0,
                            "latency_ms": 1_000_000,
                            "model_calls": 100,
                        },
                    },
                    {
                        "case_id": "not-present",
                        "category": "absent_answer",
                        "question": "lunarfridge quantumzzle",
                        "expected_evidence": [],
                        "expected_answer_terms": [],
                        "expect_absent_answer": True,
                        "original_observation": {
                            "critical_evidence": [],
                            "answer_points": [],
                            "unsupported_claim_markers": ["fabricated-original-claim"],
                            "citation_precision": 1.0,
                            "unsupported_claim_count": 0,
                            "latency_ms": 1_000_000,
                            "model_calls": 100,
                            "absent_answer_correct": True,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
    suite_payload["schema_version"] = 3
    suite_payload["navigator_model_profile_digest"] = "3" * 64
    suite_payload["minimum_navigator_repetitions"] = 3
    suite_payload["max_graph_model_calls"] = 100
    for case in suite_payload["cases"]:
        observation = case["original_observation"]
        observation.update(
            {
                "original_commit_sha": "1" * 40,
                "model_profile_digest": "2" * 64,
                "sample_count": 3,
                "latency_variance_ms": 25.0,
            }
        )
    suite_path.write_text(json.dumps(suite_payload), encoding="utf-8")

    evaluator = DesktopRetrievalEvaluator(
        kb_dir,
        model_gateway=DesktopModelGateway(answer_response),
        model_profile_digest="3" * 64,
    )
    report = evaluator.evaluate(DesktopRetrievalEvaluationSuite.from_json(suite_path))

    expected_corpus_digest, _files = evaluation_corpus_identity(suite_path)
    assert report.corpus_digest == expected_corpus_digest
    assert report.final_knowledge_snapshot_digest == report.knowledge_snapshot_digest
    assert report.final_knowledge_snapshot_revision == report.knowledge_snapshot_revision
    assert report.final_derived_snapshot_digest is not None
    assert set(report.metrics) == {
        "fts",
        "structure_lexical",
        "wiki",
        "baseline",
        "local_graph",
        "document_page_tree",
        "catalog + document_page_tree",
        "navigator",
    }
    assert (
        report.metrics["local_graph"].evidence_recall_at_k
        == report.metrics["baseline"].evidence_recall_at_k
    )
    assert (
        report.metrics["local_graph"].citation_precision
        >= report.metrics["baseline"].citation_precision
    )
    assert (
        report.metrics["local_graph"].answer_faithfulness
        >= report.metrics["baseline"].answer_faithfulness
    )
    assert report.metrics["local_graph"].model_cost.model_calls > 0
    assert report.metrics["catalog + document_page_tree"].retrieval_p95_ms >= 0
    assert report.metrics["catalog + document_page_tree"].absent_answer_accuracy == 1, [
        result
        for result in report.results
        if result.variant == "catalog + document_page_tree" and result.category == "absent_answer"
    ]
    assert report.metrics["catalog + document_page_tree"].model_cost.model_calls > 0
    assert report.stability["navigator"].latency_stddev_ms >= 0
    assert 0 <= report.stability["navigator"].stable_case_rate <= 1
    assert len(report.stability["navigator"].cases) == len(suite_payload["cases"])
    assert all(item.repetitions == 3 for item in report.stability["navigator"].cases)
    assert any(
        result.page_tree_selection_triggered
        and result.page_tree_generation_ids
        and result.long_document
        for result in report.results
        if result.variant == "catalog + document_page_tree"
    )
    assert any(
        result.page_tree_selection_triggered
        and result.page_tree_generation_ids
        and result.long_document
        for result in report.results
        if result.variant == "document_page_tree"
    )
    assert len(answer_requests) >= 35
    assert report.gate.passed
    assert report.gate.page_tree_selection_exercised
    assert report.gate.derived_identity_bound
    assert not report.local_graph_gate.passed
    assert report.source_integrity_healthy
    assert not report.navigator_gate.passed
    assert report.navigator_gate.frozen_reference_complete
    assert report.catalog_generation_ids
    assert report.page_tree_providers
    suite = DesktopRetrievalEvaluationSuite.from_json(suite_path)
    evaluator.require_page_tree_promotion_eligible(report, suite)
    with pytest.raises(ValueError, match="cannot promote the Navigator"):
        evaluator.require_navigator_promotion_eligible(report, suite)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "openkb.retrieval.service.select_page_tree_evidence",
            lambda *_args, **_kwargs: PageTreeSelectionResult(
                trigger_reasons=("long_document",),
                degradation_reasons=("page_tree_selection_failed",),
            ),
        )
        degraded_report = evaluator.evaluate(suite)
    assert not degraded_report.gate.degradation_free
    assert not degraded_report.gate.passed
    first_snapshot = evaluator._derived_snapshot()
    changed_snapshot = type(first_snapshot)(
        knowledge_snapshot_digest=first_snapshot.knowledge_snapshot_digest,
        knowledge_snapshot_revision=first_snapshot.knowledge_snapshot_revision,
        catalog_generation_ids=("changed-catalog-generation",),
        page_tree_generations=first_snapshot.page_tree_generations,
    )
    snapshots = iter((first_snapshot, changed_snapshot))
    with monkeypatch.context() as scoped:
        scoped.setattr(evaluator, "_derived_snapshot", lambda: next(snapshots))
        unstable_report = evaluator.evaluate(suite)
    assert not unstable_report.gate.derived_generations_stable
    assert not unstable_report.gate.passed
    changed_suite_path = tmp_path / "changed-suite.json"
    changed_suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
    changed_suite_payload["max_additional_model_calls_per_case"] = 0
    changed_suite_payload["max_navigator_model_calls_per_case"] = 0
    changed_suite_path.write_text(json.dumps(changed_suite_payload), encoding="utf-8")
    changed_suite = DesktopRetrievalEvaluationSuite.from_json(changed_suite_path)
    assert changed_suite.max_navigator_model_calls_per_case == 0
    cost_blocked_report = evaluator.evaluate(changed_suite)
    assert not cost_blocked_report.gate.model_cost_within_budget
    assert not cost_blocked_report.gate.passed
    assert not cost_blocked_report.navigator_gate.model_cost_within_budget
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
        evaluator.require_page_tree_promotion_eligible(report, changed_suite)
    generation_content = "This generated page is unrelated to the fixture questions."
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        publish_generation_changes_in(
            connection,
            current_generation_id=current_generation_id_in(connection),
            changes=(
                KnowledgeGenerationChange(
                    document_id=documents[0].document_id,
                    kind="concept",
                    title="Unrelated generated page",
                    normalized_title="unrelated generated page",
                    content_markdown=generation_content,
                    content_sha256=knowledge_content_sha256(generation_content),
                ),
            ),
            now="2026-08-15T00:00:00+00:00",
        )
        connection.commit()
    with pytest.raises(ValueError, match="non-passing retrieval evaluation"):
        evaluator.promote_local_graph(report)
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
        evaluator.require_page_tree_promotion_eligible(report, suite)
    assert not local_graph_default_enabled(kb_dir)

    assert rebuild_pending_catalog(kb_dir)
    report = evaluator.evaluate(suite)
    assert report.gate.passed
    assert not report.local_graph_gate.passed
    with pytest.raises(ValueError, match="non-passing retrieval evaluation"):
        evaluator.promote_local_graph(report)
    assert not local_graph_default_enabled(kb_dir)
    report = evaluator.evaluate(suite)
    assert not report.navigator_gate.passed
    assert report.navigator_gate.frozen_reference_complete
    with pytest.raises(ValueError, match="cannot promote the Navigator"):
        evaluator.require_navigator_promotion_eligible(report, suite)
    tainted_results = [
        replace(result, unsupported_claim_count=1)
        if result.variant == "navigator" and result.category != "absent_answer"
        else result
        for result in report.results
    ]
    tainted_gate = navigator_evaluation_gate(
        suite,
        tainted_results,
        report.metrics,
        knowledge_snapshot_stable=True,
        source_integrity_healthy=True,
    )
    assert not tainted_gate.unsupported_claim_non_regression
    assert not tainted_gate.passed
    report_path = tmp_path / "report.json"
    report.write(report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["suite_snapshot_id"] == "desktop-vectorless-fixture-v1"
    assert payload["knowledge_snapshot_digest"] == report.knowledge_snapshot_digest
    assert payload["knowledge_snapshot_revision"] == report.knowledge_snapshot_revision
    assert payload["corpus_digest"] == report.corpus_digest
    assert payload["final_derived_snapshot_digest"] == report.final_derived_snapshot_digest
    assert payload["metrics"]["local_graph"]["evidence_recall_k"] == 6
    assert "latency_stddev_ms" in payload["stability"]["navigator"]
    assert len(payload["stability"]["navigator"]["cases"]) == len(suite_payload["cases"])
    assert payload["gate"]["passed"] is True
    assert payload["local_graph_gate"]["passed"] is False
    assert payload["navigator_gate"]["passed"] is False
    assert payload["catalog_generation_ids"] == list(report.catalog_generation_ids)
    assert payload["page_tree_providers"]
    assert payload["page_tree_generations"] == [
        generation.as_dict() for generation in report.page_tree_generations
    ]

    other_kb_dir = tmp_path / "other-desktop-kb"
    DesktopKnowledgeBaseRuntime().create(other_kb_dir)
    with pytest.raises(ValueError, match="non-passing retrieval evaluation"):
        DesktopRetrievalEvaluator(other_kb_dir).promote_local_graph(report)
    assert not local_graph_default_enabled(other_kb_dir)

    late_source = tmp_path / "late-update.txt"
    late_source.write_text("The later source changes the retrieval corpus.\n", encoding="utf-8")
    importer.import_text(late_source)
    assert not local_graph_default_enabled(kb_dir)
    with pytest.raises(ValueError, match="corpus does not match the fixed suite"):
        evaluator.evaluate(suite)


def test_evaluation_snapshot_tracks_base_and_enrichment_generations(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    source = tmp_path / "source.md"
    source.write_text("# Topic\n\nA source fact.\n", encoding="utf-8")
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    imported = DesktopTextImportService(kb_dir).import_text(source)
    evaluator = DesktopRetrievalEvaluator(kb_dir)
    before = evaluator._derived_snapshot()
    identity = next(
        item
        for item in before.page_tree_generations
        if item.document_id == imported.document.document_id
    )
    assert identity.base_generation_id is not None

    database_path = desktop_state_database_path(kb_dir)
    with sqlite3.connect(database_path) as connection:
        values = (
            imported.document.document_id,
            identity.base_generation_id,
            "provider",
            "model",
            "prompt",
            "2026-08-20T00:00:00Z",
        )
        connection.execute(
            "INSERT INTO document_page_tree_enrichment_generations "
            "(enrichment_generation_id, document_id, base_generation_id, provider, model, "
            "prompt_digest, status, created_at) VALUES ('overlay-a', ?, ?, ?, ?, ?, 'current', ?)",
            values,
        )
        connection.execute(
            "INSERT INTO document_page_tree_enrichment_current "
            "(document_id, enrichment_generation_id, base_generation_id, activated_at) "
            "VALUES (?, 'overlay-a', ?, ?)",
            (imported.document.document_id, identity.base_generation_id, values[-1]),
        )
        connection.commit()
    overlay_a = evaluator._derived_snapshot()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE document_page_tree_enrichment_generations SET status = 'superseded' "
            "WHERE enrichment_generation_id = 'overlay-a'"
        )
        connection.execute(
            "INSERT INTO document_page_tree_enrichment_generations "
            "(enrichment_generation_id, document_id, base_generation_id, provider, model, "
            "prompt_digest, status, created_at) VALUES ('overlay-b', ?, ?, ?, ?, ?, 'current', ?)",
            values,
        )
        connection.execute(
            "UPDATE document_page_tree_enrichment_current SET "
            "enrichment_generation_id = 'overlay-b'"
        )
        connection.commit()
    overlay_b = evaluator._derived_snapshot()

    assert overlay_a != overlay_b
    assert overlay_a.page_tree_generations[0].enrichment_generation_id == "overlay-a"
    assert overlay_b.page_tree_generations[0].enrichment_generation_id == "overlay-b"


def test_evaluator_read_only_open_does_not_create_a_state_database(tmp_path) -> None:
    kb_dir = tmp_path / "missing-database"
    (kb_dir / ".openkb").mkdir(parents=True)

    with pytest.raises(ValueError, match="open knowledge base"):
        DesktopRetrievalEvaluator(kb_dir)._derived_snapshot()

    assert not desktop_state_database_path(kb_dir).exists()


def test_query_planning_cost_counts_the_single_physical_attempt(tmp_path) -> None:
    attempts = 0

    def transport(request, _timeout_seconds):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError
        return '{"terms":["alpha"]}'

    result = DesktopEvidenceRetriever(
        tmp_path,
        model_gateway=DesktopModelGateway(transport, sleep=lambda _seconds: None),
    ).build_plan_with_cost("alpha question")

    assert attempts == 1
    assert result.degradations == ("query_planning_failed",)
    assert result.model_cost.model_calls == 1
    assert result.model_cost.input_characters > len("alpha question")
    assert result.model_cost.output_characters == 0


def test_retrieval_planning_does_not_charge_an_attempt_that_never_started(tmp_path) -> None:
    class ExhaustedTransport:
        calls = 0

        def prepare_terminal_model_attempt(self, _is_cancelled):
            raise DesktopModelCancelledError()

        def __call__(self, _request, _timeout_seconds):
            self.calls += 1
            return '{"terms":["unreachable"]}'

    transport = ExhaustedTransport()
    result = DesktopEvidenceRetriever(
        tmp_path, model_gateway=DesktopModelGateway(transport)
    ).build_plan_with_cost("alpha question")

    assert transport.calls == 0
    assert result.degradations == ("query_planning_cancelled",)
    assert result.model_cost.model_calls == 0
    assert result.model_cost.input_characters == 0


def test_production_pack_bound_is_applied_before_evaluation_scoring() -> None:
    plan = DesktopRetrievalPlan("question", ("question",), "deterministic")
    pack = DesktopEvidencePack(
        retrieval_plan=plan,
        evidence=(
            DesktopEvidenceRef(
                "evidence",
                "document",
                "large.md",
                "Section",
                {},
                "x" * 12_100,
                ("fts",),
            ),
        ),
    )

    prepared = prepare_grounded_evidence_pack(pack)

    assert prepared.evidence == ()
    assert prepared.retrieval_trace.canonical_evidence_ids == ()


def test_grounding_budget_reserves_output_and_caps_guidance_at_one_quarter() -> None:
    plan = DesktopRetrievalPlan("question", ("question",), "deterministic")
    evidence = tuple(
        DesktopEvidenceRef(
            f"evidence-{ordinal}",
            f"document-{ordinal}",
            f"document-{ordinal}.md",
            "Section",
            {},
            f"Original evidence {ordinal}. " + "fact " * 80,
            ("fts",),
        )
        for ordinal in range(8)
    )
    guidance = tuple(
        DesktopKnowledgeGuidance(
            route=f"procedures/route-{ordinal}",
            kind="procedure",
            authority="published_generation",
            title=f"Procedure {ordinal}",
            content_markdown="- Navigation guidance. " + "guide " * 50,
            source_evidence_ids=(f"evidence-{ordinal}",),
        )
        for ordinal in range(4)
    )

    prepared = prepare_grounded_evidence_pack(
        DesktopEvidencePack(plan, evidence, guidance=guidance),
        context_capacity_tokens=4_096,
    )

    trace = prepared.retrieval_trace
    assert trace.grounding_input_budget_tokens == int((4_096 - 2_048) * 0.7)
    assert trace.evidence_input_tokens <= int(trace.grounding_input_budget_tokens * 0.75)
    assert trace.guidance_input_tokens <= (
        trace.grounding_input_budget_tokens - int(trace.grounding_input_budget_tokens * 0.75)
    )
    assert trace.evidence_input_tokens + trace.guidance_input_tokens <= (
        trace.grounding_input_budget_tokens
    )
    assert trace.navigation_routes == tuple(item.route for item in prepared.guidance)


def test_page_tree_gate_requires_standalone_gain_and_clean_execution() -> None:
    cases = tuple(
        DesktopRetrievalEvaluationCase(
            case_id=category,
            category=category,
            question=category,
            expected_evidence=(),
            expected_answer_terms=(),
            expect_absent_answer=category == "absent_answer",
            long_document=category == "multi_hop",
        )
        for category in sorted(EVALUATION_CATEGORIES)
    )
    suite = DesktopRetrievalEvaluationSuite("snapshot", cases, "digest")
    baseline = _gate_metrics(long_recall=0.5)
    metrics = {
        variant: baseline
        for variant in (
            "fts",
            "structure_lexical",
            "wiki",
            "baseline",
            "local_graph",
            "document_page_tree",
            "catalog + document_page_tree",
        )
    }
    metrics["document_page_tree"] = _gate_metrics(long_recall=0.0, degradation_runs=1)
    metrics["catalog + document_page_tree"] = _gate_metrics(long_recall=0.6)
    results = [
        DesktopRetrievalEvaluationCaseResult(
            case_id=case.case_id,
            category=case.category,
            repetition=1,
            variant=variant,
            expected_evidence_ids=(),
            evidence_recall_at_k=0.0 if variant == "document_page_tree" else 1.0,
            citation_precision=1.0,
            absent_answer_correct=case.expect_absent_answer,
            answer_faithfulness=1.0,
            latency_ms=1.0,
            retrieval_latency_ms=1.0,
            answer_latency_ms=0.0,
            model_cost=DesktopEvaluationModelCost(),
            answer_status="completed",
            long_document=case.long_document,
            page_tree_selection_triggered=variant == "catalog + document_page_tree",
            degradation_reasons=("page_tree_query_failed",)
            if variant == "document_page_tree"
            else (),
            page_tree_generation_ids=("generation",),
        )
        for variant in ("document_page_tree", "catalog + document_page_tree")
        for case in cases
    ]

    gate = _page_tree_gate(
        suite,
        results,
        metrics,
        knowledge_snapshot_stable=True,
        derived_generations_stable=True,
        derived_identity_bound=True,
    )

    assert not gate.long_document_recall_gain
    assert not gate.page_tree_selection_exercised
    assert not gate.degradation_free
    assert not gate.passed


def test_page_tree_latency_budget_cannot_exceed_ten_seconds(tmp_path) -> None:
    suite_path = tmp_path / "suite.json"
    payload = _minimal_suite_payload()
    payload["max_additional_retrieval_p95_ms"] = 20_000
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot exceed 10 seconds"):
        DesktopRetrievalEvaluationSuite.from_json(suite_path)


def _gate_metrics(
    *, long_recall: float, degradation_runs: int = 0
) -> DesktopRetrievalEvaluationMetrics:
    return DesktopRetrievalEvaluationMetrics(
        case_runs=5,
        evidence_recall_k=6,
        evidence_recall_at_k=long_recall,
        long_document_evidence_recall_at_k=long_recall,
        citation_precision=1.0,
        absent_answer_accuracy=1.0,
        answer_faithfulness=1.0,
        mean_latency_ms=1.0,
        retrieval_p95_ms=1.0,
        model_cost=DesktopEvaluationModelCost(),
        degradation_runs=degradation_runs,
    )


def _minimal_suite_payload() -> dict[str, object]:
    cases = []
    for category in sorted(EVALUATION_CATEGORIES):
        absent = category == "absent_answer"
        cases.append(
            {
                "case_id": category,
                "category": category,
                "question": category,
                "expected_evidence": []
                if absent
                else [{"document_name": "source.md", "text_contains": "fact"}],
                "expected_answer_terms": [] if absent else ["fact"],
                "expect_absent_answer": absent,
                "long_document": category == "multi_hop",
            }
        )
    return {"schema_version": 1, "snapshot_id": "snapshot", "cases": cases}
