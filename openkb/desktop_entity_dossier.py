"""Generation-bound Entity Dossier plans and deterministic factual rendering."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Literal, cast

from openkb.desktop_entity_dossier_contract import (
    DOSSIER_PRESENTATIONS,
    DOSSIER_PURPOSES,
)
from openkb.desktop_knowledge_sources import stable_source_id

DossierPurpose = Literal[
    "identity_and_role",
    "composition",
    "capabilities",
    "applicability",
    "requirements",
    "operations",
    "limitations",
    "troubleshooting",
    "version_evolution",
    "related_identities",
    "details",
]
DossierPresentation = Literal["paragraph", "list", "table"]

ENTITY_DOSSIER_CONTRACT_VERSION = "openkb.entity-dossier.v1"

_ROLE_PURPOSE = {
    "definition": "identity_and_role",
    "purpose": "identity_and_role",
    "mechanism": "capabilities",
    "capability": "capabilities",
    "scope": "applicability",
    "prerequisite": "requirements",
    "step": "operations",
    "validation": "operations",
    "rollback": "operations",
    "troubleshooting": "troubleshooting",
    "limitation": "limitations",
    "relation": "related_identities",
    "detail": "details",
}
_SECTION_TITLES = {
    "en": {
        "identity_and_role": "Identity and role",
        "composition": "Composition",
        "capabilities": "Capabilities",
        "applicability": "Deployment and applicability",
        "requirements": "Requirements",
        "operations": "Operations",
        "limitations": "Limitations",
        "troubleshooting": "Troubleshooting",
        "version_evolution": "Version evolution",
        "related_identities": "Related identities",
        "details": "Details",
    },
    "zh": {
        "identity_and_role": "身份与定位",
        "composition": "组成",
        "capabilities": "能力与机制",
        "applicability": "部署与适用范围",
        "requirements": "要求与前置条件",
        "operations": "操作与验证",
        "limitations": "限制",
        "troubleshooting": "故障排查",
        "version_evolution": "版本演进",
        "related_identities": "相关身份",
        "details": "其他事实",
    },
}
_LIST_PURPOSES = frozenset(("requirements", "operations", "limitations", "troubleshooting"))


@dataclass(frozen=True)
class DossierClaimSnapshot:
    """One immutable, evidence-bound claim available to a Dossier plan."""

    generation_id: int
    candidate_generation_id: str
    candidate_id: str
    claim_ordinal: int
    claim_id: str
    identity_id: str
    text: str
    role: str
    applicability: tuple[tuple[str, str], ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EntityDossierUnit:
    presentation: DossierPresentation
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class EntityDossierSection:
    title: str
    purpose: DossierPurpose
    units: tuple[EntityDossierUnit, ...]


@dataclass(frozen=True)
class EntityDossierPlan:
    generation_id: int
    identity_id: str
    summary_claim_ids: tuple[str, ...]
    sections: tuple[EntityDossierSection, ...]
    related_identity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedEntityDossier:
    """A generated page plus stable facts/source accounting for qualification."""

    markdown: str
    content_digest: str
    fact_count: int
    evidence_ids: tuple[str, ...]


def candidate_claim_id(
    candidate_generation_id: str,
    candidate_id: str,
    claim_ordinal: int,
) -> str:
    """Return the stable claim reference used by generation-owned plans."""
    value = f"{candidate_generation_id}\x1f{candidate_id}\x1f{claim_ordinal}"
    return "claim-" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def entity_dossier_plan_payload(plan: EntityDossierPlan) -> dict[str, object]:
    """Return the canonical, fact-free plan representation used for persistence."""
    return {
        "contract_version": ENTITY_DOSSIER_CONTRACT_VERSION,
        "generation_id": plan.generation_id,
        "identity_id": plan.identity_id,
        "summary_claim_ids": list(plan.summary_claim_ids),
        "sections": [
            {
                "title": section.title,
                "purpose": section.purpose,
                "units": [
                    {
                        "presentation": unit.presentation,
                        "claim_ids": list(unit.claim_ids),
                    }
                    for unit in section.units
                ],
            }
            for section in plan.sections
        ],
        "related_identity_ids": list(plan.related_identity_ids),
    }


def entity_dossier_plan_digest(plan: EntityDossierPlan) -> str:
    payload = json.dumps(
        entity_dossier_plan_payload(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DossierPlanValidationError(ValueError):
    """A plan cannot be projected without crossing an authority boundary."""

    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__(", ".join(issues))


def parse_entity_dossier_plan(
    content: str,
    *,
    expected_generation_id: int,
    expected_identity_id: str,
    claims: tuple[DossierClaimSnapshot, ...],
    known_related_identity_ids: frozenset[str] = frozenset(),
) -> EntityDossierPlan:
    """Parse the ID-only provider response and enforce its complete closed shape."""
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError) as error:
        raise DossierPlanValidationError(("invalid_json",)) from error
    if not isinstance(value, dict):
        raise DossierPlanValidationError(("invalid_top_level",))
    _require_fields(
        value,
        frozenset(
            (
                "generation_id",
                "identity_id",
                "summary_claim_ids",
                "sections",
                "related_identity_ids",
            )
        ),
        "plan",
    )
    generation_id = value.get("generation_id")
    identity_id = value.get("identity_id")
    if generation_id != expected_generation_id:
        raise DossierPlanValidationError(("generation_mismatch",))
    if identity_id != expected_identity_id:
        raise DossierPlanValidationError(("identity_mismatch",))
    summary_claim_ids = _identifier_list(value.get("summary_claim_ids"), "summary_claim_ids")
    related_identity_ids = _identifier_list(
        value.get("related_identity_ids"), "related_identity_ids"
    )
    unknown_related = tuple(
        identity for identity in related_identity_ids if identity not in known_related_identity_ids
    )
    if unknown_related:
        raise DossierPlanValidationError(
            tuple(f"unknown_related_identity:{identity}" for identity in unknown_related)
        )
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list) or len(raw_sections) > 32:
        raise DossierPlanValidationError(("invalid_sections",))
    sections: list[EntityDossierSection] = []
    for section_index, raw_section in enumerate(raw_sections):
        if not isinstance(raw_section, dict):
            raise DossierPlanValidationError((f"invalid_section:{section_index}",))
        _require_fields(
            raw_section,
            frozenset(("title", "purpose", "units")),
            f"sections[{section_index}]",
        )
        title = raw_section.get("title")
        purpose = raw_section.get("purpose")
        raw_units = raw_section.get("units")
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise DossierPlanValidationError((f"invalid_title:{section_index}",))
        if not isinstance(purpose, str) or purpose not in DOSSIER_PURPOSES:
            raise DossierPlanValidationError((f"invalid_purpose:{section_index}",))
        if not isinstance(raw_units, list) or not raw_units or len(raw_units) > 64:
            raise DossierPlanValidationError((f"empty_section:{section_index}",))
        units: list[EntityDossierUnit] = []
        for unit_index, raw_unit in enumerate(raw_units):
            if not isinstance(raw_unit, dict):
                raise DossierPlanValidationError((f"invalid_unit:{section_index}:{unit_index}",))
            _require_fields(
                raw_unit,
                frozenset(("presentation", "claim_ids")),
                f"sections[{section_index}].units[{unit_index}]",
            )
            presentation = raw_unit.get("presentation")
            if not isinstance(presentation, str) or presentation not in DOSSIER_PRESENTATIONS:
                raise DossierPlanValidationError(
                    (f"invalid_presentation:{section_index}:{unit_index}",)
                )
            claim_ids = _identifier_list(
                raw_unit.get("claim_ids"),
                f"sections[{section_index}].units[{unit_index}].claim_ids",
            )
            if not claim_ids:
                raise DossierPlanValidationError((f"empty_unit:{section_index}:{unit_index}",))
            units.append(
                EntityDossierUnit(
                    cast(DossierPresentation, presentation),
                    claim_ids,
                )
            )
        sections.append(
            EntityDossierSection(title.strip(), cast(DossierPurpose, purpose), tuple(units))
        )
    plan = EntityDossierPlan(
        generation_id=expected_generation_id,
        identity_id=expected_identity_id,
        summary_claim_ids=summary_claim_ids,
        sections=tuple(sections),
        related_identity_ids=related_identity_ids,
    )
    return validate_entity_dossier_plan(plan, claims)


def plan_entity_dossier(
    *,
    generation_id: int,
    identity_id: str,
    claims: tuple[DossierClaimSnapshot, ...],
    language: Literal["en", "zh"],
) -> EntityDossierPlan:
    """Build a complete deterministic plan when no provider plan is available."""
    if not claims:
        raise DossierPlanValidationError(("empty_claim_snapshot",))
    summary_claim_ids: tuple[str, ...] = ()
    remaining = list(claims)
    summary_index = next(
        (index for index, claim in enumerate(remaining) if claim.role in {"definition", "purpose"}),
        None,
    )
    if summary_index is not None:
        summary_claim_ids = (remaining.pop(summary_index).claim_id,)
    by_purpose: dict[str, list[str]] = {}
    for claim in remaining:
        purpose = _ROLE_PURPOSE.get(claim.role, "details")
        by_purpose.setdefault(purpose, []).append(claim.claim_id)
    sections = tuple(
        EntityDossierSection(
            title=_SECTION_TITLES[language][purpose],
            purpose=cast(DossierPurpose, purpose),
            units=(
                EntityDossierUnit(
                    presentation=(
                        "list" if purpose in _LIST_PURPOSES or len(claim_ids) > 1 else "paragraph"
                    ),
                    claim_ids=tuple(claim_ids),
                ),
            ),
        )
        for purpose, claim_ids in by_purpose.items()
    )
    plan = EntityDossierPlan(
        generation_id=generation_id,
        identity_id=identity_id,
        summary_claim_ids=summary_claim_ids,
        sections=sections,
    )
    return validate_entity_dossier_plan(plan, claims)


def render_entity_dossier(
    plan: EntityDossierPlan,
    claims: tuple[DossierClaimSnapshot, ...],
    *,
    language: Literal["en", "zh"],
) -> RenderedEntityDossier:
    """Render only registered claims, with one Evidence marker on every factual line."""
    validate_entity_dossier_plan(plan, claims)
    groups: dict[tuple[str, tuple[tuple[str, str], ...]], list[DossierClaimSnapshot]] = {}
    group_for_claim: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
    for claim in claims:
        key = (_normalized_text(claim.text), claim.applicability)
        groups.setdefault(key, []).append(claim)
        group_for_claim[claim.claim_id] = key
    emitted: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    evidence_ids: list[str] = []
    output: list[str] = []

    def rendered_claims(claim_ids: tuple[str, ...]) -> list[str]:
        lines: list[str] = []
        for claim_id in claim_ids:
            key = group_for_claim[claim_id]
            if key in emitted:
                continue
            emitted.add(key)
            group = groups[key]
            source_ids = tuple(
                dict.fromkeys(evidence_id for claim in group for evidence_id in claim.evidence_ids)
            )
            evidence_ids.extend(
                evidence_id for evidence_id in source_ids if evidence_id not in evidence_ids
            )
            lines.append(_render_claim(group[0], source_ids=source_ids, language=language))
        return lines

    summary_lines = rendered_claims(plan.summary_claim_ids)
    output.extend(summary_lines)
    for section in plan.sections:
        section_output: list[str] = []
        for unit in section.units:
            facts = rendered_claims(unit.claim_ids)
            if not facts:
                continue
            if unit.presentation == "list":
                section_output.extend(f"- {fact}" for fact in facts)
            elif unit.presentation == "table":
                headers = ("Fact", "Evidence") if language == "en" else ("事实", "证据")
                section_output.extend(
                    (
                        f"| {headers[0]} | {headers[1]} |",
                        "| --- | --- |",
                        *(f"| {fact.replace('|', r'\|')} | ✓ |" for fact in facts),
                    )
                )
            else:
                section_output.extend(facts)
        if section_output:
            if output:
                output.append("")
            output.append(f"## {_escape_markdown(section.title)}")
            output.append("")
            output.extend(_separate_paragraphs(section_output, section.units))
    markdown = "\n".join(output).strip()
    return RenderedEntityDossier(
        markdown=markdown,
        content_digest=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        fact_count=len(emitted),
        evidence_ids=tuple(evidence_ids),
    )


def validate_entity_dossier_plan(
    plan: EntityDossierPlan,
    claims: tuple[DossierClaimSnapshot, ...],
) -> EntityDossierPlan:
    """Reject plan references outside the supplied immutable claim snapshot."""
    issues: list[str] = []
    if any(claim.generation_id != plan.generation_id for claim in claims):
        issues.append("generation_mismatch")
    if any(claim.identity_id != plan.identity_id for claim in claims):
        issues.append("identity_mismatch")
    if any(not claim.evidence_ids for claim in claims):
        issues.append("claim_without_evidence")
    for section_index, section in enumerate(plan.sections):
        if not section.units:
            issues.append(f"empty_section:{section_index}")
        if section.purpose not in DOSSIER_PURPOSES:
            issues.append(f"invalid_purpose:{section_index}")
        if (
            not section.title.strip()
            or len(section.title) > 80
            or "\n" in section.title
            or "://" in section.title
            or "[^" in section.title
        ):
            issues.append(f"invalid_title:{section_index}")
        for unit_index, unit in enumerate(section.units):
            if not unit.claim_ids:
                issues.append(f"empty_unit:{section_index}:{unit_index}")
            if unit.presentation not in DOSSIER_PRESENTATIONS:
                issues.append(f"invalid_presentation:{section_index}:{unit_index}")
    known_claim_ids = {claim.claim_id for claim in claims}
    referenced_claim_ids = (
        *plan.summary_claim_ids,
        *(
            claim_id
            for section in plan.sections
            for unit in section.units
            for claim_id in unit.claim_ids
        ),
    )
    unknown = tuple(
        claim_id for claim_id in referenced_claim_ids if claim_id not in known_claim_ids
    )
    issues.extend(f"unknown_claim:{claim_id}" for claim_id in dict.fromkeys(unknown))
    duplicate_claims = tuple(
        claim_id for claim_id, count in Counter(referenced_claim_ids).items() if count > 1
    )
    issues.extend(f"duplicate_claim_placement:{claim_id}" for claim_id in duplicate_claims)
    referenced = set(referenced_claim_ids)
    issues.extend(f"unplaced_claim:{claim_id}" for claim_id in sorted(known_claim_ids - referenced))
    if issues:
        raise DossierPlanValidationError(tuple(issues))
    return plan


def _require_fields(value: dict[object, object], expected: frozenset[str], path: str) -> None:
    fields = frozenset(key for key in value if isinstance(key, str))
    if fields != expected or len(fields) != len(value):
        raise DossierPlanValidationError((f"unexpected_field:{path}",))


def _identifier_list(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise DossierPlanValidationError((f"invalid_identifiers:{path}",))
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 160:
            raise DossierPlanValidationError((f"invalid_identifier:{path}",))
        result.append(item)
    return tuple(result)


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _render_claim(
    claim: DossierClaimSnapshot,
    *,
    source_ids: tuple[str, ...],
    language: Literal["en", "zh"],
) -> str:
    text = _escape_markdown(" ".join(claim.text.split()))
    scope = tuple((dimension, value) for dimension, value in claim.applicability if value)
    if scope:
        values = "; ".join(
            f"{dimension.replace('_', ' ')}: {_escape_markdown(value)}"
            for dimension, value in scope
        )
        text += f" ({values})" if language == "en" else f"（适用：{values}）"
    markers = "".join(f"[^{stable_source_id(evidence_id)}]" for evidence_id in source_ids)
    return f"{text}{markers}"


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for marker in ("*", "_", "[", "]", "<", ">"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped.replace("|", "\\|")


def _separate_paragraphs(lines: list[str], units: tuple[EntityDossierUnit, ...]) -> list[str]:
    if any(unit.presentation in {"list", "table"} for unit in units):
        return lines
    output: list[str] = []
    for line in lines:
        if output:
            output.append("")
        output.append(line)
    return output
