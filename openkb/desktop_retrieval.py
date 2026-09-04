"""Deterministic vectorless evidence retrieval for Desktop grounded answers."""

from __future__ import annotations

import inspect
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TypeAlias

from openkb.desktop_adaptive_navigation import (
    NAVIGATION_MAX_WALL_SECONDS,
    answer_kind_for_question,
    current_navigation_snapshot_id,
)
from openkb.desktop_answer_types import (
    DesktopAnswerError,
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopRetrievalModelCost,
    DesktopRetrievalPlan,
)
from openkb.desktop_catalog_store import lease_catalog_generation
from openkb.desktop_graph_feature_flags import local_graph_default_enabled
from openkb.desktop_knowledge_graph import (
    PinnedGraphGenerations,
    bounded_graph_rows,
    local_graph_evidence_ids,
    record_query_diagnostic,
)
from openkb.desktop_knowledge_navigation import (
    NAVIGATION_MAX_READS,
    NAVIGATION_MAX_SOURCE_WINDOWS,
    DesktopKnowledgeNavigationResult,
    build_knowledge_navigation_in,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_navigation_session import run_navigation_session
from openkb.desktop_page_tree import PageTreeGeneration
from openkb.desktop_page_tree_selection import (
    PageTreeLeaseFactory,
    PageTreeSelectionResult,
    select_page_tree_evidence,
)
from openkb.desktop_page_tree_store import lease_current_page_tree, lease_page_tree_generation
from openkb.desktop_retrieval_candidates import (
    catalog_channel_candidates as _scoped_catalog_channel_candidates,
)
from openkb.desktop_retrieval_candidates import (
    page_tree_candidates as _scoped_page_tree_candidates,
)
from openkb.desktop_retrieval_candidates import variant_evidence as _scoped_variant_evidence
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
from openkb.desktop_scoped_evidence import (
    ScopedEvidenceView,
    project_scoped_evidence_in,
    scoped_source_images,
)
from openkb.desktop_source_image_locator import source_image_matches_evidence
from openkb.desktop_version_retrieval_context import (
    capture_version_navigation_snapshot,
    coerce_retrieval_request,
    require_version_snapshot_current_in,
    version_scope_degradations,
)
from openkb.desktop_version_scope import NavigationSnapshot, RetrievalRequest
from openkb.desktop_workspace import desktop_state_database_path

_source_image_matches_evidence = source_image_matches_evidence
_Candidate: TypeAlias = RetrievalCandidate
_fuse_candidates = fuse_candidates

DESKTOP_EVIDENCE_RECALL_K = BASELINE_EVIDENCE_PACK_LIMIT
logger = logging.getLogger(__name__)


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
        question: str | RetrievalRequest,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        on_model_event: Callable[[object], None] | None = None,
        operation_retry_scopes: Mapping[str, str] | None = None,
    ) -> DesktopEvidencePack:
        """Run one bounded adaptive session with deterministic evidence as its fallback."""
        request = coerce_retrieval_request(question)
        normalized_question, version_snapshot = capture_version_navigation_snapshot(
            self._database_path, request
        )
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
            normalized_question,
            variant=variant,
            _version_navigation_snapshot=version_snapshot,
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
            normalized_question,
            variant=variant,
            _version_navigation_snapshot=version_snapshot,
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
                normalized_question,
                variant=variant,
                retrieval_plan=deterministic_plan(normalized_question),
                _version_navigation_snapshot=version_snapshot,
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
                question=normalized_question,
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
            question=normalized_question,
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
        _navigation_focus_terms: tuple[str, ...] = (),
        _navigation_excluded_routes: frozenset[str] = frozenset(),
        _navigation_prior_evidence: tuple[DesktopEvidenceRef, ...] = (),
        _navigation_requested_routes: tuple[str, ...] = (),
        _navigation_source_anchors: tuple[str, ...] = (),
        _bounded_model_attempts: bool = False,
        _model_response_deadline: float | None = None,
        _version_navigation_snapshot: NavigationSnapshot | None = None,
    ) -> DesktopEvidencePack:
        if variant not in DESKTOP_EVALUATION_VARIANTS or variant == "navigator":
            raise ValueError(f"Unsupported Desktop retrieval variant: {variant}")
        normalized_question = validate_question(question)
        if _version_navigation_snapshot is None:
            normalized_question, _version_navigation_snapshot = capture_version_navigation_snapshot(
                self._database_path, RetrievalRequest(question=normalized_question)
            )
        scoped_view = ScopedEvidenceView(_version_navigation_snapshot.version_scope)
        scope_degradations = version_scope_degradations(_version_navigation_snapshot)
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
            all_degradations = tuple((*degradations, *scope_degradations, *planning.degradations))
        else:
            if retrieval_plan.query != normalized_question:
                raise DesktopAnswerError(
                    "desktop_retrieval_plan_invalid",
                    "The evaluation retrieval plan does not match the question.",
                )
            plan = retrieval_plan
            planning_cost = DesktopRetrievalModelCost()
            all_degradations = tuple((*degradations, *scope_degradations))
        graph_error_code: str | None = None
        selection = PageTreeSelectionResult()
        navigation = DesktopKnowledgeNavigationResult()
        with _best_effort_catalog_lease(
            self._kb_dir,
            enabled=_enable_navigation or variant in CATALOG_RETRIEVAL_VARIANTS,
            lease_factory=lambda kb_dir: lease_catalog_generation(
                kb_dir, _version_navigation_snapshot.catalog_generation_id
            ),
        ) as (
            catalog,
            lease_degradations,
        ):
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN")
                require_version_snapshot_current_in(connection, _version_navigation_snapshot)
                catalog_candidates, catalog_degradation = _scoped_catalog_channel_candidates(
                    connection,
                    plan.terms,
                    catalog,
                    variant,
                    lease_degradations,
                    scoped_view=scoped_view,
                )
                variant_evidence = _scoped_variant_evidence(
                    connection,
                    plan.terms,
                    variant,
                    scoped_view=scoped_view,
                    catalog_candidates=catalog_candidates,
                    graph_lookup=partial(
                        local_graph_evidence_ids,
                        generation_snapshot=PinnedGraphGenerations(
                            _version_navigation_snapshot.active_knowledge_generation_id,
                            _version_navigation_snapshot.graph_result_ids,
                        ),
                    ),
                    graph_row_fetcher=bounded_graph_rows,
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
                    lease_tree=_snapshot_page_tree_lease_factory(
                        _version_navigation_snapshot,
                        fallback_factory=self._page_tree_lease,
                    ),
                    retry_scope=(operation_retry_scopes or {}).get("page_tree_selection"),
                    bounded_model_attempts=_bounded_model_attempts,
                    response_deadline=_model_response_deadline,
                    allowed_document_ids=scoped_view.scope.allowed_document_ids,
                )
            connection = _connect(self._database_path)
            try:
                connection.execute("BEGIN")
                require_version_snapshot_current_in(connection, _version_navigation_snapshot)
                page_tree_candidates = _scoped_page_tree_candidates(
                    connection,
                    selection.evidence_ids,
                    scoped_view=scoped_view,
                )
                if _enable_navigation and not (is_cancelled is not None and is_cancelled()):
                    try:
                        navigation_scope = (
                            {"scoped_view": scoped_view}
                            if "scoped_view"
                            in inspect.signature(build_knowledge_navigation_in).parameters
                            else {}
                        )
                        navigation = build_knowledge_navigation_in(
                            connection,
                            catalog_generation_id=(
                                catalog.generation_id if catalog is not None else None
                            ),
                            terms=plan.terms,
                            focus_terms=_navigation_focus_terms,
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
                            answer_kind=answer_kind_for_question(normalized_question),
                            **navigation_scope,  # type: ignore[arg-type]
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
                evidence = project_scoped_evidence_in(
                    connection,
                    _fuse_candidates(
                        candidates,
                        protected=variant_evidence.protected_candidates,
                        routed=(*navigation_candidates, *page_tree_candidates),
                    ),
                    scoped_view,
                )
                guidance, coverage_gate_state = navigation.grounded_guidance(
                    tuple(reference.evidence_id for reference in evidence),
                    page_tree_supplemented=bool(selection.evidence_ids),
                )
                source_images = scoped_source_images(
                    source_images_for_evidence(connection, evidence, self._kb_dir),
                    evidence,
                    scoped_view,
                )
                require_version_snapshot_current_in(connection, _version_navigation_snapshot)
            finally:
                connection.rollback()
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
            version_navigation_snapshot_id=_version_navigation_snapshot.snapshot_id,
            version_catalog_revision_id=(_version_navigation_snapshot.version_catalog_revision_id),
            version_catalog_digest=_version_navigation_snapshot.version_catalog_digest,
            version_scope_mode=scoped_view.scope.mode,
            version_scope_status=scoped_view.scope.status,
            version_scope_lineage_ids=scoped_view.scope.lineage_ids,
            version_scope_labels=scoped_view.scope.requested_labels,
            version_scope_document_ids=tuple(sorted(scoped_view.scope.allowed_document_ids)),
            version_scope_selection_reason=scoped_view.scope.selection_reason,
            version_scope_degradation_reason=(scoped_view.scope.degradation_reason or ""),
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


def _snapshot_page_tree_lease_factory(
    snapshot: NavigationSnapshot,
    *,
    fallback_factory: PageTreeLeaseFactory,
) -> PageTreeLeaseFactory:
    bindings = dict(snapshot.page_tree_generation_bindings)
    unusable_documents = frozenset(snapshot.unusable_page_tree_document_ids)

    @contextmanager
    def lease(kb_dir: Path, document_id: str) -> Iterator[PageTreeGeneration | None]:
        if document_id in unusable_documents:
            raise ValueError("The captured PageTree pointer is not current.")
        generation_id = bindings.get(document_id)
        if generation_id is None:
            yield None
            return
        manager = (
            lease_page_tree_generation(kb_dir, document_id, generation_id)
            if fallback_factory is lease_current_page_tree
            else fallback_factory(kb_dir, document_id)
        )
        with manager as generation:
            if generation is None or generation.generation_id != generation_id:
                yield None
            else:
                yield generation

    return lease


def _sum_model_cost(
    first: DesktopRetrievalModelCost, second: DesktopRetrievalModelCost
) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=first.model_calls + second.model_calls,
        input_characters=first.input_characters + second.input_characters,
        output_characters=first.output_characters + second.output_characters,
    )
