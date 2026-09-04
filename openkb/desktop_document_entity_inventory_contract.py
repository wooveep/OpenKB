"""Provider schema and code-owned enums for Document Entity Inventory."""

from __future__ import annotations

from openkb.desktop_knowledge_entity_types import ENTITY_SUBTYPES

INVENTORY_DECISIONS = frozenset(("create", "update", "alias", "review", "reject"))
INVENTORY_REASON_CODES = frozenset(
    (
        "durable_named_entity",
        "existing_identity_match",
        "supported_alias",
        "ambiguous_identity",
        "literal_or_metadata",
        "incidental_mention",
        "insufficient_description",
        "identity_conflict",
        "domain_specific_named_entity",
        "ontology_gap_named_entity",
    )
)
DOCUMENT_ENTITY_INVENTORY_SCHEMA_VERSION = "openkb.document-entity-inventory.v1"


def document_entity_inventory_output_schema() -> dict[str, object]:
    """Return the provider-visible ID-only inventory decision schema."""
    identifier: dict[str, object] = {"type": "string", "minLength": 1, "maxLength": 160}
    identifier_array: dict[str, object] = {
        "type": "array",
        "maxItems": 4_096,
        "items": dict(identifier),
    }
    decision = {
        "type": "object",
        "properties": {
            "proposal_id": dict(identifier),
            "decision": {"enum": sorted(INVENTORY_DECISIONS)},
            "canonical_title": dict(identifier),
            "entity_subtype": {"anyOf": [{"enum": sorted(ENTITY_SUBTYPES)}, {"type": "null"}]},
            "claim_ids": identifier_array,
            "target_identity_id": {"anyOf": [dict(identifier), {"type": "null"}]},
            "reason_codes": {
                "type": "array",
                "minItems": 1,
                "items": {"enum": sorted(INVENTORY_REASON_CODES)},
            },
            "supporting_proposal_ids": {**identifier_array, "minItems": 1},
            "corpus_brief_ids": identifier_array,
        },
        "required": [
            "proposal_id",
            "decision",
            "canonical_title",
            "entity_subtype",
            "claim_ids",
            "target_identity_id",
            "reason_codes",
            "supporting_proposal_ids",
            "corpus_brief_ids",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "document_version_id": dict(identifier),
            "analysis_generation_id": dict(identifier),
            "decisions": {"type": "array", "maxItems": 4_096, "items": decision},
        },
        "required": ["document_version_id", "analysis_generation_id", "decisions"],
        "additionalProperties": False,
    }
