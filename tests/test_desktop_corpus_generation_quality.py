"""Generation-content quality gates inspect real identities and Entity dossiers."""

from __future__ import annotations

from typing import Literal

from openkb.desktop_corpus_generation_quality import (
    GenerationDossierSnapshot,
    GenerationIdentitySnapshot,
    measure_generation_content_quality,
)
from openkb.desktop_entity_dossier import (
    DossierClaimSnapshot,
    EntityDossierPlan,
    EntityDossierSection,
    EntityDossierUnit,
    render_entity_dossier,
)


def _claims(
    roles: tuple[str, ...],
    *,
    identity_id: str = "entity-alpha",
    text: str | None = None,
) -> tuple[DossierClaimSnapshot, ...]:
    return tuple(
        DossierClaimSnapshot(
            generation_id=7,
            candidate_generation_id="candidate-generation-alpha",
            candidate_id="candidate-alpha",
            claim_ordinal=index,
            claim_id=f"claim-{index}",
            identity_id=identity_id,
            text=text or f"Alpha has a supported {role} fact number {index}.",
            role=role,
            applicability=(),
            evidence_ids=(f"evidence-{index}",),
        )
        for index, role in enumerate(roles)
    )


def _identity(
    title: str,
    *,
    identity_id: str = "entity-alpha",
    kind: str = "entity",
    subtype: str | None = "service",
    aliases: tuple[str, ...] = (),
    claims: tuple[DossierClaimSnapshot, ...] | None = None,
) -> GenerationIdentitySnapshot:
    return GenerationIdentitySnapshot(
        identity_id=identity_id,
        kind=kind,
        title=title,
        subtype=subtype,
        aliases=aliases,
        claims=claims or _claims(("definition",), identity_id=identity_id),
    )


def _dossier(
    claims: tuple[DossierClaimSnapshot, ...],
    plan: EntityDossierPlan,
    *,
    language: Literal["en", "zh"] = "en",
) -> GenerationDossierSnapshot:
    rendered = render_entity_dossier(plan, claims, language=language)
    return GenerationDossierSnapshot(
        identity_id=plan.identity_id,
        plan=plan,
        claims=claims,
        rendered_markdown=rendered.markdown,
        language=language,
    )


def test_entity_noise_is_measured_from_published_content_not_provenance() -> None:
    noisy_claims = _claims(
        ("definition",),
        text="setup.jar is described as a durable software component.",
    )

    quality = measure_generation_content_quality(
        (_identity("setup.jar", subtype="software_component", claims=noisy_claims),),
        (),
    )

    assert quality.entity_noise_leakage_rate == 1.0
    assert "entity_noise" in quality.issues


def test_duplicate_rate_uses_controlled_variants_aliases_and_kind_boundaries() -> None:
    identities = (
        _identity("OCloudView", identity_id="entity-a"),
        _identity("OCloud View", identity_id="entity-b"),
        _identity(
            "Control Center",
            identity_id="entity-c",
            aliases=("OCloud-View",),
        ),
        _identity("OCloudView", identity_id="concept-a", kind="concept", subtype=None),
        _identity("OCloudView Manager", identity_id="entity-d"),
    )

    quality = measure_generation_content_quality(identities, ())

    assert quality.duplicate_identity_rate == 0.4
    assert "duplicate_identity" in quality.issues


def test_complex_dossier_must_cover_evidence_facets_without_one_section_claim_wall() -> None:
    roles = (
        "definition",
        "purpose",
        "mechanism",
        "capability",
        "scope",
        "prerequisite",
        "step",
        "validation",
        "rollback",
        "troubleshooting",
        "limitation",
        "relation",
    )
    claims = _claims(roles)
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=(),
        sections=(
            EntityDossierSection(
                title="Details",
                purpose="details",
                units=(
                    EntityDossierUnit(
                        presentation="paragraph",
                        claim_ids=tuple(claim.claim_id for claim in claims),
                    ),
                ),
            ),
        ),
    )

    quality = measure_generation_content_quality(
        (_identity("Alpha", claims=claims),),
        (_dossier(claims, plan),),
    )

    assert quality.dossier_readability_passed is False
    assert quality.dossier_facet_coverage < 1.0
    assert "dossier_insufficient_semantic_purposes" in quality.issues
    assert "dossier_dominant_section" in quality.issues
    assert "dossier_facet_coverage" in quality.issues


def test_dossier_hard_paragraph_limit_is_enforced() -> None:
    claims = _claims(("definition",), text=f"Alpha {'x' * 810}")
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("claim-0",),
        sections=(),
    )

    quality = measure_generation_content_quality(
        (_identity("Alpha", claims=claims),),
        (_dossier(claims, plan),),
    )

    assert quality.dossier_readability_passed is False
    assert "dossier_paragraph_too_long" in quality.issues


def test_chinese_dossier_quality_uses_the_persisted_render_language() -> None:
    claims = _claims(("definition",), text="Alpha 是具有来源依据的服务。")
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("claim-0",),
        sections=(),
    )

    quality = measure_generation_content_quality(
        (_identity("Alpha", claims=claims),),
        (_dossier(claims, plan, language="zh"),),
    )

    assert quality.dossier_readability_passed is True
    assert "dossier_render_mismatch" not in quality.issues


def test_evidence_shaped_complex_dossier_passes_readability_and_facet_coverage() -> None:
    claims = _claims(
        (
            "definition",
            "purpose",
            "mechanism",
            "capability",
            "capability",
            "scope",
            "prerequisite",
            "step",
            "validation",
            "troubleshooting",
            "limitation",
            "detail",
        )
    )
    by_role = {claim.role: [] for claim in claims}
    for claim in claims:
        by_role[claim.role].append(claim.claim_id)
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=tuple(by_role["definition"] + by_role["purpose"]),
        sections=(
            EntityDossierSection(
                "How it works",
                "capabilities",
                (
                    EntityDossierUnit(
                        "paragraph", tuple(by_role["mechanism"] + by_role["capability"])
                    ),
                ),
            ),
            EntityDossierSection(
                "Where it applies",
                "applicability",
                (EntityDossierUnit("paragraph", tuple(by_role["scope"])),),
            ),
            EntityDossierSection(
                "Requirements",
                "requirements",
                (EntityDossierUnit("list", tuple(by_role["prerequisite"])),),
            ),
            EntityDossierSection(
                "Operations",
                "operations",
                (EntityDossierUnit("list", tuple(by_role["step"] + by_role["validation"])),),
            ),
            EntityDossierSection(
                "Troubleshooting",
                "troubleshooting",
                (EntityDossierUnit("list", tuple(by_role["troubleshooting"])),),
            ),
            EntityDossierSection(
                "Known limits",
                "limitations",
                (EntityDossierUnit("list", tuple(by_role["limitation"])),),
            ),
            EntityDossierSection(
                "Additional context",
                "details",
                (EntityDossierUnit("paragraph", tuple(by_role["detail"])),),
            ),
        ),
    )

    quality = measure_generation_content_quality(
        (_identity("Alpha", claims=claims),),
        (_dossier(claims, plan),),
    )

    assert quality.dossier_readability_passed is True
    assert quality.dossier_readability_rate == 1.0
    assert quality.dossier_facet_coverage == 1.0
