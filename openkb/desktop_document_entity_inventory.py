"""Document-level Entity Inventory snapshots and their closed local boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, cast

from openkb.desktop_document_entity_consolidation import consolidate_inventory_entities
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
from openkb.desktop_model_gateway import (
    DesktopModelRequest,
    DesktopModelResult,
    DesktopProviderTokenUsage,
)
from openkb.desktop_structured_output import (
    DesktopStructuredOutputInvalidError,
    DesktopValidatedStructuredOutput,
    run_structured_output,
    structured_output_reached_limit,
)

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
    decision_proposal_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        decision_ids = set(
            self.decision_proposal_ids or (proposal.proposal_id for proposal in self.proposals)
        )
        return {
            "schema_version": DOCUMENT_ENTITY_INVENTORY_SCHEMA_VERSION,
            "document_version_id": self.document_version_id,
            "analysis_generation_id": self.analysis_generation_id,
            "knowledge_language": self.language,
            "entity_subtype_ontology_version": ENTITY_SUBTYPE_ONTOLOGY_VERSION,
            "section_outline": [list(path) for path in self.section_outline],
            "document_summary": list(self.document_summary),
            "decision_rule": (
                "Return exactly one decision for each decision_proposal_id; "
                "supporting_proposals are document-global context only."
            ),
            "decision_proposal_ids": list(
                self.decision_proposal_ids or (proposal.proposal_id for proposal in self.proposals)
            ),
            "proposals": [
                _proposal_as_dict(proposal)
                for proposal in self.proposals
                if proposal.proposal_id in decision_ids
            ],
            "supporting_proposals": [
                _proposal_as_dict(proposal)
                for proposal in self.proposals
                if proposal.proposal_id not in decision_ids
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

    def as_dict(self) -> dict[str, object]:
        return {
            "document_version_id": self.document_version_id,
            "analysis_generation_id": self.analysis_generation_id,
            "decisions": [
                {
                    "proposal_id": decision.proposal_id,
                    "decision": decision.decision,
                    "canonical_title": decision.canonical_title,
                    "entity_subtype": decision.entity_subtype,
                    "claim_ids": list(decision.claim_ids),
                    "target_identity_id": decision.target_identity_id,
                    "reason_codes": list(decision.reason_codes),
                    "supporting_proposal_ids": list(decision.supporting_proposal_ids),
                    "corpus_brief_ids": list(decision.corpus_brief_ids),
                }
                for decision in self.decisions
            ],
        }


@dataclass(frozen=True)
class DocumentEntityInventoryRun:
    analysis: DesktopKnowledgeAnalysis
    inventory: DocumentEntityInventory
    result: DesktopModelResult | None
    repaired: bool
    split_leaf_count: int = 1
    output_limit_recovery_count: int = 0
    decision_coverage_recovery_count: int = 0


@dataclass(frozen=True)
class _RecoveredInventoryBranch:
    inventory: DocumentEntityInventory
    results: tuple[DesktopModelResult, ...]
    repaired: bool
    split_leaf_count: int
    output_limit_recovery_count: int
    decision_coverage_recovery_count: int


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
    target_ids = _decision_target_ids(snapshot)
    if not isinstance(raw_decisions, list) or len(raw_decisions) > len(target_ids):
        raise InventoryValidationError(("invalid_decisions",))
    proposals = {item.proposal_id: item for item in snapshot.proposals}
    briefs = {item.brief_id: item for item in snapshot.corpus_briefs}
    identities = {item.identity_id for item in snapshot.corpus_briefs}
    decisions = tuple(
        _parse_decision(value, index, proposals=proposals, briefs=briefs, identities=identities)
        for index, value in enumerate(raw_decisions)
    )
    returned = tuple(item.proposal_id for item in decisions)
    expected = target_ids
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
        entities=consolidate_inventory_entities(tuple(entities)),
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
    complete_snapshot = replace(
        snapshot,
        decision_proposal_ids=tuple(proposal.proposal_id for proposal in snapshot.proposals),
    )
    recovered = _recover_inventory_branch(
        document_name=document_name,
        snapshot=complete_snapshot,
        invoke=invoke,
        contract_snapshot=contract_snapshot,
        repair_contract_snapshot=repair_contract_snapshot,
    )
    result = (
        recovered.results[-1]
        if (
            recovered.output_limit_recovery_count == 0
            and recovered.decision_coverage_recovery_count == 0
        )
        else _aggregate_inventory_result(recovered)
    )
    return DocumentEntityInventoryRun(
        apply_document_entity_inventory(
            analysis,
            snapshot=complete_snapshot,
            inventory=recovered.inventory,
        ),
        recovered.inventory,
        result,
        recovered.repaired,
        recovered.split_leaf_count,
        recovered.output_limit_recovery_count,
        recovered.decision_coverage_recovery_count,
    )


def _recover_inventory_branch(
    *,
    document_name: str,
    snapshot: DocumentEntityInventorySnapshot,
    invoke: Callable[[DesktopModelRequest], DesktopModelResult],
    contract_snapshot: dict[str, object] | None,
    repair_contract_snapshot: dict[str, object] | None,
) -> _RecoveredInventoryBranch:
    try:
        output = run_structured_output(
            operation="document_entity_inventory",
            document_name=document_name,
            source_material=document_entity_inventory_prompt(snapshot),
            invoke=invoke,
            validate=lambda content: parse_document_entity_inventory(content, snapshot=snapshot),
            contract_snapshot=contract_snapshot,
            repair_contract_snapshot=repair_contract_snapshot,
        )
    except DesktopStructuredOutputInvalidError as error:
        target_ids = _decision_target_ids(snapshot)
        output_limit_recovery = structured_output_reached_limit(error)
        decision_coverage_recovery = _decision_coverage_failure(error)
        if not (output_limit_recovery or decision_coverage_recovery) or len(target_ids) <= 1:
            raise
        split_at = len(target_ids) // 2
        branches = tuple(
            _recover_inventory_branch(
                document_name=document_name,
                snapshot=replace(snapshot, decision_proposal_ids=child_ids),
                invoke=invoke,
                contract_snapshot=contract_snapshot,
                repair_contract_snapshot=repair_contract_snapshot,
            )
            for child_ids in (target_ids[:split_at], target_ids[split_at:])
        )
        inventory = DocumentEntityInventory(
            snapshot.document_version_id,
            snapshot.analysis_generation_id,
            tuple(decision for branch in branches for decision in branch.inventory.decisions),
        )
        return _RecoveredInventoryBranch(
            inventory=inventory,
            results=(
                *_failed_structured_results(error),
                *(result for branch in branches for result in branch.results),
            ),
            repaired=any(branch.repaired for branch in branches),
            split_leaf_count=sum(branch.split_leaf_count for branch in branches),
            output_limit_recovery_count=(
                int(output_limit_recovery)
                + sum(branch.output_limit_recovery_count for branch in branches)
            ),
            decision_coverage_recovery_count=(
                int(decision_coverage_recovery)
                + sum(branch.decision_coverage_recovery_count for branch in branches)
            ),
        )
    return _RecoveredInventoryBranch(
        inventory=output.value,
        results=_structured_results(output),
        repaired=output.repaired,
        split_leaf_count=1,
        output_limit_recovery_count=0,
        decision_coverage_recovery_count=0,
    )


def _decision_coverage_failure(error: DesktopStructuredOutputInvalidError) -> bool:
    cause = error.__cause__
    return isinstance(cause, InventoryValidationError) and cause.issues == (
        "proposal_decisions_incomplete_or_duplicate",
    )


def _decision_target_ids(snapshot: DocumentEntityInventorySnapshot) -> tuple[str, ...]:
    proposal_ids = tuple(proposal.proposal_id for proposal in snapshot.proposals)
    targets = snapshot.decision_proposal_ids or proposal_ids
    if len(targets) != len(set(targets)) or any(target not in proposal_ids for target in targets):
        raise InventoryValidationError(("invalid_decision_proposal_ids",))
    return targets


def _structured_results(
    output: DesktopValidatedStructuredOutput[DocumentEntityInventory],
) -> tuple[DesktopModelResult, ...]:
    return (
        (output.initial_result, output.result)
        if output.initial_result is not None
        else (output.result,)
    )


def _failed_structured_results(
    error: DesktopStructuredOutputInvalidError,
) -> tuple[DesktopModelResult, ...]:
    return (
        (error.initial_result, error.final_result)
        if error.repair_attempted
        else (error.initial_result,)
    )


def _aggregate_inventory_result(recovered: _RecoveredInventoryBranch) -> DesktopModelResult:
    call_identity = ":".join(result.call_id for result in recovered.results)
    return DesktopModelResult(
        call_id="inventory-split-" + hashlib.sha256(call_identity.encode("utf-8")).hexdigest()[:24],
        content=json.dumps(
            recovered.inventory.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        attempt_count=sum(result.attempt_count for result in recovered.results),
        usage=_aggregate_usage(recovered.results),
        diagnostic_context={
            "output_limit_split_leaf_count": recovered.split_leaf_count,
            "decision_split_leaf_count": recovered.split_leaf_count,
            "output_limit_recovery_count": recovered.output_limit_recovery_count,
            "decision_coverage_recovery_count": recovered.decision_coverage_recovery_count,
        },
    )


def _aggregate_usage(
    results: tuple[DesktopModelResult, ...],
) -> DesktopProviderTokenUsage | None:
    usages = tuple(result.usage for result in results)
    if any(usage is None for usage in usages):
        return None
    complete = tuple(usage for usage in usages if usage is not None)
    return DesktopProviderTokenUsage(
        input_tokens=sum(usage.input_tokens for usage in complete),
        output_tokens=sum(usage.output_tokens for usage in complete),
        total_tokens=sum(usage.total_tokens for usage in complete),
    )


def _proposal_as_dict(proposal: DocumentEntityProposal) -> dict[str, object]:
    return {
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
    provided_supporting_ids = _identifier_list(
        value.get("supporting_proposal_ids"), f"supporting_proposal_ids:{proposal_id}"
    )
    if any(item not in proposals for item in provided_supporting_ids):
        raise InventoryValidationError((f"invalid_supporting_proposals:{proposal_id}",))
    supporting_ids = tuple(dict.fromkeys((proposal_id, *provided_supporting_ids)))
    brief_ids = _identifier_list(value.get("corpus_brief_ids"), f"corpus_brief_ids:{proposal_id}")
    if any(item not in briefs for item in brief_ids):
        raise InventoryValidationError((f"unknown_corpus_brief:{proposal_id}",))
    subtype_value = value.get("entity_subtype")
    entity_subtype = subtype_value if isinstance(subtype_value, str) else None
    if entity_subtype is not None and not is_supported_entity_subtype(entity_subtype):
        raise InventoryValidationError((f"invalid_entity_subtype:{proposal_id}",))
    target_value = value.get("target_identity_id")
    if target_value is not None and not isinstance(target_value, str):
        raise InventoryValidationError((f"invalid_target_identity:{proposal_id}",))
    target_identity_id = target_value if isinstance(target_value, str) else None
    target_identity_generation_id: int | None = None
    target_briefs: tuple[CorpusEntityBrief, ...] = ()
    if decision in {"update", "alias"}:
        target_briefs = tuple(
            briefs[brief_id]
            for brief_id in brief_ids
            if briefs[brief_id].identity_id == target_identity_id
        )
        if target_identity_id not in identities or not target_briefs:
            raise InventoryValidationError((f"unknown_target_identity:{proposal_id}",))
        target_generations = {brief.generation_id for brief in target_briefs}
        if len(target_generations) != 1 or None in target_generations:
            raise InventoryValidationError((f"unknown_target_generation:{proposal_id}",))
        target_identity_generation_id = cast(int, next(iter(target_generations)))
        target_subtypes = {
            brief.entity_subtype
            for brief in target_briefs
            if is_supported_entity_subtype(brief.entity_subtype)
        }
        if len(target_subtypes) > 1:
            raise InventoryValidationError((f"conflicting_target_subtypes:{proposal_id}",))
        if target_subtypes:
            target_subtype = cast(str, next(iter(target_subtypes)))
            if entity_subtype is not None and entity_subtype != target_subtype:
                raise InventoryValidationError((f"target_subtype_mismatch:{proposal_id}",))
            entity_subtype = target_subtype
    elif target_identity_id is not None:
        raise InventoryValidationError((f"unexpected_target_identity:{proposal_id}",))
    if (
        decision in {"create", "update", "alias"}
        and entity_subtype is None
        and is_supported_entity_subtype(proposal.proposed_subtype)
    ):
        entity_subtype = cast(str, proposal.proposed_subtype)
    allowed_titles = {
        normalize_knowledge_title(title)[1]
        for supporting_id in supporting_ids
        for title in (proposals[supporting_id].title, *proposals[supporting_id].aliases)
    }
    if target_briefs:
        allowed_titles.update(
            normalize_knowledge_title(title)[1]
            for brief in target_briefs
            for title in (brief.canonical_title, *brief.aliases)
        )
    canonical_title_is_known = normalize_knowledge_title(canonical_title)[1] in allowed_titles
    local_title_review = decision in {"create", "update", "alias"} and not canonical_title_is_known
    if not canonical_title_is_known:
        canonical_title = proposal.title
    claim_ids = _identifier_list(value.get("claim_ids"), f"claim_ids:{proposal_id}")
    claim_lookup = {
        claim.claim_id: claim
        for supporting_id in supporting_ids
        for claim in proposals[supporting_id].claims
    }
    if any(claim_id not in claim_lookup for claim_id in claim_ids):
        raise InventoryValidationError((f"unknown_claim_id:{proposal_id}",))
    if local_title_review:
        decision = "review"
        reason_codes = ("ambiguous_identity",)
        claim_ids = ()
        target_identity_id = None
        target_identity_generation_id = None
        entity_subtype = None
    elif decision in {"create", "update", "alias"}:
        local_rejection_reason: str | None = None
        if not claim_ids:
            local_rejection_reason = "no_substantive_claim"
        elif not is_supported_entity_subtype(entity_subtype):
            local_rejection_reason = "unsupported_entity_subtype"
        else:
            admission_claims = tuple(
                (claim_lookup[item].role, claim_lookup[item].text) for item in claim_ids
            )
            admission = assess_knowledge_candidate(
                kind="entity",
                title=canonical_title,
                subtype=entity_subtype,
                claims=admission_claims,
                decision_reasons=reason_codes,
            )
            if not admission.admitted and admission.reason == "entity_not_independently_described":
                identity_names = tuple(
                    dict.fromkeys(
                        title
                        for supporting_id in supporting_ids
                        for title in (
                            proposals[supporting_id].title,
                            *proposals[supporting_id].aliases,
                        )
                    )
                ) + tuple(
                    dict.fromkeys(
                        title
                        for brief in target_briefs
                        for title in (brief.canonical_title, *brief.aliases)
                    )
                )
                for identity_name in identity_names:
                    folded_name = " ".join(identity_name.split()).casefold()
                    if not folded_name or not any(
                        folded_name in text.casefold() for _role, text in admission_claims
                    ):
                        continue
                    retry = assess_knowledge_candidate(
                        kind="entity",
                        title=identity_name,
                        subtype=entity_subtype,
                        claims=admission_claims,
                        decision_reasons=reason_codes,
                    )
                    if retry.admitted:
                        admission = retry
                        break
            if not admission.admitted:
                local_rejection_reason = admission.reason
        if local_rejection_reason is not None:
            decision, reason_codes = _local_inventory_disposition(local_rejection_reason)
            claim_ids = ()
            target_identity_id = None
            target_identity_generation_id = None
            entity_subtype = None
    elif entity_subtype is not None:
        entity_subtype = None
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


def _local_inventory_disposition(
    admission_reason: str,
) -> tuple[InventoryDecisionKind, tuple[str, ...]]:
    if admission_reason in {"raw_literal", "document_scaffolding"}:
        return "reject", ("literal_or_metadata",)
    if admission_reason == "relation_phrase":
        return "reject", ("incidental_mention",)
    if admission_reason in {"no_substantive_claim", "entity_not_independently_described"}:
        return "reject", ("insufficient_description",)
    return "review", ("ambiguous_identity",)


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
