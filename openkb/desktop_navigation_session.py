"""Private bounded feedback loop behind DesktopEvidenceRetriever.retrieve."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from openkb.desktop_adaptive_navigation import (
    NAVIGATION_MAX_LOGICAL_READS,
    NAVIGATION_MAX_LOGICAL_READS_PER_ROUND,
    NAVIGATION_MAX_MODEL_CALLS,
    NAVIGATION_MAX_ROUNDS,
    NAVIGATION_MAX_SEARCH_ACTIONS,
    NAVIGATION_MAX_SOURCE_TOKENS,
    NAVIGATION_MAX_WALL_SECONDS,
    NavigationBudget,
    NavigationObjective,
    coverage_complete,
    coverage_gate_state,
    current_navigation_snapshot_id,
    estimated_source_tokens,
    initial_navigation_objective,
    navigation_requires_model,
    run_navigation_step,
    seed_facet_coverage,
)
from openkb.desktop_answer_types import (
    DesktopAnswerSourceImage,
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopKnowledgeGuidance,
    DesktopKnowledgeRouteOption,
    DesktopRetrievalPlan,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_navigation_budget import (
    NavigationEvidenceEnvelope,
    navigation_evidence_envelope,
)
from openkb.desktop_navigation_budget import (
    group_read_budget as _group_read_budget,
)
from openkb.desktop_navigation_budget import (
    group_read_limits as _group_read_limits,
)
from openkb.desktop_navigation_evidence import (
    NAVIGATION_MAX_EVIDENCE_REFS,
)
from openkb.desktop_navigation_evidence import (
    allocate_evidence as _allocate_evidence,
)
from openkb.desktop_navigation_evidence import (
    new_action_evidence_ids as _new_action_evidence_ids,
)
from openkb.desktop_navigation_evidence import (
    targeted_source_sequence_evidence_ids as _targeted_source_sequence_evidence_ids,
)
from openkb.desktop_navigation_references import unique_actions as _unique_actions
from openkb.desktop_navigation_references import unresolved_reference_actions
from openkb.desktop_navigation_session_values import (
    logical_reads as _logical_reads,
)
from openkb.desktop_navigation_session_values import (
    order_evidence_by_coverage as _order_evidence_by_coverage,
)
from openkb.desktop_navigation_session_values import (
    sum_cost as _sum_cost,
)
from openkb.desktop_navigation_session_values import (
    with_added_cost as _with_added_cost,
)
from openkb.desktop_navigation_validation import NavigationAction
from openkb.desktop_retrieval_trace import (
    DesktopFacetCoverageTrace,
    DesktopRetrievalChannelTrace,
    DesktopRetrievalTrace,
)


class NavigationRoundRetriever(Protocol):
    """Internal callable that reuses the fixed retrieval variants for one round."""

    def __call__(
        self,
        *,
        retrieval_plan: DesktopRetrievalPlan,
        _navigation_max_reads: int,
        _navigation_max_source_windows: int,
        _navigation_focus_terms: tuple[str, ...],
        _navigation_excluded_routes: frozenset[str],
        _navigation_prior_evidence: tuple[DesktopEvidenceRef, ...],
        _navigation_requested_routes: tuple[str, ...],
        _navigation_source_anchors: tuple[str, ...],
    ) -> DesktopEvidencePack: ...


def run_navigation_session(
    *,
    kb_dir: Path,
    database_path: Path,
    question: str,
    pinned_snapshot_id: str,
    initial_pack: DesktopEvidencePack,
    model_gateway: DesktopModelGateway | None,
    retrieve_round: NavigationRoundRetriever,
    is_cancelled: Callable[[], bool] | None = None,
    on_model_event: Callable[[object], None] | None = None,
    retry_scope: str | None = None,
    initial_stop_reason: str | None = None,
    initial_degradations: tuple[str, ...] = (),
    session_started_at: float | None = None,
    session_deadline: float | None = None,
) -> DesktopEvidencePack:
    """Observe, replan, retrieve and stop within one immutable session envelope."""
    started_at = time.monotonic() if session_started_at is None else session_started_at
    deadline = (
        started_at + NAVIGATION_MAX_WALL_SECONDS if session_deadline is None else session_deadline
    )
    evidence_envelope = navigation_evidence_envelope(model_gateway)
    pack, source_budget_reduced = _bounded_initial_pack(
        initial_pack,
        evidence_envelope=evidence_envelope,
    )
    objective = initial_navigation_objective(question, pack.retrieval_plan, pack.retrieval_trace)
    coverage = seed_facet_coverage(objective, pack)
    rounds = 0
    model_calls = initial_pack.retrieval_model_cost.model_calls
    logical_reads = _logical_reads(pack.retrieval_trace)
    source_tokens = estimated_source_tokens(pack)
    search_actions = 0
    preferred_evidence_ids: tuple[str, ...] = ()
    priority_evidence_ids: tuple[str, ...] = ()
    visited_action_ids: set[str] = set()
    action_kinds: list[str] = []
    degradations = list(initial_degradations)
    if source_budget_reduced:
        degradations.append("knowledge_navigation_source_budget_exhausted")

    if time.monotonic() >= deadline:
        return _finalize(
            pack,
            pinned_snapshot_id=pinned_snapshot_id,
            objective=objective,
            coverage=coverage,
            rounds=rounds,
            action_kinds=action_kinds,
            stop_reason="budget_exhausted",
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            degradations=degradations,
        )
    if is_cancelled is not None and is_cancelled():
        degradations.append("knowledge_navigation_cancelled")
        return _finalize(
            pack,
            pinned_snapshot_id=pinned_snapshot_id,
            objective=objective,
            coverage=coverage,
            rounds=rounds,
            action_kinds=action_kinds,
            stop_reason="cancelled",
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            degradations=degradations,
        )
    if current_navigation_snapshot_id(database_path) != pinned_snapshot_id:
        degradations.append("knowledge_navigation_snapshot_changed")
        return _finalize(
            pack,
            pinned_snapshot_id=pinned_snapshot_id,
            objective=objective,
            coverage=coverage,
            rounds=rounds,
            action_kinds=action_kinds,
            stop_reason="snapshot_degraded",
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            degradations=degradations,
        )
    if initial_stop_reason is not None:
        return _finalize(
            pack,
            pinned_snapshot_id=pinned_snapshot_id,
            objective=objective,
            coverage=coverage,
            rounds=rounds,
            action_kinds=action_kinds,
            stop_reason=initial_stop_reason,
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            degradations=degradations,
        )
    if not navigation_requires_model(objective, coverage):
        return _finalize(
            pack,
            pinned_snapshot_id=pinned_snapshot_id,
            objective=objective,
            coverage=coverage,
            rounds=rounds,
            action_kinds=action_kinds,
            stop_reason=(
                "semantic_structure_unknown"
                if objective.semantic_structure_state == "unknown"
                else "covered"
            ),
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            degradations=degradations,
        )

    stop_reason = "partial"
    while True:
        budget_stop = _budget_stop_reason(
            deadline=deadline,
            rounds=rounds,
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            search_actions=search_actions,
            source_token_limit=evidence_envelope.max_source_tokens,
        )
        if budget_stop is not None:
            stop_reason = budget_stop
            break
        if is_cancelled is not None and is_cancelled():
            stop_reason = "cancelled"
            degradations.append("knowledge_navigation_cancelled")
            break
        if current_navigation_snapshot_id(database_path) != pinned_snapshot_id:
            stop_reason = "snapshot_degraded"
            degradations.append("knowledge_navigation_snapshot_changed")
            break

        rounds += 1
        step = run_navigation_step(
            kb_dir=kb_dir,
            model_gateway=model_gateway,
            question=question,
            snapshot_id=pinned_snapshot_id,
            objective=objective,
            pack=pack,
            current_coverage=coverage,
            visited_action_ids=frozenset(visited_action_ids),
            budget=_remaining_budget(
                rounds=rounds,
                model_calls=model_calls,
                logical_reads=logical_reads,
                source_tokens=source_tokens,
                search_actions=search_actions,
                source_token_limit=evidence_envelope.max_source_tokens,
            ),
            round_number=rounds,
            preferred_evidence_ids=preferred_evidence_ids,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            retry_scope=retry_scope,
            response_deadline=deadline,
        )
        model_calls += step.model_cost.model_calls
        pack = _with_added_cost(pack, step.model_cost)
        if step.stop_reason == "cancelled" and time.monotonic() >= deadline:
            stop_reason = "budget_exhausted"
            break
        degradations.extend(step.degradations)
        if step.decision is None:
            stop_reason = step.stop_reason or "model_degraded"
            break

        coverage = step.decision.coverage
        action_limit = min(3, max(0, NAVIGATION_MAX_SEARCH_ACTIONS - search_actions))
        reference_actions = unresolved_reference_actions(
            pack.evidence,
            tuple(item for item in coverage if item.facet_id in objective.required_facet_ids),
            visited_action_ids=frozenset(visited_action_ids),
            maximum=action_limit,
        )
        if coverage_complete(objective, coverage):
            stop_reason = "covered"
            break
        actions = _unique_actions((*reference_actions, *step.decision.actions))[:action_limit]
        if step.decision.decision == "stop" and not actions:
            stop_reason = (
                "absent" if coverage_gate_state(objective, coverage) == "uncovered" else "partial"
            )
            break

        action_groups = _actions_by_facet(actions)
        reference_action_ids = frozenset(action.identity for action in reference_actions)
        if not any(action.terms or action.routes or action.evidence_ids for action in actions):
            stop_reason = _incomplete_stop_reason(objective, coverage)
            break
        for action in actions:
            visited_action_ids.add(action.identity)
            action_kinds.append(action.kind)
        search_actions += len(actions)

        if is_cancelled is not None and is_cancelled():
            stop_reason = "cancelled"
            degradations.append("knowledge_navigation_cancelled")
            break
        if current_navigation_snapshot_id(database_path) != pinned_snapshot_id:
            stop_reason = "snapshot_degraded"
            degradations.append("knowledge_navigation_snapshot_changed")
            break

        prior_ids = frozenset(item.evidence_id for item in pack.evidence)
        prior_routes = frozenset(pack.retrieval_trace.navigation_routes)
        prior_options = frozenset(item.route for item in pack.route_options)
        facet_evidence_ids: dict[str, tuple[str, ...]] = {}
        earlier_priority_evidence_ids = priority_evidence_ids
        round_priority_evidence_ids: tuple[str, ...] = ()
        round_stop_reason: str | None = None
        round_logical_read_limit = min(
            NAVIGATION_MAX_LOGICAL_READS,
            logical_reads + NAVIGATION_MAX_LOGICAL_READS_PER_ROUND,
        )
        for group_index, (facet_id, group) in enumerate(action_groups):
            if is_cancelled is not None and is_cancelled():
                degradations.append("knowledge_navigation_cancelled")
                round_stop_reason = "cancelled"
                break
            if current_navigation_snapshot_id(database_path) != pinned_snapshot_id:
                degradations.append("knowledge_navigation_snapshot_changed")
                round_stop_reason = "snapshot_degraded"
                break
            remaining_reads = max(0, round_logical_read_limit - logical_reads)
            groups_left = len(action_groups) - group_index
            group_read_budget = _group_read_budget(
                group,
                remaining_reads=remaining_reads,
                groups_left=groups_left,
                reference_action_ids=reference_action_ids,
            )
            if group_read_budget <= 0:
                continue
            knowledge_read_limit, source_read_limit = _group_read_limits(
                group,
                read_budget=group_read_budget,
                reference_action_ids=reference_action_ids,
            )
            group_prior_ids = frozenset(item.evidence_id for item in pack.evidence)
            new_terms = _new_action_terms(group, pack.retrieval_plan)
            supplement = retrieve_round(
                retrieval_plan=_expanded_plan(pack.retrieval_plan, new_terms),
                _navigation_max_reads=knowledge_read_limit,
                _navigation_max_source_windows=source_read_limit,
                _navigation_focus_terms=new_terms,
                _navigation_excluded_routes=frozenset(pack.retrieval_trace.navigation_routes),
                _navigation_prior_evidence=pack.evidence,
                _navigation_requested_routes=_unique(
                    route for action in group for route in action.routes
                ),
                _navigation_source_anchors=_unique(
                    evidence_id for action in group for evidence_id in action.evidence_ids
                ),
            )
            if current_navigation_snapshot_id(database_path) != pinned_snapshot_id:
                degradations.append("knowledge_navigation_snapshot_changed")
                round_stop_reason = "snapshot_degraded"
                break
            model_calls += supplement.retrieval_model_cost.model_calls
            logical_reads += _logical_reads(supplement.retrieval_trace)
            facet_evidence_ids[facet_id] = _new_action_evidence_ids(
                supplement.evidence,
                prior_ids=group_prior_ids,
            )
            if any(
                action.kind in {"read_routes", "read_source_sections"}
                or action.identity in reference_action_ids
                for action in group
            ):
                round_priority_evidence_ids = _unique(
                    (
                        *round_priority_evidence_ids,
                        *_targeted_source_sequence_evidence_ids(
                            supplement.evidence,
                            prior_ids=group_prior_ids,
                        ),
                    )
                )[: evidence_envelope.max_evidence_refs]
                priority_evidence_ids = _unique(
                    (*earlier_priority_evidence_ids, *round_priority_evidence_ids)
                )
            pack = _merge_packs(
                pack,
                supplement,
                coverage,
                facet_evidence_ids=facet_evidence_ids,
                priority_evidence_ids=priority_evidence_ids,
                evidence_envelope=evidence_envelope,
            )
            source_tokens = estimated_source_tokens(pack)
        if round_stop_reason is not None:
            stop_reason = round_stop_reason
            break
        coverage = _bind_observed_facet_evidence(coverage, facet_evidence_ids)
        preferred_evidence_ids = _unique(
            evidence_id
            for evidence_ids in facet_evidence_ids.values()
            for evidence_id in evidence_ids
        )
        observation_progress = (
            frozenset(item.evidence_id for item in pack.evidence) - prior_ids
            or frozenset(pack.retrieval_trace.navigation_routes) - prior_routes
            or frozenset(item.route for item in pack.route_options) - prior_options
        )
        if not observation_progress:
            stop_reason = _incomplete_stop_reason(objective, coverage)
            break

    return _finalize(
        pack,
        pinned_snapshot_id=pinned_snapshot_id,
        objective=objective,
        coverage=coverage,
        rounds=rounds,
        action_kinds=action_kinds,
        stop_reason=stop_reason,
        model_calls=model_calls,
        logical_reads=logical_reads,
        source_tokens=source_tokens,
        degradations=degradations,
    )


def _remaining_budget(
    *,
    rounds: int,
    model_calls: int,
    logical_reads: int,
    source_tokens: int,
    search_actions: int,
    source_token_limit: int = NAVIGATION_MAX_SOURCE_TOKENS,
) -> NavigationBudget:
    return NavigationBudget(
        rounds=NAVIGATION_MAX_ROUNDS - rounds + 1,
        model_calls=NAVIGATION_MAX_MODEL_CALLS - model_calls,
        logical_reads=NAVIGATION_MAX_LOGICAL_READS - logical_reads,
        source_tokens=source_token_limit - source_tokens,
        search_actions=NAVIGATION_MAX_SEARCH_ACTIONS - search_actions,
    )


def _budget_stop_reason(
    *,
    deadline: float,
    rounds: int,
    model_calls: int,
    logical_reads: int,
    source_tokens: int,
    search_actions: int,
    source_token_limit: int = NAVIGATION_MAX_SOURCE_TOKENS,
) -> str | None:
    if (
        rounds >= NAVIGATION_MAX_ROUNDS
        or model_calls >= NAVIGATION_MAX_MODEL_CALLS
        or logical_reads >= NAVIGATION_MAX_LOGICAL_READS
        or source_tokens >= source_token_limit
        or search_actions >= NAVIGATION_MAX_SEARCH_ACTIONS
        or time.monotonic() >= deadline
    ):
        return "budget_exhausted"
    return None


def _incomplete_stop_reason(
    objective: NavigationObjective,
    coverage: tuple[DesktopFacetCoverageTrace, ...],
) -> str:
    return "absent" if coverage_gate_state(objective, coverage) == "uncovered" else "partial"


def _expanded_plan(plan: DesktopRetrievalPlan, new_terms: tuple[str, ...]) -> DesktopRetrievalPlan:
    terms: list[str] = []
    seen: set[str] = set()
    # A feedback round is a scoped search for the missing facet. Put its terms first so
    # generic seed words cannot keep winning every bounded channel ranking.
    for term in (*new_terms, *plan.terms[:12]):
        normalized = " ".join(term.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            terms.append(normalized)
        if len(terms) == 24:
            break
    return DesktopRetrievalPlan(query=plan.query, terms=tuple(terms), source="adaptive")


def _actions_by_facet(
    actions: tuple[NavigationAction, ...],
) -> tuple[tuple[str, tuple[NavigationAction, ...]], ...]:
    grouped: dict[str, list[NavigationAction]] = {}
    for action in actions:
        grouped.setdefault(action.facet_id, []).append(action)
    return tuple((facet_id, tuple(values)) for facet_id, values in grouped.items())


def _bind_observed_facet_evidence(
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    evidence_ids_by_facet: dict[str, tuple[str, ...]],
) -> tuple[DesktopFacetCoverageTrace, ...]:
    """Retain last-round candidate support without claiming model-confirmed coverage."""
    updated: list[DesktopFacetCoverageTrace] = []
    for item in coverage:
        observed = _unique(evidence_ids_by_facet.get(item.facet_id, ()))[:16]
        if item.state in {"missing", "partial"} and observed:
            updated.append(
                DesktopFacetCoverageTrace(
                    item.facet_id,
                    "partial",
                    _unique((*item.evidence_ids, *observed))[:16],
                )
            )
        else:
            updated.append(item)
    return tuple(updated)


def _new_action_terms(
    actions: tuple[NavigationAction, ...], plan: DesktopRetrievalPlan
) -> tuple[str, ...]:
    known = {existing.casefold() for existing in plan.terms}
    return _unique(
        term for action in actions for term in action.terms if term.casefold() not in known
    )


def _merge_packs(
    current: DesktopEvidencePack,
    supplement: DesktopEvidencePack,
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    *,
    facet_evidence_ids: dict[str, tuple[str, ...]] | None = None,
    priority_evidence_ids: tuple[str, ...] = (),
    evidence_envelope: NavigationEvidenceEnvelope | None = None,
) -> DesktopEvidencePack:
    envelope = evidence_envelope or NavigationEvidenceEnvelope(
        NAVIGATION_MAX_EVIDENCE_REFS,
        NAVIGATION_MAX_SOURCE_TOKENS,
    )
    evidence = _allocate_evidence(
        current.evidence,
        supplement.evidence,
        coverage,
        facet_evidence_ids=facet_evidence_ids,
        priority_evidence_ids=priority_evidence_ids,
        max_evidence_refs=envelope.max_evidence_refs,
        max_source_tokens=envelope.max_source_tokens,
    )
    evidence_ids = frozenset(item.evidence_id for item in evidence)
    guidance = _merge_guidance(current.guidance, supplement.guidance, evidence_ids)
    images = _merge_images(current.source_images, supplement.source_images, evidence_ids)
    trace = _merge_trace(current.retrieval_trace, supplement.retrieval_trace, evidence)
    return DesktopEvidencePack(
        retrieval_plan=supplement.retrieval_plan,
        evidence=evidence,
        degradations=_unique((*current.degradations, *supplement.degradations)),
        source_images=images,
        retrieval_trace=trace,
        retrieval_model_cost=_sum_cost(
            current.retrieval_model_cost, supplement.retrieval_model_cost
        ),
        guidance=guidance,
        route_options=_merge_route_options(current, supplement),
    )


def _bounded_initial_pack(
    pack: DesktopEvidencePack,
    *,
    evidence_envelope: NavigationEvidenceEnvelope | None = None,
) -> tuple[DesktopEvidencePack, bool]:
    """Apply the session source envelope before seed coverage can finish the request."""
    envelope = evidence_envelope or NavigationEvidenceEnvelope(
        NAVIGATION_MAX_EVIDENCE_REFS,
        NAVIGATION_MAX_SOURCE_TOKENS,
    )
    if (
        len(pack.evidence) <= envelope.max_evidence_refs
        and estimated_source_tokens(pack) <= envelope.max_source_tokens
    ):
        return pack, False
    evidence = _allocate_evidence(
        (),
        pack.evidence,
        (),
        max_evidence_refs=envelope.max_evidence_refs,
        max_source_tokens=envelope.max_source_tokens,
    )
    source_budget_reduced = len(evidence) < len(pack.evidence)
    if not source_budget_reduced and tuple(item.evidence_id for item in evidence) == tuple(
        item.evidence_id for item in pack.evidence
    ):
        return pack, False
    evidence_ids = frozenset(item.evidence_id for item in evidence)
    return (
        replace(
            pack,
            evidence=evidence,
            source_images=tuple(
                item for item in pack.source_images if item.evidence_id in evidence_ids
            ),
            guidance=_merge_guidance((), pack.guidance, evidence_ids),
            retrieval_trace=pack.retrieval_trace.with_canonical_evidence_ids(
                tuple(item.evidence_id for item in evidence)
            ),
        ),
        source_budget_reduced,
    )


def _merge_guidance(
    first: tuple[DesktopKnowledgeGuidance, ...],
    second: tuple[DesktopKnowledgeGuidance, ...],
    evidence_ids: frozenset[str],
) -> tuple[DesktopKnowledgeGuidance, ...]:
    by_route: dict[str, DesktopKnowledgeGuidance] = {}
    for item in (*first, *second):
        valid_ids = tuple(
            evidence_id for evidence_id in item.source_evidence_ids if evidence_id in evidence_ids
        )
        if not valid_ids:
            continue
        existing = by_route.get(item.route)
        if existing is None:
            by_route[item.route] = replace(item, source_evidence_ids=valid_ids)
            continue
        lines = _unique(
            (*existing.content_markdown.splitlines(), *item.content_markdown.splitlines())
        )
        by_route[item.route] = replace(
            existing,
            content_markdown="\n".join(lines),
            source_evidence_ids=_unique((*existing.source_evidence_ids, *valid_ids)),
        )
    return tuple(by_route.values())


def _merge_images(
    first: tuple[DesktopAnswerSourceImage, ...],
    second: tuple[DesktopAnswerSourceImage, ...],
    evidence_ids: frozenset[str],
) -> tuple[DesktopAnswerSourceImage, ...]:
    images: dict[str, DesktopAnswerSourceImage] = {}
    for image in (*first, *second):
        if image.evidence_id in evidence_ids:
            images.setdefault(image.source_image_id, image)
    return tuple(images.values())


def _merge_route_options(
    first: DesktopEvidencePack,
    second: DesktopEvidencePack,
) -> tuple[DesktopKnowledgeRouteOption, ...]:
    # The latest read can reveal routes from an index. Keep those discoveries ahead of
    # older advertisements so the next bounded model prompt can actually observe them.
    by_route: dict[str, DesktopKnowledgeRouteOption] = {}
    for item in (*second.route_options, *first.route_options):
        by_route.setdefault(item.route, item)
    return tuple(by_route.values())


def _merge_trace(
    first: DesktopRetrievalTrace,
    second: DesktopRetrievalTrace,
    evidence: tuple[DesktopEvidenceRef, ...],
) -> DesktopRetrievalTrace:
    channel_names = _unique(
        (*(item.channel for item in first.channels), *(item.channel for item in second.channels))
    )
    channels = tuple(
        DesktopRetrievalChannelTrace(
            channel=name,
            candidate_count=sum(
                item.candidate_count
                for item in (*first.channels, *second.channels)
                if item.channel == name
            ),
            trigger_reasons=_unique(
                reason
                for item in (*first.channels, *second.channels)
                if item.channel == name
                for reason in item.trigger_reasons
            ),
            degradation_reasons=_unique(
                reason
                for item in (*first.channels, *second.channels)
                if item.channel == name
                for reason in item.degradation_reasons
            ),
        )
        for name in channel_names
    )
    return replace(
        first,
        catalog_generation_ids=_unique(
            (*first.catalog_generation_ids, *second.catalog_generation_ids)
        ),
        page_tree_generation_ids=_unique(
            (*first.page_tree_generation_ids, *second.page_tree_generation_ids)
        ),
        channels=channels,
        trigger_reasons=_unique((*first.trigger_reasons, *second.trigger_reasons)),
        degradation_reasons=_unique((*first.degradation_reasons, *second.degradation_reasons)),
        selected_node_ids=_unique((*first.selected_node_ids, *second.selected_node_ids)),
        canonical_evidence_ids=tuple(item.evidence_id for item in evidence),
        fusion_policy_version=second.fusion_policy_version or first.fusion_policy_version,
        navigation_snapshot_ids=_unique(
            (*first.navigation_snapshot_ids, *second.navigation_snapshot_ids)
        ),
        navigation_routes=_unique((*first.navigation_routes, *second.navigation_routes)),
        navigation_read_count=first.navigation_read_count + second.navigation_read_count,
        source_window_count=first.source_window_count + second.source_window_count,
        link_hop_count=max(first.link_hop_count, second.link_hop_count),
        page_tree_supplement_count=(
            first.page_tree_supplement_count + second.page_tree_supplement_count
        ),
        coverage_gate_state=second.coverage_gate_state,
        grounding_input_budget_tokens=max(
            first.grounding_input_budget_tokens, second.grounding_input_budget_tokens
        ),
        evidence_input_tokens=max(first.evidence_input_tokens, second.evidence_input_tokens),
        guidance_input_tokens=max(first.guidance_input_tokens, second.guidance_input_tokens),
    )


def _finalize(
    pack: DesktopEvidencePack,
    *,
    pinned_snapshot_id: str,
    objective: NavigationObjective,
    coverage: tuple[DesktopFacetCoverageTrace, ...],
    rounds: int,
    action_kinds: list[str],
    stop_reason: str,
    model_calls: int,
    logical_reads: int,
    source_tokens: int,
    degradations: list[str],
) -> DesktopEvidencePack:
    evidence = _order_evidence_by_coverage(pack.evidence, coverage)
    evidence_ids = frozenset(item.evidence_id for item in evidence)
    trace_degradations = _unique((*pack.retrieval_trace.degradation_reasons, *degradations))
    trace = replace(
        pack.retrieval_trace,
        degradation_reasons=trace_degradations,
        canonical_evidence_ids=tuple(item.evidence_id for item in evidence),
        navigation_snapshot_ids=_unique(
            (pinned_snapshot_id, *pack.retrieval_trace.navigation_snapshot_ids)
        ),
        coverage_gate_state=coverage_gate_state(objective, coverage),
        navigation_round_count=rounds,
        navigation_action_kinds=_unique(action_kinds),
        navigation_stop_reason=stop_reason,
        facet_coverage=coverage,
        navigation_model_calls=model_calls,
        navigation_logical_read_count=logical_reads,
        navigation_source_tokens=source_tokens,
    )
    return replace(
        pack,
        evidence=evidence,
        degradations=_unique((*pack.degradations, *degradations)),
        source_images=tuple(
            item for item in pack.source_images if item.evidence_id in evidence_ids
        ),
        guidance=_merge_guidance((), pack.guidance, evidence_ids),
        retrieval_trace=trace,
    )


def _unique(values) -> tuple:
    return tuple(dict.fromkeys(values))
