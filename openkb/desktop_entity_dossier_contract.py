"""Provider schema and code-owned enums for Entity Dossier planning."""

from __future__ import annotations

DOSSIER_PURPOSES = frozenset(
    (
        "identity_and_role",
        "composition",
        "capabilities",
        "applicability",
        "requirements",
        "operations",
        "limitations",
        "troubleshooting",
        "version_evolution",
        "related_identities",
        "details",
    )
)
DOSSIER_PRESENTATIONS = frozenset(("paragraph", "list", "table"))


def entity_dossier_plan_output_schema() -> dict[str, object]:
    """Return a planning schema that deliberately has no fact-text field."""
    identifier = {"type": "string", "minLength": 1, "maxLength": 160}
    identifiers = {
        "type": "array",
        "maxItems": 4_096,
        "items": dict(identifier),
    }
    return {
        "type": "object",
        "properties": {
            "generation_id": {"type": "integer", "minimum": 1},
            "identity_id": dict(identifier),
            "summary_claim_ids": identifiers,
            "sections": {
                "type": "array",
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 80},
                        "purpose": {"enum": sorted(DOSSIER_PURPOSES)},
                        "units": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 64,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "presentation": {"enum": sorted(DOSSIER_PRESENTATIONS)},
                                    "claim_ids": {**identifiers, "minItems": 1},
                                },
                                "required": ["presentation", "claim_ids"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["title", "purpose", "units"],
                    "additionalProperties": False,
                },
            },
            "related_identity_ids": identifiers,
        },
        "required": [
            "generation_id",
            "identity_id",
            "summary_claim_ids",
            "sections",
            "related_identity_ids",
        ],
        "additionalProperties": False,
    }
