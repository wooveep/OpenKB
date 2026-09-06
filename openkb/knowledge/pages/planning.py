"""Validate generation-bound Knowledge Page Plans without assigning domain semantics."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Literal, cast

from openkb.models.semantic_structure_contracts import (
    SEMANTIC_PRESENTATIONS,
    SEMANTIC_STRUCTURE_LIMITS,
    normalize_dynamic_semantic_text,
)
from openkb.shared.canonical_json import canonical_json_digest

KnowledgePagePresentation = Literal["paragraph", "unordered_list", "ordered_list"]


@dataclass(frozen=True)
class KnowledgePageUnit:
    unit_id: str
    presentation: KnowledgePagePresentation
    claim_ids: tuple[str, ...]
    relation_assertion_ids: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgePageSection:
    section_id: str
    title: str
    units: tuple[KnowledgePageUnit, ...]
    sections: tuple[KnowledgePageSection, ...] = ()


@dataclass(frozen=True)
class KnowledgePagePlan:
    generation_id: int
    identity_id: str
    claim_snapshot_digest: str
    lead: KnowledgePageUnit | None
    sections: tuple[KnowledgePageSection, ...]
    digest: str

    @property
    def placed_claim_ids(self) -> tuple[str, ...]:
        placed: list[str] = []
        if self.lead is not None:
            placed.extend(self.lead.claim_ids)
        for section in self.sections:
            _append_section_claim_ids(section, placed)
        return tuple(placed)


class KnowledgePagePlanValidationError(ValueError):
    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__(", ".join(issues))


@dataclass(frozen=True)
class _RawUnit:
    presentation: KnowledgePagePresentation
    claim_ids: tuple[str, ...]
    relation_assertion_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RawSection:
    title: str
    units: tuple[_RawUnit, ...]
    sections: tuple[_RawSection, ...]


def parse_knowledge_page_plan(
    content: str,
    *,
    expected_generation_id: int,
    expected_identity_id: str,
    claim_snapshot_digest: str,
    eligible_claim_ids: tuple[str, ...],
    available_relation_assertion_ids: frozenset[str] = frozenset(),
) -> KnowledgePagePlan:
    """Validate one fact-free plan completely, then derive all plan-local identifiers."""
    limits = SEMANTIC_STRUCTURE_LIMITS
    if not 1 <= len(eligible_claim_ids) <= limits.max_claims_per_identity:
        raise KnowledgePagePlanValidationError(("invalid_claim_snapshot_size",))
    if len(set(eligible_claim_ids)) != len(eligible_claim_ids):
        raise KnowledgePagePlanValidationError(("duplicate_snapshot_claim",))
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError) as error:
        raise KnowledgePagePlanValidationError(("invalid_json",)) from error
    if not isinstance(value, dict) or set(value) != {
        "generation_id",
        "identity_id",
        "lead",
        "sections",
    }:
        raise KnowledgePagePlanValidationError(("invalid_plan_shape",))
    if value.get("generation_id") != expected_generation_id:
        raise KnowledgePagePlanValidationError(("generation_mismatch",))
    if value.get("identity_id") != expected_identity_id:
        raise KnowledgePagePlanValidationError(("identity_mismatch",))

    known_claim_ids = frozenset(eligible_claim_ids)
    placed_claim_ids: list[str] = []
    placed_relation_ids: list[str] = []
    lead_value = value.get("lead")
    lead = (
        None
        if lead_value is None
        else _parse_unit(
            lead_value,
            path="lead",
            known_claim_ids=known_claim_ids,
            known_relation_ids=available_relation_assertion_ids,
            placed_claim_ids=placed_claim_ids,
            placed_relation_ids=placed_relation_ids,
        )
    )
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list):
        raise KnowledgePagePlanValidationError(("invalid_sections",))
    section_count = [0]
    sections = tuple(
        _parse_section(
            section,
            path=f"sections[{index}]",
            depth=1,
            section_count=section_count,
            known_claim_ids=known_claim_ids,
            known_relation_ids=available_relation_assertion_ids,
            placed_claim_ids=placed_claim_ids,
            placed_relation_ids=placed_relation_ids,
        )
        for index, section in enumerate(raw_sections)
    )
    issues = _placement_issues(
        eligible_claim_ids,
        placed_claim_ids,
        placed_relation_ids,
    )
    if issues:
        raise KnowledgePagePlanValidationError(issues)

    canonical = {
        "generation_id": expected_generation_id,
        "identity_id": expected_identity_id,
        "claim_snapshot_digest": claim_snapshot_digest,
        "lead": _raw_unit_payload(lead) if lead is not None else None,
        "sections": [_raw_section_payload(section) for section in sections],
    }
    digest = canonical_json_digest(canonical)
    return KnowledgePagePlan(
        generation_id=expected_generation_id,
        identity_id=expected_identity_id,
        claim_snapshot_digest=claim_snapshot_digest,
        lead=_materialize_unit(lead, digest, "lead") if lead is not None else None,
        sections=tuple(
            _materialize_section(section, digest, (index,))
            for index, section in enumerate(sections)
        ),
        digest=digest,
    )


def _parse_section(
    value: object,
    *,
    path: str,
    depth: int,
    section_count: list[int],
    known_claim_ids: frozenset[str],
    known_relation_ids: frozenset[str],
    placed_claim_ids: list[str],
    placed_relation_ids: list[str],
) -> _RawSection:
    limits = SEMANTIC_STRUCTURE_LIMITS
    if depth > limits.max_page_depth:
        raise KnowledgePagePlanValidationError((f"section_depth_exceeded:{path}",))
    section_count[0] += 1
    if section_count[0] > limits.max_sections:
        raise KnowledgePagePlanValidationError(("section_limit_exceeded",))
    required = (
        {"title", "units"}
        if depth == limits.max_page_depth
        else {
            "title",
            "units",
            "sections",
        }
    )
    if not isinstance(value, dict) or set(value) != required:
        raise KnowledgePagePlanValidationError((f"invalid_section_shape:{path}",))
    title = normalize_dynamic_semantic_text(
        value.get("title"),
        field=f"{path}.title",
        maximum_characters=limits.max_label_characters,
    )
    raw_units = value.get("units")
    if not isinstance(raw_units, list) or len(raw_units) > limits.max_claims_per_identity:
        raise KnowledgePagePlanValidationError((f"invalid_section_units:{path}",))
    units = tuple(
        _parse_unit(
            unit,
            path=f"{path}.units[{index}]",
            known_claim_ids=known_claim_ids,
            known_relation_ids=known_relation_ids,
            placed_claim_ids=placed_claim_ids,
            placed_relation_ids=placed_relation_ids,
        )
        for index, unit in enumerate(raw_units)
    )
    if depth == limits.max_page_depth:
        sections: tuple[_RawSection, ...] = ()
    else:
        raw_children = value.get("sections")
        if not isinstance(raw_children, list):
            raise KnowledgePagePlanValidationError((f"invalid_child_sections:{path}",))
        sections = tuple(
            _parse_section(
                child,
                path=f"{path}.sections[{index}]",
                depth=depth + 1,
                section_count=section_count,
                known_claim_ids=known_claim_ids,
                known_relation_ids=known_relation_ids,
                placed_claim_ids=placed_claim_ids,
                placed_relation_ids=placed_relation_ids,
            )
            for index, child in enumerate(raw_children)
        )
    if not units and not sections:
        raise KnowledgePagePlanValidationError((f"empty_section:{path}",))
    return _RawSection(title, units, sections)


def _parse_unit(
    value: object,
    *,
    path: str,
    known_claim_ids: frozenset[str],
    known_relation_ids: frozenset[str],
    placed_claim_ids: list[str],
    placed_relation_ids: list[str],
) -> _RawUnit:
    if not isinstance(value, dict) or set(value) != {
        "presentation",
        "claim_ids",
        "relation_assertion_ids",
    }:
        raise KnowledgePagePlanValidationError((f"invalid_unit_shape:{path}",))
    presentation = value.get("presentation")
    if not isinstance(presentation, str) or presentation not in SEMANTIC_PRESENTATIONS:
        raise KnowledgePagePlanValidationError((f"invalid_presentation:{path}",))
    claim_ids = _identifier_list(value.get("claim_ids"), f"{path}.claim_ids")
    relation_ids = _identifier_list(
        value.get("relation_assertion_ids"),
        f"{path}.relation_assertion_ids",
    )
    if not claim_ids and not relation_ids:
        raise KnowledgePagePlanValidationError((f"empty_unit:{path}",))
    unknown_claims = tuple(claim_id for claim_id in claim_ids if claim_id not in known_claim_ids)
    if unknown_claims:
        raise KnowledgePagePlanValidationError(
            tuple(f"unknown_claim:{claim_id}" for claim_id in unknown_claims)
        )
    unknown_relations = tuple(
        relation_id for relation_id in relation_ids if relation_id not in known_relation_ids
    )
    if unknown_relations:
        raise KnowledgePagePlanValidationError(
            tuple(f"unknown_relation:{relation_id}" for relation_id in unknown_relations)
        )
    placed_claim_ids.extend(claim_ids)
    placed_relation_ids.extend(relation_ids)
    return _RawUnit(cast(KnowledgePagePresentation, presentation), claim_ids, relation_ids)


def _identifier_list(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > SEMANTIC_STRUCTURE_LIMITS.max_claims_per_identity
    ):
        raise KnowledgePagePlanValidationError((f"invalid_identifiers:{field}",))
    if not all(isinstance(identifier, str) and identifier for identifier in value):
        raise KnowledgePagePlanValidationError((f"invalid_identifiers:{field}",))
    return tuple(value)


def _placement_issues(
    eligible_claim_ids: tuple[str, ...],
    placed_claim_ids: list[str],
    placed_relation_ids: list[str],
) -> tuple[str, ...]:
    claim_counts = Counter(placed_claim_ids)
    relation_counts = Counter(placed_relation_ids)
    duplicates = tuple(
        f"duplicate_claim_placement:{claim_id}"
        for claim_id, count in claim_counts.items()
        if count > 1
    )
    unplaced = tuple(
        f"unplaced_claim:{claim_id}"
        for claim_id in eligible_claim_ids
        if claim_counts[claim_id] == 0
    )
    duplicate_relations = tuple(
        f"duplicate_relation_placement:{relation_id}"
        for relation_id, count in relation_counts.items()
        if count > 1
    )
    return (*duplicates, *unplaced, *duplicate_relations)


def _raw_unit_payload(unit: _RawUnit) -> dict[str, object]:
    return {
        "presentation": unit.presentation,
        "claim_ids": list(unit.claim_ids),
        "relation_assertion_ids": list(unit.relation_assertion_ids),
    }


def _raw_section_payload(section: _RawSection) -> dict[str, object]:
    return {
        "title": section.title,
        "units": [_raw_unit_payload(unit) for unit in section.units],
        "sections": [_raw_section_payload(child) for child in section.sections],
    }


def _materialize_unit(unit: _RawUnit, digest: str, path: str) -> KnowledgePageUnit:
    return KnowledgePageUnit(
        unit_id="unit-" + hashlib.sha256(f"{digest}\x1f{path}".encode()).hexdigest(),
        presentation=unit.presentation,
        claim_ids=unit.claim_ids,
        relation_assertion_ids=unit.relation_assertion_ids,
    )


def _materialize_section(
    section: _RawSection,
    digest: str,
    path: tuple[int, ...],
) -> KnowledgePageSection:
    path_text = ".".join(str(index) for index in path)
    return KnowledgePageSection(
        section_id="section-" + hashlib.sha256(f"{digest}\x1f{path_text}".encode()).hexdigest(),
        title=section.title,
        units=tuple(
            _materialize_unit(unit, digest, f"{path_text}.unit.{index}")
            for index, unit in enumerate(section.units)
        ),
        sections=tuple(
            _materialize_section(child, digest, (*path, index))
            for index, child in enumerate(section.sections)
        ),
    )


def _append_section_claim_ids(section: KnowledgePageSection, target: list[str]) -> None:
    for unit in section.units:
        target.extend(unit.claim_ids)
    for child in section.sections:
        _append_section_claim_ids(child, target)
