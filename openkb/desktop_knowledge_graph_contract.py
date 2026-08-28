"""Strict, evidence-bound validation for Knowledge Graph model responses."""

from __future__ import annotations

import json
from collections.abc import Iterable

from openkb.desktop_knowledge_graph_store import GraphEdge, GraphNode, GraphPayload

_NODE_TYPES = frozenset(("entity", "concept", "claim"))
_EDGE_TYPES = frozenset(
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
_MAX_NODES_PER_EVIDENCE = 12
_MAX_EDGES_PER_EVIDENCE = 16
_MAX_INPUT_EVIDENCE = 12
_MAX_GRAPH_NODES = _MAX_INPUT_EVIDENCE * _MAX_NODES_PER_EVIDENCE
_MAX_GRAPH_EDGES = _MAX_INPUT_EVIDENCE * _MAX_EDGES_PER_EVIDENCE
_MAX_LABEL_CHARS = 320
_GRAPH_PAYLOAD_FIELDS = frozenset(("nodes", "edges"))
_GRAPH_NODE_FIELDS = frozenset(("id", "evidence_id", "type", "label"))
_GRAPH_EDGE_FIELDS = frozenset(("evidence_id", "source_id", "target_id", "type"))
_TRIMMED_NONEMPTY_PATTERN = r"^\S(?:[\s\S]*\S)?$"


def knowledge_graph_output_schema() -> dict[str, object]:
    """Return the exact JSON Schema used by extraction and its one repair."""
    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 80,
        "pattern": _TRIMMED_NONEMPTY_PATTERN,
    }
    return {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "maxItems": _MAX_GRAPH_NODES,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": dict(identifier),
                        "evidence_id": dict(identifier),
                        "type": {"enum": sorted(_NODE_TYPES)},
                        "label": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_LABEL_CHARS,
                            "pattern": _TRIMMED_NONEMPTY_PATTERN,
                        },
                    },
                    "required": ["id", "evidence_id", "type", "label"],
                    "additionalProperties": False,
                },
            },
            "edges": {
                "type": "array",
                "maxItems": _MAX_GRAPH_EDGES,
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_id": dict(identifier),
                        "source_id": dict(identifier),
                        "target_id": dict(identifier),
                        "type": {"enum": sorted(_EDGE_TYPES)},
                    },
                    "required": ["evidence_id", "source_id", "target_id", "type"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["nodes", "edges"],
        "additionalProperties": False,
    }


def validate_knowledge_graph_response(
    content: str, *, known_evidence_ids: Iterable[str]
) -> GraphPayload:
    """Validate one model graph result against the public evidence-bound contract."""
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict):
        raise ValueError("Knowledge graph payload must be an object.")
    _require_no_unexpected_fields(payload, _GRAPH_PAYLOAD_FIELDS, "payload")
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("Knowledge graph payload must include nodes and edges arrays.")
    if len(raw_nodes) > _MAX_GRAPH_NODES or len(raw_edges) > _MAX_GRAPH_EDGES:
        raise ValueError("Knowledge graph payload budget exceeded.")
    nodes = _model_nodes(raw_nodes, set(known_evidence_ids))
    edges = _model_edges(raw_edges, nodes)
    return GraphPayload(nodes=nodes, edges=edges)


def _model_nodes(values: list[object], evidence_ids: set[str]) -> tuple[GraphNode, ...]:
    nodes: list[GraphNode] = []
    identifiers: set[str] = set()
    by_evidence: dict[str, int] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Knowledge graph node must be an object.")
        _require_no_unexpected_fields(value, _GRAPH_NODE_FIELDS, "node")
        local_id = _required_string(value, "id", max_chars=80)
        evidence_id = _required_string(value, "evidence_id", max_chars=80)
        node_type = _required_string(value, "type", max_chars=16)
        label = _required_string(value, "label", max_chars=_MAX_LABEL_CHARS)
        if (
            local_id in identifiers
            or evidence_id not in evidence_ids
            or node_type not in _NODE_TYPES
        ):
            raise ValueError("Knowledge graph node is invalid.")
        count = by_evidence.get(evidence_id, 0) + 1
        if count > _MAX_NODES_PER_EVIDENCE:
            raise ValueError("Knowledge graph node budget exceeded.")
        identifiers.add(local_id)
        by_evidence[evidence_id] = count
        nodes.append(GraphNode(local_id, evidence_id, node_type, label, "model"))
    return tuple(nodes)


def _model_edges(values: list[object], nodes: tuple[GraphNode, ...]) -> tuple[GraphEdge, ...]:
    node_evidence = {node.local_id: node.evidence_id for node in nodes}
    edges: list[GraphEdge] = []
    by_evidence: dict[str, int] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Knowledge graph edge must be an object.")
        _require_no_unexpected_fields(value, _GRAPH_EDGE_FIELDS, "edge")
        evidence_id = _required_string(value, "evidence_id", max_chars=80)
        source = _required_string(value, "source_id", max_chars=80)
        target = _required_string(value, "target_id", max_chars=80)
        edge_type = _required_string(value, "type", max_chars=24)
        if (
            source == target
            or edge_type not in _EDGE_TYPES
            or node_evidence.get(source) != evidence_id
            or node_evidence.get(target) != evidence_id
        ):
            raise ValueError("Knowledge graph edge is invalid.")
        count = by_evidence.get(evidence_id, 0) + 1
        if count > _MAX_EDGES_PER_EVIDENCE:
            raise ValueError("Knowledge graph edge budget exceeded.")
        by_evidence[evidence_id] = count
        edges.append(GraphEdge(evidence_id, source, target, edge_type, 0.75, "model"))
    return tuple(edges)


def _required_string(value: dict[object, object], key: str, *, max_chars: int) -> str:
    candidate = value.get(key)
    if (
        not isinstance(candidate, str)
        or not candidate
        or candidate != candidate.strip()
        or len(candidate) > max_chars
    ):
        raise ValueError(f"Knowledge graph {key} is invalid.")
    return candidate


def _require_no_unexpected_fields(
    value: dict[object, object], expected: frozenset[str], subject: str
) -> None:
    unexpected = sorted(str(key) for key in value if key not in expected)
    if unexpected:
        raise ValueError(
            f"Knowledge graph {subject} has unexpected fields: {', '.join(unexpected)}."
        )


def _json_object_text(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        normalized = normalized.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return normalized
