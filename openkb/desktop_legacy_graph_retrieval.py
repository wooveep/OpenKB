"""Bounded retrieval from graph rows that still qualify for legacy fallback."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from openkb.desktop_retrieval_rows import placeholders

RowFetcher = Callable[[str, tuple[object, ...]], list[tuple[object, ...]]]

_MAX_HOPS = 2
_MAX_ROOTS = 12
_MAX_EXPANDED_NODES = 32
_MAX_EVIDENCE = 8


def legacy_graph_evidence_ids_in(
    *,
    terms: tuple[str, ...],
    anchor_evidence_ids: tuple[str, ...],
    fetch_rows: RowFetcher,
    allowed_document_ids: frozenset[str] | None = None,
    result_ids: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Traverse only evidence whose available documents have no admitted identities."""
    if allowed_document_ids == frozenset():
        return ()
    if result_ids == ():
        return ()
    anchors = tuple(dict.fromkeys(anchor_evidence_ids))[:_MAX_ROOTS]
    conditions: list[str] = []
    parameters: list[object] = []
    if anchors:
        conditions.append(f"nodes.evidence_id IN ({placeholders(anchors)})")
        parameters.extend(anchors)
    for term in terms:
        conditions.append("instr(nodes.normalized_label, ?) > 0")
        parameters.append(term)
    if not conditions:
        return ()
    node_scope, node_scope_parameters = _evidence_scope("nodes", allowed_document_ids)
    node_generation_scope, node_generation_parameters = _generation_scope("nodes", result_ids)
    root_rows = fetch_rows(
        f"""
        SELECT nodes.node_id, nodes.evidence_id, nodes.normalized_label
        FROM {_graph_source("nodes", result_ids)} AS nodes
        WHERE ({" OR ".join(conditions)})
          {node_generation_scope}
          AND {node_scope}
        ORDER BY nodes.evidence_id, nodes.node_id
        LIMIT ?
        """,
        (*parameters, *node_generation_parameters, *node_scope_parameters, _MAX_ROOTS),
    )
    if not root_rows:
        return ()

    root_ids = [str(row[0]) for row in root_rows]
    evidence_ids: list[str] = []
    _append_unique(evidence_ids, (str(row[1]) for row in root_rows))
    labels = tuple(dict.fromkeys(str(row[2]) for row in root_rows))
    if labels:
        equivalent_rows = fetch_rows(
            f"""
            SELECT nodes.node_id, nodes.evidence_id
            FROM {_graph_source("nodes", result_ids)} AS nodes
            WHERE nodes.normalized_label IN ({placeholders(labels)})
              {node_generation_scope}
              AND {node_scope}
            ORDER BY nodes.evidence_id, nodes.node_id
            LIMIT ?
            """,
            (*labels, *node_generation_parameters, *node_scope_parameters, _MAX_ROOTS),
        )
        _append_unique(root_ids, (str(row[0]) for row in equivalent_rows))
        _append_unique(evidence_ids, (str(row[1]) for row in equivalent_rows))

    root_ids = root_ids[:_MAX_EXPANDED_NODES]
    seen_nodes = set(root_ids)
    frontier = list(root_ids)
    for _hop in range(_MAX_HOPS):
        if not frontier or len(seen_nodes) >= _MAX_EXPANDED_NODES:
            break
        edge_scope, edge_scope_parameters = _evidence_scope("edges", allowed_document_ids)
        edge_generation_scope, edge_generation_parameters = _generation_scope("edges", result_ids)
        edge_rows = fetch_rows(
            f"""
            SELECT edges.evidence_id, edges.source_node_id, edges.target_node_id
            FROM {_graph_source("edges", result_ids)} AS edges
            WHERE (edges.source_node_id IN ({placeholders(tuple(frontier))})
                OR edges.target_node_id IN ({placeholders(tuple(frontier))}))
              {edge_generation_scope}
              AND {edge_scope}
            ORDER BY edges.edge_id
            LIMIT ?
            """,
            (
                *frontier,
                *frontier,
                *edge_generation_parameters,
                *edge_scope_parameters,
                _MAX_EXPANDED_NODES - len(seen_nodes),
            ),
        )
        _append_unique(evidence_ids, (str(row[0]) for row in edge_rows))
        next_ids: list[str] = []
        for _evidence_id, source_id, target_id in edge_rows:
            for node_id in (str(source_id), str(target_id)):
                if node_id not in seen_nodes:
                    seen_nodes.add(node_id)
                    next_ids.append(node_id)
                    if len(seen_nodes) == _MAX_EXPANDED_NODES:
                        break
            if len(seen_nodes) == _MAX_EXPANDED_NODES:
                break
        if not next_ids:
            break
        node_rows = fetch_rows(
            f"""
            SELECT nodes.node_id, nodes.evidence_id
            FROM {_graph_source("nodes", result_ids)} AS nodes
            WHERE nodes.node_id IN ({placeholders(tuple(next_ids))})
              {node_generation_scope}
              AND {node_scope}
            ORDER BY nodes.node_id
            """,
            (*next_ids, *node_generation_parameters, *node_scope_parameters),
        )
        _append_unique(evidence_ids, (str(row[1]) for row in node_rows))
        frontier = next_ids
    return tuple(evidence_ids[:_MAX_EVIDENCE])


def _generation_scope(
    table_alias: str, result_ids: tuple[str, ...] | None
) -> tuple[str, tuple[object, ...]]:
    if table_alias not in {"nodes", "edges"}:
        raise ValueError("Unsupported legacy graph table alias.")
    if result_ids is None:
        return "", ()
    membership_table = (
        "knowledge_graph_result_nodes" if table_alias == "nodes" else "knowledge_graph_result_edges"
    )
    identity_column = "node_id" if table_alias == "nodes" else "edge_id"
    return (
        f"AND EXISTS (SELECT 1 FROM {membership_table} AS pinned_memberships "
        f"WHERE pinned_memberships.{identity_column} = {table_alias}.{identity_column} "
        f"AND pinned_memberships.result_id IN ({placeholders(result_ids)}))",
        result_ids,
    )


def _graph_source(table_alias: str, result_ids: tuple[str, ...] | None) -> str:
    if table_alias not in {"nodes", "edges"}:
        raise ValueError("Unsupported legacy graph table alias.")
    prefix = "knowledge_graph" if result_ids is not None else "current_knowledge_graph"
    return f"{prefix}_{table_alias}"


def _evidence_scope(
    table_alias: str, allowed_document_ids: frozenset[str] | None
) -> tuple[str, tuple[object, ...]]:
    if table_alias not in {"nodes", "edges"}:
        raise ValueError("Unsupported legacy graph table alias.")
    allowed = tuple(sorted(allowed_document_ids or ()))
    document_scope = (
        f"AND legacy_occurrences.document_id IN ({placeholders(allowed)})"
        if allowed_document_ids is not None and allowed
        else ("AND 0" if allowed_document_ids is not None else "")
    )
    return (
        f"""
        EXISTS (
            SELECT 1
            FROM evidence_occurrences AS legacy_occurrences
            JOIN source_documents AS legacy_documents
              ON legacy_documents.document_id = legacy_occurrences.document_id
            WHERE legacy_occurrences.evidence_id = {table_alias}.evidence_id
              AND legacy_documents.availability = 'available'
              {document_scope}
              AND NOT EXISTS (
                  SELECT 1
                  FROM knowledge_document_candidates AS admitted_candidates
                  WHERE admitted_candidates.document_id = legacy_occurrences.document_id
                    AND admitted_candidates.admission_state = 'admitted'
              )
        )
    """,
        allowed,
    )


def _append_unique(values: list[str], incoming: Iterable[str]) -> None:
    for value in incoming:
        if value not in values:
            values.append(value)
