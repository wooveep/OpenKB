"""Content-sensitive quality measurements for one immutable corpus generation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from openkb.desktop_entity_dossier import (
    DossierClaimSnapshot,
    DossierPlanValidationError,
    EntityDossierPlan,
    render_entity_dossier,
    validate_entity_dossier_plan,
)
from openkb.desktop_entity_dossier_store import (
    dossier_claims_for_identity_in,
    generation_entity_dossiers_in,
)
from openkb.desktop_knowledge_candidate_admission import assess_knowledge_candidate
from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_knowledge_titles import (
    controlled_latin_title_key,
    normalize_knowledge_title,
)

_COMPLEX_ENTITY_CLAIM_COUNT = 12
_MAX_PROSE_PARAGRAPH_CHARACTERS = 800
_MAX_SECTION_CLAIM_SHARE = 0.70
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
_DEFAULT_SECTION_TITLES = frozenset(
    {
        "Identity and role",
        "Composition",
        "Capabilities",
        "Deployment and applicability",
        "Requirements",
        "Operations",
        "Limitations",
        "Troubleshooting",
        "Version evolution",
        "Related identities",
        "Details",
        "\u8eab\u4efd\u4e0e\u5b9a\u4f4d",
        "\u7ec4\u6210",
        "\u80fd\u529b\u4e0e\u673a\u5236",
        "\u90e8\u7f72\u4e0e\u9002\u7528\u8303\u56f4",
        "\u8981\u6c42\u4e0e\u524d\u7f6e\u6761\u4ef6",
        "\u64cd\u4f5c\u4e0e\u9a8c\u8bc1",
        "\u9650\u5236",
        "\u6545\u969c\u6392\u67e5",
        "\u7248\u672c\u6f14\u8fdb",
        "\u76f8\u5173\u8eab\u4efd",
        "\u5176\u4ed6\u4e8b\u5b9e",
    }
)


@dataclass(frozen=True)
class GenerationIdentitySnapshot:
    """Identity content used by a quality gate, independent of persistence details."""

    identity_id: str
    kind: str
    title: str
    subtype: str | None
    aliases: tuple[str, ...]
    claims: tuple[DossierClaimSnapshot, ...]


@dataclass(frozen=True)
class GenerationDossierSnapshot:
    """A stored Entity Dossier and the exact claim snapshot it renders."""

    identity_id: str
    plan: EntityDossierPlan
    claims: tuple[DossierClaimSnapshot, ...]
    rendered_markdown: str


@dataclass(frozen=True)
class GenerationContentQuality:
    entity_noise_leakage_rate: float
    duplicate_identity_rate: float
    dossier_readability_rate: float
    dossier_facet_coverage: float
    dossier_readability_passed: bool
    issues: tuple[str, ...]


def generation_content_quality_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> GenerationContentQuality:
    """Load and measure only artifacts pinned to ``generation_id``."""
    rows = connection.execute(
        "SELECT item_key, identity_id, kind, title, entity_subtype, aliases_json "
        "FROM knowledge_generation_items WHERE generation_id = ? ORDER BY item_key",
        (generation_id,),
    ).fetchall()
    identities: list[GenerationIdentitySnapshot] = []
    for row in rows:
        identity_id = str(row[1]) if row[1] is not None else f"unbound:{row[0]}"
        claims = (
            dossier_claims_for_identity_in(connection, generation_id, identity_id)
            if str(row[2]) == "entity" and row[1] is not None
            else ()
        )
        try:
            aliases = decode_knowledge_labels(row[5])
        except (TypeError, ValueError, json.JSONDecodeError):
            aliases = ()
        identities.append(
            GenerationIdentitySnapshot(
                identity_id=identity_id,
                kind=str(row[2]),
                title=str(row[3]),
                subtype=str(row[4]) if row[4] is not None else None,
                aliases=aliases,
                claims=claims,
            )
        )
    item_markdown = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT identity_id, content_markdown FROM knowledge_generation_items "
            "WHERE generation_id = ? AND kind = 'entity' AND identity_id IS NOT NULL",
            (generation_id,),
        )
    }
    dossiers = tuple(
        GenerationDossierSnapshot(
            identity_id=dossier.identity_id,
            plan=dossier.plan,
            claims=dossier_claims_for_identity_in(connection, generation_id, dossier.identity_id),
            rendered_markdown=item_markdown.get(dossier.identity_id, ""),
        )
        for dossier in generation_entity_dossiers_in(connection, generation_id)
    )
    return measure_generation_content_quality(
        tuple(identities),
        dossiers,
        review_identity_groups=_review_identity_groups_in(connection, generation_id),
    )


def measure_generation_content_quality(
    identities: tuple[GenerationIdentitySnapshot, ...],
    dossiers: tuple[GenerationDossierSnapshot, ...],
    *,
    review_identity_groups: tuple[tuple[str, ...], ...] = (),
) -> GenerationContentQuality:
    """Measure actual entities, identity signals, and rendered dossier structure."""
    issues: list[str] = []
    entities = tuple(identity for identity in identities if identity.kind == "entity")
    noisy = sum(_entity_is_noise(identity) for identity in entities)
    noise_rate = noisy / len(entities) if entities else 0.0
    if noisy:
        issues.append("entity_noise")

    duplicate_rate = _duplicate_identity_rate(identities, review_identity_groups)
    if duplicate_rate:
        issues.append("duplicate_identity")

    dossier_by_identity = {dossier.identity_id: dossier for dossier in dossiers}
    failed_dossiers: set[str] = set()
    covered_facets = 0
    expected_facets = 0
    complex_headings: dict[str, tuple[str, ...]] = {}
    for identity in entities:
        dossier = dossier_by_identity.get(identity.identity_id)
        if dossier is None:
            failed_dossiers.add(identity.identity_id)
            issues.append("dossier_missing")
            expected_facets += len({_purpose(claim.role) for claim in identity.claims})
            continue
        dossier_issues, covered, expected = _dossier_issues(dossier)
        covered_facets += covered
        expected_facets += expected
        if dossier_issues:
            failed_dossiers.add(identity.identity_id)
            issues.extend(dossier_issues)
        if len(dossier.claims) >= _COMPLEX_ENTITY_CLAIM_COUNT:
            complex_headings[identity.identity_id] = tuple(
                section.title for section in dossier.plan.sections
            )
    if _uses_one_fixed_heading_template(complex_headings):
        failed_dossiers.update(complex_headings)
        issues.append("dossier_fixed_heading_template")
    readability_rate = (len(entities) - len(failed_dossiers)) / len(entities) if entities else 1.0
    facet_coverage = covered_facets / expected_facets if expected_facets else 1.0
    readability_passed = readability_rate == 1.0 and facet_coverage == 1.0
    return GenerationContentQuality(
        entity_noise_leakage_rate=noise_rate,
        duplicate_identity_rate=duplicate_rate,
        dossier_readability_rate=readability_rate,
        dossier_facet_coverage=facet_coverage,
        dossier_readability_passed=readability_passed,
        issues=tuple(dict.fromkeys(issues)),
    )


def _entity_is_noise(identity: GenerationIdentitySnapshot) -> bool:
    decision_reasons = (
        ("domain_specific_named_entity",) if identity.subtype == "other_named_entity" else ()
    )
    admission = assess_knowledge_candidate(
        kind="entity",
        title=identity.title,
        subtype=identity.subtype,
        claims=tuple((claim.role, claim.text) for claim in identity.claims),
        decision_reasons=decision_reasons,
    )
    return not admission.admitted


def _duplicate_identity_rate(
    identities: tuple[GenerationIdentitySnapshot, ...],
    review_identity_groups: tuple[tuple[str, ...], ...],
) -> float:
    if not identities:
        return 0.0
    parents = {identity.identity_id: identity.identity_id for identity in identities}
    by_id = {identity.identity_id: identity for identity in identities}

    def root(identity_id: str) -> str:
        while parents[identity_id] != identity_id:
            parents[identity_id] = parents[parents[identity_id]]
            identity_id = parents[identity_id]
        return identity_id

    def union(left: str, right: str) -> None:
        left_root = root(left)
        right_root = root(right)
        if left_root != right_root:
            parents[right_root] = left_root

    signals = {identity.identity_id: _identity_signals(identity) for identity in identities}
    for index, left in enumerate(identities):
        for right in identities[index + 1 :]:
            if left.kind == right.kind and signals[left.identity_id] & signals[right.identity_id]:
                union(left.identity_id, right.identity_id)
    for group in review_identity_groups:
        eligible = tuple(identity_id for identity_id in group if identity_id in by_id)
        for left_id, right_id in zip(eligible, eligible[1:], strict=False):
            if by_id[left_id].kind == by_id[right_id].kind:
                union(left_id, right_id)
    cluster_sizes: dict[str, int] = {}
    for identity_id in parents:
        cluster_root = root(identity_id)
        cluster_sizes[cluster_root] = cluster_sizes.get(cluster_root, 0) + 1
    duplicate_count = sum(size - 1 for size in cluster_sizes.values() if size > 1)
    return duplicate_count / len(identities)


def _identity_signals(identity: GenerationIdentitySnapshot) -> frozenset[str]:
    values = (identity.title, *identity.aliases)
    signals: set[str] = set()
    for value in values:
        normalized = normalize_knowledge_title(value)[1]
        controlled = controlled_latin_title_key(value)
        if normalized:
            signals.add(f"normalized:{normalized}")
        if controlled:
            signals.add(f"controlled:{controlled}")
    return frozenset(signals)


def _dossier_issues(dossier: GenerationDossierSnapshot) -> tuple[tuple[str, ...], int, int]:
    issues: list[str] = []
    try:
        validate_entity_dossier_plan(dossier.plan, dossier.claims)
        rendered = render_entity_dossier(dossier.plan, dossier.claims, language="en")
    except DossierPlanValidationError:
        return ("dossier_invalid_plan",), 0, len({_purpose(claim.role) for claim in dossier.claims})
    if rendered.markdown != dossier.rendered_markdown:
        issues.append("dossier_render_mismatch")
    if not _factual_lines_have_sources(dossier.rendered_markdown):
        issues.append("dossier_source_marker_missing")

    placement_purposes: dict[str, str] = {
        claim_id: _purpose(
            next(claim.role for claim in dossier.claims if claim.claim_id == claim_id)
        )
        for claim_id in dossier.plan.summary_claim_ids
    }
    section_counts: list[int] = [len(dossier.plan.summary_claim_ids)]
    used_purposes = set(placement_purposes.values())
    paragraph_claim_ids = set(dossier.plan.summary_claim_ids)
    for section in dossier.plan.sections:
        section_claim_ids = tuple(claim_id for unit in section.units for claim_id in unit.claim_ids)
        section_counts.append(len(section_claim_ids))
        if section_claim_ids:
            used_purposes.add(section.purpose)
        for claim_id in section_claim_ids:
            placement_purposes[claim_id] = section.purpose
        paragraph_claim_ids.update(
            claim_id
            for unit in section.units
            if unit.presentation == "paragraph"
            for claim_id in unit.claim_ids
        )

    expected = {_purpose(claim.role) for claim in dossier.claims}
    covered = {
        _purpose(claim.role)
        for claim in dossier.claims
        if placement_purposes.get(claim.claim_id) == _purpose(claim.role)
    }
    if covered != expected:
        issues.append("dossier_facet_coverage")
    if any(
        len(" ".join(claim.text.split())) > _MAX_PROSE_PARAGRAPH_CHARACTERS
        for claim in dossier.claims
        if claim.claim_id in paragraph_claim_ids
    ):
        issues.append("dossier_paragraph_too_long")
    if len(dossier.claims) >= _COMPLEX_ENTITY_CLAIM_COUNT:
        if len(expected) >= 3 and len(used_purposes) < 3:
            issues.append("dossier_insufficient_semantic_purposes")
        if (
            len(expected) > 1
            and section_counts
            and max(section_counts) / len(dossier.claims) > _MAX_SECTION_CLAIM_SHARE
        ):
            issues.append("dossier_dominant_section")
    return tuple(dict.fromkeys(issues)), len(covered), len(expected)


def _purpose(role: str) -> str:
    return _ROLE_PURPOSE.get(role, "details")


def _factual_lines_have_sources(markdown: str) -> bool:
    for line in markdown.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("## ")
            or stripped
            in {"| Fact | Evidence |", "| \u4e8b\u5b9e | \u8bc1\u636e |", "| --- | --- |"}
        ):
            continue
        if "[^" not in stripped:
            return False
    return bool(markdown.strip())


def _uses_one_fixed_heading_template(
    headings: dict[str, tuple[str, ...]],
) -> bool:
    templates = set(headings.values())
    if len(headings) < 2 or len(templates) != 1:
        return False
    template = next(iter(templates))
    return bool(template) and all(title in _DEFAULT_SECTION_TITLES for title in template)


def _review_identity_groups_in(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[tuple[str, ...], ...]:
    candidate_identities = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT candidate_id, identity_id FROM knowledge_generation_identity_mappings "
            "WHERE generation_id = ?",
            (generation_id,),
        )
    }
    groups: list[tuple[str, ...]] = []
    for (payload,) in connection.execute(
        "SELECT candidate_ids_json FROM knowledge_identity_review_items "
        "WHERE status = 'pending' ORDER BY review_id"
    ):
        try:
            candidate_ids = json.loads(str(payload))
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate_ids, list):
            continue
        identities = tuple(
            dict.fromkeys(
                candidate_identities[candidate_id]
                for candidate_id in candidate_ids
                if isinstance(candidate_id, str) and candidate_id in candidate_identities
            )
        )
        if len(identities) > 1:
            groups.append(identities)
    return tuple(groups)
