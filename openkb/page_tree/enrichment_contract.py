"""Bounded request and strict response contract for PageTree enrichment."""

from __future__ import annotations

import json
import sqlite3

PAGE_TREE_ENRICHMENT_SCHEMA = "openkb.page-tree-enrichment.v1"
_MAX_INPUT_NODES = 96
_MAX_INPUT_CHARS = 24_000
_MAX_EVIDENCE_PER_NODE = 2
_MAX_EVIDENCE_CHARS = 320
_MAX_SUMMARY_CHARS = 600


def page_tree_enrichment_request_in(
    connection: sqlite3.Connection,
    base_generation_id: str,
    document_name: str,
) -> tuple[str, frozenset[str]]:
    """Build one bounded request from immutable deterministic PageTree nodes."""
    rows = connection.execute(
        """
        SELECT node_id, parent_node_id, node_order, depth, kind, title
        FROM document_page_tree_nodes
        WHERE generation_id = ? ORDER BY node_order LIMIT ?
        """,
        (base_generation_id, _MAX_INPUT_NODES),
    ).fetchall()
    if not rows:
        raise ValueError("PageTree enrichment requires a deterministic tree.")
    evidence_rows = connection.execute(
        """
        SELECT bindings.node_id, refs.evidence_id, refs.text
        FROM document_page_tree_node_evidence AS bindings
        JOIN evidence_refs AS refs ON refs.evidence_id = bindings.evidence_id
        WHERE bindings.generation_id = ?
        ORDER BY bindings.node_id, bindings.association_order
        """,
        (base_generation_id,),
    ).fetchall()
    evidence_by_node: dict[str, list[dict[str, str]]] = {}
    for node_id, evidence_id, text in evidence_rows:
        values = evidence_by_node.setdefault(str(node_id), [])
        if len(values) < _MAX_EVIDENCE_PER_NODE:
            values.append(
                {
                    "evidence_id": str(evidence_id),
                    "excerpt": str(text)[:_MAX_EVIDENCE_CHARS],
                }
            )
    nodes: list[dict[str, object]] = []
    remaining = _MAX_INPUT_CHARS
    for row in rows:
        node_id = str(row[0])
        value: dict[str, object] = {
            "node_id": node_id,
            "parent_node_id": str(row[1]) if row[1] is not None else None,
            "order": int(row[2]),
            "depth": int(row[3]),
            "kind": str(row[4]),
            "title": str(row[5]),
            "evidence": evidence_by_node.get(node_id, []),
        }
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        if nodes and size > remaining:
            break
        nodes.append(value)
        remaining -= size
    payload = {
        "schema_version": PAGE_TREE_ENRICHMENT_SCHEMA,
        "document_name": document_name,
        "nodes": nodes,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), frozenset(
        str(node["node_id"]) for node in nodes
    )


def parse_page_tree_enrichment_summaries(
    content: str, allowed_node_ids: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    """Accept only summaries for nodes present in the exact request."""
    payload = json.loads(_json_object_text(content))
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "summaries"}:
        raise ValueError("PageTree enrichment returned an unsupported response shape.")
    if payload.get("schema_version") != PAGE_TREE_ENRICHMENT_SCHEMA:
        raise ValueError("PageTree enrichment returned an unsupported schema version.")
    values = payload.get("summaries")
    if not isinstance(values, list) or len(values) > len(allowed_node_ids):
        raise ValueError("PageTree enrichment summaries are invalid.")
    summaries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"node_id", "summary"}:
            raise ValueError("PageTree enrichment summary is invalid.")
        node_id = value.get("node_id")
        summary = value.get("summary")
        if not isinstance(node_id, str) or node_id not in allowed_node_ids or node_id in seen:
            raise ValueError("PageTree enrichment node identity is invalid.")
        if not isinstance(summary, str):
            raise ValueError("PageTree enrichment summary text is invalid.")
        normalized = " ".join(summary.split())
        if not normalized or len(normalized) > _MAX_SUMMARY_CHARS:
            raise ValueError("PageTree enrichment summary text is invalid.")
        seen.add(node_id)
        summaries.append((node_id, normalized))
    return tuple(summaries)


def _json_object_text(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 : -3].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("PageTree enrichment did not return JSON.")
    return stripped[start : end + 1]
