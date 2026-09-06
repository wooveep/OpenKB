"""Deterministic rendering for validated, generation-owned Knowledge Page Plans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from openkb.desktop_canonical_json import canonical_json_digest
from openkb.desktop_knowledge_page_planning import (
    KnowledgePagePlan,
    KnowledgePagePlanValidationError,
    KnowledgePageSection,
    KnowledgePageUnit,
)
from openkb.desktop_knowledge_sources import stable_source_id


@dataclass(frozen=True)
class KnowledgePageClaimSnapshot:
    """One immutable claim the page planner is allowed to place."""

    generation_id: int
    identity_id: str
    candidate_generation_id: str
    candidate_id: str
    claim_ordinal: int
    claim_id: str
    text: str
    applicability: tuple[tuple[str, str], ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgePageRelationSnapshot:
    """One immutable directed relation the page planner may optionally place."""

    generation_id: int
    assertion_id: str
    source_identity_id: str
    source_title: str
    target_identity_id: str
    target_title: str
    label: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RenderedKnowledgePage:
    markdown: str
    content_digest: str
    factual_unit_count: int
    evidence_ids: tuple[str, ...]


def knowledge_page_claim_id(
    generation_id: int,
    identity_id: str,
    text: str,
    applicability: tuple[tuple[str, str], ...],
) -> str:
    """Derive a generation-local reference after exact claim consolidation."""
    payload = {
        "generation_id": generation_id,
        "identity_id": identity_id,
        "text": " ".join(text.split()).casefold(),
        "applicability": [list(item) for item in applicability],
    }
    return "claim-" + canonical_json_digest(payload)


def knowledge_page_claim_snapshot_digest(
    claims: tuple[KnowledgePageClaimSnapshot, ...],
) -> str:
    """Bind a plan to the complete ordered claim inputs without retaining extra prose."""
    payload = [
        {
            "generation_id": claim.generation_id,
            "identity_id": claim.identity_id,
            "candidate_generation_id": claim.candidate_generation_id,
            "candidate_id": claim.candidate_id,
            "claim_ordinal": claim.claim_ordinal,
            "claim_id": claim.claim_id,
            "text": claim.text,
            "applicability": [list(item) for item in claim.applicability],
            "evidence_ids": list(claim.evidence_ids),
        }
        for claim in claims
    ]
    return canonical_json_digest(payload)


def render_knowledge_page(
    plan: KnowledgePagePlan,
    claims: tuple[KnowledgePageClaimSnapshot, ...],
    *,
    relations: tuple[KnowledgePageRelationSnapshot, ...],
) -> RenderedKnowledgePage:
    """Render only supplied factual snapshots in the model-selected safe structure."""
    _validate_render_inputs(plan, claims, relations)
    claims_by_id = {claim.claim_id: claim for claim in claims}
    relations_by_id = {relation.assertion_id: relation for relation in relations}
    evidence_ids: list[str] = []
    output: list[str] = []
    factual_unit_count = 0

    def render_unit(unit: KnowledgePageUnit) -> list[str]:
        nonlocal factual_unit_count
        facts = [_render_claim(claims_by_id[claim_id], evidence_ids) for claim_id in unit.claim_ids]
        facts.extend(
            _render_relation(relations_by_id[relation_id], evidence_ids)
            for relation_id in unit.relation_assertion_ids
        )
        factual_unit_count += len(facts)
        if unit.presentation == "unordered_list":
            return [f"- {fact}" for fact in facts]
        if unit.presentation == "ordered_list":
            return [f"1. {fact}" for fact in facts]
        paragraphs: list[str] = []
        for fact in facts:
            if paragraphs:
                paragraphs.append("")
            paragraphs.append(fact)
        return paragraphs

    if plan.lead is not None:
        output.extend(render_unit(plan.lead))
    for section in plan.sections:
        _render_section(section, depth=2, output=output, render_unit=render_unit)
    markdown = "\n".join(output).strip()
    return RenderedKnowledgePage(
        markdown=markdown,
        content_digest=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        factual_unit_count=factual_unit_count,
        evidence_ids=tuple(evidence_ids),
    )


def _validate_render_inputs(
    plan: KnowledgePagePlan,
    claims: tuple[KnowledgePageClaimSnapshot, ...],
    relations: tuple[KnowledgePageRelationSnapshot, ...],
) -> None:
    issues: list[str] = []
    if any(claim.generation_id != plan.generation_id for claim in claims):
        issues.append("claim_generation_mismatch")
    if any(claim.identity_id != plan.identity_id for claim in claims):
        issues.append("claim_identity_mismatch")
    if any(not claim.evidence_ids for claim in claims):
        issues.append("claim_without_evidence")
    if knowledge_page_claim_snapshot_digest(claims) != plan.claim_snapshot_digest:
        issues.append("claim_snapshot_digest_mismatch")
    if set(plan.placed_claim_ids) != {claim.claim_id for claim in claims}:
        issues.append("claim_snapshot_placement_mismatch")
    relation_ids = _placed_relation_ids(plan)
    relations_by_id = {relation.assertion_id: relation for relation in relations}
    if len(relations_by_id) != len(relations):
        issues.append("duplicate_relation_snapshot")
    for relation_id in relation_ids:
        relation = relations_by_id.get(relation_id)
        if relation is None:
            issues.append(f"unknown_relation:{relation_id}")
        elif relation.generation_id != plan.generation_id:
            issues.append(f"relation_generation_mismatch:{relation_id}")
        elif plan.identity_id not in {
            relation.source_identity_id,
            relation.target_identity_id,
        }:
            issues.append(f"relation_identity_mismatch:{relation_id}")
        elif not relation.evidence_ids:
            issues.append(f"relation_without_evidence:{relation_id}")
    if issues:
        raise KnowledgePagePlanValidationError(tuple(issues))


def _render_section(
    section: KnowledgePageSection,
    *,
    depth: int,
    output: list[str],
    render_unit,
) -> None:
    if output:
        output.append("")
    output.append(f"{'#' * depth} {_escape_markdown(section.title)}")
    for unit in section.units:
        rendered = render_unit(unit)
        if rendered:
            output.append("")
            output.extend(rendered)
    for child in section.sections:
        _render_section(child, depth=depth + 1, output=output, render_unit=render_unit)


def _render_claim(claim: KnowledgePageClaimSnapshot, evidence_ids: list[str]) -> str:
    text = _escape_markdown(" ".join(claim.text.split()))
    if claim.applicability:
        scope = "; ".join(
            f"{_escape_markdown(dimension)}: {_escape_markdown(value)}"
            for dimension, value in claim.applicability
        )
        text = f"{text} ({scope})"
    return text + _source_markers(claim.evidence_ids, evidence_ids)


def _render_relation(
    relation: KnowledgePageRelationSnapshot,
    evidence_ids: list[str],
) -> str:
    statement = (
        f"{_escape_markdown(relation.source_title)} — "
        f"{_escape_markdown(relation.label)} → "
        f"{_escape_markdown(relation.target_title)}"
    )
    return statement + _source_markers(relation.evidence_ids, evidence_ids)


def _source_markers(source_ids: tuple[str, ...], evidence_ids: list[str]) -> str:
    for evidence_id in source_ids:
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    return "".join(f"[^{stable_source_id(evidence_id)}]" for evidence_id in source_ids)


def _placed_relation_ids(plan: KnowledgePagePlan) -> tuple[str, ...]:
    result: list[str] = []
    if plan.lead is not None:
        result.extend(plan.lead.relation_assertion_ids)
    for section in plan.sections:
        _append_section_relation_ids(section, result)
    return tuple(result)


def _append_section_relation_ids(
    section: KnowledgePageSection,
    target: list[str],
) -> None:
    for unit in section.units:
        target.extend(unit.relation_assertion_ids)
    for child in section.sections:
        _append_section_relation_ids(child, target)


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for marker in ("*", "_", "[", "]", "<", ">", "|"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped
