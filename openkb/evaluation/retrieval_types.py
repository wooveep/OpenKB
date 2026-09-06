"""Versioned contracts for fixed Desktop retrieval evaluation reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, cast

from openkb.evaluation.navigator_types import DesktopNavigatorEvaluationGate
from openkb.evaluation.original_agent_observation import (
    DesktopEvaluationEvidenceSelector,
    DesktopOriginalAgentObservation,
    evidence_selector,
    original_agent_observation,
)
from openkb.evaluation.retrieval_validation import (
    optional_nonnegative_int as _optional_nonnegative_int,
)
from openkb.retrieval.channels import (
    DESKTOP_EVALUATION_VARIANTS,
    DesktopEvaluationVariant,
    normalize_retrieval_channel,
)

if TYPE_CHECKING:
    from openkb.evaluation.retrieval_stability import (
        DesktopRetrievalEvaluationVariantStability,
    )
from openkb.evaluation.retrieval_validation import (
    optional_positive_float as _optional_positive_float,
)
from openkb.evaluation.retrieval_validation import (
    optional_report_bool as _optional_report_bool,
)
from openkb.evaluation.retrieval_validation import (
    optional_report_float as _optional_report_float,
)
from openkb.evaluation.retrieval_validation import (
    optional_sha256 as _optional_sha256,
)
from openkb.evaluation.retrieval_validation import (
    optional_string as _optional_string,
)
from openkb.evaluation.retrieval_validation import (
    report_bool as _report_bool,
)
from openkb.evaluation.retrieval_validation import (
    report_float as _report_float,
)
from openkb.evaluation.retrieval_validation import (
    report_int as _report_int,
)
from openkb.evaluation.retrieval_validation import (
    required_sha256 as _required_sha256,
)
from openkb.evaluation.retrieval_validation import (
    required_string as _required_string,
)
from openkb.evaluation.retrieval_validation import (
    required_strings as _required_strings,
)
from openkb.locks import atomic_write_text

EvaluationCategory = Literal[
    "local_fact", "multi_hop", "cross_document_conflict", "global_theme", "absent_answer"
]
EVALUATION_CATEGORIES: frozenset[EvaluationCategory] = frozenset(
    ("local_fact", "multi_hop", "cross_document_conflict", "global_theme", "absent_answer")
)
PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS = 10_000.0


@dataclass(frozen=True)
class DesktopEvaluationModelCost:
    """Provider-neutral model-cost record; characters are a stable cost proxy."""

    model_calls: int = 0
    input_characters: int = 0
    output_characters: int = 0

    def plus(self, other: DesktopEvaluationModelCost) -> DesktopEvaluationModelCost:
        return DesktopEvaluationModelCost(
            model_calls=self.model_calls + other.model_calls,
            input_characters=self.input_characters + other.input_characters,
            output_characters=self.output_characters + other.output_characters,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "input_characters": self.input_characters,
            "output_characters": self.output_characters,
        }


@dataclass(frozen=True)
class DesktopEvaluationAnswer:
    """One production-contract answer adapted for evaluation scoring."""

    text: str
    model_cost: DesktopEvaluationModelCost = DesktopEvaluationModelCost()
    status: str = "completed"
    cited_evidence_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class DesktopRetrievalEvaluationCase:
    """One frozen question and its source-evidence/answer oracle."""

    case_id: str
    category: EvaluationCategory
    question: str
    expected_evidence: tuple[DesktopEvaluationEvidenceSelector, ...]
    expected_answer_terms: tuple[str, ...]
    expect_absent_answer: bool = False
    long_document: bool = False
    original_observation: DesktopOriginalAgentObservation | None = None


@dataclass(frozen=True)
class DesktopRetrievalEvaluationSuite:
    """A corpus-snapshot-bound suite with an explicit long-document cohort."""

    snapshot_id: str
    cases: tuple[DesktopRetrievalEvaluationCase, ...]
    digest: str
    max_graph_latency_ms: float | None = None
    max_graph_model_calls: int | None = None
    max_additional_retrieval_p95_ms: float = PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS
    max_additional_model_calls_per_case: int = 1
    max_navigator_model_calls_per_case: int = 8
    schema_version: int = 1
    navigator_model_profile_digest: str | None = None
    minimum_navigator_repetitions: int = 1

    @classmethod
    def from_json(cls, path: Path) -> DesktopRetrievalEvaluationSuite:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Desktop retrieval evaluation suite is unreadable.") from error
        if not isinstance(payload, dict):
            raise ValueError("Desktop retrieval evaluation suite must be a JSON object.")
        schema_version = payload.get("schema_version")
        if schema_version not in {1, 2, 3}:
            raise ValueError("Desktop retrieval evaluation suite schema_version must be 1, 2 or 3.")
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("Desktop retrieval evaluation suite must contain cases.")
        cases = tuple(
            _evaluation_case(
                value,
                require_original=schema_version >= 2,
                require_reference_identity=schema_version >= 3,
            )
            for value in raw_cases
        )
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("Desktop retrieval evaluation case IDs must be unique.")
        categories = {case.category for case in cases}
        if categories != EVALUATION_CATEGORIES:
            missing = sorted(EVALUATION_CATEGORIES - categories)
            extra = sorted(categories - EVALUATION_CATEGORIES)
            raise ValueError(
                f"Evaluation suite category coverage is invalid: missing={missing}, extra={extra}."
            )
        if not any(case.long_document and not case.expect_absent_answer for case in cases):
            raise ValueError("Desktop retrieval evaluation suite needs a long-document case.")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            snapshot_id=_required_string(payload, "snapshot_id"),
            cases=cases,
            digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            max_graph_latency_ms=_optional_positive_float(payload, "max_graph_latency_ms"),
            max_graph_model_calls=_optional_nonnegative_int(payload, "max_graph_model_calls"),
            max_additional_retrieval_p95_ms=_page_tree_latency_budget(payload),
            max_additional_model_calls_per_case=_model_call_budget(payload),
            max_navigator_model_calls_per_case=_navigator_model_call_budget(payload),
            schema_version=schema_version,
            navigator_model_profile_digest=(
                _required_sha256(payload, "navigator_model_profile_digest")
                if schema_version >= 3
                else _optional_sha256(payload, "navigator_model_profile_digest")
            ),
            minimum_navigator_repetitions=_minimum_navigator_repetitions(
                payload, required=schema_version >= 3
            ),
        )


def evaluation_corpus_digest(records: Iterable[tuple[str, str]]) -> str:
    """Hash a unique, ordered source-file inventory without retaining content."""
    normalized = sorted({(name, digest) for name, digest in records})
    if not normalized or len({name for name, _digest in normalized}) != len(normalized):
        raise ValueError("Desktop retrieval evaluation corpus inventory is invalid.")
    if any(
        Path(name).name != name
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for name, digest in normalized
    ):
        raise ValueError("Desktop retrieval evaluation corpus inventory is invalid.")
    payload = [{"file": name, "sha256": digest} for name, digest in normalized]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluation_corpus_identity(suite_path: Path) -> tuple[str, tuple[str, ...]]:
    """Hash the exact source files named by a fixed evaluation suite."""
    suite = DesktopRetrievalEvaluationSuite.from_json(suite_path)
    file_names = tuple(
        sorted(
            {
                selector.document_name
                for case in suite.cases
                for selector in (
                    *case.expected_evidence,
                    *(
                        case.original_observation.critical_evidence
                        if case.original_observation is not None
                        else ()
                    ),
                )
            }
        )
    )
    records: list[tuple[str, str]] = []
    for name in file_names:
        path = suite_path.parent / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError("Desktop retrieval evaluation corpus is incomplete.") from error
        records.append((name, digest))
    return evaluation_corpus_digest(records), file_names


@dataclass(frozen=True, order=True)
class DesktopPageTreeProviderIdentity:
    provider_kind: str
    provider_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider_kind": self.provider_kind,
            "provider_version": self.provider_version,
        }


@dataclass(frozen=True)
class DesktopPageTreeGenerationIdentity:
    """Per-document base and optional enrichment generation bound to one report."""

    document_id: str
    base_generation_id: str | None
    provider_kind: str | None
    provider_version: str | None
    enrichment_generation_id: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "document_id": self.document_id,
            "base_generation_id": self.base_generation_id,
            "provider_kind": self.provider_kind,
            "provider_version": self.provider_version,
            "enrichment_generation_id": self.enrichment_generation_id,
        }


def evaluation_derived_snapshot_digest(
    catalog_generation_ids: tuple[str, ...],
    page_tree_generations: tuple[DesktopPageTreeGenerationIdentity, ...],
) -> str:
    """Hash the complete derived-generation identity used by one evaluation."""
    payload = {
        "catalog_generation_ids": list(catalog_generation_ids),
        "page_tree_generations": [item.as_dict() for item in page_tree_generations],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DesktopRetrievalEvaluationCaseResult:
    case_id: str
    category: EvaluationCategory
    repetition: int
    variant: DesktopEvaluationVariant
    expected_evidence_ids: tuple[str, ...]
    evidence_recall_at_k: float
    citation_precision: float
    absent_answer_correct: bool
    answer_faithfulness: float
    latency_ms: float
    retrieval_latency_ms: float
    answer_latency_ms: float
    model_cost: DesktopEvaluationModelCost
    answer_status: str
    long_document: bool = False
    page_tree_selection_triggered: bool = False
    degradation_reasons: tuple[str, ...] = ()
    catalog_generation_ids: tuple[str, ...] = ()
    page_tree_generation_ids: tuple[str, ...] = ()
    cited_evidence_ids: tuple[str, ...] = ()
    original_evidence_ids: tuple[str, ...] = ()
    original_evidence_recall_at_k: float = 0.0
    original_citation_precision: float = 0.0
    original_answer_point_coverage: float = 0.0
    unsupported_claim_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "repetition": self.repetition,
            "variant": self.variant,
            "expected_evidence_ids": list(self.expected_evidence_ids),
            "evidence_recall_at_k": self.evidence_recall_at_k,
            "citation_precision": self.citation_precision,
            "absent_answer_correct": self.absent_answer_correct,
            "answer_faithfulness": self.answer_faithfulness,
            "latency_ms": self.latency_ms,
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "answer_latency_ms": self.answer_latency_ms,
            "model_cost": self.model_cost.as_dict(),
            "answer_status": self.answer_status,
            "long_document": self.long_document,
            "page_tree_selection_triggered": self.page_tree_selection_triggered,
            "degradation_reasons": list(self.degradation_reasons),
            "catalog_generation_ids": list(self.catalog_generation_ids),
            "page_tree_generation_ids": list(self.page_tree_generation_ids),
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "original_evidence_ids": list(self.original_evidence_ids),
            "original_evidence_recall_at_k": self.original_evidence_recall_at_k,
            "original_citation_precision": self.original_citation_precision,
            "original_answer_point_coverage": self.original_answer_point_coverage,
            "unsupported_claim_count": self.unsupported_claim_count,
        }


@dataclass(frozen=True)
class DesktopRetrievalEvaluationMetrics:
    case_runs: int
    evidence_recall_k: int
    evidence_recall_at_k: float
    long_document_evidence_recall_at_k: float
    citation_precision: float
    absent_answer_accuracy: float
    answer_faithfulness: float
    mean_latency_ms: float
    retrieval_p95_ms: float
    model_cost: DesktopEvaluationModelCost
    degradation_runs: int = 0
    original_answer_point_coverage: float = 0.0
    unsupported_claim_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "case_runs": self.case_runs,
            "evidence_recall_k": self.evidence_recall_k,
            "evidence_recall_at_k": self.evidence_recall_at_k,
            "long_document_evidence_recall_at_k": self.long_document_evidence_recall_at_k,
            "citation_precision": self.citation_precision,
            "absent_answer_accuracy": self.absent_answer_accuracy,
            "answer_faithfulness": self.answer_faithfulness,
            "mean_latency_ms": self.mean_latency_ms,
            "retrieval_p95_ms": self.retrieval_p95_ms,
            "model_cost": self.model_cost.as_dict(),
            "degradation_runs": self.degradation_runs,
            "original_answer_point_coverage": self.original_answer_point_coverage,
            "unsupported_claim_count": self.unsupported_claim_count,
        }


@dataclass(frozen=True)
class DesktopPageTreeEvaluationGate:
    passed: bool
    long_document_recall_gain: bool
    page_tree_selection_exercised: bool
    citation_precision_within_one_point: bool
    absent_answer_within_one_point: bool
    faithfulness_non_regression: bool
    retrieval_p95_within_budget: bool
    model_cost_within_budget: bool
    degradation_free: bool
    knowledge_snapshot_stable: bool
    derived_generations_stable: bool
    derived_identity_bound: bool
    fixed_suite_complete: bool

    def as_dict(self) -> dict[str, bool]:
        return {field: bool(getattr(self, field)) for field in self.__dataclass_fields__}


@dataclass(frozen=True)
class DesktopLocalGraphEvaluationGate:
    passed: bool
    evidence_recall_improved: bool
    no_critical_evidence_loss: bool
    citation_precision_non_regression: bool
    faithfulness_non_regression: bool
    latency_within_budget: bool
    model_cost_within_budget: bool
    knowledge_snapshot_stable: bool

    def as_dict(self) -> dict[str, bool]:
        return {field: bool(getattr(self, field)) for field in self.__dataclass_fields__}


@dataclass(frozen=True)
class DesktopRetrievalEvaluationReport:
    suite_snapshot_id: str
    suite_digest: str
    knowledge_snapshot_digest: str
    knowledge_snapshot_revision: int
    catalog_generation_ids: tuple[str, ...]
    page_tree_providers: tuple[DesktopPageTreeProviderIdentity, ...]
    page_tree_generations: tuple[DesktopPageTreeGenerationIdentity, ...]
    repetitions: int
    results: tuple[DesktopRetrievalEvaluationCaseResult, ...]
    metrics: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationMetrics]
    gate: DesktopPageTreeEvaluationGate
    local_graph_gate: DesktopLocalGraphEvaluationGate
    navigator_gate: DesktopNavigatorEvaluationGate
    source_integrity_healthy: bool
    model_profile_digest: str | None = None
    corpus_digest: str | None = None
    pageindex_worker_sha256: str | None = None
    final_knowledge_snapshot_digest: str | None = None
    final_knowledge_snapshot_revision: int | None = None
    final_derived_snapshot_digest: str | None = None
    stability: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationVariantStability] = field(
        default_factory=dict
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "suite_snapshot_id": self.suite_snapshot_id,
            "suite_digest": self.suite_digest,
            "knowledge_snapshot_digest": self.knowledge_snapshot_digest,
            "knowledge_snapshot_revision": self.knowledge_snapshot_revision,
            "catalog_generation_ids": list(self.catalog_generation_ids),
            "page_tree_providers": [provider.as_dict() for provider in self.page_tree_providers],
            "page_tree_generations": [
                generation.as_dict() for generation in self.page_tree_generations
            ],
            "repetitions": self.repetitions,
            "results": [result.as_dict() for result in self.results],
            "metrics": {variant: metrics.as_dict() for variant, metrics in self.metrics.items()},
            "gate": self.gate.as_dict(),
            "local_graph_gate": self.local_graph_gate.as_dict(),
            "navigator_gate": self.navigator_gate.as_dict(),
            "source_integrity_healthy": self.source_integrity_healthy,
            "model_profile_digest": self.model_profile_digest,
            "corpus_digest": self.corpus_digest,
            "pageindex_worker_sha256": self.pageindex_worker_sha256,
            "final_knowledge_snapshot_digest": self.final_knowledge_snapshot_digest,
            "final_knowledge_snapshot_revision": self.final_knowledge_snapshot_revision,
            "final_derived_snapshot_digest": self.final_derived_snapshot_digest,
            "stability": {variant: record.as_dict() for variant, record in self.stability.items()},
        }

    def write(self, path: Path) -> None:
        atomic_write_text(path, json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> DesktopRetrievalEvaluationReport:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Desktop retrieval evaluation report is unreadable.") from error
        if not isinstance(payload, dict):
            raise ValueError("Desktop retrieval evaluation report must be a JSON object.")
        return _evaluation_report(payload)


def _evaluation_case(
    value: object,
    *,
    require_original: bool = False,
    require_reference_identity: bool = False,
) -> DesktopRetrievalEvaluationCase:
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation cases must be objects.")
    category_value = _required_string(value, "category")
    if category_value not in EVALUATION_CATEGORIES:
        raise ValueError("Desktop retrieval evaluation case category is unsupported.")
    raw_expected_evidence = value.get("expected_evidence")
    if not isinstance(raw_expected_evidence, list):
        raise ValueError("Desktop retrieval evaluation expected_evidence must be an array.")
    expected_evidence = tuple(evidence_selector(item) for item in raw_expected_evidence)
    expected_answer_terms = _required_strings(value, "expected_answer_terms")
    expect_absent_answer = value.get("expect_absent_answer", False)
    long_document = value.get("long_document", False)
    if not isinstance(expect_absent_answer, bool) or not isinstance(long_document, bool):
        raise ValueError("Evaluation case boolean fields are invalid.")
    if expect_absent_answer != (category_value == "absent_answer"):
        raise ValueError("Only the absent_answer category can expect an absent answer.")
    if expect_absent_answer:
        if expected_evidence:
            raise ValueError("An absent-answer case cannot expect source evidence.")
    elif not expected_evidence or not expected_answer_terms:
        raise ValueError("A grounded evaluation case needs evidence and answer-term expectations.")
    return DesktopRetrievalEvaluationCase(
        case_id=_required_string(value, "case_id"),
        category=cast(EvaluationCategory, category_value),
        question=_required_string(value, "question"),
        expected_evidence=expected_evidence,
        expected_answer_terms=expected_answer_terms,
        expect_absent_answer=expect_absent_answer,
        long_document=long_document,
        original_observation=original_agent_observation(
            value.get("original_observation"),
            required=require_original,
            expect_absent_answer=expect_absent_answer,
            require_reference_identity=require_reference_identity,
        ),
    )


def _evaluation_report(value: dict[object, object]) -> DesktopRetrievalEvaluationReport:
    from openkb.evaluation.retrieval_stability import report_stability

    raw_results = value.get("results")
    raw_metrics = value.get("metrics")
    raw_gate = value.get("gate")
    raw_graph_gate = value.get("local_graph_gate")
    raw_navigator_gate = value.get("navigator_gate")
    if not isinstance(raw_results, list) or not isinstance(raw_metrics, dict):
        raise ValueError("Desktop retrieval evaluation report collections are invalid.")
    if not isinstance(raw_gate, dict):
        raise ValueError("Desktop retrieval evaluation report gate must be an object.")
    metrics: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationMetrics] = {}
    for raw_variant, raw_value in raw_metrics.items():
        if not isinstance(raw_variant, str) or not isinstance(raw_value, dict):
            raise ValueError("Desktop retrieval evaluation report has invalid metrics.")
        variant = normalize_retrieval_channel(raw_variant)
        if variant not in DESKTOP_EVALUATION_VARIANTS or variant in metrics:
            raise ValueError("Desktop retrieval evaluation report has an invalid variant.")
        metrics[cast(DesktopEvaluationVariant, variant)] = _report_metrics(raw_value)
    accepted_variant_sets = (
        DESKTOP_EVALUATION_VARIANTS,
        DESKTOP_EVALUATION_VARIANTS - {"navigator"},
    )
    if set(metrics) not in accepted_variant_sets:
        raise ValueError("Desktop retrieval evaluation report has incomplete metrics.")
    providers = _report_providers(value.get("page_tree_providers", []))
    generations = _report_page_tree_generations(value.get("page_tree_generations", []))
    return DesktopRetrievalEvaluationReport(
        suite_snapshot_id=_required_string(value, "suite_snapshot_id"),
        suite_digest=_required_string(value, "suite_digest"),
        knowledge_snapshot_digest=_required_string(value, "knowledge_snapshot_digest"),
        knowledge_snapshot_revision=_report_int(value, "knowledge_snapshot_revision"),
        catalog_generation_ids=_required_strings(value, "catalog_generation_ids"),
        page_tree_providers=providers,
        page_tree_generations=generations,
        repetitions=_report_int(value, "repetitions", minimum=1),
        results=tuple(_report_result(item) for item in raw_results),
        metrics=metrics,
        gate=_report_page_tree_gate(raw_gate),
        local_graph_gate=(
            _report_local_graph_gate(raw_graph_gate)
            if isinstance(raw_graph_gate, dict)
            else _failed_local_graph_gate()
        ),
        navigator_gate=(
            _report_navigator_gate(raw_navigator_gate)
            if isinstance(raw_navigator_gate, dict)
            else _failed_navigator_gate()
        ),
        source_integrity_healthy=_optional_report_bool(value, "source_integrity_healthy"),
        model_profile_digest=_optional_sha256(value, "model_profile_digest"),
        corpus_digest=_optional_sha256(value, "corpus_digest"),
        pageindex_worker_sha256=_optional_sha256(value, "pageindex_worker_sha256"),
        final_knowledge_snapshot_digest=_optional_sha256(value, "final_knowledge_snapshot_digest"),
        final_knowledge_snapshot_revision=(
            _report_int(value, "final_knowledge_snapshot_revision")
            if value.get("final_knowledge_snapshot_revision") is not None
            else None
        ),
        final_derived_snapshot_digest=_optional_sha256(value, "final_derived_snapshot_digest"),
        stability=report_stability(value.get("stability")),
    )


def _report_result(value: object) -> DesktopRetrievalEvaluationCaseResult:
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation report results must be objects.")
    category = _required_string(value, "category")
    variant = normalize_retrieval_channel(_required_string(value, "variant"))
    raw_cost = value.get("model_cost")
    if category not in EVALUATION_CATEGORIES or variant not in DESKTOP_EVALUATION_VARIANTS:
        raise ValueError("Desktop retrieval evaluation report result is unsupported.")
    if not isinstance(raw_cost, dict):
        raise ValueError("Desktop retrieval evaluation report model_cost must be an object.")
    latency = _report_float(value, "latency_ms")
    return DesktopRetrievalEvaluationCaseResult(
        case_id=_required_string(value, "case_id"),
        category=cast(EvaluationCategory, category),
        repetition=_report_int(value, "repetition", minimum=1),
        variant=cast(DesktopEvaluationVariant, variant),
        expected_evidence_ids=_required_strings(value, "expected_evidence_ids"),
        evidence_recall_at_k=_report_float(value, "evidence_recall_at_k"),
        citation_precision=_report_float(value, "citation_precision"),
        absent_answer_correct=_optional_report_bool(value, "absent_answer_correct"),
        answer_faithfulness=_report_float(value, "answer_faithfulness"),
        latency_ms=latency,
        retrieval_latency_ms=_optional_report_float(value, "retrieval_latency_ms", latency),
        answer_latency_ms=_optional_report_float(value, "answer_latency_ms", 0.0),
        model_cost=_report_model_cost(raw_cost),
        answer_status=_required_string(value, "answer_status"),
        long_document=_optional_report_bool(value, "long_document"),
        page_tree_selection_triggered=_optional_report_bool(value, "page_tree_selection_triggered"),
        degradation_reasons=_required_strings(value, "degradation_reasons"),
        catalog_generation_ids=_required_strings(value, "catalog_generation_ids"),
        page_tree_generation_ids=_required_strings(value, "page_tree_generation_ids"),
        cited_evidence_ids=_required_strings(value, "cited_evidence_ids"),
        original_evidence_ids=_required_strings(value, "original_evidence_ids"),
        original_evidence_recall_at_k=_optional_report_float(
            value, "original_evidence_recall_at_k", 0.0
        ),
        original_citation_precision=_optional_report_float(
            value, "original_citation_precision", 0.0
        ),
        original_answer_point_coverage=_optional_report_float(
            value, "original_answer_point_coverage", 0.0
        ),
        unsupported_claim_count=(
            _report_int(value, "unsupported_claim_count")
            if "unsupported_claim_count" in value
            else 0
        ),
    )


def _report_metrics(value: dict[object, object]) -> DesktopRetrievalEvaluationMetrics:
    raw_cost = value.get("model_cost")
    if not isinstance(raw_cost, dict):
        raise ValueError("Desktop retrieval evaluation report model_cost must be an object.")
    recall = _report_float(value, "evidence_recall_at_k")
    mean_latency = _report_float(value, "mean_latency_ms")
    return DesktopRetrievalEvaluationMetrics(
        case_runs=_report_int(value, "case_runs"),
        evidence_recall_k=_report_int(value, "evidence_recall_k"),
        evidence_recall_at_k=recall,
        long_document_evidence_recall_at_k=_optional_report_float(
            value, "long_document_evidence_recall_at_k", recall
        ),
        citation_precision=_report_float(value, "citation_precision"),
        absent_answer_accuracy=_optional_report_float(value, "absent_answer_accuracy", 0.0),
        answer_faithfulness=_report_float(value, "answer_faithfulness"),
        mean_latency_ms=mean_latency,
        retrieval_p95_ms=_optional_report_float(value, "retrieval_p95_ms", mean_latency),
        model_cost=_report_model_cost(raw_cost),
        degradation_runs=(
            _report_int(value, "degradation_runs") if "degradation_runs" in value else 0
        ),
        original_answer_point_coverage=_optional_report_float(
            value, "original_answer_point_coverage", 0.0
        ),
        unsupported_claim_count=(
            _report_int(value, "unsupported_claim_count")
            if "unsupported_claim_count" in value
            else 0
        ),
    )


def _report_page_tree_gate(value: dict[object, object]) -> DesktopPageTreeEvaluationGate:
    fields = DesktopPageTreeEvaluationGate.__dataclass_fields__
    return DesktopPageTreeEvaluationGate(**{field: _report_bool(value, field) for field in fields})


def _report_local_graph_gate(value: dict[object, object]) -> DesktopLocalGraphEvaluationGate:
    fields = DesktopLocalGraphEvaluationGate.__dataclass_fields__
    return DesktopLocalGraphEvaluationGate(
        **{field: _report_bool(value, field) for field in fields}
    )


def _report_navigator_gate(value: dict[object, object]) -> DesktopNavigatorEvaluationGate:
    fields = DesktopNavigatorEvaluationGate.__dataclass_fields__
    return DesktopNavigatorEvaluationGate(
        **{field: _report_bool(value, field) if field in value else False for field in fields}
    )


def _failed_local_graph_gate() -> DesktopLocalGraphEvaluationGate:
    return DesktopLocalGraphEvaluationGate(False, False, False, False, False, False, False, False)


def _failed_navigator_gate() -> DesktopNavigatorEvaluationGate:
    return DesktopNavigatorEvaluationGate(
        **{field: False for field in DesktopNavigatorEvaluationGate.__dataclass_fields__}
    )


def _report_providers(value: object) -> tuple[DesktopPageTreeProviderIdentity, ...]:
    if not isinstance(value, list):
        raise ValueError("Desktop retrieval evaluation providers must be an array.")
    providers: list[DesktopPageTreeProviderIdentity] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Desktop retrieval evaluation provider is invalid.")
        providers.append(
            DesktopPageTreeProviderIdentity(
                _required_string(item, "provider_kind"),
                _required_string(item, "provider_version"),
            )
        )
    return tuple(providers)


def _report_page_tree_generations(
    value: object,
) -> tuple[DesktopPageTreeGenerationIdentity, ...]:
    if not isinstance(value, list):
        raise ValueError("Desktop retrieval evaluation PageTree generations must be an array.")
    generations: list[DesktopPageTreeGenerationIdentity] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Desktop retrieval evaluation PageTree generation is invalid.")
        generations.append(
            DesktopPageTreeGenerationIdentity(
                document_id=_required_string(item, "document_id"),
                base_generation_id=_optional_string(item, "base_generation_id"),
                provider_kind=_optional_string(item, "provider_kind"),
                provider_version=_optional_string(item, "provider_version"),
                enrichment_generation_id=_optional_string(item, "enrichment_generation_id"),
            )
        )
    return tuple(generations)


def _report_model_cost(value: dict[object, object]) -> DesktopEvaluationModelCost:
    return DesktopEvaluationModelCost(
        model_calls=_report_int(value, "model_calls"),
        input_characters=_report_int(value, "input_characters"),
        output_characters=_report_int(value, "output_characters"),
    )


def _minimum_navigator_repetitions(value: dict[object, object], *, required: bool) -> int:
    candidate = value.get("minimum_navigator_repetitions")
    if candidate is None and not required:
        return 1
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 2:
        raise ValueError(
            "Desktop retrieval evaluation field minimum_navigator_repetitions must be at least two."
        )
    return candidate


def _page_tree_latency_budget(value: dict[object, object]) -> float:
    configured = _optional_positive_float(value, "max_additional_retrieval_p95_ms")
    if configured is not None and configured > PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS:
        raise ValueError("Desktop PageTree added retrieval p95 cannot exceed 10 seconds.")
    return configured or PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS


def _model_call_budget(value: dict[object, object]) -> int:
    if "max_additional_model_calls_per_case" not in value:
        return 1
    budget = _optional_nonnegative_int(value, "max_additional_model_calls_per_case")
    if budget is None:
        raise ValueError(
            "Desktop retrieval evaluation field max_additional_model_calls_per_case "
            "must be nonnegative."
        )
    return budget


def _navigator_model_call_budget(value: dict[object, object]) -> int:
    key = "max_navigator_model_calls_per_case"
    if key not in value:
        return 8
    budget = _optional_nonnegative_int(value, key)
    if budget is None:
        raise ValueError(f"Desktop retrieval evaluation field {key} must be nonnegative.")
    return budget
