"""Deterministic vectorless evidence retrieval for Desktop grounded answers."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TypeAlias

from openkb import desktop_retrieval_rows as retrieval_rows
from openkb.desktop_adaptive_navigation import (
    NAVIGATION_MAX_WALL_SECONDS,
    current_navigation_snapshot_id,
)
from openkb.desktop_answer_types import (
    DesktopAnswerError,
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopRetrievalModelCost,
    DesktopRetrievalPlan,
)
from openkb.desktop_catalog_retrieval import catalog_route_rows_in
from openkb.desktop_catalog_store import CatalogGenerationLease, lease_current_catalog
from openkb.desktop_graph_feature_flags import local_graph_default_enabled
from openkb.desktop_knowledge_graph import (
    DesktopKnowledgeGraphQueryError,
    bounded_graph_rows,
    graph_query_deadline,
    local_graph_evidence_ids,
    record_query_diagnostic,
)
from openkb.desktop_knowledge_navigation import (
    NAVIGATION_MAX_READS,
    NAVIGATION_MAX_SOURCE_WINDOWS,
    DesktopKnowledgeNavigationResult,
    build_knowledge_navigation_in,
)
from openkb.desktop_knowledge_source_retrieval import knowledge_source_rows_in
from openkb.desktop_knowledge_sources import AVAILABLE_EVIDENCE_OCCURRENCES_CTE
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_navigation_session import run_navigation_session
from openkb.desktop_page_tree_selection import (
    PageTreeLeaseFactory,
    PageTreeSelectionResult,
    select_page_tree_evidence,
)
from openkb.desktop_page_tree_store import lease_current_page_tree
from openkb.desktop_retrieval_catalog_lease import (
    best_effort_catalog_lease as _best_effort_catalog_lease,
)
from openkb.desktop_retrieval_channels import (
    CATALOG_RETRIEVAL_VARIANTS,
    DESKTOP_EVALUATION_VARIANTS,
    PAGE_TREE_EVALUATION_VARIANTS,
    RETRIEVAL_CHANNELS_BY_VARIANT,
    DesktopEvaluationVariant,
)
from openkb.desktop_retrieval_fusion import (
    BASELINE_EVIDENCE_PACK_LIMIT,
    GRAPH_CANDIDATE_LIMIT,
    RetrievalCandidate,
    fuse_candidates,
)
from openkb.desktop_retrieval_images import source_images_for_evidence
from openkb.desktop_retrieval_plan import deterministic_plan, validate_question
from openkb.desktop_retrieval_planning import (
    DesktopRetrievalPlanningResult,
    build_retrieval_plan,
)
from openkb.desktop_retrieval_trace import (
    FUSION_POLICY_VERSION,
    DesktopRetrievalChannelTrace,
    DesktopRetrievalTrace,
)
from openkb.desktop_source_image_locator import source_image_matches_evidence
from openkb.desktop_workspace import desktop_state_database_path

_source_image_matches_evidence = source_image_matches_evidence
_Candidate: TypeAlias = RetrievalCandidate
_fuse_candidates = fuse_candidates

_CHANNEL_LIMIT = 12
DESKTOP_EVIDENCE_RECALL_K = BASELINE_EVIDENCE_PACK_LIMIT
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _VariantEvidence:
    evidence: tuple[DesktopEvidenceRef, ...]
    candidates: tuple[_Candidate, ...]
    protected_candidates: tuple[_Candidate, ...]
    channel_counts: tuple[tuple[str, int], ...]
    graph_error_code: str | None = None


class DesktopEvidenceRetriever:
    def __init__(
        self,
        kb_dir: Path,
        *,
        model_gateway: DesktopModelGateway | None = None,
        page_tree_lease: PageTreeLeaseFactory = lease_current_page_tree,
    ) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._model_gateway = model_gateway
        self._page_tree_lease = page_tree_lease

    def retrieve(
        self,
        question: str,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        on_model_event: Callable[[object], None] | None = None,
        operation_retry_scopes: Mapping[str, str] | None = None,
    ) -> DesktopEvidencePack:
        """Run one bounded adaptive session with deterministic evidence as its fallback."""
        navigation_started_at = time.monotonic()
        navigation_deadline = navigation_started_at + NAVIGATION_MAX_WALL_SECONDS

        def session_cancelled() -> bool:
            return bool(is_cancelled is not None and is_cancelled()) or (
                time.monotonic() >= navigation_deadline
            )

        variant: DesktopEvaluationVariant = (
            "local_graph" if local_graph_default_enabled(self._kb_dir) else "baseline"
        )
        pinned_snapshot_id = current_navigation_snapshot_id(self._database_path)
        initial_pack = self.retrieve_variant(
            question,
            variant=variant,
            is_cancelled=session_cancelled,
            on_model_event=on_model_event,
            operation_retry_scopes=operation_retry_scopes,
            _enable_page_tree_selection=True,
            _enable_navigation=True,
            _bounded_model_attempts=True,
            _model_response_deadline=navigation_deadline,
        )
        retrieve_round = partial(
            self.retrieve_variant,
            question,
            variant=variant,
            is_cancelled=session_cancelled,
            on_model_event=on_model_event,
            operation_retry_scopes=operation_retry_scopes,
            _enable_page_tree_selection=False,
            _enable_navigation=True,
            _model_response_deadline=navigation_deadline,
        )
        if current_navigation_snapshot_id(self._database_path) != pinned_snapshot_id:
            fallback_snapshot_id = current_navigation_snapshot_id(self._database_path)
            fallback = self.retrieve_variant(
                question,
                variant=variant,
                retrieval_plan=deterministic_plan(validate_question(question)),
            )
            if current_navigation_snapshot_id(self._database_path) != fallback_snapshot_id:
                fallback_snapshot_id = current_navigation_snapshot_id(self._database_path)
                fallback = replace(
                    fallback,
                    evidence=(),
                    source_images=(),
                    retrieval_trace=DesktopRetrievalTrace(),
                    guidance=(),
                    route_options=(),
                )
            fallback = replace(
                fallback,
                retrieval_model_cost=_sum_model_cost(
                    initial_pack.retrieval_model_cost,
                    fallback.retrieval_model_cost,
                ),
            )
            return run_navigation_session(
                kb_dir=self._kb_dir,
                database_path=self._database_path,
                question=question,
                pinned_snapshot_id=fallback_snapshot_id,
                initial_pack=fallback,
                model_gateway=self._model_gateway,
                retrieve_round=retrieve_round,
                is_cancelled=session_cancelled,
                on_model_event=on_model_event,
                retry_scope=(operation_retry_scopes or {}).get("knowledge_navigation_step"),
                initial_stop_reason="snapshot_degraded",
                initial_degradations=(
                    *initial_pack.degradations,
                    "knowledge_navigation_snapshot_changed",
                ),
                session_started_at=navigation_started_at,
                session_deadline=navigation_deadline,
            )
        return run_navigation_session(
            kb_dir=self._kb_dir,
            database_path=self._database_path,
            question=question,
            pinned_snapshot_id=pinned_snapshot_id,
            initial_pack=initial_pack,
            model_gateway=self._model_gateway,
            retrieve_round=retrieve_round,
            is_cancelled=session_cancelled,
            on_model_event=on_model_event,
            retry_scope=(operation_retry_scopes or {}).get("knowledge_navigation_step"),
            session_started_at=navigation_started_at,
            session_deadline=navigation_deadline,
        )

    def build_plan(
        self, question: str, *, is_cancelled: Callable[[], bool] | None = None
    ) -> tuple[DesktopRetrievalPlan, tuple[str, ...]]:
        result = self.build_plan_with_cost(question, is_cancelled=is_cancelled)
        return result.plan, result.degradations

    def build_plan_with_cost(
        self,
        question: str,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        on_model_event: Callable[[object], None] | None = None,
    ) -> DesktopRetrievalPlanningResult:
        return build_retrieval_plan(
            validate_question(question),
            self._model_gateway,
            kb_dir=self._kb_dir,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
        )

    def retrieve_variant(
        self,
        question: str,
        *,
        variant: DesktopEvaluationVariant,
        retrieval_plan: DesktopRetrievalPlan | None = None,
        degradations: tuple[str, ...] = (),
        is_cancelled: Callable[[], bool] | None = None,
        on_model_event: Callable[[object], None] | None = None,
        operation_retry_scopes: Mapping[str, str] | None = None,
        _enable_page_tree_selection: bool = False,
        _enable_navigation: bool = False,
        _navigation_max_reads: int | None = None,
        _navigation_max_source_windows: int | None = None,
        _navigation_excluded_routes: frozenset[str] = frozenset(),
        _navigation_prior_evidence: tuple[DesktopEvidenceRef, ...] = (),
        _navigation_requested_routes: tuple[str, ...] = (),
        _navigation_source_anchors: tuple[str, ...] = (),
        _bounded_model_attempts: bool = False,
        _model_response_deadline: float | None = None,
    ) -> DesktopEvidencePack:
        if variant not in DESKTOP_EVALUATION_VARIANTS or variant == "navigator":
            raise ValueError(f"Unsupported Desktop retrieval variant: {variant}")
        normalized_question = validate_question(question)
        if retrieval_plan is None:
            planning = build_retrieval_plan(
                normalized_question,
                self._model_gateway,
                kb_dir=self._kb_dir,
                is_cancelled=is_cancelled,
                on_model_event=on_model_event,
                retry_scope=(operation_retry_scopes or {}).get("retrieval_plan"),
                bounded_model_attempts=_bounded_model_attempts,
                response_deadline=_model_response_deadline,
            )
            plan = planning.plan
            planning_cost = planning.model_cost
            all_degradations = tuple((*degradations, *planning.degradations))
        else:
            if retrieval_plan.query != normalized_question:
                raise DesktopAnswerError(
                    "desktop_retrieval_plan_invalid",
                    "The evaluation retrieval plan does not match the question.",
                )
            plan = retrieval_plan
            planning_cost = DesktopRetrievalModelCost()
            all_degradations = degradations
        graph_error_code: str | None = None
        selection = PageTreeSelectionResult()
        navigation = DesktopKnowledgeNavigationResult()
        with _best_effort_catalog_lease(
            self._kb_dir,
            enabled=_enable_navigation or variant in CATALOG_RETRIEVAL_VARIANTS,
            lease_factory=lease_current_catalog,
        ) as (
            catalog,
            lease_degradations,
        ):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN")
                catalog_candidates, catalog_degradation = _catalog_channel_candidates(
                    connection,
                    plan.terms,
                    catalog,
                    variant,
                    lease_degradations,
                )
                variant_evidence = _variant_evidence(
                    connection,
                    plan.terms,
                    variant,
                    catalog_candidates=catalog_candidates,
                )
                graph_error_code = variant_evidence.graph_error_code
            finally:
                connection.rollback()
                connection.close()
            page_tree_enabled = variant in PAGE_TREE_EVALUATION_VARIANTS or (
                _enable_page_tree_selection and variant in {"baseline", "local_graph"}
            )
            cancelled_before_derived_reads = bool(is_cancelled is not None and is_cancelled())
            if page_tree_enabled and not cancelled_before_derived_reads:
                selection = select_page_tree_evidence(
                    self._kb_dir,
                    normalized_question,
                    plan,
                    variant_evidence.evidence,
                    self._model_gateway,
                    is_cancelled=is_cancelled,
                    on_model_event=on_model_event,
                    lease_tree=self._page_tree_lease,
                    retry_scope=(operation_retry_scopes or {}).get("page_tree_selection"),
                    bounded_model_attempts=_bounded_model_attempts,
                    response_deadline=_model_response_deadline,
                )
            connection = _connect(self._database_path)
            try:
                page_tree_candidates = _page_tree_candidates(connection, selection.evidence_ids)
                if _enable_navigation and not (is_cancelled is not None and is_cancelled()):
                    try:
                        navigation = build_knowledge_navigation_in(
                            connection,
                            catalog_generation_id=(
                                catalog.generation_id if catalog is not None else None
                            ),
                            terms=plan.terms,
                            baseline_evidence=(
                                *_navigation_prior_evidence,
                                *variant_evidence.evidence,
                                *(candidate.reference for candidate in page_tree_candidates),
                            ),
                            max_reads=(
                                _navigation_max_reads
                                if _navigation_max_reads is not None
                                else NAVIGATION_MAX_READS
                            ),
                            max_source_windows=(
                                _navigation_max_source_windows
                                if _navigation_max_source_windows is not None
                                else NAVIGATION_MAX_SOURCE_WINDOWS
                            ),
                            excluded_routes=_navigation_excluded_routes,
                            requested_routes=_navigation_requested_routes,
                            requested_evidence_ids=_navigation_source_anchors,
                        )
                    except (KeyError, TypeError, ValueError, sqlite3.Error):
                        logger.warning(
                            "Knowledge Navigation degraded to baseline retrieval.",
                            exc_info=True,
                        )
                        navigation = DesktopKnowledgeNavigationResult(
                            degradation_reasons=("knowledge_navigation_failed",)
                        )
                navigation_candidates = tuple(
                    _Candidate(
                        reference=reference,
                        channel="knowledge_navigation_source_window",
                        rank=rank,
                        weight=1.0,
                    )
                    for rank, reference in enumerate(navigation.source_windows, start=1)
                )
                candidates = (
                    *variant_evidence.candidates,
                    *navigation_candidates,
                    *page_tree_candidates,
                )
                evidence = _fuse_candidates(
                    candidates,
                    protected=variant_evidence.protected_candidates,
                    routed=(*navigation_candidates, *page_tree_candidates),
                )
                guidance, coverage_gate_state = navigation.grounded_guidance(
                    tuple(reference.evidence_id for reference in evidence),
                    page_tree_supplemented=bool(selection.evidence_ids),
                )
                source_images = source_images_for_evidence(connection, evidence, self._kb_dir)
            finally:
                connection.close()
        if graph_error_code is not None:
            record_query_diagnostic(self._kb_dir, graph_error_code)
        retrieval_degradations = tuple(
            dict.fromkeys(
                (
                    *all_degradations,
                    *catalog_degradation,
                    *selection.degradation_reasons,
                )
            )
        )
        trace_degradations = tuple(
            dict.fromkeys(
                (
                    *retrieval_degradations,
                    *navigation.degradation_reasons,
                    *((graph_error_code,) if graph_error_code else ()),
                )
            )
        )
        channel_counts = dict(variant_evidence.channel_counts)
        for channel in RETRIEVAL_CHANNELS_BY_VARIANT[variant]:
            channel_counts.setdefault(channel, 0)
        if page_tree_enabled:
            channel_counts["document_page_tree"] = len(page_tree_candidates)
        if _enable_navigation:
            channel_counts["knowledge_navigation_source_window"] = len(navigation_candidates)
        channel_degradations = {
            "catalog": catalog_degradation,
            "knowledge_graph": ((graph_error_code,) if graph_error_code else ()),
            "document_page_tree": selection.degradation_reasons,
        }
        trace = DesktopRetrievalTrace(
            catalog_generation_ids=((catalog.generation_id,) if catalog is not None else ()),
            page_tree_generation_ids=selection.generation_ids,
            channels=tuple(
                DesktopRetrievalChannelTrace(
                    channel,
                    count,
                    trigger_reasons=(
                        selection.trigger_reasons if channel == "document_page_tree" else ()
                    ),
                    degradation_reasons=tuple(
                        dict.fromkeys((*all_degradations, *channel_degradations.get(channel, ())))
                    ),
                )
                for channel, count in channel_counts.items()
            ),
            trigger_reasons=selection.trigger_reasons,
            degradation_reasons=trace_degradations,
            selected_node_ids=selection.selected_node_ids,
            canonical_evidence_ids=tuple(reference.evidence_id for reference in evidence),
            fusion_policy_version=FUSION_POLICY_VERSION,
            navigation_snapshot_ids=(
                (navigation.snapshot_id,) if navigation.snapshot_id is not None else ()
            ),
            navigation_routes=navigation.routes,
            navigation_read_count=navigation.read_count,
            source_window_count=navigation.source_window_count,
            link_hop_count=navigation.link_hop_count,
            page_tree_supplement_count=1 if selection.evidence_ids else 0,
            coverage_gate_state=coverage_gate_state,
        )
        return DesktopEvidencePack(
            retrieval_plan=plan,
            evidence=evidence,
            degradations=retrieval_degradations,
            source_images=source_images,
            retrieval_trace=trace,
            retrieval_model_cost=DesktopRetrievalModelCost(
                model_calls=planning_cost.model_calls + selection.model_cost.model_calls,
                input_characters=(
                    planning_cost.input_characters + selection.model_cost.input_characters
                ),
                output_characters=(
                    planning_cost.output_characters + selection.model_cost.output_characters
                ),
            ),
            guidance=guidance,
            route_options=navigation.route_options,
        )


def _connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise DesktopAnswerError(
            "desktop_knowledge_base_not_found",
            "Open a Desktop Knowledge Base before asking a question.",
        )
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _sum_model_cost(
    first: DesktopRetrievalModelCost, second: DesktopRetrievalModelCost
) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=first.model_calls + second.model_calls,
        input_characters=first.input_characters + second.input_characters,
        output_characters=first.output_characters + second.output_characters,
    )


def _fts_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    if not terms:
        return ()
    query = " OR ".join(f'"{term}"' for term in terms)
    try:
        rows = connection.execute(
            f"""
            {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
            SELECT available_evidence_occurrences.evidence_id,
                available_evidence_occurrences.document_id,
                available_evidence_occurrences.display_name,
                available_evidence_occurrences.heading_path,
                available_evidence_occurrences.locator_json,
                available_evidence_occurrences.text
            FROM evidence_fts
            JOIN available_evidence_occurrences
                ON available_evidence_occurrences.evidence_id = evidence_fts.evidence_id
            WHERE evidence_fts MATCH ? AND available_evidence_occurrences.occurrence_rank = 1
            ORDER BY bm25(evidence_fts), available_evidence_occurrences.document_id,
                available_evidence_occurrences.ordinal
            LIMIT ?
            """,
            (query, _CHANNEL_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = _like_rows(connection, terms)
    return retrieval_rows.ranked_candidates(rows, "fts")


def _like_rows(connection: sqlite3.Connection, terms: tuple[str, ...]) -> list[tuple[object, ...]]:
    clauses = " OR ".join("lower(text) LIKE ?" for _ in terms)
    return connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND ({clauses})
        ORDER BY document_id, ordinal
        LIMIT ?
        """,
        tuple(f"%{term}%" for term in terms) + (_CHANNEL_LIMIT,),
    ).fetchall()


def _structure_lexical_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    if not terms:
        return ()
    return retrieval_rows.ranked_candidates(
        retrieval_rows.scored_rows(
            connection,
            terms,
            weighted_columns=(("heading_path", 2), ("text", 1)),
        ),
        "structure_lexical",
    )


def _wiki_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    """Find evidence from canonical published document and section names.

    T19 will add editable concept/entity pages to this logical channel. Until then,
    the imported document names and headings are already published knowledge names,
    so this route stays useful rather than becoming an empty future placeholder.
    """
    if not terms:
        return ()
    return retrieval_rows.ranked_candidates(
        retrieval_rows.scored_rows(
            connection,
            terms,
            weighted_columns=(
                ("display_name", 2),
                ("heading_path", 1),
            ),
        ),
        "wiki",
    )


def _knowledge_source_candidates(
    connection: sqlite3.Connection, terms: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    """Use published claim wording only to route back to its mapped original evidence."""
    return retrieval_rows.ranked_candidates(
        knowledge_source_rows_in(connection, terms, limit=_CHANNEL_LIMIT),
        "knowledge_source",
    )


def _catalog_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    generation_id: str | None,
) -> tuple[_Candidate, ...]:
    if generation_id is None:
        return ()
    rows = catalog_route_rows_in(connection, generation_id, terms, limit=_CHANNEL_LIMIT)
    values: list[_Candidate] = []
    for rank, row in enumerate(rows, start=1):
        reference = DesktopEvidenceRef(
            evidence_id=str(row[0]),
            document_id=str(row[1]),
            document_name=str(row[2]),
            section=retrieval_rows.section_from_json(str(row[3])),
            locator=retrieval_rows.json_object(str(row[4])),
            excerpt=str(row[5]),
            channels=("catalog",),
        )
        route_weight = row[6]
        if isinstance(route_weight, bool) or not isinstance(route_weight, (int, float)):
            raise DesktopAnswerError(
                "desktop_catalog_state_invalid",
                "The current Knowledge Catalog contains an invalid route weight.",
            )
        values.append(_Candidate(reference, "catalog", rank, weight=float(route_weight)))
    return tuple(values)


def _catalog_channel_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    catalog: CatalogGenerationLease | None,
    variant: DesktopEvaluationVariant,
    lease_degradations: tuple[str, ...],
) -> tuple[tuple[_Candidate, ...], tuple[str, ...]]:
    """Drop only the optional Catalog channel when its derived state is invalid."""
    if variant not in CATALOG_RETRIEVAL_VARIANTS:
        return (), ()
    if lease_degradations:
        return (), lease_degradations
    try:
        candidates = _catalog_candidates(
            connection,
            terms,
            catalog.generation_id if catalog is not None else None,
        )
        return candidates, _catalog_degradation(connection, catalog, variant)
    except Exception:
        logger.warning("Knowledge Catalog query failed; using baseline retrieval.", exc_info=True)
        return (), ("catalog_query_failed",)


def _graph_candidates(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    baseline: tuple[DesktopEvidenceRef, ...],
) -> tuple[_Candidate, ...]:
    """Resolve a bounded graph neighborhood back to available EvidenceRefs."""
    deadline = graph_query_deadline()
    evidence_ids = local_graph_evidence_ids(
        connection,
        terms=terms,
        anchor_evidence_ids=tuple(reference.evidence_id for reference in baseline),
        deadline=deadline,
    )
    if not evidence_ids:
        return ()
    rows = bounded_graph_rows(
        connection,
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND evidence_id IN ({retrieval_rows.placeholders(evidence_ids)})
        """,
        evidence_ids,
        deadline,
    )
    rows_by_evidence_id = {str(row[0]): row for row in rows}
    ordered_rows = [
        rows_by_evidence_id[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in rows_by_evidence_id
    ]
    return retrieval_rows.ranked_candidates(ordered_rows, "knowledge_graph")


def _variant_evidence(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    variant: DesktopEvaluationVariant,
    catalog_candidates: tuple[_Candidate, ...] = (),
) -> _VariantEvidence:
    """Build one evaluation candidate set without adding unrequested channels."""
    if variant == "fts":
        candidates = _fts_candidates(connection, terms)
        return _variant_result(candidates)
    if variant == "structure_lexical":
        candidates = _structure_lexical_candidates(connection, terms)
        return _variant_result(candidates)
    if variant in PAGE_TREE_EVALUATION_VARIANTS:
        protected = _structure_lexical_candidates(connection, terms)
        candidates = protected + catalog_candidates
        return _variant_result(candidates, protected=protected)
    if variant == "wiki":
        candidates = (
            _wiki_candidates(connection, terms)
            + _knowledge_source_candidates(connection, terms)
            + catalog_candidates
        )
        return _variant_result(candidates)

    protected = _fts_candidates(connection, terms) + _structure_lexical_candidates(
        connection, terms
    )
    candidates = (
        protected
        + _wiki_candidates(connection, terms)
        + _knowledge_source_candidates(connection, terms)
        + catalog_candidates
    )
    baseline = _fuse_candidates(candidates, protected=protected)
    if variant == "baseline":
        return _variant_result(candidates, protected=protected)
    try:
        graph_candidates = _graph_candidates(connection, terms, baseline)
    except DesktopKnowledgeGraphQueryError as error:
        # A failed graph capability is never user-visible and never removes
        # the independently retrieved baseline.
        return _variant_result(
            candidates,
            protected=protected,
            graph_error_code=error.code,
            extra_channel_counts=(("knowledge_graph", 0),),
        )
    bounded_graph = graph_candidates[:GRAPH_CANDIDATE_LIMIT]
    return _variant_result(
        (*candidates, *bounded_graph),
        protected=protected,
        extra_channel_counts=(("knowledge_graph", len(bounded_graph)),),
    )


def _variant_result(
    candidates: tuple[_Candidate, ...],
    *,
    protected: tuple[_Candidate, ...] = (),
    graph_error_code: str | None = None,
    extra_channel_counts: tuple[tuple[str, int], ...] = (),
) -> _VariantEvidence:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.channel] = counts.get(candidate.channel, 0) + 1
    counts.update(extra_channel_counts)
    return _VariantEvidence(
        evidence=_fuse_candidates(candidates, protected=protected),
        candidates=candidates,
        protected_candidates=protected,
        channel_counts=tuple(counts.items()),
        graph_error_code=graph_error_code,
    )


def _page_tree_candidates(
    connection: sqlite3.Connection, evidence_ids: tuple[str, ...]
) -> tuple[_Candidate, ...]:
    """Resolve selected tree bindings back to canonical Available EvidenceRefs."""
    if not evidence_ids:
        return ()
    rows = connection.execute(
        f"""
        {AVAILABLE_EVIDENCE_OCCURRENCES_CTE}
        SELECT evidence_id, document_id, display_name, heading_path, locator_json, text
        FROM available_evidence_occurrences
        WHERE occurrence_rank = 1 AND evidence_id IN ({retrieval_rows.placeholders(evidence_ids)})
        """,
        evidence_ids,
    ).fetchall()
    rows_by_evidence_id = {str(row[0]): row for row in rows}
    ordered_rows = [
        rows_by_evidence_id[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in rows_by_evidence_id
    ]
    return retrieval_rows.ranked_candidates(ordered_rows, "document_page_tree")


def _catalog_degradation(
    connection: sqlite3.Connection,
    catalog: CatalogGenerationLease | None,
    variant: DesktopEvaluationVariant,
) -> tuple[str, ...]:
    if variant not in CATALOG_RETRIEVAL_VARIANTS:
        return ()
    if catalog is not None:
        return ("catalog_stale",) if catalog.is_stale else ()
    row = connection.execute(
        "SELECT status FROM knowledge_catalog_rebuild_tasks WHERE singleton = 1"
    ).fetchone()
    return ("catalog_unavailable",) if row is not None else ()
