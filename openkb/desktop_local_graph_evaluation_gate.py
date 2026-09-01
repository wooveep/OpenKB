"""Release-quality gate for the optional local-graph retrieval channel."""

from __future__ import annotations

from openkb.desktop_retrieval_channels import DesktopEvaluationVariant
from openkb.desktop_retrieval_evaluation_types import (
    DesktopLocalGraphEvaluationGate,
    DesktopRetrievalEvaluationCaseResult,
    DesktopRetrievalEvaluationMetrics,
    DesktopRetrievalEvaluationSuite,
)


def local_graph_evaluation_gate(
    suite: DesktopRetrievalEvaluationSuite,
    results: list[DesktopRetrievalEvaluationCaseResult],
    metrics: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationMetrics],
    *,
    knowledge_snapshot_stable: bool,
) -> DesktopLocalGraphEvaluationGate:
    """Compare the graph channel with its deterministic fused baseline."""
    baseline = metrics["baseline"]
    local_graph = metrics["local_graph"]
    baseline_by_case = {
        (result.case_id, result.repetition): result
        for result in results
        if result.variant == "baseline"
    }
    graph_by_case = {
        (result.case_id, result.repetition): result
        for result in results
        if result.variant == "local_graph"
    }
    no_critical_loss = bool(baseline_by_case) and all(
        key in graph_by_case
        and graph_by_case[key].evidence_recall_at_k >= result.evidence_recall_at_k
        for key, result in baseline_by_case.items()
    )
    evidence_recall_improved = local_graph.evidence_recall_at_k > baseline.evidence_recall_at_k
    citation_precision_non_regression = (
        local_graph.citation_precision >= baseline.citation_precision
    )
    faithfulness_non_regression = local_graph.answer_faithfulness >= baseline.answer_faithfulness
    latency_within_budget = (
        suite.max_graph_latency_ms is None
        or local_graph.mean_latency_ms <= suite.max_graph_latency_ms
    )
    model_cost_within_budget = (
        suite.max_graph_model_calls is None
        or local_graph.model_cost.model_calls <= suite.max_graph_model_calls
    )
    checks = (
        evidence_recall_improved,
        no_critical_loss,
        citation_precision_non_regression,
        faithfulness_non_regression,
        latency_within_budget,
        model_cost_within_budget,
        knowledge_snapshot_stable,
    )
    return DesktopLocalGraphEvaluationGate(
        passed=all(checks),
        evidence_recall_improved=evidence_recall_improved,
        no_critical_evidence_loss=no_critical_loss,
        citation_precision_non_regression=citation_precision_non_regression,
        faithfulness_non_regression=faithfulness_non_regression,
        latency_within_budget=latency_within_budget,
        model_cost_within_budget=model_cost_within_budget,
        knowledge_snapshot_stable=knowledge_snapshot_stable,
    )
