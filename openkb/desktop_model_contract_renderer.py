"""Render code-owned structured contracts into provider-visible instructions."""

from __future__ import annotations

import json

from openkb.desktop_model_gateway import DesktopModelRequest
from openkb.desktop_prompt_contracts import prompt_contract_for

_RENDERED_STRUCTURED_OPERATIONS = frozenset(
    {
        "knowledge_fact_harvest",
        "document_entity_inventory",
        "entity_dossier_planning",
        "knowledge_analysis",
        "knowledge_analysis_batch",
        "knowledge_analysis_merge",
        "knowledge_navigation_step",
        "knowledge_graph_extraction",
        "knowledge_relation_analysis",
        "page_tree_selection",
    }
)


def render_provider_visible_contract(
    request: DesktopModelRequest,
    instructions: str,
) -> str:
    """Render adopted contracts for adapters whose native mode is not authoritative."""
    parent_operation = request.parent_operation
    if (
        request.operation not in _RENDERED_STRUCTURED_OPERATIONS
        and parent_operation not in _RENDERED_STRUCTURED_OPERATIONS
    ):
        return instructions
    if request.response_schema is None:
        return instructions
    snapshot = request.prompt_contract_snapshot or {}
    rendered = {
        "contract_version": request.prompt_contract_version,
        "local_validation_required": request.local_validation_required,
        "output_example": request.response_example,
        "output_schema": request.response_schema,
        "structured_output_mode": request.structured_output_mode,
        "validation_rules": snapshot.get("validation_rules", []),
    }
    if parent_operation in _RENDERED_STRUCTURED_OPERATIONS:
        parent_contract = prompt_contract_for(parent_operation)
        rendered.update(
            {
                "parent_contract_version": parent_contract.version,
                "parent_instructions": parent_contract.instructions,
                "parent_validation_rules": list(parent_contract.validation_rules),
            }
        )
    return (
        f"{instructions.rstrip()}\n\n"
        "STRUCTURED OUTPUT CONTRACT (authoritative; return only a matching JSON object):\n"
        + json.dumps(rendered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
