"""Provider schema for bounded, evidence-bound dynamic relation assertions."""

from __future__ import annotations

from openkb.desktop_semantic_structure_contracts import SEMANTIC_STRUCTURE_LIMITS

MAX_SEMANTIC_RELATIONS_PER_BATCH = 64
MAX_SEMANTIC_SUPPORT_CLAIMS = 8
MAX_SEMANTIC_IDENTIFIER_CHARS = 160


def semantic_relation_output_schema() -> dict[str, object]:
    """Return the node-free schema; relation semantics remain model-owned labels."""
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_SEMANTIC_IDENTIFIER_CHARS,
        "pattern": r"^\S(?:[\s\S]*\S)?$",
    }
    support = {
        "type": "object",
        "properties": {
            "candidate_id": dict(identifier),
            "claim_ordinal": {"type": "integer", "minimum": 0},
        },
        "required": ["candidate_id", "claim_ordinal"],
        "additionalProperties": False,
    }
    relation = {
        "type": "object",
        "properties": {
            "source_candidate_id": dict(identifier),
            "target_candidate_id": dict(identifier),
            "label": {
                "type": "string",
                "minLength": 1,
                "maxLength": SEMANTIC_STRUCTURE_LIMITS.max_label_characters,
            },
            "supporting_claims": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SEMANTIC_SUPPORT_CLAIMS,
                "items": support,
            },
        },
        "required": [
            "source_candidate_id",
            "target_candidate_id",
            "label",
            "supporting_claims",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "relations": {
                "type": "array",
                "maxItems": MAX_SEMANTIC_RELATIONS_PER_BATCH,
                "items": relation,
            }
        },
        "required": ["relations"],
        "additionalProperties": False,
    }
