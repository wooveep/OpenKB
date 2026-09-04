"""Code-owned ontology and provider schema for the Knowledge Identity Graph."""

from __future__ import annotations

SEMANTIC_GRAPH_NODE_KINDS = frozenset(("concept", "entity", "procedure"))
SEMANTIC_GRAPH_RELATION_KINDS = frozenset(
    (
        "IS_A",
        "PART_OF",
        "RELATED_TO",
        "DEPENDS_ON",
        "USES",
        "PRODUCES",
        "LOCATED_IN",
        "CREATED_BY",
        "PRECEDES",
        "REPLACES",
    )
)

MAX_SEMANTIC_RELATIONS_PER_BATCH = 64
MAX_SEMANTIC_SUPPORT_CLAIMS = 4
MAX_SEMANTIC_IDENTIFIER_CHARS = 80

_ALL_PAIRS = frozenset(
    (source, target) for source in SEMANTIC_GRAPH_NODE_KINDS for target in SEMANTIC_GRAPH_NODE_KINDS
)
_RELATION_ENDPOINTS: dict[str, frozenset[tuple[str, str]]] = {
    "IS_A": frozenset(
        {
            ("entity", "concept"),
            ("concept", "concept"),
            ("procedure", "concept"),
        }
    ),
    # A durable named component is an Entity. PART_OF expresses composition;
    # claims, headings, and procedure steps can never be its endpoints.
    "PART_OF": frozenset({("entity", "entity")}),
    "RELATED_TO": _ALL_PAIRS,
    "DEPENDS_ON": frozenset(
        {
            ("entity", "entity"),
            ("entity", "procedure"),
            ("procedure", "entity"),
            ("procedure", "procedure"),
        }
    ),
    "USES": frozenset({("entity", "entity"), ("procedure", "entity")}),
    "PRODUCES": frozenset(
        {
            ("entity", "entity"),
            ("entity", "concept"),
            ("procedure", "entity"),
            ("procedure", "concept"),
        }
    ),
    "LOCATED_IN": frozenset({("entity", "entity")}),
    "CREATED_BY": frozenset(
        {
            ("entity", "entity"),
            ("concept", "entity"),
            ("procedure", "entity"),
        }
    ),
    "PRECEDES": frozenset({("procedure", "procedure")}),
    "REPLACES": frozenset((kind, kind) for kind in SEMANTIC_GRAPH_NODE_KINDS),
}


def relation_endpoint_allowed(relation_kind: str, source_kind: str, target_kind: str) -> bool:
    """Return whether the canonical ontology permits one directed endpoint pair."""
    return (source_kind, target_kind) in _RELATION_ENDPOINTS.get(relation_kind, frozenset())


def semantic_relation_output_schema() -> dict[str, object]:
    """Return the exact node-free schema used by semantic relation analysis."""
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
            "type": {"enum": sorted(SEMANTIC_GRAPH_RELATION_KINDS)},
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
            "type",
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
