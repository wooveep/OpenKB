"""Shared eligible route inventory for virtual navigation and Portable Wiki export."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from openkb.desktop_knowledge_routes import (
    knowledge_route,
    semantic_slug,
    source_route,
    summary_route,
)


@dataclass(frozen=True)
class RouteKindSpec:
    kind: str
    directory: str
    title: str


ROUTE_KIND_SPECS = (
    RouteKindSpec("summary", "summaries", "Document summaries"),
    RouteKindSpec("procedure", "procedures", "Procedures"),
    RouteKindSpec("concept", "concepts", "Concepts"),
    RouteKindSpec("entity", "entities", "Entities"),
    RouteKindSpec("source", "sources", "Source documents"),
)
KNOWLEDGE_ROUTE_KINDS = ("concept", "entity", "procedure")
ROUTE_INDEX_KINDS = tuple(item.kind for item in ROUTE_KIND_SPECS)


@dataclass(frozen=True)
class DesktopKnowledgeRoute:
    """One collision-safe route visible in the current published read model."""

    route: str
    kind: str
    authority: str
    identity: str
    title: str
    metadata_json: str = "{}"
    snapshot_token: str = ""


def eligible_knowledge_routes_in(
    connection: sqlite3.Connection,
) -> tuple[DesktopKnowledgeRoute, ...]:
    """Return the one eligible inventory shared by navigation and export."""
    primary_candidates = (*_document_routes_in(connection), *_knowledge_routes_in(connection))
    primary_assigned = _assign_unique_routes(
        (
            (f"{item.authority}:{item.kind}:{item.identity}", item.route)
            for item in primary_candidates
        )
    )
    primary = tuple(
        DesktopKnowledgeRoute(
            route=primary_assigned[f"{item.authority}:{item.kind}:{item.identity}"],
            kind=item.kind,
            authority=item.authority,
            identity=item.identity,
            title=item.title,
            metadata_json=item.metadata_json,
            snapshot_token=item.snapshot_token,
        )
        for item in primary_candidates
    )
    source_routes = {
        item.identity: item.route for item in primary if item.authority == "source_document"
    }
    section_candidates = _source_section_routes_in(connection, source_routes)
    section_assigned = _assign_unique_routes(
        (
            (f"{item.authority}:{item.kind}:{item.identity}", item.route)
            for item in section_candidates
        ),
        reserved_routes=frozenset(item.route for item in primary),
    )
    sections = tuple(
        DesktopKnowledgeRoute(
            route=section_assigned[f"{item.authority}:{item.kind}:{item.identity}"],
            kind=item.kind,
            authority=item.authority,
            identity=item.identity,
            title=item.title,
            metadata_json=item.metadata_json,
            snapshot_token=item.snapshot_token,
        )
        for item in section_candidates
    )
    return (*primary, *sections)


def index_route(kind: str | None = None) -> str:
    """Return the deterministic virtual root or per-kind index route."""
    if kind is None:
        return "index"
    return f"{route_kind_spec(kind).directory}/index"


def route_kind_spec(kind: str) -> RouteKindSpec:
    """Resolve the shared directory and title for one semantic route kind."""
    for item in ROUTE_KIND_SPECS:
        if item.kind == kind:
            return item
    raise ValueError(f"Unsupported route index kind: {kind}")


def _document_routes_in(
    connection: sqlite3.Connection,
) -> tuple[DesktopKnowledgeRoute, ...]:
    rows = connection.execute(
        """
        SELECT document_id, display_name
        FROM source_documents
        WHERE availability = 'available'
        ORDER BY display_name, created_at, document_id
        """
    ).fetchall()
    routes: list[DesktopKnowledgeRoute] = []
    for document_id_value, title_value in rows:
        document_id, title = str(document_id_value), str(title_value)
        routes.extend(
            (
                DesktopKnowledgeRoute(
                    summary_route(title, document_id),
                    "summary",
                    "document_summary",
                    document_id,
                    title,
                    "{}",
                    document_id,
                ),
                DesktopKnowledgeRoute(
                    source_route(title, document_id),
                    "source",
                    "source_document",
                    document_id,
                    title,
                    "{}",
                    document_id,
                ),
            )
        )
    return tuple(routes)


def _knowledge_routes_in(
    connection: sqlite3.Connection,
) -> tuple[DesktopKnowledgeRoute, ...]:
    user_rows = connection.execute(
        """
        SELECT pages.page_id, pages.kind, pages.title, revisions.revision_id
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
          ON revisions.revision_id = pages.current_revision_id
        WHERE pages.lifecycle_state = 'stable'
          AND revisions.provenance_state = 'source_backed'
          AND pages.kind IN ('concept', 'entity', 'procedure')
          AND EXISTS (
              SELECT 1
              FROM knowledge_page_revision_sources AS sources
              WHERE sources.revision_id = revisions.revision_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_page_revision_sources AS sources
              WHERE sources.revision_id = revisions.revision_id
                AND NOT EXISTS (
                    SELECT 1
                    FROM evidence_occurrences AS occurrences
                    JOIN source_documents AS documents
                      ON documents.document_id = occurrences.document_id
                    WHERE occurrences.evidence_id = sources.evidence_id
                      AND documents.availability = 'available'
                )
          )
        ORDER BY pages.kind, pages.normalized_title, pages.page_id
        """
    ).fetchall()
    generated_rows = connection.execute(
        """
        SELECT items.item_key, items.kind, items.title, items.generation_id
        FROM knowledge_generation_state AS state
        JOIN knowledge_generations AS generations
          ON generations.generation_id = state.current_generation_id
        JOIN knowledge_generation_items AS items
          ON items.generation_id = state.current_generation_id
        WHERE state.singleton = 1
          AND generations.qualification_state = 'qualified'
          AND items.provenance_state = 'source_backed'
          AND items.kind IN ('concept', 'entity', 'procedure')
          AND EXISTS (
              SELECT 1
              FROM knowledge_generation_item_sources AS sources
              WHERE sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
          )
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_generation_item_sources AS sources
              WHERE sources.generation_id = items.generation_id
                AND sources.item_key = items.item_key
                AND NOT EXISTS (
                    SELECT 1
                    FROM evidence_occurrences AS occurrences
                    JOIN source_documents AS documents
                      ON documents.document_id = occurrences.document_id
                    WHERE occurrences.evidence_id = sources.evidence_id
                      AND documents.availability = 'available'
                )
          )
        ORDER BY items.kind, items.normalized_title, items.item_key
        """
    ).fetchall()
    routes = [
        DesktopKnowledgeRoute(
            knowledge_route(str(kind), "user_revision", str(title), str(identity)),
            str(kind),
            "user_revision",
            str(identity),
            str(title),
            json.dumps({"revision_id": str(revision_id)}, sort_keys=True),
            str(revision_id),
        )
        for identity, kind, title, revision_id in user_rows
    ]
    routes.extend(
        DesktopKnowledgeRoute(
            knowledge_route(str(kind), "published_generation", str(title), str(identity)),
            str(kind),
            "published_generation",
            str(identity),
            str(title),
            json.dumps({"generation_id": int(generation_id)}, sort_keys=True),
            f"{generation_id}:{identity}",
        )
        for identity, kind, title, generation_id in generated_rows
    )
    return tuple(routes)


def _source_section_routes_in(
    connection: sqlite3.Connection,
    source_routes: dict[str, str],
) -> tuple[DesktopKnowledgeRoute, ...]:
    rows = connection.execute(
        """
        SELECT blocks.document_id, blocks.heading_path, MIN(blocks.ordinal)
        FROM document_ir_blocks AS blocks
        JOIN source_documents AS documents ON documents.document_id = blocks.document_id
        WHERE documents.availability = 'available'
          AND trim(blocks.heading_path) NOT IN ('', '[]')
        GROUP BY blocks.document_id, blocks.heading_path
        ORDER BY documents.display_name, blocks.document_id, MIN(blocks.ordinal)
        """
    ).fetchall()
    routes: list[DesktopKnowledgeRoute] = []
    for document_id_value, heading_path_value, _ordinal in rows:
        document_id = str(document_id_value)
        parent_route = source_routes.get(document_id)
        heading_path_json = str(heading_path_value)
        parts = _heading_parts(heading_path_json)
        if parent_route is None or not parts:
            continue
        identity = source_section_identity(document_id, heading_path_json)
        anchor = source_section_anchor(document_id, heading_path_json)
        title = " / ".join(parts)
        routes.append(
            DesktopKnowledgeRoute(
                route=f"{parent_route}/sections/{semantic_slug(parts[-1], identity)}",
                kind="source",
                authority="source_section",
                identity=identity,
                title=title,
                metadata_json=json.dumps(
                    {
                        "anchor": anchor,
                        "document_id": document_id,
                        "heading_path_json": heading_path_json,
                    },
                    sort_keys=True,
                ),
                snapshot_token=f"{document_id}:{identity}",
            )
        )
    return tuple(routes)


def source_section_identity(document_id: str, heading_path_json: str) -> str:
    """Return a stable identity for one logical section in a document version."""
    material = json.dumps(
        [document_id, _heading_parts(heading_path_json)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def source_section_anchor(document_id: str, heading_path_json: str) -> str:
    """Return the Portable Source View anchor shared with the logical route."""
    return f"section-{source_section_identity(document_id, heading_path_json)}"


def _heading_parts(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return ()
    return tuple(" ".join(item.split()) for item in parsed if item.strip())


def _assign_unique_routes(
    candidates: Iterable[tuple[str, str]],
    *,
    reserved_routes: frozenset[str] = frozenset(),
) -> dict[str, str]:
    values = tuple(candidates)
    result: dict[str, str] = {}
    used = {
        *reserved_routes,
        index_route(),
        *(index_route(kind) for kind in ROUTE_INDEX_KINDS),
    }
    for identity, base in sorted(values, key=lambda value: (value[1], value[0])):
        route = base
        if route in used:
            suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            route = f"{base}-{suffix}"
            ordinal = 2
            while route in used:
                route = f"{base}-{suffix}-{ordinal}"
                ordinal += 1
        used.add(route)
        result[identity] = route
    return result
