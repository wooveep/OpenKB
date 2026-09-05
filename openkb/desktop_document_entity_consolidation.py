"""Deterministically consolidate Inventory proposals resolved to one Entity identity."""

from __future__ import annotations

from dataclasses import replace

from openkb.desktop_knowledge_analysis import (
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
    KnowledgeInventoryDecision,
)
from openkb.desktop_knowledge_titles import normalize_knowledge_title

_ACCEPTED_DECISIONS = frozenset({"create", "update", "alias"})


def consolidate_inventory_entities(
    entities: tuple[KnowledgeAnalysisCandidate, ...],
) -> tuple[KnowledgeAnalysisCandidate, ...]:
    """Return one checkpoint-safe candidate for each normalized Inventory title."""
    consolidated: list[KnowledgeAnalysisCandidate] = []
    indexes: dict[str, int] = {}
    for entity in entities:
        identity = normalize_knowledge_title(entity.title)[1]
        index = indexes.get(identity)
        if index is None:
            indexes[identity] = len(consolidated)
            consolidated.append(entity)
            continue
        consolidated[index] = _merge_same_title(consolidated[index], entity)
    return tuple(consolidated)


def _merge_same_title(
    first: KnowledgeAnalysisCandidate,
    second: KnowledgeAnalysisCandidate,
) -> KnowledgeAnalysisCandidate:
    target_pairs = {
        (candidate.inventory_target_identity_id, candidate.inventory_target_generation_id)
        for candidate in (first, second)
    }
    subtypes = {candidate.subtype for candidate in (first, second)}
    decisions = {candidate.inventory_decision for candidate in (first, second)}
    compatible = len(target_pairs) == 1 and len(subtypes) == 1 and decisions <= _ACCEPTED_DECISIONS
    decision: KnowledgeInventoryDecision
    if compatible:
        target_identity_id, target_generation_id = next(iter(target_pairs))
        decision = (
            "update"
            if "update" in decisions
            else "alias"
            if target_identity_id is not None
            else "create"
        )
        reasons = _unique((*first.admission_reason_codes, *second.admission_reason_codes))
    else:
        target_identity_id = None
        target_generation_id = None
        decision = "review"
        reasons = ("ambiguous_identity",)
    return replace(
        first,
        aliases=_aliases(first, second),
        tags=_unique((*first.tags, *second.tags)),
        claims=_claims(first.claims, second.claims),
        admission_reason_codes=reasons,
        inventory_decision=decision,
        inventory_target_identity_id=target_identity_id,
        inventory_target_generation_id=target_generation_id,
    )


def _aliases(
    first: KnowledgeAnalysisCandidate,
    second: KnowledgeAnalysisCandidate,
) -> tuple[str, ...]:
    title_key = normalize_knowledge_title(first.title)[1]
    values = (*first.aliases, second.title, *second.aliases)
    aliases: list[str] = []
    identities: set[str] = set()
    for value in values:
        identity = normalize_knowledge_title(value)[1]
        if identity == title_key or identity in identities:
            continue
        identities.add(identity)
        aliases.append(value)
    return tuple(aliases)


def _claims(
    first: tuple[KnowledgeAnalysisClaim, ...],
    second: tuple[KnowledgeAnalysisClaim, ...],
) -> tuple[KnowledgeAnalysisClaim, ...]:
    claims: list[KnowledgeAnalysisClaim] = []
    indexes: dict[tuple[str, str, object], int] = {}
    for claim in (*first, *second):
        key = (" ".join(claim.text.split()).casefold(), claim.role, claim.applicability)
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(claims)
            claims.append(claim)
            continue
        existing = claims[index]
        claims[index] = replace(
            existing,
            source_evidence_ids=_unique(
                (*existing.source_evidence_ids, *claim.source_evidence_ids)
            ),
        )
    return tuple(claims)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
