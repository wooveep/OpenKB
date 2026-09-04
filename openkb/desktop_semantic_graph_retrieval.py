"""Bounded traversal from canonical Knowledge Identities back to EvidenceRefs."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from openkb.desktop_retrieval_rows import placeholders as sqlite_placeholders

RowFetcher = Callable[[str, tuple[object, ...]], list[tuple[object, ...]]]

_MAX_ROOTS = 12
_MAX_ITEMS = 32
_MAX_EVIDENCE = 24
_MAX_HOPS = 2


def semantic_graph_evidence_ids_in(
    connection: sqlite3.Connection,
    *,
    terms: tuple[str, ...],
    anchor_evidence_ids: tuple[str, ...],
    fetch_rows: RowFetcher,
    allowed_document_ids: frozenset[str] | None = None,
    generation_id: int | None = None,
    use_current_generation: bool = True,
) -> tuple[str, ...] | None:
    """Return ``None`` when no semantic identity root matched, enabling legacy fallback."""
    if use_current_generation:
        generation = fetch_rows(
            "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1",
            (),
        )
        if not generation or generation[0][0] is None:
            return None
        generation_value = generation[0][0]
        if isinstance(generation_value, bool) or not isinstance(generation_value, int):
            return None
        generation_id = generation_value
    elif generation_id is None:
        return None
    conditions: list[str] = []
    item_scope, scope_parameters = _item_scope(allowed_document_ids)
    parameters: list[object] = [generation_id, *scope_parameters]
    anchors = tuple(dict.fromkeys(anchor_evidence_ids))[:_MAX_ROOTS]
    if anchors:
        conditions.append(
            "EXISTS (SELECT 1 FROM knowledge_generation_item_sources AS anchored_sources "
            "WHERE anchored_sources.generation_id = items.generation_id "
            "AND anchored_sources.item_key = items.item_key "
            f"AND anchored_sources.evidence_id IN ({sqlite_placeholders(anchors)}))"
        )
        parameters.extend(anchors)
    for term in terms:
        conditions.append(
            "(instr(items.normalized_title, ?) > 0 OR EXISTS ("
            "SELECT 1 FROM json_each(items.aliases_json) AS aliases "
            "WHERE instr(lower(CAST(aliases.value AS TEXT)), ?) > 0))"
        )
        parameters.extend((term, term))
    if not conditions:
        return None
    root_rows = fetch_rows(
        f"""
        SELECT items.item_key
        FROM knowledge_generation_items AS items
        WHERE items.generation_id = ? {item_scope}
          AND ({" OR ".join(conditions)})
        ORDER BY items.kind, items.normalized_title, items.item_key
        LIMIT ?
        """,
        (*parameters, _MAX_ROOTS),
    )
    if not root_rows:
        return None

    item_ids = [str(row[0]) for row in root_rows]
    seen = set(item_ids)
    frontier = list(item_ids)
    evidence_ids: list[str] = []
    _append_item_evidence(
        fetch_rows,
        generation_id,
        item_ids,
        evidence_ids,
        allowed_document_ids=allowed_document_ids,
    )
    for _hop in range(_MAX_HOPS):
        if not frontier or len(seen) >= _MAX_ITEMS:
            break
        placeholders = sqlite_placeholders(tuple(frontier))
        evidence_scope, evidence_scope_parameters = _evidence_scope(
            "sources.evidence_id", allowed_document_ids
        )
        edge_rows = fetch_rows(
            f"""
            SELECT relationships.source_item_key, relationships.target_item_key,
                sources.binding_role, sources.evidence_id
            FROM knowledge_generation_relationships AS relationships
            JOIN knowledge_generation_relationship_sources AS sources
              ON sources.generation_id = relationships.generation_id
             AND sources.source_item_key = relationships.source_item_key
             AND sources.target_item_key = relationships.target_item_key
             AND sources.relation_kind = relationships.relation_kind
            WHERE relationships.generation_id = ?
              AND (relationships.source_item_key IN ({placeholders})
                   OR relationships.target_item_key IN ({placeholders}))
              {evidence_scope}
            ORDER BY CASE sources.binding_role WHEN 'assertion' THEN 0
                     WHEN 'target' THEN 1 ELSE 2 END,
                relationships.source_item_key, relationships.target_item_key,
                relationships.relation_kind, sources.evidence_id
            LIMIT ?
            """,
            (
                generation_id,
                *frontier,
                *frontier,
                *evidence_scope_parameters,
                (_MAX_ITEMS - len(seen)) * 16,
            ),
        )
        next_items: list[str] = []
        for source_item_key, target_item_key, _role, evidence_id in edge_rows:
            _append_available_evidence(
                fetch_rows,
                str(evidence_id),
                evidence_ids,
                allowed_document_ids=allowed_document_ids,
            )
            for item_key in (str(source_item_key), str(target_item_key)):
                if item_key not in seen:
                    seen.add(item_key)
                    next_items.append(item_key)
                    if len(seen) >= _MAX_ITEMS:
                        break
            if len(seen) >= _MAX_ITEMS:
                break
        if not next_items:
            break
        _append_item_evidence(
            fetch_rows,
            generation_id,
            next_items,
            evidence_ids,
            allowed_document_ids=allowed_document_ids,
        )
        frontier = next_items
    return tuple(evidence_ids[:_MAX_EVIDENCE])


def _append_item_evidence(
    fetch_rows: RowFetcher,
    generation_id: int,
    item_ids: list[str],
    evidence_ids: list[str],
    *,
    allowed_document_ids: frozenset[str] | None = None,
) -> None:
    if not item_ids:
        return
    evidence_scope, scope_parameters = _evidence_scope("sources.evidence_id", allowed_document_ids)
    rows = fetch_rows(
        f"""
        SELECT sources.evidence_id
        FROM knowledge_generation_item_sources AS sources
        WHERE sources.generation_id = ?
          AND sources.item_key IN ({sqlite_placeholders(tuple(item_ids))})
          {evidence_scope}
        ORDER BY sources.item_key, sources.evidence_id
        """,
        (generation_id, *item_ids, *scope_parameters),
    )
    for row in rows:
        _append_available_evidence(
            fetch_rows,
            str(row[0]),
            evidence_ids,
            allowed_document_ids=allowed_document_ids,
        )


def _append_available_evidence(
    fetch_rows: RowFetcher,
    evidence_id: str,
    values: list[str],
    *,
    allowed_document_ids: frozenset[str] | None = None,
) -> None:
    if evidence_id in values or len(values) >= _MAX_EVIDENCE:
        return
    document_scope, scope_parameters = _document_scope(allowed_document_ids)
    available = fetch_rows(
        f"""
        SELECT 1 FROM evidence_occurrences AS occurrences
        JOIN source_documents AS documents
          ON documents.document_id = occurrences.document_id
        WHERE occurrences.evidence_id = ? AND documents.availability = 'available'
          {document_scope}
        LIMIT 1
        """,
        (evidence_id, *scope_parameters),
    )
    if available:
        values.append(evidence_id)


def _item_scope(
    allowed_document_ids: frozenset[str] | None,
) -> tuple[str, tuple[object, ...]]:
    if allowed_document_ids is None:
        return "", ()
    allowed = tuple(sorted(allowed_document_ids))
    if not allowed:
        return "AND 0", ()
    return (
        "AND EXISTS (SELECT 1 FROM knowledge_generation_item_sources AS scoped_sources "
        "JOIN evidence_occurrences AS scoped_occurrences "
        "ON scoped_occurrences.evidence_id = scoped_sources.evidence_id "
        "JOIN source_documents AS scoped_documents "
        "ON scoped_documents.document_id = scoped_occurrences.document_id "
        "WHERE scoped_sources.generation_id = items.generation_id "
        "AND scoped_sources.item_key = items.item_key "
        "AND scoped_documents.availability = 'available' "
        f"AND scoped_occurrences.document_id IN ({sqlite_placeholders(allowed)}))",
        allowed,
    )


def _evidence_scope(
    evidence_expression: str, allowed_document_ids: frozenset[str] | None
) -> tuple[str, tuple[object, ...]]:
    if allowed_document_ids is None:
        return "", ()
    allowed = tuple(sorted(allowed_document_ids))
    if not allowed:
        return "AND 0", ()
    return (
        "AND EXISTS (SELECT 1 FROM evidence_occurrences AS scoped_occurrences "
        "JOIN source_documents AS scoped_documents "
        "ON scoped_documents.document_id = scoped_occurrences.document_id "
        f"WHERE scoped_occurrences.evidence_id = {evidence_expression} "
        "AND scoped_documents.availability = 'available' "
        f"AND scoped_occurrences.document_id IN ({sqlite_placeholders(allowed)}))",
        allowed,
    )


def _document_scope(
    allowed_document_ids: frozenset[str] | None,
) -> tuple[str, tuple[object, ...]]:
    if allowed_document_ids is None:
        return "", ()
    allowed = tuple(sorted(allowed_document_ids))
    if not allowed:
        return "AND 0", ()
    return (
        f"AND occurrences.document_id IN ({sqlite_placeholders(allowed)})",
        allowed,
    )
