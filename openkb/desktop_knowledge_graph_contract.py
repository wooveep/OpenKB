"""Provider-visible schema and shared limits for Knowledge Graph candidates."""

from __future__ import annotations

KNOWLEDGE_GRAPH_NODE_TYPES = frozenset(("entity", "concept", "claim"))
KNOWLEDGE_GRAPH_EDGE_TYPES = frozenset(
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
        "SUPPORTS",
        "CONTRADICTS",
    )
)
MAX_GRAPH_NODES_PER_EVIDENCE = 12
MAX_GRAPH_EDGES_PER_EVIDENCE = 16
MAX_GRAPH_INPUT_EVIDENCE = 12
MAX_GRAPH_NODES = MAX_GRAPH_INPUT_EVIDENCE * MAX_GRAPH_NODES_PER_EVIDENCE
MAX_GRAPH_EDGES = MAX_GRAPH_INPUT_EVIDENCE * MAX_GRAPH_EDGES_PER_EVIDENCE
MAX_GRAPH_IDENTIFIER_CHARS = 80
MAX_GRAPH_RELATION_LABEL_CHARS = 80
MAX_GRAPH_LABEL_CHARS = 320
MAX_GRAPH_SUPPORT_QUOTE_CHARS = 1_200
_TRIMMED_NONEMPTY_PATTERN = r"^\S(?:[\s\S]*\S)?$"


def knowledge_graph_output_schema() -> dict[str, object]:
    """Return the exact JSON Schema used by extraction and its one repair."""
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_GRAPH_IDENTIFIER_CHARS,
        "pattern": _TRIMMED_NONEMPTY_PATTERN,
    }
    return {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "maxItems": MAX_GRAPH_NODES,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": dict(identifier),
                        "evidence_id": dict(identifier),
                        "type": {"enum": sorted(KNOWLEDGE_GRAPH_NODE_TYPES)},
                        "label": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_GRAPH_LABEL_CHARS,
                            "pattern": _TRIMMED_NONEMPTY_PATTERN,
                        },
                        "support_quote": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_GRAPH_SUPPORT_QUOTE_CHARS,
                            "pattern": _TRIMMED_NONEMPTY_PATTERN,
                        },
                    },
                    "required": ["id", "evidence_id", "type", "label", "support_quote"],
                    "additionalProperties": False,
                },
            },
            "edges": {
                "type": "array",
                "maxItems": MAX_GRAPH_EDGES,
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_id": dict(identifier),
                        "source_id": dict(identifier),
                        "target_id": dict(identifier),
                        "type": {"enum": sorted(KNOWLEDGE_GRAPH_EDGE_TYPES)},
                        "support_quote": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_GRAPH_SUPPORT_QUOTE_CHARS,
                            "pattern": _TRIMMED_NONEMPTY_PATTERN,
                        },
                    },
                    "required": [
                        "evidence_id",
                        "source_id",
                        "target_id",
                        "type",
                        "support_quote",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["nodes", "edges"],
        "additionalProperties": False,
    }
