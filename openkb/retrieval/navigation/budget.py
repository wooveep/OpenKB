"""Pure budget allocation for one bounded adaptive Navigation action group."""

from __future__ import annotations

from dataclasses import dataclass

from openkb.answers.budget import answer_budget_for_gateway
from openkb.retrieval.navigation.adaptive import (
    NAVIGATION_MAX_BATCH_KNOWLEDGE_READS,
    NAVIGATION_MAX_BATCH_SOURCE_READS,
)
from openkb.retrieval.navigation.validation import NavigationAction


@dataclass(frozen=True)
class NavigationEvidenceEnvelope:
    """Model-aware source envelope; navigation remains smaller than grounding."""

    max_evidence_refs: int
    max_source_tokens: int


def navigation_evidence_envelope(model_gateway: object | None) -> NavigationEvidenceEnvelope:
    """Project the verified Answer budget into Navigation's existing interface."""
    budget = answer_budget_for_gateway(model_gateway)
    return NavigationEvidenceEnvelope(
        budget.navigation_max_evidence_refs,
        budget.navigation_max_source_tokens,
    )


def group_read_limits(
    actions: tuple[NavigationAction, ...],
    *,
    read_budget: int,
    reference_action_ids: frozenset[str],
) -> tuple[int, int]:
    """Split a group budget while preserving a source read for explicit targets."""
    requires_knowledge_read = any(action.kind != "read_source_sections" for action in actions)
    source_targeted = any(
        action.kind in {"read_routes", "read_source_sections"}
        or action.identity in reference_action_ids
        for action in actions
    )
    source_floor = int(source_targeted and read_budget > int(requires_knowledge_read))
    source_read_limit = min(
        NAVIGATION_MAX_BATCH_SOURCE_READS,
        max(source_floor, read_budget // 3),
    )
    knowledge_read_limit = min(
        NAVIGATION_MAX_BATCH_KNOWLEDGE_READS,
        read_budget - source_read_limit,
    )
    return knowledge_read_limit, source_read_limit


def group_read_budget(
    actions: tuple[NavigationAction, ...],
    *,
    remaining_reads: int,
    groups_left: int,
    reference_action_ids: frozenset[str],
) -> int:
    """Give a named-reference follow-up enough room for routing plus Original Evidence."""
    fair_share = (remaining_reads + groups_left - 1) // groups_left if groups_left > 0 else 0
    if any(action.identity in reference_action_ids for action in actions):
        return max(fair_share, min(3, remaining_reads))
    return fair_share
