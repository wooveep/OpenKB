"""Private bounded feedback loop behind DesktopEvidenceRetriever.retrieve."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from openkb.desktop_adaptive_navigation import (
    NAVIGATION_MAX_BATCH_KNOWLEDGE_READS,
    NAVIGATION_MAX_BATCH_SOURCE_READS,
    NAVIGATION_MAX_LOGICAL_READS,
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
    deterministic_seed_coverage,
    estimated_source_tokens,
    initial_navigation_objective,
    navigation_requires_model,
    run_navigation_step,
)
from openkb.desktop_answer_types import (
    DesktopAnswerSourceImage,
    DesktopEvidencePack,
    DesktopEvidenceRef,
    DesktopKnowledgeGuidance,
    DesktopRetrievalModelCost,
    DesktopRetrievalPlan,
)
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_retrieval_trace import (
    DesktopAnswerCoverageTrace,
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
        _navigation_excluded_routes: frozenset[str],
        _navigation_prior_evidence: tuple[DesktopEvidenceRef, ...],
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
) -> DesktopEvidencePack:
    """Observe, replan, retrieve and stop within one immutable session envelope."""
    started_at = time.monotonic()
    objective = initial_navigation_objective(question, initial_pack.retrieval_plan)
    coverage = deterministic_seed_coverage(objective, initial_pack)
    pack = initial_pack
    rounds = 0
    model_calls = 0
    logical_reads = _logical_reads(pack.retrieval_trace)
    source_tokens = estimated_source_tokens(pack)
    search_actions = 0
    visited_action_ids: set[str] = set()
    action_kinds: list[str] = []
    degradations: list[str] = []

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
    if not navigation_requires_model(objective, pack):
        return _finalize(
            pack,
            pinned_snapshot_id=pinned_snapshot_id,
            objective=objective,
            coverage=coverage,
            rounds=rounds,
            action_kinds=action_kinds,
            stop_reason="covered",
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            degradations=degradations,
        )

    stop_reason = "partial"
    while True:
        budget_stop = _budget_stop_reason(
            started_at=started_at,
            rounds=rounds,
            model_calls=model_calls,
            logical_reads=logical_reads,
            source_tokens=source_tokens,
            search_actions=search_actions,
        )
        if budget_stop is not None:
            stop_reason = budget_stop
            degradations.append("knowledge_navigation_budget_exhausted")
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
            ),
            round_number=rounds,
            is_cancelled=is_cancelled,
            on_model_event=on_model_event,
            retry_scope=retry_scope,
        )
        model_calls += step.model_cost.model_calls
        pack = _with_added_cost(pack, step.model_cost)
        degradations.extend(step.degradations)
        if step.decision is None:
            stop_reason = step.stop_reason or "model_degraded"
            break

        objective = step.decision.objective
        coverage = step.decision.coverage
        if coverage_complete(coverage):
            stop_reason = "covered"
            break
        if step.decision.decision == "stop":
            stop_reason = "absent" if not pack.evidence else "partial"
            break

        actions = step.decision.actions
        new_terms = tuple(
            term
            for action in actions
            for term in action.terms
            if term.casefold()
            not in {existing.casefold() for existing in pack.retrieval_plan.terms}
        )
        if not new_terms:
            stop_reason = "partial"
            degradations.append("knowledge_navigation_no_progress")
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

        expanded_plan = _expanded_plan(pack.retrieval_plan, new_terms)
        remaining_reads = max(0, NAVIGATION_MAX_LOGICAL_READS - logical_reads)
        source_read_limit = min(
            NAVIGATION_MAX_BATCH_SOURCE_READS,
            remaining_reads // 3,
        )
        knowledge_read_limit = min(
            NAVIGATION_MAX_BATCH_KNOWLEDGE_READS,
            remaining_reads - source_read_limit,
        )
        supplement = retrieve_round(
            retrieval_plan=expanded_plan,
            _navigation_max_reads=knowledge_read_limit,
            _navigation_max_source_windows=source_read_limit,
            _navigation_excluded_routes=frozenset(pack.retrieval_trace.navigation_routes),
            _navigation_prior_evidence=pack.evidence,
        )
        if current_navigation_snapshot_id(database_path) != pinned_snapshot_id:
            stop_reason = "snapshot_degraded"
            degradations.append("knowledge_navigation_snapshot_changed")
            break
        prior_ids = frozenset(item.evidence_id for item in pack.evidence)
        pack = _merge_packs(pack, supplement, coverage)
        logical_reads += _logical_reads(supplement.retrieval_trace)
        source_tokens = estimated_source_tokens(pack)
        if not frozenset(item.evidence_id for item in pack.evidence) - prior_ids:
            stop_reason = "partial"
            degradations.append("knowledge_navigation_no_progress")
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
) -> NavigationBudget:
    return NavigationBudget(
        rounds=NAVIGATION_MAX_ROUNDS - rounds + 1,
        model_calls=NAVIGATION_MAX_MODEL_CALLS - model_calls,
        logical_reads=NAVIGATION_MAX_LOGICAL_READS - logical_reads,
        source_tokens=NAVIGATION_MAX_SOURCE_TOKENS - source_tokens,
        search_actions=NAVIGATION_MAX_SEARCH_ACTIONS - search_actions,
    )


def _budget_stop_reason(
    *,
    started_at: float,
    rounds: int,
    model_calls: int,
    logical_reads: int,
    source_tokens: int,
    search_actions: int,
) -> str | None:
    if (
        rounds >= NAVIGATION_MAX_ROUNDS
        or model_calls >= NAVIGATION_MAX_MODEL_CALLS
        or logical_reads >= NAVIGATION_MAX_LOGICAL_READS
        or source_tokens >= NAVIGATION_MAX_SOURCE_TOKENS
        or search_actions >= NAVIGATION_MAX_SEARCH_ACTIONS
        or time.monotonic() - started_at >= NAVIGATION_MAX_WALL_SECONDS
    ):
        return "budget_exhausted"
    return None


def _expanded_plan(plan: DesktopRetrievalPlan, new_terms: tuple[str, ...]) -> DesktopRetrievalPlan:
    terms: list[str] = []
    seen: set[str] = set()
    # A feedback round is a scoped search for the missing aspect. Put its terms first so
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


def _merge_packs(
    current: DesktopEvidencePack,
    supplement: DesktopEvidencePack,
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
) -> DesktopEvidencePack:
    evidence = _allocate_evidence(current.evidence, supplement.evidence, coverage)
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
    )


def _allocate_evidence(
    current: tuple[DesktopEvidenceRef, ...],
    supplement: tuple[DesktopEvidenceRef, ...],
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
) -> tuple[DesktopEvidenceRef, ...]:
    by_id: dict[str, DesktopEvidenceRef] = {}
    for reference in (*current, *supplement):
        existing = by_id.get(reference.evidence_id)
        if existing is None:
            by_id[reference.evidence_id] = reference
        else:
            by_id[reference.evidence_id] = _merge_reference(existing, reference)
    ordered_ids = _unique(
        (
            *(evidence_id for item in coverage for evidence_id in item.evidence_ids),
            *(item.evidence_id for item in supplement),
            *(item.evidence_id for item in current),
        )
    )
    selected: list[DesktopEvidenceRef] = []
    used_tokens = 0
    for evidence_id in ordered_ids:
        reference = by_id[evidence_id]
        tokens = max(1, (len(reference.excerpt) + 3) // 4)
        if selected and used_tokens + tokens > NAVIGATION_MAX_SOURCE_TOKENS:
            continue
        selected.append(reference)
        used_tokens += tokens
        if len(selected) == 32:
            break
    return tuple(selected)


def _merge_reference(first: DesktopEvidenceRef, second: DesktopEvidenceRef) -> DesktopEvidenceRef:
    preferred = second if len(second.excerpt) > len(first.excerpt) else first
    return replace(preferred, channels=_unique((*first.channels, *second.channels)))


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
    return DesktopRetrievalTrace(
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
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
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
        coverage_gate_state=coverage_gate_state(coverage),
        navigation_answer_kind=objective.answer_kind,
        navigation_subject=objective.subject,
        navigation_round_count=rounds,
        navigation_action_kinds=_unique(action_kinds),
        navigation_stop_reason=stop_reason,
        coverage_aspects=coverage,
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


def _order_evidence_by_coverage(
    evidence: tuple[DesktopEvidenceRef, ...],
    coverage: tuple[DesktopAnswerCoverageTrace, ...],
) -> tuple[DesktopEvidenceRef, ...]:
    by_id = {item.evidence_id: item for item in evidence}
    ordered = _unique(
        (
            *(evidence_id for item in coverage for evidence_id in item.evidence_ids),
            *(item.evidence_id for item in evidence),
        )
    )
    return tuple(by_id[evidence_id] for evidence_id in ordered if evidence_id in by_id)


def _with_added_cost(
    pack: DesktopEvidencePack, cost: DesktopRetrievalModelCost
) -> DesktopEvidencePack:
    return replace(pack, retrieval_model_cost=_sum_cost(pack.retrieval_model_cost, cost))


def _sum_cost(
    first: DesktopRetrievalModelCost, second: DesktopRetrievalModelCost
) -> DesktopRetrievalModelCost:
    return DesktopRetrievalModelCost(
        model_calls=first.model_calls + second.model_calls,
        input_characters=first.input_characters + second.input_characters,
        output_characters=first.output_characters + second.output_characters,
    )


def _logical_reads(trace: DesktopRetrievalTrace) -> int:
    return (
        trace.navigation_read_count + trace.source_window_count + trace.page_tree_supplement_count
    )


def _unique(values) -> tuple:
    return tuple(dict.fromkeys(values))
