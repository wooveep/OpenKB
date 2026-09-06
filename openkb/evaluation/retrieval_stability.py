"""Repeated-run variance and stability records for retrieval evaluations."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import cast

from openkb.evaluation.retrieval_types import (
    DesktopRetrievalEvaluationCaseResult,
)
from openkb.retrieval.channels import (
    DESKTOP_EVALUATION_VARIANTS,
    DesktopEvaluationVariant,
    normalize_retrieval_channel,
)


@dataclass(frozen=True)
class DesktopRetrievalEvaluationCaseStability:
    case_id: str
    repetitions: int
    evidence_recall_stddev: float
    citation_precision_stddev: float
    answer_faithfulness_stddev: float
    latency_stddev_ms: float
    retrieval_latency_stddev_ms: float
    cited_evidence_stable: bool
    degradation_stable: bool
    answer_status_stable: bool
    stable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "repetitions": self.repetitions,
            "evidence_recall_stddev": self.evidence_recall_stddev,
            "citation_precision_stddev": self.citation_precision_stddev,
            "answer_faithfulness_stddev": self.answer_faithfulness_stddev,
            "latency_stddev_ms": self.latency_stddev_ms,
            "retrieval_latency_stddev_ms": self.retrieval_latency_stddev_ms,
            "cited_evidence_stable": self.cited_evidence_stable,
            "degradation_stable": self.degradation_stable,
            "answer_status_stable": self.answer_status_stable,
            "stable": self.stable,
        }


@dataclass(frozen=True)
class DesktopRetrievalEvaluationVariantStability:
    variant: DesktopEvaluationVariant
    repetitions: int
    stable_case_rate: float
    evidence_recall_stddev: float
    citation_precision_stddev: float
    answer_faithfulness_stddev: float
    latency_stddev_ms: float
    retrieval_latency_stddev_ms: float
    cases: tuple[DesktopRetrievalEvaluationCaseStability, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "variant": self.variant,
            "repetitions": self.repetitions,
            "stable_case_rate": self.stable_case_rate,
            "evidence_recall_stddev": self.evidence_recall_stddev,
            "citation_precision_stddev": self.citation_precision_stddev,
            "answer_faithfulness_stddev": self.answer_faithfulness_stddev,
            "latency_stddev_ms": self.latency_stddev_ms,
            "retrieval_latency_stddev_ms": self.retrieval_latency_stddev_ms,
            "cases": [item.as_dict() for item in self.cases],
        }


def stability_for(
    results: list[DesktopRetrievalEvaluationCaseResult],
    variant: DesktopEvaluationVariant,
) -> DesktopRetrievalEvaluationVariantStability:
    """Summarize within-case repeated-run variance for one retrieval variant."""
    selected = [item for item in results if item.variant == variant]
    grouped: dict[str, list[DesktopRetrievalEvaluationCaseResult]] = {}
    for item in selected:
        grouped.setdefault(item.case_id, []).append(item)
    cases = tuple(_case_stability(case_id, grouped[case_id]) for case_id in sorted(grouped))
    repetitions = max((item.repetitions for item in cases), default=0)
    return DesktopRetrievalEvaluationVariantStability(
        variant=variant,
        repetitions=repetitions,
        stable_case_rate=sum(item.stable for item in cases) / len(cases) if cases else 0.0,
        evidence_recall_stddev=_mean(item.evidence_recall_stddev for item in cases),
        citation_precision_stddev=_mean(item.citation_precision_stddev for item in cases),
        answer_faithfulness_stddev=_mean(item.answer_faithfulness_stddev for item in cases),
        latency_stddev_ms=_mean(item.latency_stddev_ms for item in cases),
        retrieval_latency_stddev_ms=_mean(item.retrieval_latency_stddev_ms for item in cases),
        cases=cases,
    )


def report_stability(
    value: object,
) -> dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationVariantStability]:
    """Restore optional stability records while retaining legacy report readability."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation stability must be an object.")
    restored: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationVariantStability] = {}
    for raw_variant, raw_record in value.items():
        if not isinstance(raw_variant, str) or not isinstance(raw_record, dict):
            raise ValueError("Desktop retrieval evaluation stability record is invalid.")
        variant = normalize_retrieval_channel(raw_variant)
        if variant not in DESKTOP_EVALUATION_VARIANTS or variant in restored:
            raise ValueError("Desktop retrieval evaluation stability variant is invalid.")
        typed_variant = cast(DesktopEvaluationVariant, variant)
        raw_cases = raw_record.get("cases")
        if not isinstance(raw_cases, list):
            raise ValueError("Desktop retrieval evaluation case stability is invalid.")
        restored[typed_variant] = DesktopRetrievalEvaluationVariantStability(
            variant=typed_variant,
            repetitions=_integer(raw_record, "repetitions"),
            stable_case_rate=_number(raw_record, "stable_case_rate"),
            evidence_recall_stddev=_number(raw_record, "evidence_recall_stddev"),
            citation_precision_stddev=_number(raw_record, "citation_precision_stddev"),
            answer_faithfulness_stddev=_number(raw_record, "answer_faithfulness_stddev"),
            latency_stddev_ms=_number(raw_record, "latency_stddev_ms"),
            retrieval_latency_stddev_ms=_number(raw_record, "retrieval_latency_stddev_ms"),
            cases=tuple(_case_from_dict(item) for item in raw_cases),
        )
    return restored


def _case_stability(
    case_id: str, values: list[DesktopRetrievalEvaluationCaseResult]
) -> DesktopRetrievalEvaluationCaseStability:
    recall = _stddev(item.evidence_recall_at_k for item in values)
    precision = _stddev(item.citation_precision for item in values)
    faithfulness = _stddev(item.answer_faithfulness for item in values)
    cited_stable = _one_value(frozenset(item.cited_evidence_ids) for item in values)
    degradation_stable = _one_value(item.degradation_reasons for item in values)
    status_stable = _one_value(item.answer_status for item in values)
    return DesktopRetrievalEvaluationCaseStability(
        case_id=case_id,
        repetitions=len(values),
        evidence_recall_stddev=recall,
        citation_precision_stddev=precision,
        answer_faithfulness_stddev=faithfulness,
        latency_stddev_ms=_stddev(item.latency_ms for item in values),
        retrieval_latency_stddev_ms=_stddev(item.retrieval_latency_ms for item in values),
        cited_evidence_stable=cited_stable,
        degradation_stable=degradation_stable,
        answer_status_stable=status_stable,
        stable=(
            recall == 0
            and precision == 0
            and faithfulness == 0
            and cited_stable
            and degradation_stable
            and status_stable
        ),
    )


def _case_from_dict(value: object) -> DesktopRetrievalEvaluationCaseStability:
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation case stability is invalid.")
    return DesktopRetrievalEvaluationCaseStability(
        case_id=_string(value, "case_id"),
        repetitions=_integer(value, "repetitions"),
        evidence_recall_stddev=_number(value, "evidence_recall_stddev"),
        citation_precision_stddev=_number(value, "citation_precision_stddev"),
        answer_faithfulness_stddev=_number(value, "answer_faithfulness_stddev"),
        latency_stddev_ms=_number(value, "latency_stddev_ms"),
        retrieval_latency_stddev_ms=_number(value, "retrieval_latency_stddev_ms"),
        cited_evidence_stable=_boolean(value, "cited_evidence_stable"),
        degradation_stable=_boolean(value, "degradation_stable"),
        answer_status_stable=_boolean(value, "answer_status_stable"),
        stable=_boolean(value, "stable"),
    )


def _stddev(values) -> float:
    material = tuple(float(value) for value in values)
    return statistics.pstdev(material) if len(material) > 1 else 0.0


def _mean(values) -> float:
    material = tuple(float(value) for value in values)
    return statistics.fmean(material) if material else 0.0


def _one_value(values) -> bool:
    return len(set(values)) <= 1


def _string(value: dict[object, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Desktop retrieval stability {field} is invalid.")
    return result


def _integer(value: dict[object, object], field: str) -> int:
    result = value.get(field)
    if not isinstance(result, int) or isinstance(result, bool) or result < 1:
        raise ValueError(f"Desktop retrieval stability {field} is invalid.")
    return result


def _number(value: dict[object, object], field: str) -> float:
    result = value.get(field)
    if not isinstance(result, (int, float)) or isinstance(result, bool) or result < 0:
        raise ValueError(f"Desktop retrieval stability {field} is invalid.")
    return float(result)


def _boolean(value: dict[object, object], field: str) -> bool:
    result = value.get(field)
    if not isinstance(result, bool):
        raise ValueError(f"Desktop retrieval stability {field} is invalid.")
    return result
