"""Fixed-suite ablation reports for Desktop's vectorless retrieval channels.

The evaluator deliberately has no Community, Global GraphRAG, DRIFT, embedding,
or vector variant. It reports lexical retrieval, bounded Document PageTree
selection, Catalog fusion, and the existing local-graph experiment without
changing any runtime default.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openkb.desktop_answer_types import DesktopEvidencePack, DesktopRetrievalModelCost
from openkb.desktop_graph_feature_flags import (
    enable_local_graph_after_evaluation,
    knowledge_snapshot_digest_in,
    knowledge_snapshot_revision_in,
)
from openkb.desktop_grounded_answer import (
    generate_grounded_answer,
    prepare_grounded_evidence_pack,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_page_tree import PageTreeGeneration
from openkb.desktop_readonly import connect_desktop_read_only
from openkb.desktop_retrieval import (
    DESKTOP_EVIDENCE_RECALL_K,
    DesktopEvidenceRetriever,
)
from openkb.desktop_retrieval_channels import (
    DESKTOP_EVALUATION_VARIANT_ORDER,
    PAGE_TREE_EVALUATION_VARIANTS,
    DesktopEvaluationVariant,
)
from openkb.desktop_retrieval_evaluation_types import (
    EVALUATION_CATEGORIES,
    PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS,
    DesktopEvaluationAnswer,
    DesktopEvaluationModelCost,
    DesktopLocalGraphEvaluationGate,
    DesktopPageTreeEvaluationGate,
    DesktopPageTreeGenerationIdentity,
    DesktopPageTreeProviderIdentity,
    DesktopRetrievalEvaluationCase,
    DesktopRetrievalEvaluationCaseResult,
    DesktopRetrievalEvaluationMetrics,
    DesktopRetrievalEvaluationReport,
    DesktopRetrievalEvaluationSuite,
    evaluation_corpus_digest,
    evaluation_derived_snapshot_digest,
)
from openkb.desktop_workspace import desktop_state_database_path

AnswerGenerator = Callable[[str, DesktopEvidencePack], DesktopEvaluationAnswer]


class EvaluationPageTreeProvider(Protocol):
    generations: tuple[DesktopPageTreeGenerationIdentity, ...]
    degradations: tuple[str, ...]

    def lease(
        self, kb_dir: Path, document_id: str
    ) -> AbstractContextManager[PageTreeGeneration | None]: ...


@dataclass(frozen=True)
class _DerivedEvaluationSnapshot:
    knowledge_snapshot_digest: str
    knowledge_snapshot_revision: int
    catalog_generation_ids: tuple[str, ...]
    page_tree_generations: tuple[DesktopPageTreeGenerationIdentity, ...]
    provider_documents_complete: bool = True

    @property
    def page_tree_providers(self) -> tuple[DesktopPageTreeProviderIdentity, ...]:
        values = {
            DesktopPageTreeProviderIdentity(item.provider_kind, item.provider_version)
            for item in self.page_tree_generations
            if item.provider_kind is not None and item.provider_version is not None
        }
        return tuple(sorted(values))

    @property
    def identity_bound(self) -> bool:
        return (
            bool(self.catalog_generation_ids)
            and bool(self.page_tree_generations)
            and self.provider_documents_complete
            and all(
                item.base_generation_id is not None
                and item.provider_kind is not None
                and item.provider_version is not None
                for item in self.page_tree_generations
            )
        )


class DesktopRetrievalEvaluator:
    """Run a frozen suite against each supported local retrieval variant."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        model_gateway: DesktopModelGateway | None = None,
        answer_generator: AnswerGenerator | None = None,
        page_tree_provider: EvaluationPageTreeProvider | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        if page_tree_provider is None:
            self._retriever = DesktopEvidenceRetriever(self._kb_dir, model_gateway=model_gateway)
        else:
            self._retriever = DesktopEvidenceRetriever(
                self._kb_dir,
                model_gateway=model_gateway,
                page_tree_lease=page_tree_provider.lease,
            )
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._answer_generator = answer_generator or _production_answer_generator(model_gateway)
        self._page_tree_provider = page_tree_provider
        self._clock = clock

    def evaluate(
        self,
        suite: DesktopRetrievalEvaluationSuite,
        *,
        repetitions: int = 1,
        pageindex_worker_sha256: str | None = None,
    ) -> DesktopRetrievalEvaluationReport:
        """Measure every fixed case without enabling deferred graph capabilities."""
        if repetitions < 1:
            raise ValueError("Desktop retrieval evaluation repetitions must be at least one.")
        snapshot = self._derived_snapshot()
        corpus_digest = self._evaluated_corpus_digest(suite)
        results: list[DesktopRetrievalEvaluationCaseResult] = []
        for case in suite.cases:
            expected_evidence_ids = self._expected_evidence_ids(case)
            for repetition in range(1, repetitions + 1):
                planning_started = self._clock()
                planning = self._retriever.build_plan_with_cost(case.question)
                planning_latency_ms = (self._clock() - planning_started) * 1_000
                planning_cost = _retrieval_model_cost(planning.model_cost)
                for variant in DESKTOP_EVALUATION_VARIANT_ORDER:
                    retrieval_started = self._clock()
                    pack = self._retriever.retrieve_variant(
                        case.question,
                        variant=variant,
                        retrieval_plan=planning.plan,
                        degradations=tuple(
                            dict.fromkeys(
                                (
                                    *planning.degradations,
                                    *(
                                        self._page_tree_provider.degradations
                                        if self._page_tree_provider is not None
                                        and variant in PAGE_TREE_EVALUATION_VARIANTS
                                        else ()
                                    ),
                                )
                            )
                        ),
                    )
                    retrieval_latency_ms = (self._clock() - retrieval_started) * 1_000
                    answer_started = self._clock()
                    pack = prepare_grounded_evidence_pack(pack)
                    answer = _safe_answer(self._answer_generator, case.question, pack)
                    answer_latency_ms = (self._clock() - answer_started) * 1_000
                    latency_ms = planning_latency_ms + retrieval_latency_ms + answer_latency_ms
                    results.append(
                        _case_result(
                            case,
                            repetition,
                            variant,
                            expected_evidence_ids,
                            pack,
                            answer,
                            latency_ms,
                            retrieval_latency_ms,
                            answer_latency_ms,
                            planning_cost.plus(_retrieval_cost(pack)).plus(answer.model_cost),
                        )
                    )
        metrics = {
            variant: _metrics_for(results, variant) for variant in DESKTOP_EVALUATION_VARIANT_ORDER
        }
        final_snapshot = self._derived_snapshot()
        knowledge_snapshot_stable = (
            snapshot.knowledge_snapshot_digest == final_snapshot.knowledge_snapshot_digest
            and snapshot.knowledge_snapshot_revision == final_snapshot.knowledge_snapshot_revision
        )
        derived_generations_stable = (
            snapshot.catalog_generation_ids == final_snapshot.catalog_generation_ids
            and snapshot.page_tree_generations == final_snapshot.page_tree_generations
        )
        return DesktopRetrievalEvaluationReport(
            suite_snapshot_id=suite.snapshot_id,
            suite_digest=suite.digest,
            knowledge_snapshot_digest=snapshot.knowledge_snapshot_digest,
            knowledge_snapshot_revision=snapshot.knowledge_snapshot_revision,
            catalog_generation_ids=snapshot.catalog_generation_ids,
            page_tree_providers=snapshot.page_tree_providers,
            page_tree_generations=snapshot.page_tree_generations,
            repetitions=repetitions,
            results=tuple(results),
            metrics=metrics,
            gate=_page_tree_gate(
                suite,
                results,
                metrics,
                knowledge_snapshot_stable=knowledge_snapshot_stable,
                derived_generations_stable=derived_generations_stable,
                derived_identity_bound=snapshot.identity_bound,
            ),
            local_graph_gate=_local_graph_gate(
                suite,
                results,
                metrics,
                knowledge_snapshot_stable=knowledge_snapshot_stable,
            ),
            corpus_digest=corpus_digest,
            pageindex_worker_sha256=pageindex_worker_sha256,
            final_knowledge_snapshot_digest=final_snapshot.knowledge_snapshot_digest,
            final_knowledge_snapshot_revision=final_snapshot.knowledge_snapshot_revision,
            final_derived_snapshot_digest=evaluation_derived_snapshot_digest(
                final_snapshot.catalog_generation_ids,
                final_snapshot.page_tree_generations,
            ),
        )

    def promote_local_graph(self, report: DesktopRetrievalEvaluationReport) -> None:
        """Enable default local graph retrieval only after this gate has passed."""
        if not report.local_graph_gate.passed:
            raise ValueError(
                "A non-passing retrieval evaluation cannot enable local graph retrieval."
            )
        enable_local_graph_after_evaluation(
            self._kb_dir,
            report.suite_digest,
            report.knowledge_snapshot_digest,
            report.knowledge_snapshot_revision,
        )

    def require_page_tree_promotion_eligible(
        self,
        report: DesktopRetrievalEvaluationReport,
        suite: DesktopRetrievalEvaluationSuite,
    ) -> None:
        """Reject a stale or non-passing report before a later provider promotion."""
        current_derived = self._derived_snapshot()
        unchanged = (
            report.suite_snapshot_id == suite.snapshot_id
            and report.suite_digest == suite.digest
            and report.knowledge_snapshot_digest == current_derived.knowledge_snapshot_digest
            and report.knowledge_snapshot_revision == current_derived.knowledge_snapshot_revision
            and report.catalog_generation_ids == current_derived.catalog_generation_ids
            and report.page_tree_providers == current_derived.page_tree_providers
            and report.page_tree_generations == current_derived.page_tree_generations
        )
        if not report.gate.passed:
            raise ValueError(
                "A non-passing retrieval evaluation cannot promote a PageTree provider."
            )
        if not unchanged:
            raise ValueError(
                "The Desktop Knowledge Base or fixed suite changed after this retrieval "
                "evaluation; run the suite again."
            )

    def _derived_snapshot(self) -> _DerivedEvaluationSnapshot:
        connection = connect_desktop_read_only(self._database_path)
        try:
            connection.execute("BEGIN")
            knowledge_snapshot_digest = knowledge_snapshot_digest_in(connection, self._kb_dir)
            knowledge_snapshot_revision = knowledge_snapshot_revision_in(connection)
            catalog_row = connection.execute(
                """
                SELECT current_generation_id
                FROM knowledge_catalog_state
                WHERE singleton = 1
                """
            ).fetchone()
            page_tree_rows = connection.execute(
                """
                SELECT documents.document_id, generations.generation_id,
                    generations.provider_kind, generations.provider_version,
                    enrichment.enrichment_generation_id
                FROM source_documents AS documents
                LEFT JOIN document_page_tree_current AS current
                    ON current.document_id = documents.document_id
                LEFT JOIN document_page_tree_generations AS generations
                    ON generations.document_id = documents.document_id
                    AND generations.generation_id = current.generation_id
                    AND generations.status = 'current'
                LEFT JOIN document_page_tree_enrichment_current AS enrichment_current
                    ON enrichment_current.document_id = documents.document_id
                    AND enrichment_current.base_generation_id = generations.generation_id
                LEFT JOIN document_page_tree_enrichment_generations AS enrichment
                    ON enrichment.enrichment_generation_id =
                        enrichment_current.enrichment_generation_id
                    AND enrichment.base_generation_id = generations.generation_id
                    AND enrichment.status = 'current'
                WHERE documents.availability = 'available'
                ORDER BY documents.document_id
                """
            ).fetchall()
        except (sqlite3.Error, ValueError) as error:
            raise ValueError("Desktop retrieval evaluation generations are unavailable.") from error
        finally:
            connection.rollback()
            connection.close()
        catalog_generation_ids = (
            (str(catalog_row[0]),) if catalog_row is not None and catalog_row[0] is not None else ()
        )
        default_generations = tuple(
            DesktopPageTreeGenerationIdentity(
                document_id=str(row[0]),
                base_generation_id=str(row[1]) if row[1] is not None else None,
                provider_kind=str(row[2]) if row[2] is not None else None,
                provider_version=str(row[3]) if row[3] is not None else None,
                enrichment_generation_id=str(row[4]) if row[4] is not None else None,
            )
            for row in page_tree_rows
        )
        provider_generations = (
            self._page_tree_provider.generations
            if self._page_tree_provider is not None
            else default_generations
        )
        return _DerivedEvaluationSnapshot(
            knowledge_snapshot_digest=knowledge_snapshot_digest,
            knowledge_snapshot_revision=knowledge_snapshot_revision,
            catalog_generation_ids=catalog_generation_ids,
            page_tree_generations=provider_generations,
            provider_documents_complete=(
                self._page_tree_provider is None
                or {item.document_id for item in provider_generations}
                == {item.document_id for item in default_generations}
            ),
        )

    def _expected_evidence_ids(self, case: DesktopRetrievalEvaluationCase) -> tuple[str, ...]:
        """Resolve immutable suite anchors without relying on random import IDs."""
        if case.expect_absent_answer:
            return ()
        connection = connect_desktop_read_only(self._database_path)
        try:
            connection.execute("BEGIN")
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
            connection.rollback()
            connection.close()
        return tuple(resolved)

    def _evaluated_corpus_digest(self, suite: DesktopRetrievalEvaluationSuite) -> str:
        """Bind the report to the exact Available raw assets used by the suite."""
        names = tuple(
            sorted(
                {
                    selector.document_name
                    for case in suite.cases
                    for selector in case.expected_evidence
                }
            )
        )
        if not names:
            raise ValueError("Desktop retrieval evaluation corpus is empty.")
        connection = connect_desktop_read_only(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT display_name, asset_sha256
                FROM source_documents
                WHERE availability = 'available'
                ORDER BY display_name, document_id
                """
            ).fetchall()
        except sqlite3.Error as error:
            raise ValueError("Desktop retrieval evaluation corpus is unavailable.") from error
        finally:
            connection.close()
        records = tuple((str(row[0]), str(row[1])) for row in rows)
        if len(records) != len(names) or tuple(name for name, _digest in records) != names:
            raise ValueError("Desktop retrieval evaluation corpus does not match the fixed suite.")
        return evaluation_corpus_digest(records)


def _retrieval_model_cost(cost: DesktopRetrievalModelCost) -> DesktopEvaluationModelCost:
    return DesktopEvaluationModelCost(
        model_calls=cost.model_calls,
        input_characters=cost.input_characters,
        output_characters=cost.output_characters,
    )


def _retrieval_cost(pack: DesktopEvidencePack) -> DesktopEvaluationModelCost:
    return _retrieval_model_cost(pack.retrieval_model_cost)


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
    variant: DesktopEvaluationVariant,
    expected_evidence_ids: tuple[str, ...],
    pack: DesktopEvidencePack,
    answer: DesktopEvaluationAnswer,
    latency_ms: float,
    retrieval_latency_ms: float,
    answer_latency_ms: float,
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
    trace = pack.retrieval_trace
    selection_triggered = (
        any(
            channel.channel == "document_page_tree" and bool(channel.trigger_reasons)
            for channel in trace.channels
        )
        and bool(trace.selected_node_ids)
        and any("document_page_tree" in reference.channels for reference in pack.evidence)
    )
    return DesktopRetrievalEvaluationCaseResult(
        case_id=case.case_id,
        category=case.category,
        repetition=repetition,
        variant=variant,
        expected_evidence_ids=expected_evidence_ids,
        evidence_recall_at_k=evidence_recall,
        citation_precision=citation_precision,
        absent_answer_correct=case.expect_absent_answer and faithful,
        answer_faithfulness=1.0 if faithful else 0.0,
        latency_ms=latency_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        answer_latency_ms=answer_latency_ms,
        model_cost=model_cost,
        answer_status=answer.status,
        long_document=case.long_document,
        page_tree_selection_triggered=selection_triggered,
        degradation_reasons=tuple(dict.fromkeys((*pack.degradations, *trace.degradation_reasons))),
        catalog_generation_ids=trace.catalog_generation_ids,
        page_tree_generation_ids=trace.page_tree_generation_ids,
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
    results: list[DesktopRetrievalEvaluationCaseResult], variant: DesktopEvaluationVariant
) -> DesktopRetrievalEvaluationMetrics:
    selected = [result for result in results if result.variant == variant]
    if not selected:
        raise ValueError("Desktop retrieval evaluation has no results for a variant.")
    total = len(selected)
    cost = DesktopEvaluationModelCost()
    for result in selected:
        cost = cost.plus(result.model_cost)
    long_document = [
        result for result in selected if result.long_document and result.category != "absent_answer"
    ]
    absent_answers = [result for result in selected if result.category == "absent_answer"]
    return DesktopRetrievalEvaluationMetrics(
        case_runs=total,
        evidence_recall_k=DESKTOP_EVIDENCE_RECALL_K,
        evidence_recall_at_k=sum(result.evidence_recall_at_k for result in selected) / total,
        long_document_evidence_recall_at_k=(
            sum(result.evidence_recall_at_k for result in long_document) / len(long_document)
        ),
        citation_precision=sum(result.citation_precision for result in selected) / total,
        absent_answer_accuracy=(
            sum(result.absent_answer_correct for result in absent_answers) / len(absent_answers)
        ),
        answer_faithfulness=sum(result.answer_faithfulness for result in selected) / total,
        mean_latency_ms=sum(result.latency_ms for result in selected) / total,
        retrieval_p95_ms=_p95(tuple(result.retrieval_latency_ms for result in selected)),
        model_cost=cost,
        degradation_runs=sum(bool(result.degradation_reasons) for result in selected),
    )


def recompute_page_tree_evaluation_gate(
    report: DesktopRetrievalEvaluationReport,
    suite: DesktopRetrievalEvaluationSuite,
    *,
    derived_identity_bound: bool,
) -> DesktopPageTreeEvaluationGate:
    """Rebuild a serialized report's metrics and gate from case-level results."""
    results = list(report.results)
    metrics = {
        variant: _metrics_for(results, variant) for variant in DESKTOP_EVALUATION_VARIANT_ORDER
    }
    if metrics != report.metrics:
        raise ValueError("Desktop retrieval evaluation metrics do not match its results.")
    knowledge_snapshot_stable = (
        report.final_knowledge_snapshot_digest is not None
        and report.final_knowledge_snapshot_revision is not None
        and report.knowledge_snapshot_digest == report.final_knowledge_snapshot_digest
        and report.knowledge_snapshot_revision == report.final_knowledge_snapshot_revision
    )
    derived_generations_stable = (
        report.final_derived_snapshot_digest is not None
        and evaluation_derived_snapshot_digest(
            report.catalog_generation_ids, report.page_tree_generations
        )
        == report.final_derived_snapshot_digest
    )
    return _page_tree_gate(
        suite,
        results,
        metrics,
        knowledge_snapshot_stable=knowledge_snapshot_stable,
        derived_generations_stable=derived_generations_stable,
        derived_identity_bound=derived_identity_bound,
    )


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Desktop retrieval evaluation latency samples are unavailable.")
    return ordered[max(0, ((95 * len(ordered) + 99) // 100) - 1)]


def _page_tree_gate(
    suite: DesktopRetrievalEvaluationSuite,
    results: list[DesktopRetrievalEvaluationCaseResult],
    metrics: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationMetrics],
    *,
    knowledge_snapshot_stable: bool,
    derived_generations_stable: bool,
    derived_identity_bound: bool,
) -> DesktopPageTreeEvaluationGate:
    baseline = metrics["structure_lexical"]
    page_tree = metrics["document_page_tree"]
    enhanced = metrics["catalog + document_page_tree"]
    evaluated = (page_tree, enhanced)
    baseline_recall = baseline.long_document_evidence_recall_at_k
    required_recall = baseline_recall * 1.10 if baseline_recall > 0 else 0.10
    long_document_recall_gain = all(
        item.long_document_evidence_recall_at_k >= required_recall for item in evaluated
    )
    citation_precision_within_one_point = all(
        item.citation_precision + 0.01 >= baseline.citation_precision for item in evaluated
    )
    absent_answer_within_one_point = all(
        item.absent_answer_accuracy + 0.01 >= baseline.absent_answer_accuracy for item in evaluated
    )
    faithfulness_non_regression = all(
        item.answer_faithfulness >= baseline.answer_faithfulness for item in evaluated
    )
    latency_budget = min(
        suite.max_additional_retrieval_p95_ms,
        PAGE_TREE_MAX_ADDITIONAL_RETRIEVAL_P95_MS,
    )
    retrieval_p95_within_budget = all(
        item.retrieval_p95_ms - baseline.retrieval_p95_ms <= latency_budget for item in evaluated
    )
    model_cost_within_budget = all(
        item.model_cost.model_calls - baseline.model_cost.model_calls
        <= suite.max_additional_model_calls_per_case * item.case_runs
        for item in evaluated
    )
    page_tree_results = [result for result in results if result.variant == "document_page_tree"]
    enhanced_results = [
        result for result in results if result.variant == "catalog + document_page_tree"
    ]
    page_tree_selection_exercised = any(
        result.long_document
        and result.page_tree_selection_triggered
        and bool(result.page_tree_generation_ids)
        for result in page_tree_results
    )
    gated_results = (*page_tree_results, *enhanced_results)
    degradation_free = (
        bool(page_tree_results)
        and bool(enhanced_results)
        and all(
            not result.degradation_reasons and result.answer_status == "completed"
            for result in gated_results
        )
    )
    repetitions = {result.repetition for result in page_tree_results}
    fixed_suite_complete = (
        bool(repetitions)
        and {case.category for case in suite.cases} == EVALUATION_CATEGORIES
        and any(case.long_document and not case.expect_absent_answer for case in suite.cases)
        and len(page_tree_results) == len(suite.cases) * len(repetitions)
        and len(enhanced_results) == len(suite.cases) * len(repetitions)
    )
    checks = (
        long_document_recall_gain,
        page_tree_selection_exercised,
        citation_precision_within_one_point,
        absent_answer_within_one_point,
        faithfulness_non_regression,
        retrieval_p95_within_budget,
        model_cost_within_budget,
        degradation_free,
        knowledge_snapshot_stable,
        derived_generations_stable,
        derived_identity_bound,
        fixed_suite_complete,
    )
    return DesktopPageTreeEvaluationGate(
        passed=all(checks),
        long_document_recall_gain=long_document_recall_gain,
        page_tree_selection_exercised=page_tree_selection_exercised,
        citation_precision_within_one_point=citation_precision_within_one_point,
        absent_answer_within_one_point=absent_answer_within_one_point,
        faithfulness_non_regression=faithfulness_non_regression,
        retrieval_p95_within_budget=retrieval_p95_within_budget,
        model_cost_within_budget=model_cost_within_budget,
        degradation_free=degradation_free,
        knowledge_snapshot_stable=knowledge_snapshot_stable,
        derived_generations_stable=derived_generations_stable,
        derived_identity_bound=derived_identity_bound,
        fixed_suite_complete=fixed_suite_complete,
    )


def _local_graph_gate(
    suite: DesktopRetrievalEvaluationSuite,
    results: list[DesktopRetrievalEvaluationCaseResult],
    metrics: dict[DesktopEvaluationVariant, DesktopRetrievalEvaluationMetrics],
    *,
    knowledge_snapshot_stable: bool,
) -> DesktopLocalGraphEvaluationGate:
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
    return DesktopLocalGraphEvaluationGate(
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
    from openkb.desktop_retrieval_evaluation_cli import run

    return run(argv)


if __name__ == "__main__":  # pragma: no cover - exercised by the maintainer command.
    raise SystemExit(main())
