"""Interpret untrusted model graph candidates before publication."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from openkb.desktop_knowledge_graph_contract import (
    KNOWLEDGE_GRAPH_EDGE_TYPES,
    KNOWLEDGE_GRAPH_NODE_TYPES,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_EDGES_PER_EVIDENCE,
    MAX_GRAPH_IDENTIFIER_CHARS,
    MAX_GRAPH_LABEL_CHARS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_NODES_PER_EVIDENCE,
    MAX_GRAPH_RELATION_LABEL_CHARS,
    MAX_GRAPH_SUPPORT_QUOTE_CHARS,
)
from openkb.desktop_knowledge_graph_store import GraphEdge, GraphNode, GraphPayload
from openkb.desktop_structured_output import normalize_structured_output

GraphLifecycle = Literal["completed", "completed_empty", "failed"]
GraphResultQuality = Literal["full", "degraded"]
_PAYLOAD_FIELDS = frozenset({"nodes", "edges"})
_NODE_FIELDS = frozenset({"id", "evidence_id", "type", "label", "support_quote"})
_EDGE_FIELDS = frozenset({"evidence_id", "source_id", "target_id", "type", "support_quote"})
_MAX_NODE_TYPE_CHARS = 16
_MAX_RESPONSE_CHARS = 1_000_000
_MAX_JSON_NESTING = 64


@dataclass(frozen=True)
class GraphEvidence:
    evidence_id: str
    text: str


@dataclass(frozen=True)
class KnowledgeGraphIssue:
    code: str
    path: str
    disposition: str
    failure_class: str


@dataclass(frozen=True)
class GraphDispositionCounts:
    retained: int
    weakened: int
    rejected: int


@dataclass(frozen=True)
class GraphInterpretation:
    payload: GraphPayload | None
    lifecycle: GraphLifecycle
    quality: GraphResultQuality | None
    issues: tuple[KnowledgeGraphIssue, ...]
    counts: GraphDispositionCounts
    repairable: bool = False
    failure_signature: str | None = None


class KnowledgeGraphInterpretationError(ValueError):
    """A content-free summary of one graph response that cannot be published."""

    def __init__(self, interpretation: GraphInterpretation) -> None:
        self.interpretation = interpretation
        details = "; ".join(f"{issue.code} at {issue.path}" for issue in interpretation.issues)
        super().__init__(details or "Knowledge graph response cannot be interpreted safely.")


class GraphExtractionBoundary:
    """Own the one public model-output-to-graph interpretation seam."""

    @staticmethod
    def interpret(
        content: str,
        evidence: tuple[GraphEvidence, ...],
    ) -> GraphInterpretation:
        if len(content) > _MAX_RESPONSE_CHARS:
            return _fatal_interpretation("response_budget_exceeded", "$", "budget", repairable=True)
        if _json_nesting_exceeded(content):
            return _fatal_interpretation("json_nesting_exceeded", "$", "shape", repairable=True)
        try:
            payload = json.loads(normalize_structured_output(content))
        except json.JSONDecodeError:
            return _fatal_interpretation("invalid_json", "$", "shape", repairable=True)
        except ValueError:
            return _fatal_interpretation(
                "json_value_limit_exceeded", "$", "budget", repairable=True
            )
        except RecursionError:
            return _fatal_interpretation("json_nesting_exceeded", "$", "shape", repairable=True)
        if not isinstance(payload, dict):
            return _fatal_interpretation("top_level_not_object", "$", "shape", repairable=True)
        if "nodes" not in payload:
            return _fatal_interpretation("missing_nodes_array", "$.nodes", "shape", repairable=True)
        if "edges" not in payload:
            return _fatal_interpretation("missing_edges_array", "$.edges", "shape", repairable=True)
        raw_nodes = payload.get("nodes")
        raw_edges = payload.get("edges")
        if not isinstance(raw_nodes, list):
            return _fatal_interpretation("invalid_nodes_array", "$.nodes", "shape", repairable=True)
        if not isinstance(raw_edges, list):
            return _fatal_interpretation("invalid_edges_array", "$.edges", "shape", repairable=True)
        if len(raw_nodes) > MAX_GRAPH_NODES:
            return _fatal_interpretation(
                "node_payload_budget_exceeded", "$.nodes", "budget", repairable=True
            )
        if len(raw_edges) > MAX_GRAPH_EDGES:
            return _fatal_interpretation(
                "edge_payload_budget_exceeded", "$.edges", "budget", repairable=True
            )
        evidence_text = {item.evidence_id: item.text for item in evidence}
        nodes: list[GraphNode] = []
        issues = _unexpected_field_issues(payload, _PAYLOAD_FIELDS, "$")
        if not raw_nodes and not raw_edges and issues:
            fatal_issues = tuple(
                KnowledgeGraphIssue(issue.code, issue.path, "fatal", issue.failure_class)
                for issue in issues
            )
            return GraphInterpretation(
                payload=None,
                lifecycle="failed",
                quality=None,
                issues=fatal_issues,
                counts=GraphDispositionCounts(retained=0, weakened=0, rejected=0),
                repairable=True,
                failure_signature=_failure_signature(fatal_issues),
            )
        repairable = False
        node_ids: set[str] = set()
        nodes_by_evidence: dict[str, int] = {}
        for index, value in enumerate(raw_nodes):
            if isinstance(value, dict):
                issues.extend(_unexpected_field_issues(value, _NODE_FIELDS, f"nodes[{index}]"))
            try:
                node = _node(value, index, evidence_text)
            except _CandidateProblem as problem:
                issues.append(problem.issue)
                repairable = repairable or problem.repairable
                continue
            if node.local_id in node_ids:
                issues.append(
                    KnowledgeGraphIssue(
                        "duplicate_node_id",
                        f"nodes[{index}].id",
                        "rejected",
                        "semantic",
                    )
                )
                continue
            evidence_node_count = nodes_by_evidence.get(node.evidence_id, 0)
            if evidence_node_count >= MAX_GRAPH_NODES_PER_EVIDENCE:
                issues.append(
                    KnowledgeGraphIssue(
                        "node_budget_exceeded",
                        f"nodes[{index}]",
                        "rejected",
                        "budget",
                    )
                )
                continue
            node_ids.add(node.local_id)
            nodes_by_evidence[node.evidence_id] = evidence_node_count + 1
            nodes.append(node)
        node_metadata = {node.local_id: (node.evidence_id, node.node_type) for node in nodes}
        edges: list[GraphEdge] = []
        edges_by_evidence: dict[str, int] = {}
        for index, value in enumerate(raw_edges):
            if isinstance(value, dict):
                issues.extend(_unexpected_field_issues(value, _EDGE_FIELDS, f"edges[{index}]"))
            try:
                edge, issue = _edge(value, index, evidence_text, node_metadata)
            except _CandidateProblem as problem:
                issues.append(problem.issue)
                repairable = repairable or problem.repairable
                continue
            if edge is not None:
                evidence_edge_count = edges_by_evidence.get(edge.evidence_id, 0)
                if evidence_edge_count >= MAX_GRAPH_EDGES_PER_EVIDENCE:
                    issues.append(
                        KnowledgeGraphIssue(
                            "edge_budget_exceeded",
                            f"edges[{index}]",
                            "rejected",
                            "budget",
                        )
                    )
                    continue
                edges_by_evidence[edge.evidence_id] = evidence_edge_count + 1
                edges.append(edge)
            if issue is not None:
                issues.append(issue)
        retained = len(nodes) + len(edges)
        counts = GraphDispositionCounts(
            retained=retained,
            weakened=sum(issue.disposition == "weakened" for issue in issues),
            rejected=sum(issue.disposition == "rejected" for issue in issues),
        )
        if raw_nodes or raw_edges:
            if retained == 0:
                issue_tuple = tuple(issues)
                return GraphInterpretation(
                    payload=None,
                    lifecycle="failed",
                    quality=None,
                    issues=issue_tuple,
                    counts=counts,
                    repairable=repairable,
                    failure_signature=_failure_signature(issue_tuple),
                )
            lifecycle: GraphLifecycle = "completed"
        else:
            lifecycle = "completed_empty"
        graph = GraphPayload(nodes=tuple(nodes), edges=tuple(edges))
        return GraphInterpretation(
            payload=graph,
            lifecycle=lifecycle,
            quality="degraded" if issues else "full",
            issues=tuple(issues),
            counts=counts,
            repairable=False,
        )


def _node(
    value: object,
    index: int,
    evidence_text: dict[str, str],
) -> GraphNode:
    if not isinstance(value, dict):
        raise _problem("invalid_candidate", f"nodes[{index}]", "shape", repairable=True)
    local_id = _string(value, "id", f"nodes[{index}].id", max_chars=MAX_GRAPH_IDENTIFIER_CHARS)
    evidence_id = _string(
        value,
        "evidence_id",
        f"nodes[{index}].evidence_id",
        max_chars=MAX_GRAPH_IDENTIFIER_CHARS,
    )
    node_type = _string(
        value,
        "type",
        f"nodes[{index}].type",
        max_chars=_MAX_NODE_TYPE_CHARS,
    )
    label = _string(value, "label", f"nodes[{index}].label", max_chars=MAX_GRAPH_LABEL_CHARS)
    quote_path = f"nodes[{index}].support_quote"
    quote = _string(
        value,
        "support_quote",
        quote_path,
        code="missing_support_quote",
        failure_class="evidence",
        repairable=True,
        max_chars=MAX_GRAPH_SUPPORT_QUOTE_CHARS,
    )
    if node_type not in KNOWLEDGE_GRAPH_NODE_TYPES:
        raise _problem("unsupported_node_type", f"nodes[{index}].type", "semantic")
    start, end = _support_range(evidence_text, evidence_id, quote, quote_path)
    return GraphNode(
        local_id,
        evidence_id,
        node_type,
        label,
        "model",
        start,
        end,
        "source_anchored",
    )


def _edge(
    value: object,
    index: int,
    evidence_text: dict[str, str],
    node_metadata: dict[str, tuple[str, str]],
) -> tuple[GraphEdge | None, KnowledgeGraphIssue | None]:
    if not isinstance(value, dict):
        raise _problem("invalid_candidate", f"edges[{index}]", "shape", repairable=True)
    evidence_id = _string(
        value,
        "evidence_id",
        f"edges[{index}].evidence_id",
        max_chars=MAX_GRAPH_IDENTIFIER_CHARS,
    )
    source = _string(
        value,
        "source_id",
        f"edges[{index}].source_id",
        max_chars=MAX_GRAPH_IDENTIFIER_CHARS,
    )
    target = _string(
        value,
        "target_id",
        f"edges[{index}].target_id",
        max_chars=MAX_GRAPH_IDENTIFIER_CHARS,
    )
    edge_type = _string(
        value,
        "type",
        f"edges[{index}].type",
        max_chars=MAX_GRAPH_RELATION_LABEL_CHARS,
    )
    quote_path = f"edges[{index}].support_quote"
    quote = _string(
        value,
        "support_quote",
        quote_path,
        code="missing_support_quote",
        failure_class="evidence",
        repairable=True,
        max_chars=MAX_GRAPH_SUPPORT_QUOTE_CHARS,
    )
    start, end = _support_range(evidence_text, evidence_id, quote, quote_path)
    if source == target:
        return (
            None,
            KnowledgeGraphIssue("self_edge", f"edges[{index}]", "rejected", "semantic"),
        )
    source_metadata = node_metadata.get(source)
    target_metadata = node_metadata.get(target)
    if source_metadata is None:
        raise _problem("unknown_source", f"edges[{index}].source_id", "semantic")
    if target_metadata is None:
        raise _problem("unknown_target", f"edges[{index}].target_id", "semantic")
    source_evidence, source_type = source_metadata
    target_evidence, target_type = target_metadata
    if source_evidence != evidence_id or target_evidence != evidence_id:
        raise _problem("cross_evidence_edge", f"edges[{index}]", "semantic")
    normalized_type = re.sub(r"[\s-]+", "_", edge_type.upper())
    canonical_type = (
        normalized_type if normalized_type in KNOWLEDGE_GRAPH_EDGE_TYPES else "RELATED_TO"
    )
    if not _legacy_relation_endpoint_allowed(canonical_type, source_type, target_type):
        return (
            None,
            KnowledgeGraphIssue(
                "invalid_relation_endpoints",
                f"edges[{index}]",
                "rejected",
                "semantic",
            ),
        )
    issue = (
        None
        if canonical_type == normalized_type
        else KnowledgeGraphIssue(
            "unsupported_relationship",
            f"edges[{index}].type",
            "weakened",
            "semantic",
        )
    )
    return (
        GraphEdge(
            evidence_id,
            source,
            target,
            canonical_type,
            0.75,
            "model",
            edge_type,
            start,
            end,
            "ambiguous" if issue is not None else "source_anchored",
        ),
        issue,
    )


def _legacy_relation_endpoint_allowed(
    relation_type: str,
    source_type: str,
    target_type: str,
) -> bool:
    """Keep the compatibility extractor from inventing structural semantics."""
    if relation_type == "PART_OF":
        return (source_type, target_type) == ("entity", "entity")
    if relation_type == "IS_A":
        return source_type in {"entity", "concept"} and target_type == "concept"
    if relation_type == "LOCATED_IN":
        return (source_type, target_type) == ("entity", "entity")
    if relation_type == "REPLACES":
        return source_type == target_type
    return True


def _string(
    value: dict[object, object],
    key: str,
    path: str,
    *,
    code: str = "invalid_scalar",
    failure_class: str = "shape",
    repairable: bool = True,
    max_chars: int,
) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
        raise _problem(code, path, failure_class, repairable=repairable)
    if len(candidate) > max_chars:
        raise _problem("scalar_too_long", path, "budget", repairable=True)
    return candidate


def _support_range(
    evidence_text: dict[str, str],
    evidence_id: str,
    quote: str,
    path: str,
) -> tuple[int, int]:
    text = evidence_text.get(evidence_id)
    if text is None:
        raise _problem(
            "unknown_evidence", path.rsplit(".", 1)[0] + ".evidence_id", "evidence", repairable=True
        )
    start = text.find(quote)
    if start < 0:
        raise _problem("support_quote_not_found", path, "evidence", repairable=True)
    return start, start + len(quote)


class _CandidateProblem(ValueError):
    def __init__(self, issue: KnowledgeGraphIssue, *, repairable: bool) -> None:
        super().__init__(f"{issue.code} at {issue.path}")
        self.issue = issue
        self.repairable = repairable


def _problem(
    code: str,
    path: str,
    failure_class: str,
    *,
    repairable: bool = False,
) -> _CandidateProblem:
    return _CandidateProblem(
        KnowledgeGraphIssue(code, path, "rejected", failure_class),
        repairable=repairable,
    )


def _failure_signature(issues: tuple[KnowledgeGraphIssue, ...]) -> str:
    encoded = json.dumps(
        sorted((issue.code, issue.path, issue.failure_class) for issue in issues),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"kg:{hashlib.sha256(encoded).hexdigest()}"


def _fatal_interpretation(
    code: str,
    path: str,
    failure_class: str,
    *,
    repairable: bool,
) -> GraphInterpretation:
    issues = (KnowledgeGraphIssue(code, path, "fatal", failure_class),)
    return GraphInterpretation(
        payload=None,
        lifecycle="failed",
        quality=None,
        issues=issues,
        counts=GraphDispositionCounts(retained=0, weakened=0, rejected=0),
        repairable=repairable,
        failure_signature=_failure_signature(issues),
    )


def _unexpected_field_issues(
    value: dict[object, object],
    expected: frozenset[str],
    path: str,
) -> list[KnowledgeGraphIssue]:
    if all(key in expected for key in value):
        return []
    return [KnowledgeGraphIssue("unexpected_field", f"{path}.*", "weakened", "shape")]


def _json_nesting_exceeded(content: str) -> bool:
    """Bound container nesting without treating brackets inside JSON strings as structure."""
    depth = 0
    in_string = False
    escaped = False
    for character in content:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_NESTING:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False
