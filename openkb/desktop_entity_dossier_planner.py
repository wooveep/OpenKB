"""Auditable model boundary for generation-owned Entity Dossier planning."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from openkb.desktop_entity_dossier import (
    DossierClaimSnapshot,
    EntityDossierPlan,
    parse_entity_dossier_plan,
)
from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult
from openkb.desktop_structured_output import (
    DesktopValidatedStructuredOutput,
    run_structured_output,
)

ENTITY_DOSSIER_INPUT_SCHEMA_VERSION = "openkb.entity-dossier-input.v1"


@dataclass(frozen=True)
class EntityDossierPlanningRun:
    plan: EntityDossierPlan
    result: DesktopModelResult
    repaired: bool
    output: DesktopValidatedStructuredOutput[EntityDossierPlan]


def entity_dossier_planning_prompt(
    *,
    generation_id: int,
    identity_id: str,
    claims: tuple[DossierClaimSnapshot, ...],
    language: Literal["en", "zh"],
    known_related_identity_ids: frozenset[str],
) -> str:
    """Serialize one immutable identity snapshot; facts are input-only, never output fields."""
    payload = {
        "schema_version": ENTITY_DOSSIER_INPUT_SCHEMA_VERSION,
        "generation_id": generation_id,
        "identity_id": identity_id,
        "knowledge_language": language,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "role": claim.role,
                "text": claim.text,
                "applicability": dict(claim.applicability),
                "evidence_ids": list(claim.evidence_ids),
            }
            for claim in claims
        ],
        "known_related_identity_ids": sorted(known_related_identity_ids),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def run_entity_dossier_planning(
    *,
    document_name: str,
    generation_id: int,
    identity_id: str,
    claims: tuple[DossierClaimSnapshot, ...],
    language: Literal["en", "zh"],
    known_related_identity_ids: frozenset[str],
    invoke: Callable[[DesktopModelRequest], DesktopModelResult],
) -> EntityDossierPlanningRun:
    """Invoke the plan-only operation and enforce the generation/identity claim registry."""
    prompt = entity_dossier_planning_prompt(
        generation_id=generation_id,
        identity_id=identity_id,
        claims=claims,
        language=language,
        known_related_identity_ids=known_related_identity_ids,
    )
    output = run_structured_output(
        operation="entity_dossier_planning",
        document_name=document_name,
        source_material=prompt,
        invoke=invoke,
        validate=lambda content: parse_entity_dossier_plan(
            content,
            expected_generation_id=generation_id,
            expected_identity_id=identity_id,
            claims=claims,
            known_related_identity_ids=known_related_identity_ids,
        ),
    )
    return EntityDossierPlanningRun(output.value, output.result, output.repaired, output)
