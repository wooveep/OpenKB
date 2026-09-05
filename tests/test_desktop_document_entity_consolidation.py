"""Document Entity Inventory must publish one candidate per resolved identity."""

from __future__ import annotations

import json

from openkb.desktop_document_entity_inventory import (
    CorpusEntityBrief,
    apply_document_entity_inventory,
    build_document_entity_inventory_snapshot,
    parse_document_entity_inventory,
)
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
    parse_knowledge_analysis,
)


def test_inventory_consolidates_proposals_resolved_to_one_existing_identity() -> None:
    analysis = DesktopKnowledgeAnalysis(
        document_description="Two sections describe one service.",
        concepts=(),
        entities=(
            _entity("Alpha", "Alpha is the durable service.", "evidence-alpha"),
            _entity(
                "Alpha Control Plane",
                "Alpha Control Plane operates the Alpha service.",
                "evidence-control-plane",
            ),
        ),
        corpus_ready=True,
    )
    brief = CorpusEntityBrief(
        brief_id="brief-alpha",
        identity_id="identity-alpha",
        canonical_title="Alpha Service",
        aliases=("Alpha", "Alpha Control Plane"),
        entity_subtype="service",
        description="An evidence-backed service.",
        source_document_count=2,
        current_claim_count=3,
        generation_id=7,
    )
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v2",
        analysis_generation_id="analysis-v2",
        language="en",
        analysis=analysis,
        corpus_briefs=(brief,),
    )
    decisions = [
        {
            "proposal_id": proposal.proposal_id,
            "decision": "update",
            "canonical_title": brief.canonical_title,
            "entity_subtype": "service",
            "claim_ids": [proposal.claims[0].claim_id],
            "target_identity_id": brief.identity_id,
            "reason_codes": ["existing_identity_match"],
            "supporting_proposal_ids": [proposal.proposal_id],
            "corpus_brief_ids": [brief.brief_id],
        }
        for proposal in snapshot.proposals
    ]
    inventory = parse_document_entity_inventory(
        json.dumps(
            {
                "document_version_id": snapshot.document_version_id,
                "analysis_generation_id": snapshot.analysis_generation_id,
                "decisions": decisions,
            }
        ),
        snapshot=snapshot,
    )

    applied = apply_document_entity_inventory(
        analysis,
        snapshot=snapshot,
        inventory=inventory,
    )

    assert len(applied.entities) == 1
    entity = applied.entities[0]
    assert entity.title == "Alpha Service"
    assert entity.aliases == ("Alpha", "Alpha Control Plane")
    assert tuple(claim.text for claim in entity.claims) == (
        "Alpha is the durable service.",
        "Alpha Control Plane operates the Alpha service.",
    )
    assert entity.inventory_decision == "update"
    assert entity.inventory_target_identity_id == brief.identity_id
    assert entity.inventory_target_generation_id == 7
    restored = parse_knowledge_analysis(json.dumps(applied.as_dict()), aggregate=True)
    assert tuple(item.title for item in restored.entities) == ("Alpha Service",)


def _entity(title: str, claim: str, evidence_id: str) -> KnowledgeAnalysisCandidate:
    return KnowledgeAnalysisCandidate(
        kind="entity",
        title=title,
        aliases=(),
        tags=("service",),
        subtype="service",
        claims=(
            KnowledgeAnalysisClaim(
                text=claim,
                source_evidence_ids=(evidence_id,),
                role="definition",
            ),
        ),
    )
