"""Closed, ID-only Document Entity Inventory boundary behavior."""

from __future__ import annotations

import json

import pytest

from openkb.desktop_document_entity_inventory import (
    CorpusEntityBrief,
    InventoryValidationError,
    apply_document_entity_inventory,
    build_document_entity_inventory_snapshot,
    parse_document_entity_inventory,
    run_document_entity_inventory,
)
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
    KnowledgeAnalysisClaim,
)
from openkb.desktop_model_gateway import (
    DesktopModelOutputObservations,
    DesktopModelRequest,
    DesktopModelResult,
)


def _analysis() -> DesktopKnowledgeAnalysis:
    return DesktopKnowledgeAnalysis(
        document_description="Alpha and a package literal.",
        concepts=(),
        entities=(
            KnowledgeAnalysisCandidate(
                kind="entity",
                title="Alpha",
                aliases=("Alpha Service",),
                tags=("service",),
                subtype="service",
                claims=(
                    KnowledgeAnalysisClaim(
                        text="Alpha is a durable named service.",
                        source_evidence_ids=("evidence-alpha",),
                        role="definition",
                    ),
                ),
            ),
            KnowledgeAnalysisCandidate(
                kind="entity",
                title="Teacher.deb",
                aliases=(),
                tags=(),
                subtype="software_component",
                claims=(
                    KnowledgeAnalysisClaim(
                        text="Teacher.deb is the package installed by the procedure.",
                        source_evidence_ids=("evidence-package",),
                        role="detail",
                    ),
                ),
            ),
        ),
        corpus_ready=True,
    )


def _decision(
    proposal_id: str,
    claim_ids: tuple[str, ...],
    *,
    decision: str,
    title: str,
    subtype: str | None,
    target_identity_id: str | None = None,
    reason_codes: tuple[str, ...] = ("durable_named_entity",),
    corpus_brief_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "decision": decision,
        "canonical_title": title,
        "entity_subtype": subtype,
        "claim_ids": list(claim_ids),
        "target_identity_id": target_identity_id,
        "reason_codes": list(reason_codes),
        "supporting_proposal_ids": [proposal_id],
        "corpus_brief_ids": list(corpus_brief_ids),
    }


def test_inventory_snapshot_has_stable_proposal_and_claim_ids() -> None:
    first = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=_analysis(),
        section_outline=(("Overview",),),
    )
    second = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=_analysis(),
        section_outline=(("Overview",),),
    )

    assert first == second
    assert len({item.proposal_id for item in first.proposals}) == 2
    assert all(item.claims and item.claims[0].claim_id for item in first.proposals)
    assert first.as_dict()["entity_subtype_ontology_version"]


def test_inventory_rejects_a_free_text_fact_and_unknown_claim_id() -> None:
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=_analysis(),
    )
    first, second = snapshot.proposals
    payload = {
        "document_version_id": snapshot.document_version_id,
        "analysis_generation_id": snapshot.analysis_generation_id,
        "decisions": [
            {
                **_decision(
                    first.proposal_id,
                    ("invented-claim",),
                    decision="create",
                    title="Alpha",
                    subtype="service",
                ),
                "body": "invented fact",
            },
            _decision(
                second.proposal_id,
                (),
                decision="reject",
                title="Teacher.deb",
                subtype=None,
                reason_codes=("literal_or_metadata",),
            ),
        ],
    }

    with pytest.raises(InventoryValidationError):
        parse_document_entity_inventory(json.dumps(payload), snapshot=snapshot)


def test_inventory_cannot_target_an_unknown_identity_or_invent_a_title() -> None:
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=_analysis(),
        corpus_briefs=(
            CorpusEntityBrief(
                brief_id="brief-existing",
                identity_id="identity-existing",
                canonical_title="Alpha Service",
                aliases=("Alpha",),
                entity_subtype="service",
                description="An evidence-backed service.",
                source_document_count=2,
                current_claim_count=3,
            ),
        ),
    )
    first, second = snapshot.proposals
    payload = {
        "document_version_id": snapshot.document_version_id,
        "analysis_generation_id": snapshot.analysis_generation_id,
        "decisions": [
            _decision(
                first.proposal_id,
                (first.claims[0].claim_id,),
                decision="update",
                title="Invented Product Name",
                subtype="service",
                target_identity_id="identity-invented",
                reason_codes=("existing_identity_match",),
                corpus_brief_ids=("brief-existing",),
            ),
            _decision(
                second.proposal_id,
                (),
                decision="reject",
                title="Teacher.deb",
                subtype=None,
                reason_codes=("literal_or_metadata",),
            ),
        ],
    }

    with pytest.raises(InventoryValidationError):
        parse_document_entity_inventory(json.dumps(payload), snapshot=snapshot)


def test_inventory_update_preserves_its_generation_bound_identity_target() -> None:
    analysis = _analysis()
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=analysis,
        corpus_briefs=(
            CorpusEntityBrief(
                brief_id="brief-existing",
                identity_id="identity-existing",
                canonical_title="Alpha Service",
                aliases=("Alpha",),
                entity_subtype="service",
                description="An evidence-backed service.",
                source_document_count=2,
                current_claim_count=3,
                generation_id=7,
            ),
        ),
    )
    first, second = snapshot.proposals
    inventory = parse_document_entity_inventory(
        json.dumps(
            {
                "document_version_id": snapshot.document_version_id,
                "analysis_generation_id": snapshot.analysis_generation_id,
                "decisions": [
                    _decision(
                        first.proposal_id,
                        (first.claims[0].claim_id,),
                        decision="update",
                        title="Alpha",
                        subtype="service",
                        target_identity_id="identity-existing",
                        reason_codes=("existing_identity_match",),
                        corpus_brief_ids=("brief-existing",),
                    ),
                    _decision(
                        second.proposal_id,
                        (),
                        decision="reject",
                        title="Teacher.deb",
                        subtype=None,
                        reason_codes=("literal_or_metadata",),
                    ),
                ],
            }
        ),
        snapshot=snapshot,
    )

    applied = apply_document_entity_inventory(
        analysis,
        snapshot=snapshot,
        inventory=inventory,
    )

    assert applied.entities[0].inventory_target_identity_id == "identity-existing"
    assert applied.entities[0].inventory_target_generation_id == 7


def test_inventory_preserves_rejected_proposals_for_candidate_audit() -> None:
    analysis = _analysis()
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=analysis,
    )
    alpha, package = snapshot.proposals
    payload = {
        "document_version_id": snapshot.document_version_id,
        "analysis_generation_id": snapshot.analysis_generation_id,
        "decisions": [
            _decision(
                alpha.proposal_id,
                (alpha.claims[0].claim_id,),
                decision="create",
                title="Alpha",
                subtype="service",
            ),
            _decision(
                package.proposal_id,
                (),
                decision="reject",
                title="Teacher.deb",
                subtype=None,
                reason_codes=("literal_or_metadata",),
            ),
        ],
    }

    inventory = parse_document_entity_inventory(json.dumps(payload), snapshot=snapshot)
    result = apply_document_entity_inventory(analysis, snapshot=snapshot, inventory=inventory)

    assert tuple(item.title for item in result.entities) == ("Alpha", "Teacher.deb")
    assert result.entities[0].claims == analysis.entities[0].claims
    assert result.entities[0].admission_reason_codes == ("durable_named_entity",)
    assert result.entities[0].inventory_decision == "create"
    assert result.entities[1].claims == analysis.entities[1].claims
    assert result.entities[1].admission_reason_codes == ("literal_or_metadata",)
    assert result.entities[1].inventory_decision == "reject"
    assert result.concepts == analysis.concepts
    assert result.corpus_ready


def test_inventory_runs_as_its_own_auditable_model_operation() -> None:
    analysis = _analysis()
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=analysis,
    )
    alpha, package = snapshot.proposals
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        return DesktopModelResult(
            "inventory-call",
            json.dumps(
                {
                    "document_version_id": snapshot.document_version_id,
                    "analysis_generation_id": snapshot.analysis_generation_id,
                    "decisions": [
                        _decision(
                            alpha.proposal_id,
                            (alpha.claims[0].claim_id,),
                            decision="create",
                            title="Alpha",
                            subtype="service",
                        ),
                        _decision(
                            package.proposal_id,
                            (),
                            decision="reject",
                            title="Teacher.deb",
                            subtype=None,
                            reason_codes=("literal_or_metadata",),
                        ),
                    ],
                }
            ),
            1,
        )

    run = run_document_entity_inventory(
        document_name="alpha.md",
        analysis=analysis,
        snapshot=snapshot,
        invoke=invoke,
    )

    assert [request.operation for request in requests] == ["document_entity_inventory"]
    assert tuple(item.title for item in run.analysis.entities) == ("Alpha", "Teacher.deb")
    assert run.result is not None and run.result.call_id == "inventory-call"


def test_inventory_output_limit_recovers_by_splitting_decisions_without_losing_context() -> None:
    analysis = _analysis()
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=analysis,
    )
    proposals = {proposal.proposal_id: proposal for proposal in snapshot.proposals}
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        if len(requests) == 1:
            return DesktopModelResult(
                "inventory-truncated",
                '{"document_version_id":"document-v1","decisions":[',
                1,
                observations=DesktopModelOutputObservations(
                    finish_reason="length",
                    final_content_observed=True,
                    final_chunk_count=1,
                    final_character_count=52,
                    output_limit_reached=True,
                ),
            )
        payload = json.loads(request.content)
        target_ids = payload["decision_proposal_ids"]
        assert len(payload["proposals"]) == len(target_ids)
        assert len(payload["proposals"]) + len(payload["supporting_proposals"]) == len(
            snapshot.proposals
        )
        decisions = []
        for proposal_id in target_ids:
            proposal = proposals[proposal_id]
            admitted = proposal.title == "Alpha"
            decisions.append(
                _decision(
                    proposal_id,
                    (proposal.claims[0].claim_id,) if admitted else (),
                    decision="create" if admitted else "reject",
                    title=proposal.title,
                    subtype=proposal.proposed_subtype if admitted else None,
                    reason_codes=("durable_named_entity",)
                    if admitted
                    else ("literal_or_metadata",),
                )
            )
        return DesktopModelResult(
            f"inventory-split-{len(requests) - 1}",
            json.dumps(
                {
                    "document_version_id": snapshot.document_version_id,
                    "analysis_generation_id": snapshot.analysis_generation_id,
                    "decisions": decisions,
                }
            ),
            1,
        )

    run = run_document_entity_inventory(
        document_name="alpha.md",
        analysis=analysis,
        snapshot=snapshot,
        invoke=invoke,
    )

    assert [request.operation for request in requests] == [
        "document_entity_inventory",
        "document_entity_inventory",
        "document_entity_inventory",
    ]
    assert run.output_limit_recovery_count == 1
    assert run.split_leaf_count == 2
    assert tuple(decision.proposal_id for decision in run.inventory.decisions) == tuple(
        proposal.proposal_id for proposal in snapshot.proposals
    )
    assert tuple(item.title for item in run.analysis.entities) == ("Alpha", "Teacher.deb")


def test_inventory_incomplete_decisions_recover_by_splitting_the_decision_scope() -> None:
    analysis = _analysis()
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id="document-v1",
        analysis_generation_id="analysis-v1",
        language="en",
        analysis=analysis,
    )
    proposals = {proposal.proposal_id: proposal for proposal in snapshot.proposals}
    requests: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        requests.append(request)
        if len(requests) <= 2:
            payload = {
                "document_version_id": snapshot.document_version_id,
                "analysis_generation_id": snapshot.analysis_generation_id,
                "decisions": [],
            }
        else:
            source = json.loads(request.content)
            proposal_id = source["decision_proposal_ids"][0]
            proposal = proposals[proposal_id]
            admitted = proposal.title == "Alpha"
            payload = {
                "document_version_id": snapshot.document_version_id,
                "analysis_generation_id": snapshot.analysis_generation_id,
                "decisions": [
                    _decision(
                        proposal_id,
                        (proposal.claims[0].claim_id,) if admitted else (),
                        decision="create" if admitted else "reject",
                        title=proposal.title,
                        subtype=proposal.proposed_subtype if admitted else None,
                        reason_codes=("durable_named_entity",)
                        if admitted
                        else ("literal_or_metadata",),
                    )
                ],
            }
        return DesktopModelResult(
            f"inventory-coverage-{len(requests)}",
            json.dumps(payload),
            1,
        )

    run = run_document_entity_inventory(
        document_name="alpha.md",
        analysis=analysis,
        snapshot=snapshot,
        invoke=invoke,
    )

    assert [request.operation for request in requests] == [
        "document_entity_inventory",
        "structured_output_repair",
        "document_entity_inventory",
        "document_entity_inventory",
    ]
    assert run.output_limit_recovery_count == 0
    assert run.decision_coverage_recovery_count == 1
    assert run.split_leaf_count == 2
    assert run.result is not None and run.result.attempt_count == 4
    assert tuple(decision.proposal_id for decision in run.inventory.decisions) == tuple(
        proposal.proposal_id for proposal in snapshot.proposals
    )
