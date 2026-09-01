"""Eligible route discovery and bounded read selection for Knowledge Navigation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_knowledge_inventory import (
    ROUTE_INDEX_KINDS,
    DesktopKnowledgeRoute,
    index_route,
    route_kind_spec,
)

NAVIGATION_MAX_LINK_HOPS = 1


@dataclass(frozen=True)
class _ReadDescriptor:
    score: int
    hop: int
    descriptor_kind: str
    authority: str
    authority_id: str
    kind: str
    title: str
    metadata_json: str
    route: str
    snapshot_token: str


def _unique_ranked_descriptors(
    descriptors: tuple[_ReadDescriptor, ...],
) -> tuple[_ReadDescriptor, ...]:
    selected: list[_ReadDescriptor] = []
    seen_routes: set[str] = set()
    for item in sorted(descriptors, key=_descriptor_sort_key):
        if item.route in seen_routes:
            continue
        seen_routes.add(item.route)
        selected.append(item)
    return tuple(selected)


def _unique_preserving_descriptors(
    descriptors: tuple[_ReadDescriptor, ...],
) -> tuple[_ReadDescriptor, ...]:
    selected: list[_ReadDescriptor] = []
    seen_routes: set[str] = set()
    for item in descriptors:
        if item.route in seen_routes:
            continue
        seen_routes.add(item.route)
        selected.append(item)
    return tuple(selected)


def _phase_diverse_route_descriptors(
    descriptors: tuple[_ReadDescriptor, ...],
) -> tuple[_ReadDescriptor, ...]:
    """Expose one source route per document phase before adjacent detail routes."""
    selected: list[_ReadDescriptor] = []
    deferred: list[_ReadDescriptor] = []
    seen_phases: set[tuple[str, ...]] = set()
    for item in descriptors:
        phase = _source_phase_key(item)
        if phase is not None and phase in seen_phases:
            deferred.append(item)
            continue
        if phase is not None:
            seen_phases.add(phase)
        selected.append(item)
    return tuple((*selected, *deferred))


def _source_phase_key(item: _ReadDescriptor) -> tuple[str, ...] | None:
    if item.authority != "source_section":
        return None
    try:
        metadata = json.loads(item.metadata_json)
        path = json.loads(metadata["heading_path_json"])
        document_id = metadata["document_id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return (item.authority_id,)
    if not isinstance(document_id, str) or not isinstance(path, list):
        return (item.authority_id,)
    normalized = tuple(
        " ".join(part.split()).casefold() for part in path[:2] if isinstance(part, str)
    )
    return (document_id, *normalized) if normalized else (item.authority_id,)


def _index_descriptors(
    inventory: tuple[DesktopKnowledgeRoute, ...],
) -> tuple[_ReadDescriptor, ...]:
    digest = hashlib.sha256("\n".join(item.route for item in inventory).encode("utf-8")).hexdigest()
    present = {item.kind for item in inventory}
    kinds = ("root", *(kind for kind in ROUTE_INDEX_KINDS if kind in present))
    return tuple(
        _ReadDescriptor(
            score=0,
            hop=0,
            descriptor_kind="index",
            authority="navigation_index",
            authority_id=kind,
            kind="index",
            title=("Knowledge index" if kind == "root" else route_kind_spec(kind).title),
            metadata_json="{}",
            route=index_route(None if kind == "root" else kind),
            snapshot_token=digest,
        )
        for kind in kinds
    )


def _inventory_descriptor(
    item: DesktopKnowledgeRoute,
    terms: tuple[str, ...],
) -> _ReadDescriptor:
    matches = sum(term in item.title.casefold() for term in terms)
    base_score = (
        85
        if item.authority == "source_section"
        else 55
        if item.kind == "summary"
        else 45
        if item.kind == "source"
        else 70
    )
    return _ReadDescriptor(
        score=base_score + matches * 8 - _route_scope_penalty(item.title, terms),
        hop=0,
        descriptor_kind=(item.kind if item.kind in {"summary", "source"} else "catalog"),
        authority=item.authority,
        authority_id=item.identity,
        kind=item.kind,
        title=item.title,
        metadata_json=item.metadata_json,
        route=item.route,
        snapshot_token=item.snapshot_token,
    )


def _route_scope_penalty(title: str, terms: tuple[str, ...]) -> int:
    """Keep unrequested lifecycle branches behind the requested base procedure."""
    normalized = title.casefold()
    markers = (
        "扩容",
        "缩容",
        "运维",
        "故障",
        "恢复",
        "升级",
        "附录",
        "faq",
        "maintenance",
        "recovery",
        "upgrade",
        "troubleshoot",
    )
    unmatched = sum(
        marker in normalized and not any(marker in term for term in terms) for marker in markers
    )
    return min(24, unmatched * 12)


def _select_read_descriptors(
    descriptors: tuple[_ReadDescriptor, ...],
    *,
    max_reads: int,
    excluded_routes: frozenset[str],
    requested_routes: tuple[str, ...] = (),
    requested_only: bool = False,
) -> tuple[_ReadDescriptor, ...]:
    """Select deterministic seed reads or exactly the routes requested by one round."""
    if max_reads <= 0:
        return ()
    unique_descriptors = _unique_ranked_descriptors(descriptors)
    by_route = {item.route: item for item in unique_descriptors}
    requested = [
        by_route[route]
        for route in dict.fromkeys(requested_routes)
        if route in by_route and route not in excluded_routes
    ][:max_reads]
    if requested_only:
        return tuple(requested)
    requested_ids = {item.route for item in requested}
    ranked = list(
        _phase_diverse_route_descriptors(
            tuple(
                sorted(
                    (
                        item
                        for item in unique_descriptors
                        if item.route not in excluded_routes and item.route not in requested_ids
                    ),
                    key=_descriptor_sort_key,
                )
            )
        )
    )
    selected = [*requested, *ranked[: max_reads - len(requested)]]
    summary_target = min(2, max(1, max_reads // 4))
    selected_routes = {item.route for item in selected}
    summary_count = sum(item.descriptor_kind == "summary" for item in selected)
    missing_summaries = [
        item
        for item in ranked
        if item.descriptor_kind == "summary" and item.route not in selected_routes
    ][: max(0, summary_target - summary_count)]
    replaceable = [
        index
        for index in range(len(selected) - 1, -1, -1)
        if selected[index].route not in requested_ids
        and selected[index].descriptor_kind != "summary"
    ]
    for summary in missing_summaries:
        if len(selected) < max_reads:
            selected.append(summary)
        elif replaceable:
            selected[replaceable.pop(0)] = summary
    selected_routes = {item.route for item in selected}
    source_outline_target = summary_target if max_reads >= 6 else 0
    source_outline_count = sum(item.authority == "source_document" for item in selected)
    missing_source_outlines = [
        item
        for item in ranked
        if item.authority == "source_document" and item.route not in selected_routes
    ][: max(0, source_outline_target - source_outline_count)]
    replaceable = [
        index
        for index in range(len(selected) - 1, -1, -1)
        if selected[index].route not in requested_ids
        and selected[index].descriptor_kind != "summary"
        and selected[index].authority != "source_document"
    ]
    for source in missing_source_outlines:
        if len(selected) < max_reads:
            selected.append(source)
        elif replaceable:
            selected[replaceable.pop(0)] = source
    return tuple(sorted(selected, key=_descriptor_sort_key))


def _catalog_descriptors_in(
    connection: sqlite3.Connection,
    generation_id: str,
    terms: tuple[str, ...],
    inventory: tuple[DesktopKnowledgeRoute, ...],
) -> tuple[_ReadDescriptor, ...]:
    if not terms:
        return ()
    score_expression = " + ".join(
        "CASE WHEN instr(nodes.search_text, ?) > 0 THEN 1 ELSE 0 END" for _term in terms
    )
    qualification = """
        (
            nodes.authority = 'user_revision'
            AND json_extract(nodes.metadata_json, '$.provenance') = 'source_backed'
        ) OR (
            nodes.authority = 'published_generation'
            AND EXISTS (
                SELECT 1 FROM knowledge_generations AS generations
                WHERE generations.generation_id = CAST(
                    json_extract(nodes.metadata_json, '$.generation_id') AS INTEGER
                ) AND generations.qualification_state = 'qualified'
            )
        )
    """
    target_qualification = qualification.replace("nodes.", "targets.")
    rows = connection.execute(
        f"""
        WITH scored AS (
            SELECT nodes.node_id, nodes.kind, nodes.authority, nodes.authority_id,
                nodes.title, nodes.metadata_json, nodes.normalized_title,
                ({score_expression}) AS match_score
            FROM knowledge_catalog_nodes AS nodes
            WHERE nodes.generation_id = ?
                AND nodes.kind IN ('concept', 'entity', 'procedure')
                AND COALESCE(nodes.lifecycle_state, 'stable') != 'deprecated'
                AND ({qualification})
        ), direct AS (
            SELECT *, 0 AS hop FROM scored WHERE match_score > 0
        ), routed AS (
            SELECT * FROM direct
            UNION ALL
            SELECT targets.node_id, targets.kind, targets.authority,
                targets.authority_id, targets.title, targets.metadata_json,
                targets.normalized_title, direct.match_score, 1 AS hop
            FROM direct
            JOIN knowledge_catalog_relationships AS links
                ON links.generation_id = ?
                AND links.source_node_id = direct.node_id
                AND links.lifecycle_eligible = 1
            JOIN knowledge_catalog_nodes AS targets
                ON targets.generation_id = links.generation_id
                AND targets.node_id = links.target_node_id
                AND targets.kind IN ('concept', 'entity', 'procedure')
                AND COALESCE(targets.lifecycle_state, 'stable') != 'deprecated'
                AND ({target_qualification})
        )
        SELECT node_id, kind, authority, authority_id, title, metadata_json,
            match_score, hop
        FROM routed
        ORDER BY match_score DESC, hop, normalized_title, node_id
        LIMIT 24
        """,
        (*terms, generation_id, generation_id),
    ).fetchall()
    inventory_by_identity = {(item.authority, item.kind, item.identity): item for item in inventory}
    descriptors: list[_ReadDescriptor] = []
    seen: set[str] = set()
    for row in rows:
        node_id = str(row[0])
        if node_id in seen:
            continue
        seen.add(node_id)
        kind, authority, authority_id = str(row[1]), str(row[2]), str(row[3])
        inventory_item = inventory_by_identity.get((authority, kind, authority_id))
        if inventory_item is None:
            continue
        metadata_json = str(row[5])
        hop = min(int(row[7]), NAVIGATION_MAX_LINK_HOPS)
        descriptors.append(
            _ReadDescriptor(
                score=100 + int(row[6]) * 10 - hop * 20,
                hop=hop,
                descriptor_kind="catalog",
                authority=authority,
                authority_id=authority_id,
                kind=kind,
                title=str(row[4]),
                metadata_json=metadata_json,
                route=inventory_item.route,
                snapshot_token=metadata_json,
            )
        )
    return tuple(descriptors)


def _summary_descriptors_in(
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    baseline_evidence: tuple[DesktopEvidenceRef, ...],
    inventory: tuple[DesktopKnowledgeRoute, ...],
) -> tuple[_ReadDescriptor, ...]:
    baseline_documents = {reference.document_id for reference in baseline_evidence}
    rows = connection.execute(
        """
        SELECT summaries.document_id, documents.display_name, summaries.updated_at,
            GROUP_CONCAT(units.unit_text, ' ')
        FROM document_summaries AS summaries
        JOIN source_documents AS documents ON documents.document_id = summaries.document_id
        JOIN document_summary_units AS units ON units.document_id = summaries.document_id
        WHERE summaries.provenance_state = 'source_backed'
            AND documents.availability = 'available'
        GROUP BY summaries.document_id, documents.display_name, summaries.updated_at
        ORDER BY documents.display_name, summaries.document_id
        """
    ).fetchall()
    routes = {
        item.identity: item.route
        for item in inventory
        if item.authority == "document_summary" and item.kind == "summary"
    }
    descriptors: list[_ReadDescriptor] = []
    for row in rows:
        document_id, title, updated_at = str(row[0]), str(row[1]), str(row[2])
        search_text = f"{title} {row[3] or ''}".casefold()
        term_score = sum(1 for term in terms if term in search_text)
        baseline_bonus = 3 if document_id in baseline_documents else 0
        if term_score == 0 and baseline_bonus == 0:
            continue
        descriptors.append(
            _ReadDescriptor(
                score=60 + term_score * 8 + baseline_bonus,
                hop=0,
                descriptor_kind="summary",
                authority="document_summary",
                authority_id=document_id,
                kind="summary",
                title=title,
                metadata_json="{}",
                route=routes[document_id],
                snapshot_token=updated_at,
            )
        )
    return tuple(descriptors)


def _descriptor_sort_key(item: _ReadDescriptor) -> tuple[int, int, str, str]:
    return (-item.score, item.hop, item.route, item.authority_id)
