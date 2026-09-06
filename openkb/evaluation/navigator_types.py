"""Serializable release-gate result for bounded adaptive navigation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesktopNavigatorEvaluationGate:
    passed: bool
    frozen_reference_complete: bool
    evidence_recall_non_regression: bool
    no_critical_evidence_loss: bool
    citation_precision_non_regression: bool
    absent_answer_non_regression: bool
    faithfulness_non_regression: bool
    retrieval_p95_within_budget: bool
    model_cost_within_budget: bool
    degradation_free: bool
    knowledge_snapshot_stable: bool
    fixed_suite_complete: bool
    original_evidence_complete: bool
    original_answer_points_non_regression: bool
    unsupported_claim_non_regression: bool
    original_citation_non_regression: bool
    original_latency_within_budget: bool
    original_model_cost_within_budget: bool
    source_integrity_healthy: bool
    original_reference_bound: bool
    model_profile_bound: bool
    repetitions_complete: bool

    def as_dict(self) -> dict[str, bool]:
        return {field: bool(getattr(self, field)) for field in self.__dataclass_fields__}
