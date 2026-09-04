"""Document-level Entity Inventory snapshots and their closed local boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, cast

from openkb.desktop_document_entity_inventory_contract import (
    DOCUMENT_ENTITY_INVENTORY_SCHEMA_VERSION,
    INVENTORY_DECISIONS,
    INVENTORY_REASON_CODES,
)
from openkb.desktop_knowledge_analysis import (
    DesktopKnowledgeAnalysis,
    KnowledgeAnalysisCandidate,
)
from openkb.desktop_knowledge_candidate_admission import assess_knowledge_candidate
from openkb.desktop_knowledge_entity_types import (
    ENTITY_SUBTYPE_ONTOLOGY_VERSION,
    is_supported_entity_subtype,
)
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult
from openkb.desktop_structured_output import run_structured_output

InventoryDecisionKind = Literal["create", "update", "alias", "review", "reject"]


@dataclass(frozen=True)
class CorpusEntityBrief:
    brief_id: str
    identity_id: str
    canonical_title: str
    aliases: tuple[str, ...]
    entity_subtype: str | None
    description: str
    source_document_count: int
    current_claim_count: int
    generation_id: int | None = None
    applicability: tuple[tuple[str, str], ...] = ()
    review_state: str = "active"
    match_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityProposalClaim:
    claim_id: str
    claim_ordinal: int
    text: str
    role: str
    source_evidence_ids: tuple[str, ...]
    applicability: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DocumentEntityProposal:
    proposal_id: str
    proposal_ordinal: int
    title: str
    aliases: tuple[str, ...]
    proposed_subtype: str | None
    tags: tuple[str, ...]
    claims: tuple[EntityProposalClaim, ...]


@dataclass(frozen=True)
class DocumentEntityInventorySnapshot:
    document_version_id: str
    analysis_generation_id: str
    language: Literal["en", "zh"]
    section_outline: tuple[tuple[str, ...], ...]
    document_summary: tuple[dict[str, object], ...]
    proposals: tuple[DocumentEntityProposal, ...]
    corpus_briefs: tuple[CorpusEntityBrief, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": DOCUMENT_ENTITY_INVENTORY_SCHEMA_VERSION,
            "document_version_id": self.document_version_id,
            "analysis_generation_id": self.analysis_generation_id,
            "knowledge_language": self.language,
            "entity_subtype_ontology_version": ENTITY_SUBTYPE_ONTOLOGY_VERSION,
            "section_outline": [list(path) for path in self.section_outline],
            "document_summary": list(self.document_summary),
            "proposals": [
                {
                    "proposal_id": proposal.proposal_id,
                    "title": proposal.title,
                    "aliases": list(proposal.aliases),
                    "proposed_subtype": proposal.proposed_subtype,
                    "tags": list(proposal.tags),
                    "claims": [
                        {
                            "claim_id": claim.claim_id,
                            "text": claim.text,
                            "role": claim.role,
                            "source_evidence_ids": list(claim.source_evidence_ids),
                            "applicability": dict(claim.applicability),
                        }
                        for claim in proposal.claims
                    ],
                }
                for proposal in self.proposals
            ],
            "corpus_entity_briefs": [
                {
                    "brief_id": brief.brief_id,
                    "identity_id": brief.identity_id,
                    "canonical_title": brief.canonical_title,
                    "aliases": list(brief.aliases),
                    "entity_subtype": brief.entity_subtype,
                    "description": brief.description,
                    "source_document_count": brief.source_document_count,
                    "current_claim_count": brief.current_claim_count,
                    "generation_id": brief.generation_id,
                    "applicability": dict(brief.applicability),
                    "review_state": brief.review_state,
                    "match_signals": list(brief.match_signals),
                }
                for brief in self.corpus_briefs
            ],
        }


@dataclass(frozen=True)
class EntityInventoryDecision:
    proposal_id: str
    decision: InventoryDecisionKind
    canonical_title: str
    entity_subtype: str | None
    claim_ids: tuple[str, ...]
    target_identity_id: str | None
    target_identity_generation_id: int | None
    reason_codes: tuple[str, ...]
    supporting_proposal_ids: tuple[str, ...]
    corpus_brief_ids: tuple[str, ...]


@dataclass(frozen=True)
class DocumentEntityInventory:
    document_version_id: str
    analysis_generation_id: str
    decisions: tuple[EntityInventoryDecision, ...]


@dataclass(frozen=True)
class DocumentEntityInventoryRun:
    analysis: DesktopKnowledgeAnalysis
    inventory: DocumentEntityInventory
    result: DesktopModelResult | None
    repaired: bool


class InventoryValidationError(ValueError):
    """The inventory crossed its ID-only decision authority boundary."""

    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__(", ".join(issues))


def build_document_entity_inventory_snapshot(
    *,
    document_version_id: str,
    analysis_generation_id: str,
    language: Literal["en", "zh"],
    analysis: DesktopKnowledgeAnalysis,
    section_outline: tuple[tuple[str, ...], ...] = (),
    corpus_briefs: tuple[CorpusEntityBrief, ...] = (),
) -> DocumentEntityInventorySnapshot:
    """Freeze every harvested Entity proposal and fact before global planning."""
    proposals: list[DocumentEntityProposal] = []
    for proposal_ordinal, candidate in enumerate(analysis.entities):
        proposal_id = _stable_id(
            "proposal", analysis_generation_id, str(proposal_ordinal), candidate.title
        )
        claims = tuple(
            EntityProposalClaim(
                claim_id=_stable_id("claim", proposal_id, str(claim_ordinal)),
                claim_ordinal=claim_ordinal,
                text=claim.text,
                role=claim.role,
                source_evidence_ids=claim.source_evidence_ids,
                applicability=claim.applicability.values(),
            )
            for claim_ordinal, claim in enumerate(candidate.claims)
        )
        proposals.append(
            DocumentEntityProposal(
                proposal_id=proposal_id,
                proposal_ordinal=proposal_ordinal,
                title=candidate.title,
                aliases=candidate.aliases,
                proposed_subtype=candidate.subtype,
                tags=candidate.tags,
                claims=claims,
            )
        )
    summary = tuple(unit.as_dict() for unit in analysis.document_summary)
    return DocumentEntityInventorySnapshot(
        document_version_id=document_version_id,
        analysis_generation_id=analysis_generation_id,
        language=language,
        section_outline=section_outline,
        document_summary=summary,
        proposals=tuple(proposals),
        corpus_briefs=corpus_briefs,
    )


def parse_document_entity_inventory(
    content: str,
    *,
    snapshot: DocumentEntityInventorySnapshot,
) -> DocumentEntityInventory:
    """Parse one model plan while rejecting unknown IDs, facts, names, and types."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as error:
        raise InventoryValidationError(("invalid_json",)) from error
    if not isinstance(payload, dict) or set(payload) != {
        "document_version_id",
        "analysis_generation_id",
        "decisions",
    }:
        raise InventoryValidationError(("invalid_top_level",))
    if payload.get("document_version_id") != snapshot.document_version_id:
        raise InventoryValidationError(("document_version_mismatch",))
    if payload.get("analysis_generation_id") != snapshot.analysis_generation_id:
        raise InventoryValidationError(("analysis_generation_mismatch",))
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list) or len(raw_decisions) > len(snapshot.proposals):
        raise InventoryValidationError(("invalid_decisions",))
    proposals = {item.proposal_id: item for item in snapshot.proposals}
    briefs = {item.brief_id: item for item in snapshot.corpus_briefs}
    identities = {item.identity_id for item in snapshot.corpus_briefs}
    decisions = tuple(
        _parse_decision(value, index, proposals=proposals, briefs=briefs, identities=identities)
        for index, value in enumerate(raw_decisions)
    )
    returned = tuple(item.proposal_id for item in decisions)
    expected = tuple(item.proposal_id for item in snapshot.proposals)
    if len(returned) != len(set(returned)) or set(returned) != set(expected):
        raise InventoryValidationError(("proposal_decisions_incomplete_or_duplicate",))
    return DocumentEntityInventory(
        document_version_id=snapshot.document_version_id,
        analysis_generation_id=snapshot.analysis_generation_id,
        decisions=decisions,
    )


def apply_document_entity_inventory(
    analysis: DesktopKnowledgeAnalysis,
    *,
    snapshot: DocumentEntityInventorySnapshot,
    inventory: DocumentEntityInventory,
) -> DesktopKnowledgeAnalysis:
    """Project accepted decisions using only harvested candidate fields and claim IDs."""
    if (
        inventory.document_version_id != snapshot.document_version_id
        or inventory.analysis_generation_id != snapshot.analysis_generation_id
    ):
        raise InventoryValidationError(("inventory_snapshot_mismatch",))
    decisions = {item.proposal_id: item for item in inventory.decisions}
    entities: list[KnowledgeAnalysisCandidate] = []
    for proposal in snapshot.proposals:
        decision = decisions[proposal.proposal_id]
        original = analysis.entities[proposal.proposal_ordinal]
        if decision.decision not in {"create", "update", "alias"}:
            entities.append(
                replace(
                    original,
                    admission_reason_codes=decision.reason_codes,
                    inventory_decision=decision.decision,
                )
            )
            continue
        by_id = {claim.claim_id: original.claims[claim.claim_ordinal] for claim in proposal.claims}
        claims = tuple(by_id[claim_id] for claim_id in decision.claim_ids)
        aliases = tuple(
            dict.fromkeys(
                value
                for value in (proposal.title, *proposal.aliases)
                if normalize_knowledge_title(value)[1]
                != normalize_knowledge_title(decision.canonical_title)[1]
            )
        )
        entities.append(
            KnowledgeAnalysisCandidate(
                kind="entity",
                title=decision.canonical_title,
                aliases=aliases,
                tags=original.tags,
                claims=claims,
                subtype=decision.entity_subtype,
                admission_reason_codes=decision.reason_codes,
                inventory_decision=decision.decision,
                inventory_target_identity_id=decision.target_identity_id,
                inventory_target_generation_id=decision.target_identity_generation_id,
            )
        )
    return DesktopKnowledgeAnalysis(
        document_description=analysis.document_description,
        concepts=analysis.concepts,
        entities=tuple(entities),
        analysis_scope=analysis.analysis_scope,
        procedures=analysis.procedures,
        document_summary=analysis.document_summary,
        corpus_ready=analysis.corpus_ready,
    )


def document_entity_inventory_prompt(snapshot: DocumentEntityInventorySnapshot) -> str:
    return json.dumps(snapshot.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def run_document_entity_inventory(
    *,
    document_name: str,
    analysis: DesktopKnowledgeAnalysis,
    snapshot: DocumentEntityInventorySnapshot,
    invoke: Callable[[DesktopModelRequest], DesktopModelResult],
    contract_snapshot: dict[str, object] | None = None,
    repair_contract_snapshot: dict[str, object] | None = None,
) -> DocumentEntityInventoryRun:
    """Run the document-global decision operation, skipping cost for a valid empty registry."""
    if not snapshot.proposals:
        inventory = DocumentEntityInventory(
            snapshot.document_version_id,
            snapshot.analysis_generation_id,
            (),
        )
        return DocumentEntityInventoryRun(analysis, inventory, None, False)
    output = run_structured_output(
        operation="document_entity_inventory",
        document_name=document_name,
        source_material=document_entity_inventory_prompt(snapshot),
        invoke=invoke,
        validate=lambda content: parse_document_entity_inventory(content, snapshot=snapshot),
        contract_snapshot=contract_snapshot,
        repair_contract_snapshot=repair_contract_snapshot,
    )
    return DocumentEntityInventoryRun(
        apply_document_entity_inventory(analysis, snapshot=snapshot, inventory=output.value),
        output.value,
        output.result,
        output.repaired,
    )


def _parse_decision(
    value: object,
    index: int,
    *,
    proposals: dict[str, DocumentEntityProposal],
    briefs: dict[str, CorpusEntityBrief],
    identities: set[str],
) -> EntityInventoryDecision:
    fields = {
        "proposal_id",
        "decision",
        "canonical_title",
        "entity_subtype",
        "claim_ids",
        "target_identity_id",
        "reason_codes",
        "supporting_proposal_ids",
        "corpus_brief_ids",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise InventoryValidationError((f"invalid_decision:{index}",))
    proposal_id = _string(value.get("proposal_id"), f"proposal_id:{index}")
    proposal = proposals.get(proposal_id)
    if proposal is None:
        raise InventoryValidationError((f"unknown_proposal:{proposal_id}",))
    raw_decision = value.get("decision")
    if not isinstance(raw_decision, str) or raw_decision not in INVENTORY_DECISIONS:
        raise InventoryValidationError((f"invalid_decision_kind:{proposal_id}",))
    decision = cast(InventoryDecisionKind, raw_decision)
    canonical_title = _string(value.get("canonical_title"), f"canonical_title:{proposal_id}")
    reason_codes = _identifier_list(value.get("reason_codes"), f"reason_codes:{proposal_id}")
    if not reason_codes or any(reason not in INVENTORY_REASON_CODES for reason in reason_codes):
        raise InventoryValidationError((f"invalid_reason_codes:{proposal_id}",))
    supporting_ids = _identifier_list(
        value.get("supporting_proposal_ids"), f"supporting_proposal_ids:{proposal_id}"
    )
    if proposal_id not in supporting_ids or any(item not in proposals for item in supporting_ids):
        raise InventoryValidationError((f"invalid_supporting_proposals:{proposal_id}",))
    brief_ids = _identifier_list(value.get("corpus_brief_ids"), f"corpus_brief_ids:{proposal_id}")
    if any(item not in briefs for item in brief_ids):
        raise InventoryValidationError((f"unknown_corpus_brief:{proposal_id}",))
    target_value = value.get("target_identity_id")
    if target_value is not None and not isinstance(target_value, str):
        raise InventoryValidationError((f"invalid_target_identity:{proposal_id}",))
    target_identity_id = target_value if isinstance(target_value, str) else None
    target_identity_generation_id: int | None = None
    if decision in {"update", "alias"}:
        if target_identity_id not in identities or not any(
            briefs[brief_id].identity_id == target_identity_id for brief_id in brief_ids
        ):
            raise InventoryValidationError((f"unknown_target_identity:{proposal_id}",))
        target_generations = {
            briefs[brief_id].generation_id
            for brief_id in brief_ids
            if briefs[brief_id].identity_id == target_identity_id
        }
        if len(target_generations) != 1 or None in target_generations:
            raise InventoryValidationError((f"unknown_target_generation:{proposal_id}",))
        target_identity_generation_id = cast(int, next(iter(target_generations)))
    elif target_identity_id is not None:
        raise InventoryValidationError((f"unexpected_target_identity:{proposal_id}",))
    allowed_titles = {
        normalize_knowledge_title(title)[1]
        for supporting_id in supporting_ids
        for title in (proposals[supporting_id].title, *proposals[supporting_id].aliases)
    }
    if target_identity_id is not None:
        allowed_titles.update(
            normalize_knowledge_title(title)[1]
            for brief_id in brief_ids
            if briefs[brief_id].identity_id == target_identity_id
            for title in (briefs[brief_id].canonical_title, *briefs[brief_id].aliases)
        )
    if normalize_knowledge_title(canonical_title)[1] not in allowed_titles:
        raise InventoryValidationError((f"invented_canonical_title:{proposal_id}",))
    subtype_value = value.get("entity_subtype")
    entity_subtype = subtype_value if isinstance(subtype_value, str) else None
    claim_ids = _identifier_list(value.get("claim_ids"), f"claim_ids:{proposal_id}")
    allowed_claims = {
        claim.claim_id
        for supporting_id in supporting_ids
        for claim in proposals[supporting_id].claims
    }
    if any(claim_id not in allowed_claims for claim_id in claim_ids):
        raise InventoryValidationError((f"unknown_claim_id:{proposal_id}",))
    if decision in {"create", "update", "alias"}:
        if not claim_ids or not is_supported_entity_subtype(entity_subtype):
            raise InventoryValidationError((f"invalid_admitted_entity:{proposal_id}",))
        claim_lookup = {
            claim.claim_id: claim
            for supporting_id in supporting_ids
            for claim in proposals[supporting_id].claims
        }
        admission = assess_knowledge_candidate(
            kind="entity",
            title=canonical_title,
            subtype=entity_subtype,
            claims=tuple((claim_lookup[item].role, claim_lookup[item].text) for item in claim_ids),
            decision_reasons=reason_codes,
        )
        if not admission.admitted:
            raise InventoryValidationError((f"local_admission:{admission.reason}:{proposal_id}",))
    elif entity_subtype is not None:
        raise InventoryValidationError((f"unexpected_entity_subtype:{proposal_id}",))
    return EntityInventoryDecision(
        proposal_id=proposal_id,
        decision=decision,
        canonical_title=canonical_title,
        entity_subtype=entity_subtype,
        claim_ids=claim_ids,
        target_identity_id=target_identity_id,
        target_identity_generation_id=target_identity_generation_id,
        reason_codes=reason_codes,
        supporting_proposal_ids=supporting_ids,
        corpus_brief_ids=brief_ids,
    )


def _identifier_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 4_096:
        raise InventoryValidationError((f"invalid_list:{label}",))
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item in result:
            raise InventoryValidationError((f"invalid_identifier:{label}",))
        result.append(item)
    return tuple(result)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise InventoryValidationError((f"invalid_string:{label}",))
    return " ".join(value.split())


def _stable_id(kind: str, *parts: str) -> str:
    material = "\x1f".join((kind, *parts))
    return f"{kind}-" + hashlib.sha256(material.encode("utf-8")).hexdigest()
