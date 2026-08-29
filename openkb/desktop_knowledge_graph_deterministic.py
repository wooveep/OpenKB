"""Provider-free fallback graph construction from published Evidence."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from openkb.desktop_knowledge_graph_store import GraphEdge, GraphNode, GraphPayload

_MAX_CLAIM_CHARS = 900
_ENTITY_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*){0,2}\b")
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u3400-\u9fff]{2,}")
_NON_ENTITY_WORDS = frozenset(("The", "This", "That", "These", "Those", "Document"))


class DeterministicGraphEvidence(Protocol):
    @property
    def evidence_id(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def section(self) -> str: ...


def deterministic_graph_payload(
    evidence: Sequence[DeterministicGraphEvidence],
) -> GraphPayload:
    """Create the bounded local fallback without invoking a model."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for ordinal, item in enumerate(evidence):
        claim_id = f"deterministic-claim-{ordinal}"
        concept_id = f"deterministic-concept-{ordinal}"
        nodes.extend(
            (
                GraphNode(
                    claim_id,
                    item.evidence_id,
                    "claim",
                    _claim_label(item.text),
                    "deterministic",
                ),
                GraphNode(
                    concept_id,
                    item.evidence_id,
                    "concept",
                    item.section or "Document",
                    "deterministic",
                ),
            )
        )
        edges.append(
            GraphEdge(
                item.evidence_id,
                concept_id,
                claim_id,
                "SUPPORTS",
                0.8,
                "deterministic",
            )
        )
        for entity_ordinal, label in enumerate(_entity_labels(item.text), start=1):
            entity_id = f"deterministic-entity-{ordinal}-{entity_ordinal}"
            nodes.append(GraphNode(entity_id, item.evidence_id, "entity", label, "deterministic"))
            edges.extend(
                (
                    GraphEdge(
                        item.evidence_id,
                        entity_id,
                        concept_id,
                        "RELATED_TO",
                        0.7,
                        "deterministic",
                    ),
                    GraphEdge(
                        item.evidence_id,
                        entity_id,
                        claim_id,
                        "SUPPORTS",
                        0.7,
                        "deterministic",
                    ),
                )
            )
    return GraphPayload(nodes=tuple(nodes), edges=tuple(edges))


def _entity_labels(text: str) -> tuple[str, ...]:
    labels: list[str] = []
    for match in _ENTITY_PATTERN.finditer(text):
        label = match.group(0).strip()
        if label not in _NON_ENTITY_WORDS:
            _append_unique(labels, (label,))
        if len(labels) == 2:
            return tuple(labels)
    for match in _WORD_PATTERN.finditer(text):
        label = match.group(0).strip()
        if label.casefold() not in {"the", "this", "that", "document"}:
            _append_unique(labels, (label,))
        if len(labels) == 2:
            break
    return tuple(labels)


def _claim_label(text: str) -> str:
    normalized = " ".join(text.split())
    return normalized[:_MAX_CLAIM_CHARS] or "Evidence claim"


def _append_unique(values: list[str], incoming: tuple[str, ...]) -> None:
    for value in incoming:
        if value and value not in values:
            values.append(value)
