"""Document-global Inventory stage between Fact Harvest and Candidate publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_corpus_entity_briefs import load_relevant_corpus_entity_briefs
from openkb.desktop_document_entity_inventory import (
    DocumentEntityInventory,
    build_document_entity_inventory_snapshot,
    run_document_entity_inventory,
)
from openkb.desktop_import_artifacts import DocumentIRBlock
from openkb.desktop_knowledge_analysis import DesktopKnowledgeAnalysis
from openkb.desktop_knowledge_analysis_plan import (
    KnowledgeAnalysisPlan,
    prompt_snapshot_for_operation,
)
from openkb.desktop_knowledge_analysis_requests import (
    prompt_snapshot_digest,
    request_pinned_to_plan,
)
from openkb.desktop_model_gateway import DesktopModelRequest, DesktopModelResult
from openkb.desktop_model_result_failure import (
    structured_model_result_failure as result_failure,
)
from openkb.desktop_structured_output import DesktopStructuredOutputInvalidError


@dataclass(frozen=True)
class KnowledgeInventoryStageResult:
    analysis: DesktopKnowledgeAnalysis
    result: DesktopModelResult
    operation: str
    metadata: dict[str, object]
    inventory: DocumentEntityInventory | None


def apply_document_inventory_stage(
    *,
    document_version_id: str,
    document_name: str,
    harvest: DesktopKnowledgeAnalysis,
    harvest_result: DesktopModelResult,
    harvest_operation: str,
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
    plan: KnowledgeAnalysisPlan,
    analyze: Callable[[DesktopModelRequest], DesktopModelResult],
    on_operation_validated: Callable[[DesktopModelRequest], None],
    knowledge_language: str | None,
    database_path: Path | None = None,
) -> KnowledgeInventoryStageResult:
    """Run one complete Inventory decision, or record a valid empty no-cost stage."""
    if not harvest.corpus_ready:
        return KnowledgeInventoryStageResult(
            harvest,
            harvest_result,
            harvest_operation,
            {},
            None,
        )
    snapshot = build_document_entity_inventory_snapshot(
        document_version_id=document_version_id,
        analysis_generation_id=plan.plan_identity,
        language="zh" if knowledge_language == "zh" else "en",
        analysis=harvest,
        section_outline=tuple(
            dict.fromkeys(block.heading_path for _evidence_id, block in evidence)
        ),
        corpus_briefs=(
            load_relevant_corpus_entity_briefs(database_path, harvest)
            if database_path is not None
            else ()
        ),
    )
    dispatched: list[DesktopModelRequest] = []

    def invoke(request: DesktopModelRequest) -> DesktopModelResult:
        pinned = request_pinned_to_plan(request, plan, batch_id="document-entity-inventory")
        dispatched.append(pinned)
        return analyze(pinned)

    try:
        run = run_document_entity_inventory(
            document_name=document_name,
            analysis=harvest,
            snapshot=snapshot,
            invoke=invoke,
            contract_snapshot=prompt_snapshot_for_operation(plan, "document_entity_inventory"),
            repair_contract_snapshot=prompt_snapshot_for_operation(
                plan, "structured_output_repair"
            ),
        )
    except DesktopStructuredOutputInvalidError as error:
        raise result_failure(
            error,
            suggested_action=(
                "Review the Analysis model contract, then explicitly retry this operation."
            ),
        ) from error
    for request in dispatched:
        on_operation_validated(request)
    if run.result is None:
        return KnowledgeInventoryStageResult(
            run.analysis,
            harvest_result,
            harvest_operation,
            {
                "inventory_state": "completed_empty",
                "inventory_snapshot_digest": _digest(snapshot.as_dict()),
                "inventory_entity_reason_codes": [],
                "inventory_entity_decisions": [],
                "inventory_target_identity_ids": [],
                "inventory_target_generation_ids": [],
            },
            run.inventory,
        )
    contract = prompt_snapshot_for_operation(plan, "document_entity_inventory")
    return KnowledgeInventoryStageResult(
        run.analysis,
        run.result,
        "document_entity_inventory",
        {
            "inventory_state": "ready",
            "inventory_snapshot_digest": _digest(snapshot.as_dict()),
            "inventory_prompt_digest": prompt_snapshot_digest(contract),
            "inventory_response_sha256": hashlib.sha256(
                run.result.content.encode("utf-8")
            ).hexdigest(),
            "inventory_repaired": run.repaired,
            "inventory_output_limit_split_leaf_count": run.split_leaf_count,
            "inventory_output_limit_recovery_count": run.output_limit_recovery_count,
            "inventory_entity_reason_codes": [
                list(candidate.admission_reason_codes) for candidate in run.analysis.entities
            ],
            "inventory_entity_decisions": [
                candidate.inventory_decision for candidate in run.analysis.entities
            ],
            "inventory_target_identity_ids": [
                candidate.inventory_target_identity_id for candidate in run.analysis.entities
            ],
            "inventory_target_generation_ids": [
                candidate.inventory_target_generation_id for candidate in run.analysis.entities
            ],
            "harvest_normalized_result": harvest.as_dict(),
            "harvest_response_sha256": hashlib.sha256(
                harvest_result.content.encode("utf-8")
            ).hexdigest(),
        },
        run.inventory,
    )


def _digest(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
