"""Serialization and validation of Knowledge Analysis result checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_knowledge_analysis import (
    KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
    KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
    DesktopKnowledgeAnalysis,
    parse_knowledge_analysis,
)
from openkb.desktop_knowledge_analysis_output_recovery import output_limit_split_leaf_count
from openkb.desktop_knowledge_analysis_plan import (
    KnowledgeAnalysisPlan,
    prompt_snapshot_for_operation,
)
from openkb.desktop_knowledge_analysis_requests import prompt_snapshot_digest
from openkb.desktop_model_gateway import DesktopModelResult


def parse_batch_checkpoint(checkpoint: object) -> DesktopKnowledgeAnalysis:
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("normalized_result"), dict
    ):
        raise _state_error("Knowledge Analysis batch checkpoint is invalid.")
    return parse_knowledge_analysis(
        _json(checkpoint["normalized_result"]),
        expected_scope=KNOWLEDGE_ANALYSIS_BATCH_SCOPE,
        aggregate=output_limit_split_leaf_count(checkpoint) > 1,
    )


def analysis_from_document_checkpoint(checkpoint: object) -> DesktopKnowledgeAnalysis:
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("normalized_result"), dict
    ):
        raise _state_error("Knowledge Analysis merge checkpoint is invalid.")
    analysis = parse_knowledge_analysis(_json(checkpoint["normalized_result"]), aggregate=True)
    return restore_inventory_reason_codes(analysis, checkpoint)


def restore_inventory_reason_codes(
    analysis: DesktopKnowledgeAnalysis,
    checkpoint: dict[str, object],
) -> DesktopKnowledgeAnalysis:
    raw = checkpoint.get("inventory_entity_reason_codes")
    decisions = checkpoint.get("inventory_entity_decisions")
    target_identity_ids = checkpoint.get("inventory_target_identity_ids")
    target_generation_ids = checkpoint.get("inventory_target_generation_ids")
    if not isinstance(raw, list) or len(raw) != len(analysis.entities):
        return analysis
    if not isinstance(decisions, list) or len(decisions) != len(analysis.entities):
        decisions = [None] * len(analysis.entities)
    if not isinstance(target_identity_ids, list) or len(target_identity_ids) != len(
        analysis.entities
    ):
        target_identity_ids = [None] * len(analysis.entities)
    if not isinstance(target_generation_ids, list) or len(target_generation_ids) != len(
        analysis.entities
    ):
        target_generation_ids = [None] * len(analysis.entities)
    reason_codes: list[tuple[str, ...]] = []
    for value in raw:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return analysis
        reason_codes.append(tuple(value))
    return replace(
        analysis,
        entities=tuple(
            replace(
                candidate,
                admission_reason_codes=codes,
                inventory_decision=(
                    decision
                    if decision in {"create", "update", "alias", "review", "reject"}
                    else None
                ),
                inventory_target_identity_id=(
                    target_identity_id if isinstance(target_identity_id, str) else None
                ),
                inventory_target_generation_id=(
                    target_generation_id
                    if type(target_generation_id) is int and target_generation_id > 0
                    else None
                ),
            )
            for candidate, codes, decision, target_identity_id, target_generation_id in zip(
                analysis.entities,
                reason_codes,
                decisions,
                target_identity_ids,
                target_generation_ids,
                strict=True,
            )
        ),
    )


def harvest_from_inventory_checkpoint(
    checkpoint: dict[str, object],
) -> DesktopKnowledgeAnalysis | None:
    value = checkpoint.get("harvest_normalized_result")
    if not isinstance(value, dict):
        return None
    return parse_knowledge_analysis(_json(value), aggregate=True)


def result_checkpoint(
    analysis: DesktopKnowledgeAnalysis,
    result: DesktopModelResult,
    *,
    plan: KnowledgeAnalysisPlan,
    provider: str,
    model: str,
    prompt_operation: str,
    engine_version: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    prompt_snapshot = prompt_snapshot_for_operation(plan, prompt_operation)
    checkpoint: dict[str, object] = {
        "schema_version": KNOWLEDGE_ANALYSIS_SCHEMA_VERSION,
        "analysis_scope": analysis.analysis_scope,
        "provider": provider,
        "model": model,
        "prompt_digest": prompt_snapshot_digest(prompt_snapshot),
        "prompt_contract_snapshot": prompt_snapshot,
        "engine_version": engine_version,
        "attempt_metadata": {
            "call_id": result.call_id,
            "attempt_count": result.attempt_count,
        },
        "response_sha256": hashlib.sha256(result.content.encode("utf-8")).hexdigest(),
        "normalized_result": analysis.as_dict(),
    }
    if extra:
        checkpoint.update(extra)
    return checkpoint


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_error(message: str) -> DesktopImportError:
    return DesktopImportError("desktop_import_state_invalid", message)
