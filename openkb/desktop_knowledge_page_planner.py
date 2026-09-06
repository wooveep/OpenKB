"""Auditable model boundary for generation-owned Knowledge Page planning."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from openkb.desktop_knowledge_page import (
    KnowledgePageClaimSnapshot,
    KnowledgePageRelationSnapshot,
    knowledge_page_claim_snapshot_digest,
)
from openkb.desktop_knowledge_page_planning import (
    KnowledgePagePlan,
    parse_knowledge_page_plan,
)
from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult
from openkb.desktop_structured_output import (
    DesktopValidatedStructuredOutput,
    run_structured_output,
)

KNOWLEDGE_PAGE_INPUT_SCHEMA_VERSION = "openkb.knowledge-page-input.v1"


@dataclass(frozen=True)
class KnowledgePagePlanningRun:
    plan: KnowledgePagePlan
    result: DesktopModelResult
    repaired: bool
    output: DesktopValidatedStructuredOutput[KnowledgePagePlan]


def knowledge_page_planning_prompt(
    *,
    generation_id: int,
    identity_id: str,
    title: str,
    claims: tuple[KnowledgePageClaimSnapshot, ...],
    relations: tuple[KnowledgePageRelationSnapshot, ...],
    knowledge_language: str,
) -> str:
    """Serialize one immutable, ID-labelled snapshot as untrusted planning data."""
    claim_snapshot_digest = knowledge_page_claim_snapshot_digest(claims)
    payload = {
        "schema_version": KNOWLEDGE_PAGE_INPUT_SCHEMA_VERSION,
        "generation_id": generation_id,
        "identity_id": identity_id,
        "title": title,
        "knowledge_language": knowledge_language,
        "claim_snapshot_digest": claim_snapshot_digest,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "text": claim.text,
                "applicability": [
                    {"dimension": dimension, "value": value}
                    for dimension, value in claim.applicability
                ],
                "evidence_ids": list(claim.evidence_ids),
            }
            for claim in claims
        ],
        "relation_assertions": [
            {
                "assertion_id": relation.assertion_id,
                "source_identity_id": relation.source_identity_id,
                "source_title": relation.source_title,
                "target_identity_id": relation.target_identity_id,
                "target_title": relation.target_title,
                "label": relation.label,
                "evidence_ids": list(relation.evidence_ids),
            }
            for relation in relations
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def run_knowledge_page_planning(
    *,
    document_name: str,
    generation_id: int,
    identity_id: str,
    title: str,
    claims: tuple[KnowledgePageClaimSnapshot, ...],
    relations: tuple[KnowledgePageRelationSnapshot, ...],
    knowledge_language: str,
    invoke: Callable[[DesktopModelRequest], DesktopModelResult],
) -> KnowledgePagePlanningRun:
    """Plan one identity and spend at most one repair call on that logical result."""
    claim_snapshot_digest = knowledge_page_claim_snapshot_digest(claims)
    prompt = knowledge_page_planning_prompt(
        generation_id=generation_id,
        identity_id=identity_id,
        title=title,
        claims=claims,
        relations=relations,
        knowledge_language=knowledge_language,
    )
    output = run_structured_output(
        operation="knowledge_page_planning",
        document_name=document_name,
        source_material=prompt,
        invoke=invoke,
        validate=lambda content: parse_knowledge_page_plan(
            content,
            expected_generation_id=generation_id,
            expected_identity_id=identity_id,
            claim_snapshot_digest=claim_snapshot_digest,
            eligible_claim_ids=tuple(claim.claim_id for claim in claims),
            available_relation_assertion_ids=frozenset(
                relation.assertion_id for relation in relations
            ),
        ),
        repair_output_limit=True,
    )
    return KnowledgePagePlanningRun(output.value, output.result, output.repaired, output)
