"""Release-quality gate for the bounded adaptive Navigator variant."""

from __future__ import annotations

from openkb.desktop_retrieval_channels import DesktopEvaluationVariant
from openkb.desktop_retrieval_evaluation_types import (
    EVALUATION_CATEGORIES,
    PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS,
    DesktopNavigatorEvaluationGate,
    DesktopRetrievalEvaluationCaseResult,
    DesktopRetrievalEvaluationMetrics,
    DesktopRetrievalEvaluationSuite,
)


def navigator_evaluation_gate(
    suite: DesktopRetrievalEvaluationSuite,
    results: list[DesktopRetrievalEvaluationCaseResult],
    metrics: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationMetrics],
    *,
    knowledge_snapshot_stable: bool,
) -> DesktopNavigatorEvaluationGate:
    """Compare Navigator to baseline and to the suite's frozen behavioral oracle."""
    baseline = metrics["baseline"]
    navigator = metrics["navigator"]
    baseline_by_case = {
        (result.case_id, result.repetition): result
        for result in results
        if result.variant == "baseline"
    }
    navigator_by_case = {
        (result.case_id, result.repetition): result
        for result in results
        if result.variant == "navigator"
    }
    navigator_results = tuple(navigator_by_case.values())
    no_critical_loss = bool(baseline_by_case) and all(
        key in navigator_by_case
        and navigator_by_case[key].evidence_recall_at_k >= baseline_result.evidence_recall_at_k
        for key, baseline_result in baseline_by_case.items()
    )
    frozen_reference_complete = bool(navigator_results) and all(
        result.absent_answer_correct
        if result.category == "absent_answer"
        else result.evidence_recall_at_k == 1.0 and result.answer_faithfulness == 1.0
        for result in navigator_results
    )
    evidence_recall_non_regression = navigator.evidence_recall_at_k >= baseline.evidence_recall_at_k
    citation_precision_non_regression = navigator.citation_precision >= baseline.citation_precision
    absent_answer_non_regression = (
        navigator.absent_answer_accuracy >= baseline.absent_answer_accuracy
    )
    faithfulness_non_regression = navigator.answer_faithfulness >= baseline.answer_faithfulness
    latency_budget = min(
        suite.max_additional_retrieval_p95_ms,
        PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS,
    )
    retrieval_p95_within_budget = (
        navigator.retrieval_p95_ms - baseline.retrieval_p95_ms <= latency_budget
    )
    model_cost_within_budget = (
        navigator.model_cost.model_calls - baseline.model_cost.model_calls
        <= suite.max_navigator_model_calls_per_case * navigator.case_runs
    )
    degradation_free = bool(navigator_results) and all(
        not result.degradation_reasons and result.answer_status == "completed"
        for result in navigator_results
    )
    repetitions = {result.repetition for result in navigator_results}
    fixed_suite_complete = (
        bool(repetitions)
        and {case.category for case in suite.cases} == EVALUATION_CATEGORIES
        and len(navigator_results) == len(suite.cases) * len(repetitions)
    )
    checks = (
        frozen_reference_complete,
        evidence_recall_non_regression,
        no_critical_loss,
        citation_precision_non_regression,
        absent_answer_non_regression,
        faithfulness_non_regression,
        retrieval_p95_within_budget,
        model_cost_within_budget,
        degradation_free,
        knowledge_snapshot_stable,
        fixed_suite_complete,
    )
    return DesktopNavigatorEvaluationGate(
        passed=all(checks),
        frozen_reference_complete=frozen_reference_complete,
        evidence_recall_non_regression=evidence_recall_non_regression,
        no_critical_evidence_loss=no_critical_loss,
        citation_precision_non_regression=citation_precision_non_regression,
        absent_answer_non_regression=absent_answer_non_regression,
        faithfulness_non_regression=faithfulness_non_regression,
        retrieval_p95_within_budget=retrieval_p95_within_budget,
        model_cost_within_budget=model_cost_within_budget,
        degradation_free=degradation_free,
        knowledge_snapshot_stable=knowledge_snapshot_stable,
        fixed_suite_complete=fixed_suite_complete,
    )
