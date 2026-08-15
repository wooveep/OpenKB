"""Fixed-suite ablation reports for Desktop's vectorless retrieval channels.

The evaluator deliberately has no Community, Global GraphRAG, DRIFT, or
embedding variant.  It compares the shipped lexical/PageTree/wiki baseline to
the optional evidence-anchored local graph, records auditable metrics, and
returns a conservative eligibility gate instead of silently broadening graph
use after one encouraging run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openkb.desktop_answer_types import DesktopEvidencePack
from openkb.desktop_graph_feature_flags import (
    desktop_knowledge_snapshot_digest,
    desktop_knowledge_snapshot_revision,
    enable_local_graph_after_evaluation,
)
from openkb.desktop_grounded_answer import generate_grounded_answer
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval import (
    DESKTOP_EVIDENCE_RECALL_K,
    DesktopEvidenceRetriever,
    DesktopRetrievalVariant,
)
from openkb.desktop_workspace import desktop_state_database_path
from openkb.locks import atomic_write_text

EvaluationCategory = Literal[
    "local_fact", "multi_hop", "cross_document_conflict", "global_theme", "absent_answer"
]
_EVALUATION_CATEGORIES: frozenset[EvaluationCategory] = frozenset(
    ("local_fact", "multi_hop", "cross_document_conflict", "global_theme", "absent_answer")
)
_RETRIEVAL_VARIANTS: tuple[DesktopRetrievalVariant, ...] = (
    "fts",
    "page_tree",
    "wiki",
    "baseline",
    "local_graph",
)


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
    """A pluggable answer result used only to score a retrieval evaluation."""

    text: str
    model_cost: DesktopEvaluationModelCost = DesktopEvaluationModelCost()
    status: str = "completed"


AnswerGenerator = Callable[[str, DesktopEvidencePack], DesktopEvaluationAnswer]


@dataclass(frozen=True)
class DesktopRetrievalEvaluationCase:
    """One frozen question and its source-evidence/answer oracle."""

    case_id: str
    category: EvaluationCategory
    question: str
    expected_evidence: tuple[DesktopEvaluationEvidenceSelector, ...]
    expected_answer_terms: tuple[str, ...]
    expect_absent_answer: bool = False


@dataclass(frozen=True)
class DesktopEvaluationEvidenceSelector:
    """A durable corpus-snapshot anchor, resolved to a run-local EvidenceRef."""

    document_name: str
    text_contains: str


@dataclass(frozen=True)
class DesktopRetrievalEvaluationSuite:
    """A corpus-snapshot-bound suite loaded from a versioned JSON file."""

    snapshot_id: str
    cases: tuple[DesktopRetrievalEvaluationCase, ...]
    digest: str
    max_graph_latency_ms: float | None = None
    max_graph_model_calls: int | None = None

    @classmethod
    def from_json(cls, path: Path) -> DesktopRetrievalEvaluationSuite:
        """Load a fixed suite and reject incomplete category coverage."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Desktop retrieval evaluation suite is unreadable.") from error
        if not isinstance(payload, dict):
            raise ValueError("Desktop retrieval evaluation suite must be a JSON object.")
        if payload.get("schema_version") != 1:
            raise ValueError("Desktop retrieval evaluation suite schema_version must be 1.")
        snapshot_id = _required_string(payload, "snapshot_id")
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("Desktop retrieval evaluation suite must contain cases.")
        cases = tuple(_evaluation_case(value) for value in raw_cases)
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("Desktop retrieval evaluation case IDs must be unique.")
        categories = {case.category for case in cases}
        if categories != _EVALUATION_CATEGORIES:
            missing = sorted(_EVALUATION_CATEGORIES - categories)
            extra = sorted(categories - _EVALUATION_CATEGORIES)
            raise ValueError(
                f"Evaluation suite category coverage is invalid: missing={missing}, extra={extra}."
            )
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            snapshot_id=snapshot_id,
            cases=cases,
            digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            max_graph_latency_ms=_optional_positive_float(payload, "max_graph_latency_ms"),
            max_graph_model_calls=_optional_nonnegative_int(payload, "max_graph_model_calls"),
        )


@dataclass(frozen=True)
class DesktopRetrievalEvaluationCaseResult:
    case_id: str
    category: EvaluationCategory
    repetition: int
    variant: DesktopRetrievalVariant
    expected_evidence_ids: tuple[str, ...]
    evidence_recall_at_k: float
    citation_precision: float
    answer_faithfulness: float
    latency_ms: float
    model_cost: DesktopEvaluationModelCost
    answer_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "repetition": self.repetition,
            "variant": self.variant,
            "expected_evidence_ids": list(self.expected_evidence_ids),
            "evidence_recall_at_k": self.evidence_recall_at_k,
            "citation_precision": self.citation_precision,
            "answer_faithfulness": self.answer_faithfulness,
            "latency_ms": self.latency_ms,
            "model_cost": self.model_cost.as_dict(),
            "answer_status": self.answer_status,
        }


@dataclass(frozen=True)
class DesktopRetrievalEvaluationMetrics:
    case_runs: int
    evidence_recall_k: int
    evidence_recall_at_k: float
    citation_precision: float
    answer_faithfulness: float
    mean_latency_ms: float
    model_cost: DesktopEvaluationModelCost

    def as_dict(self) -> dict[str, object]:
        return {
            "case_runs": self.case_runs,
            "evidence_recall_k": self.evidence_recall_k,
            "evidence_recall_at_k": self.evidence_recall_at_k,
            "citation_precision": self.citation_precision,
            "answer_faithfulness": self.answer_faithfulness,
            "mean_latency_ms": self.mean_latency_ms,
            "model_cost": self.model_cost.as_dict(),
        }


@dataclass(frozen=True)
class DesktopRetrievalEvaluationGate:
    """The conservative condition for broadening default local-graph use."""

    passed: bool
    evidence_recall_improved: bool
    no_critical_evidence_loss: bool
    citation_precision_non_regression: bool
    faithfulness_non_regression: bool
    latency_within_budget: bool
    model_cost_within_budget: bool
    knowledge_snapshot_stable: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "passed": self.passed,
            "evidence_recall_improved": self.evidence_recall_improved,
            "no_critical_evidence_loss": self.no_critical_evidence_loss,
            "citation_precision_non_regression": self.citation_precision_non_regression,
            "faithfulness_non_regression": self.faithfulness_non_regression,
            "latency_within_budget": self.latency_within_budget,
            "model_cost_within_budget": self.model_cost_within_budget,
            "knowledge_snapshot_stable": self.knowledge_snapshot_stable,
        }


@dataclass(frozen=True)
class DesktopRetrievalEvaluationReport:
    suite_snapshot_id: str
    suite_digest: str
    knowledge_snapshot_digest: str
    knowledge_snapshot_revision: int
    repetitions: int
    results: tuple[DesktopRetrievalEvaluationCaseResult, ...]
    metrics: dict[DesktopRetrievalVariant, DesktopRetrievalEvaluationMetrics]
    gate: DesktopRetrievalEvaluationGate

    def as_dict(self) -> dict[str, object]:
        return {
            "suite_snapshot_id": self.suite_snapshot_id,
            "suite_digest": self.suite_digest,
            "knowledge_snapshot_digest": self.knowledge_snapshot_digest,
            "knowledge_snapshot_revision": self.knowledge_snapshot_revision,
            "repetitions": self.repetitions,
            "results": [result.as_dict() for result in self.results],
            "metrics": {variant: metrics.as_dict() for variant, metrics in self.metrics.items()},
            "gate": self.gate.as_dict(),
        }

    def write(self, path: Path) -> None:
        """Durably record the exact suite digest and all raw metric values."""
        atomic_write_text(path, json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n")


class DesktopRetrievalEvaluator:
    """Run a frozen suite against each supported local retrieval variant."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        model_gateway: DesktopModelGateway | None = None,
        answer_generator: AnswerGenerator | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._retriever = DesktopEvidenceRetriever(self._kb_dir, model_gateway=model_gateway)
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._answer_generator = answer_generator or _production_answer_generator(model_gateway)
        self._clock = clock

    def evaluate(
        self, suite: DesktopRetrievalEvaluationSuite, *, repetitions: int = 1
    ) -> DesktopRetrievalEvaluationReport:
        """Measure every fixed case without enabling deferred graph capabilities."""
        if repetitions < 1:
            raise ValueError("Desktop retrieval evaluation repetitions must be at least one.")
        knowledge_snapshot_revision = desktop_knowledge_snapshot_revision(self._kb_dir)
        knowledge_snapshot_digest = desktop_knowledge_snapshot_digest(self._kb_dir)
        snapshot_captured_consistently = (
            knowledge_snapshot_revision == desktop_knowledge_snapshot_revision(self._kb_dir)
        )
        results: list[DesktopRetrievalEvaluationCaseResult] = []
        for case in suite.cases:
            expected_evidence_ids = self._expected_evidence_ids(case)
            for repetition in range(1, repetitions + 1):
                planning_started = self._clock()
                plan, degradations = self._retriever.build_plan(case.question)
                planning_latency_ms = (self._clock() - planning_started) * 1_000
                planning_cost = _planning_cost(case.question, plan.source, plan.terms)
                for variant in _RETRIEVAL_VARIANTS:
                    started = self._clock()
                    pack = self._retriever.retrieve_variant(
                        case.question,
                        variant=variant,
                        retrieval_plan=plan,
                        degradations=degradations,
                    )
                    answer = _safe_answer(self._answer_generator, case.question, pack)
                    latency_ms = planning_latency_ms + ((self._clock() - started) * 1_000)
                    results.append(
                        _case_result(
                            case,
                            repetition,
                            variant,
                            expected_evidence_ids,
                            pack,
                            answer,
                            latency_ms,
                            planning_cost.plus(answer.model_cost),
                        )
                    )
        metrics = {variant: _metrics_for(results, variant) for variant in _RETRIEVAL_VARIANTS}
        knowledge_snapshot_stable = (
            snapshot_captured_consistently
            and knowledge_snapshot_revision == desktop_knowledge_snapshot_revision(self._kb_dir)
        )
        return DesktopRetrievalEvaluationReport(
            suite_snapshot_id=suite.snapshot_id,
            suite_digest=suite.digest,
            knowledge_snapshot_digest=knowledge_snapshot_digest,
            knowledge_snapshot_revision=knowledge_snapshot_revision,
            repetitions=repetitions,
            results=tuple(results),
            metrics=metrics,
            gate=_gate(
                suite,
                results,
                metrics,
                knowledge_snapshot_stable=knowledge_snapshot_stable,
            ),
        )

    def promote_local_graph(self, report: DesktopRetrievalEvaluationReport) -> None:
        """Enable default local graph retrieval only after this gate has passed."""
        if not report.gate.passed:
            raise ValueError(
                "A non-passing retrieval evaluation cannot enable local graph retrieval."
            )
        enable_local_graph_after_evaluation(
            self._kb_dir,
            report.suite_digest,
            report.knowledge_snapshot_digest,
            report.knowledge_snapshot_revision,
        )

    def _expected_evidence_ids(self, case: DesktopRetrievalEvaluationCase) -> tuple[str, ...]:
        """Resolve immutable suite anchors without relying on random import IDs."""
        if case.expect_absent_answer:
            return ()
        connection = sqlite3.connect(self._database_path)
        try:
            resolved: list[str] = []
            for selector in case.expected_evidence:
                rows = connection.execute(
                    """
                    SELECT DISTINCT evidence_occurrences.evidence_id
                    FROM evidence_occurrences
                    JOIN evidence_refs
                        ON evidence_refs.evidence_id = evidence_occurrences.evidence_id
                    JOIN source_documents
                        ON source_documents.document_id = evidence_occurrences.document_id
                    WHERE source_documents.availability = 'available'
                        AND source_documents.display_name = ?
                        AND instr(evidence_refs.text, ?) > 0
                    ORDER BY evidence_occurrences.evidence_id
                    """,
                    (selector.document_name, selector.text_contains),
                ).fetchall()
                if len(rows) != 1:
                    raise ValueError(
                        f"Evaluation evidence selector for case {case.case_id} is not unique."
                    )
                resolved.append(str(rows[0][0]))
        except sqlite3.Error as error:
            raise ValueError("Desktop retrieval evaluation evidence is unavailable.") from error
        finally:
            connection.close()
        return tuple(resolved)


def _evaluation_case(value: object) -> DesktopRetrievalEvaluationCase:
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation cases must be objects.")
    category_value = _required_string(value, "category")
    if category_value not in _EVALUATION_CATEGORIES:
        raise ValueError("Desktop retrieval evaluation case category is unsupported.")
    raw_expected_evidence = value.get("expected_evidence")
    if not isinstance(raw_expected_evidence, list):
        raise ValueError("Desktop retrieval evaluation expected_evidence must be an array.")
    expected_evidence = tuple(_evidence_selector(item) for item in raw_expected_evidence)
    expected_answer_terms = _required_strings(value, "expected_answer_terms")
    expect_absent_answer = value.get("expect_absent_answer", False)
    if not isinstance(expect_absent_answer, bool):
        raise ValueError("expect_absent_answer must be a boolean.")
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
    )


def _evidence_selector(value: object) -> DesktopEvaluationEvidenceSelector:
    if not isinstance(value, dict):
        raise ValueError("Desktop retrieval evaluation evidence selectors must be objects.")
    return DesktopEvaluationEvidenceSelector(
        document_name=_required_string(value, "document_name"),
        text_contains=_required_string(value, "text_contains"),
    )


def _required_string(value: dict[object, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"Desktop retrieval evaluation suite field {key} must be a string.")
    return candidate.strip()


def _required_strings(value: dict[object, object], key: str) -> tuple[str, ...]:
    candidates = value.get(key)
    if not isinstance(candidates, list):
        raise ValueError(f"Desktop retrieval evaluation suite field {key} must be an array.")
    values = tuple(item.strip() for item in candidates if isinstance(item, str) and item.strip())
    if len(values) != len(candidates):
        raise ValueError(f"Desktop retrieval evaluation suite field {key} contains invalid values.")
    return values


def _optional_positive_float(value: dict[object, object], key: str) -> float | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or candidate <= 0:
        raise ValueError(f"Desktop retrieval evaluation suite field {key} must be positive.")
    return float(candidate)


def _optional_nonnegative_int(value: dict[object, object], key: str) -> int | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        raise ValueError(f"Desktop retrieval evaluation suite field {key} must be nonnegative.")
    return candidate


def _planning_cost(
    question: str, source: str, terms: tuple[str, ...]
) -> DesktopEvaluationModelCost:
    if source != "model":
        return DesktopEvaluationModelCost()
    return DesktopEvaluationModelCost(
        model_calls=1,
        input_characters=len(question),
        output_characters=len(" ".join(terms)),
    )


def _production_answer_generator(
    model_gateway: DesktopModelGateway | None,
) -> AnswerGenerator:
    """Adapt the normal non-persistent answer generation to evaluation metrics."""

    def generate(question: str, pack: DesktopEvidencePack) -> DesktopEvaluationAnswer:
        generation = generate_grounded_answer(
            question,
            pack,
            model_gateway=model_gateway,
        )
        return DesktopEvaluationAnswer(
            generation.answer_text or "",
            model_cost=DesktopEvaluationModelCost(
                model_calls=generation.model_calls,
                input_characters=generation.model_input_characters,
                output_characters=generation.model_output_characters,
            ),
            status=(
                "failed"
                if generation.answer_text is None
                else "fallback"
                if generation.degradations
                else "completed"
            ),
        )

    return generate


def _safe_answer(
    generator: AnswerGenerator, question: str, pack: DesktopEvidencePack
) -> DesktopEvaluationAnswer:
    try:
        return generator(question, pack)
    except Exception:
        # A report must not persist an adapter/provider exception, which could
        # contain remote details.  The failed answer simply cannot be faithful.
        return DesktopEvaluationAnswer("", status="failed")


def _case_result(
    case: DesktopRetrievalEvaluationCase,
    repetition: int,
    variant: DesktopRetrievalVariant,
    expected_evidence_ids: tuple[str, ...],
    pack: DesktopEvidencePack,
    answer: DesktopEvaluationAnswer,
    latency_ms: float,
    model_cost: DesktopEvaluationModelCost,
) -> DesktopRetrievalEvaluationCaseResult:
    cited = {reference.evidence_id for reference in pack.evidence}
    expected = set(expected_evidence_ids)
    if expected:
        evidence_recall = len(cited & expected) / len(expected)
        citation_precision = len(cited & expected) / len(cited) if cited else 0.0
    else:
        evidence_recall = 1.0 if not cited else 0.0
        citation_precision = evidence_recall
    faithful = _answer_is_faithful(case, expected, cited, answer.text)
    return DesktopRetrievalEvaluationCaseResult(
        case_id=case.case_id,
        category=case.category,
        repetition=repetition,
        variant=variant,
        expected_evidence_ids=expected_evidence_ids,
        evidence_recall_at_k=evidence_recall,
        citation_precision=citation_precision,
        answer_faithfulness=1.0 if faithful else 0.0,
        latency_ms=latency_ms,
        model_cost=model_cost,
        answer_status=answer.status,
    )


def _answer_is_faithful(
    case: DesktopRetrievalEvaluationCase,
    expected: set[str],
    cited: set[str],
    answer_text: str,
) -> bool:
    normalized_answer = answer_text.casefold()
    if case.expect_absent_answer:
        return not cited and "no available source evidence" in normalized_answer
    if not expected.issubset(cited):
        return False
    return all(term.casefold() in normalized_answer for term in case.expected_answer_terms)


def _metrics_for(
    results: list[DesktopRetrievalEvaluationCaseResult], variant: DesktopRetrievalVariant
) -> DesktopRetrievalEvaluationMetrics:
    selected = [result for result in results if result.variant == variant]
    if not selected:
        raise ValueError("Desktop retrieval evaluation has no results for a variant.")
    total = len(selected)
    cost = DesktopEvaluationModelCost()
    for result in selected:
        cost = cost.plus(result.model_cost)
    return DesktopRetrievalEvaluationMetrics(
        case_runs=total,
        evidence_recall_k=DESKTOP_EVIDENCE_RECALL_K,
        evidence_recall_at_k=sum(result.evidence_recall_at_k for result in selected) / total,
        citation_precision=sum(result.citation_precision for result in selected) / total,
        answer_faithfulness=sum(result.answer_faithfulness for result in selected) / total,
        mean_latency_ms=sum(result.latency_ms for result in selected) / total,
        model_cost=cost,
    )


def _gate(
    suite: DesktopRetrievalEvaluationSuite,
    results: list[DesktopRetrievalEvaluationCaseResult],
    metrics: dict[DesktopRetrievalVariant, DesktopRetrievalEvaluationMetrics],
    *,
    knowledge_snapshot_stable: bool,
) -> DesktopRetrievalEvaluationGate:
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
    passed = (
        evidence_recall_improved
        and no_critical_loss
        and citation_precision_non_regression
        and faithfulness_non_regression
        and latency_within_budget
        and model_cost_within_budget
        and knowledge_snapshot_stable
    )
    return DesktopRetrievalEvaluationGate(
        passed=passed,
        evidence_recall_improved=evidence_recall_improved,
        no_critical_evidence_loss=no_critical_loss,
        citation_precision_non_regression=citation_precision_non_regression,
        faithfulness_non_regression=faithfulness_non_regression,
        latency_within_budget=latency_within_budget,
        model_cost_within_budget=model_cost_within_budget,
        knowledge_snapshot_stable=knowledge_snapshot_stable,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a JSON suite; a nonzero exit code prevents an unproven expansion."""
    parser = argparse.ArgumentParser(description="Evaluate Desktop vectorless retrieval variants.")
    parser.add_argument("kb_dir", type=Path)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--promote-local-graph", action="store_true")
    args = parser.parse_args(argv)
    from openkb.desktop_model_transport import desktop_model_gateway_for

    evaluator = DesktopRetrievalEvaluator(
        args.kb_dir, model_gateway=desktop_model_gateway_for(args.kb_dir)
    )
    report = evaluator.evaluate(
        DesktopRetrievalEvaluationSuite.from_json(args.suite), repetitions=args.repetitions
    )
    if args.output is None:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        report.write(args.output)
    if args.promote_local_graph and report.gate.passed:
        evaluator.promote_local_graph(report)
    return 0 if report.gate.passed else 2


if __name__ == "__main__":  # pragma: no cover - exercised by the maintainer command.
    raise SystemExit(main())
