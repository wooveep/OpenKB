"""Acceptance checks for the fixed Desktop retrieval-ablation gate."""

from __future__ import annotations

import json
import sqlite3

import pytest

from openkb.desktop_answer_types import (
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopRetrievalPlan,
)
from openkb.desktop_catalog_store import rebuild_pending_catalog
from openkb.desktop_graph_feature_flags import local_graph_default_enabled
from openkb.desktop_grounded_answer import prepare_grounded_evidence_pack
from openkb.desktop_import import DesktopTextImportService
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_generation_changes_in,
)
from openkb.desktop_knowledge_graph import DesktopKnowledgeGraphService
from openkb.desktop_model_gateway import DesktopModelCancelledError, DesktopModelGateway
from openkb.desktop_page_tree_selection import PageTreeSelectionResult
from openkb.desktop_retrieval import DesktopEvidenceRetriever
from openkb.desktop_retrieval_evaluation import (
    DesktopRetrievalEvaluationReport,
    DesktopRetrievalEvaluationSuite,
    DesktopRetrievalEvaluator,
    _page_tree_gate,
)
from openkb.desktop_retrieval_evaluation_types import (
    EVALUATION_CATEGORIES,
    DesktopEvaluationModelCost,
    DesktopRetrievalEvaluationCase,
    DesktopRetrievalEvaluationCaseResult,
    DesktopRetrievalEvaluationMetrics,
    evaluation_corpus_identity,
)
from openkb.desktop_workspace import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_fixed_suite_compares_all_vectorless_variants_and_gates_graph_gain(tmp_path, monkeypatch):
    """A snapshot suite covers every required question type and reports all metrics."""
    monkeypatch.setattr(
        "openkb.desktop_import_runner.start_graph_extraction", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "openkb.desktop_catalog_store.start_catalog_rebuilds", lambda *_args, **_kwargs: None
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

    def graph_response(request, _timeout_seconds):
        evidence = json.loads(request.content)["evidence"]
        nodes = []
        for ordinal, item in enumerate(evidence):
            text = item["text"]
            if text.startswith("The Relay") or text.startswith("The Gate"):
                label = "controlplane raptor"
            elif "policy" in text:
                label = "policyconflict zeta"
            elif "Archive" in text or "Ledger" in text:
                label = "retentiontheme omega"
            else:
                label = f"other-{ordinal}"
            nodes.append(
                {
                    "id": f"node-{ordinal}",
                    "evidence_id": item["evidence_id"],
                    "type": "entity",
                    "label": label,
                }
            )
        return json.dumps({"nodes": nodes, "edges": []})

    graph = DesktopKnowledgeGraphService(kb_dir, model_gateway=DesktopModelGateway(graph_response))
    for document in documents:
        assert graph.extract_document(document.document_id)
    assert rebuild_pending_catalog(kb_dir)

    answer_requests = []

    def answer_response(request, _timeout_seconds):
        if request.operation == "retrieval_plan":
            return json.dumps({"terms": request.content.casefold().split()})
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
        assert request.operation == "grounded_answer"
        answer_requests.append(request)
        if request.content.rstrip().endswith("Evidence:"):
            return "No available source evidence was found for this question."
        return request.content

    suite_path = tmp_path / "fixed-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
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
                    },
                    {
                        "case_id": "not-present",
                        "category": "absent_answer",
                        "question": "lunarfridge quantumzzle",
                        "expected_evidence": [],
                        "expected_answer_terms": [],
                        "expect_absent_answer": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    evaluator = DesktopRetrievalEvaluator(
        kb_dir,
        model_gateway=DesktopModelGateway(answer_response),
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
    }
    assert (
        report.metrics["local_graph"].evidence_recall_at_k
        > report.metrics["baseline"].evidence_recall_at_k
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
    assert report.local_graph_gate.passed
    assert report.catalog_generation_ids
    assert report.page_tree_providers
    suite = DesktopRetrievalEvaluationSuite.from_json(suite_path)
    evaluator.require_page_tree_promotion_eligible(report, suite)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "openkb.desktop_retrieval.select_page_tree_evidence",
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
    changed_suite_path.write_text(json.dumps(changed_suite_payload), encoding="utf-8")
    changed_suite = DesktopRetrievalEvaluationSuite.from_json(changed_suite_path)
    cost_blocked_report = evaluator.evaluate(changed_suite)
    assert not cost_blocked_report.gate.model_cost_within_budget
    assert not cost_blocked_report.gate.passed
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
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
        evaluator.promote_local_graph(report)
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
        evaluator.require_page_tree_promotion_eligible(report, suite)
    assert not local_graph_default_enabled(kb_dir)

    assert rebuild_pending_catalog(kb_dir)
    report = evaluator.evaluate(suite)
    assert report.gate.passed
    assert report.local_graph_gate.passed
    evaluator.promote_local_graph(report)
    assert local_graph_default_enabled(kb_dir)
    assert "knowledge_graph" in {
        channel
        for reference in DesktopEvidenceRetriever(kb_dir).retrieve("policyconflict zeta").evidence
        for channel in reference.channels
    }
    report_path = tmp_path / "report.json"
    report.write(report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["suite_snapshot_id"] == "desktop-vectorless-fixture-v1"
    assert payload["knowledge_snapshot_digest"] == report.knowledge_snapshot_digest
    assert payload["knowledge_snapshot_revision"] == report.knowledge_snapshot_revision
    assert payload["corpus_digest"] == report.corpus_digest
    assert payload["final_derived_snapshot_digest"] == report.final_derived_snapshot_digest
    assert payload["metrics"]["local_graph"]["evidence_recall_k"] == 6
    assert payload["gate"]["passed"] is True
    assert payload["local_graph_gate"]["passed"] is True
    assert payload["catalog_generation_ids"] == list(report.catalog_generation_ids)
    assert payload["page_tree_providers"]
    assert payload["page_tree_generations"] == [
        generation.as_dict() for generation in report.page_tree_generations
    ]

    legacy_payload = report.as_dict()
    legacy_payload["metrics"]["page_tree"] = legacy_payload["metrics"].pop("structure_lexical")
    for result in legacy_payload["results"]:
        if result["variant"] == "structure_lexical":
            result["variant"] = "page_tree"
    legacy_report_path = tmp_path / "legacy-report.json"
    legacy_report_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    restored = DesktopRetrievalEvaluationReport.read(legacy_report_path)

    assert "structure_lexical" in restored.metrics
    assert "page_tree" not in restored.metrics
    assert {result.variant for result in restored.results} == set(restored.metrics)

    other_kb_dir = tmp_path / "other-desktop-kb"
    DesktopKnowledgeBaseRuntime().create(other_kb_dir)
    with pytest.raises(ValueError, match="changed after this retrieval evaluation"):
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


def test_retrieval_planning_cost_counts_physical_retries(tmp_path) -> None:
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

    assert result.model_cost.model_calls == 2
    assert result.model_cost.input_characters == len("alpha question") * 2
    assert result.model_cost.output_characters == len('{"terms":["alpha"]}')


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
    assert result.degradations == ("retrieval_plan_cancelled",)
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
