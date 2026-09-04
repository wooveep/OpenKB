"""Entity Dossier planning and deterministic rendering behavior."""

from __future__ import annotations

import json

import pytest

from openkb.desktop_entity_dossier import (
    DossierClaimSnapshot,
    DossierPlanValidationError,
    EntityDossierPlan,
    EntityDossierSection,
    EntityDossierUnit,
    parse_entity_dossier_plan,
    plan_entity_dossier,
    render_entity_dossier,
    validate_entity_dossier_plan,
)
from openkb.desktop_entity_dossier_planner import run_entity_dossier_planning
from openkb.desktop_knowledge_sources import stable_source_id
from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult


def _claim(
    claim_id: str,
    *,
    generation_id: int = 7,
    identity_id: str = "entity-alpha",
    text: str = "Alpha is a durable service.",
    role: str = "definition",
    evidence_ids: tuple[str, ...] = ("evidence-alpha",),
    applicability: tuple[tuple[str, str], ...] = (),
) -> DossierClaimSnapshot:
    return DossierClaimSnapshot(
        generation_id=generation_id,
        candidate_generation_id="candidate-generation-alpha",
        candidate_id="candidate-alpha",
        claim_ordinal=0,
        claim_id=claim_id,
        identity_id=identity_id,
        text=text,
        role=role,
        applicability=applicability,
        evidence_ids=evidence_ids,
    )


def test_dossier_plan_with_an_unknown_claim_fails_closed() -> None:
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("unknown-claim",),
        sections=(
            EntityDossierSection(
                title="Capabilities",
                purpose="capabilities",
                units=(
                    EntityDossierUnit(
                        presentation="paragraph",
                        claim_ids=("claim-alpha",),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(DossierPlanValidationError, match="unknown_claim"):
        validate_entity_dossier_plan(plan, (_claim("claim-alpha"),))


@pytest.mark.parametrize(
    ("claim", "issue"),
    (
        (_claim("claim-alpha", generation_id=8), "generation_mismatch"),
        (_claim("claim-alpha", identity_id="entity-beta"), "identity_mismatch"),
    ),
)
def test_dossier_plan_rejects_cross_generation_or_identity_claims(
    claim: DossierClaimSnapshot,
    issue: str,
) -> None:
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("claim-alpha",),
        sections=(),
    )

    with pytest.raises(DossierPlanValidationError, match=issue):
        validate_entity_dossier_plan(plan, (claim,))


def test_dossier_model_boundary_rejects_free_fact_fields() -> None:
    payload = {
        "generation_id": 7,
        "identity_id": "entity-alpha",
        "summary_claim_ids": [],
        "related_identity_ids": [],
        "sections": [
            {
                "title": "Capabilities",
                "purpose": "capabilities",
                "units": [
                    {
                        "presentation": "paragraph",
                        "claim_ids": ["claim-alpha"],
                        "body": "An invented fact cannot cross this boundary.",
                    }
                ],
            }
        ],
    }

    with pytest.raises(DossierPlanValidationError, match="unexpected_field"):
        parse_entity_dossier_plan(
            json.dumps(payload),
            expected_generation_id=7,
            expected_identity_id="entity-alpha",
            claims=(_claim("claim-alpha"),),
        )


def test_dossier_plan_rejects_duplicate_claim_placement() -> None:
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("claim-alpha",),
        sections=(
            EntityDossierSection(
                title="Capabilities",
                purpose="capabilities",
                units=(EntityDossierUnit("paragraph", ("claim-alpha",)),),
            ),
        ),
    )

    with pytest.raises(DossierPlanValidationError, match="duplicate_claim_placement"):
        validate_entity_dossier_plan(plan, (_claim("claim-alpha"),))


def test_dossier_plan_cannot_silently_drop_a_registered_claim() -> None:
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("claim-alpha",),
        sections=(),
    )

    with pytest.raises(DossierPlanValidationError, match="unplaced_claim:claim-beta"):
        validate_entity_dossier_plan(
            plan,
            (
                _claim("claim-alpha"),
                _claim("claim-beta", text="Alpha also supports snapshots."),
            ),
        )


def test_complex_entity_dossier_is_fact_shaped_and_preserves_scope_and_sources() -> None:
    roles = (
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
        "relation",
    )
    claims = tuple(
        _claim(
            f"claim-{index}",
            text=(
                "Alpha supports immutable snapshots."
                if index in {3, 4}
                else f"Alpha {role} fact {index}."
            ),
            role=role,
            evidence_ids=(f"evidence-{index}",),
            applicability=(
                ("product_version", "V2"),
                ("platform", "Linux"),
            ),
        )
        for index, role in enumerate(roles)
    )

    plan = plan_entity_dossier(
        generation_id=7,
        identity_id="entity-alpha",
        claims=claims,
        language="en",
    )
    rendered = render_entity_dossier(plan, claims, language="en")

    assert len({section.purpose for section in plan.sections}) >= 3
    assert all(section.units for section in plan.sections)
    assert rendered.markdown.count("Alpha supports immutable snapshots.") == 1
    assert f"[^{stable_source_id('evidence-3')}]" in rendered.markdown
    assert f"[^{stable_source_id('evidence-4')}]" in rendered.markdown
    assert "product version: V2" in rendered.markdown
    assert "platform: Linux" in rendered.markdown
    assert max(map(len, rendered.markdown.splitlines())) < 800
    assert rendered.fact_count == len(claims) - 1


def test_dossier_plan_cannot_hide_an_empty_section() -> None:
    plan = EntityDossierPlan(
        generation_id=7,
        identity_id="entity-alpha",
        summary_claim_ids=("claim-alpha",),
        sections=(
            EntityDossierSection(
                title="Empty",
                purpose="details",
                units=(),
            ),
        ),
    )

    with pytest.raises(DossierPlanValidationError, match="empty_section"):
        validate_entity_dossier_plan(plan, (_claim("claim-alpha"),))


def test_dossier_planning_runs_as_an_id_only_operation() -> None:
    claims = (_claim("claim-alpha"),)
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        return DesktopModelResult(
            "dossier-call",
            json.dumps(
                {
                    "generation_id": 7,
                    "identity_id": "entity-alpha",
                    "summary_claim_ids": [],
                    "sections": [
                        {
                            "title": "Identity",
                            "purpose": "identity_and_role",
                            "units": [
                                {
                                    "presentation": "paragraph",
                                    "claim_ids": ["claim-alpha"],
                                }
                            ],
                        }
                    ],
                    "related_identity_ids": [],
                }
            ),
            1,
        )

    run = run_entity_dossier_planning(
        document_name="Alpha",
        generation_id=7,
        identity_id="entity-alpha",
        claims=claims,
        language="en",
        known_related_identity_ids=frozenset(),
        invoke=invoke,
    )

    assert [request.operation for request in requests] == ["entity_dossier_planning"]
    assert run.plan.sections[0].units[0].claim_ids == ("claim-alpha",)
    assert run.result.call_id == "dossier-call"
