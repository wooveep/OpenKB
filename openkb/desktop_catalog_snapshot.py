"""Build one deterministic corpus Catalog from a consistent SQLite snapshot."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from openkb.desktop_knowledge_inventory import eligible_knowledge_routes_in
from openkb.desktop_knowledge_metadata import decode_knowledge_labels

_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class CatalogNode:
    node_id: str
    parent_node_id: str | None
    order: int
    depth: int
    kind: str
    authority: str
    authority_id: str
    title: str
    normalized_title: str
    search_text: str
    lifecycle_state: str | None
    availability: str | None
    metadata_json: str


@dataclass(frozen=True)
class CatalogSource:
    node_id: str
    source_id: str
    evidence_id: str
    document_id: str
    availability: str
    order: int


@dataclass(frozen=True)
class CatalogRelationshipSource:
    binding_role: str
    source_id: str
    evidence_id: str
    document_id: str
    availability: str


@dataclass(frozen=True)
class CatalogLink:
    from_node_id: str
    to_node_id: str
    source_route: str
    target_route: str
    relation_kind: str
    provenance: str
    lifecycle_eligible: bool
    source_bindings: tuple[CatalogRelationshipSource, ...]
    weight: float = 0.2


@dataclass(frozen=True)
class CatalogSnapshot:
    source_revision: int
    snapshot_digest: str
    nodes: tuple[CatalogNode, ...]
    sources: tuple[CatalogSource, ...]
    links: tuple[CatalogLink, ...]

    @property
    def generation_id(self) -> str:
        value = f"{self.source_revision}:{self.snapshot_digest}".encode()
        return f"catalog-{hashlib.sha256(value).hexdigest()[:24]}"


@dataclass(frozen=True)
class _KnowledgeValue:
    node: CatalogNode
    relative_path: str
    content_markdown: str


def build_catalog_snapshot_in(
    connection: sqlite3.Connection, source_revision: int
) -> CatalogSnapshot:
    """Read the five authority inputs without retaining source Evidence text."""
    occurrence_by_evidence = _preferred_occurrences_in(connection)
    page_values, page_sources = _published_pages_in(connection, occurrence_by_evidence)
    generated_values, generated_sources = _generated_items_in(connection, occurrence_by_evidence)
    document_values, document_sources = _source_documents_in(connection)
    values = tuple((*page_values, *generated_values))
    leaves = tuple(value.node for value in values) + document_values
    nodes = _ordered_nodes(leaves)
    sources = tuple(
        sorted(
            (*page_sources, *generated_sources, *document_sources),
            key=lambda item: (item.node_id, item.order, item.evidence_id),
        )
    )
    inventory = eligible_knowledge_routes_in(connection)
    routes = {(item.authority, item.kind, item.identity): item.route for item in inventory}
    links = _knowledge_links(values, nodes, sources, routes)
    digest = _snapshot_digest(nodes, sources, links)
    return CatalogSnapshot(source_revision, digest, nodes, sources, links)


def _published_pages_in(
    connection: sqlite3.Connection,
    occurrences: dict[str, tuple[str, str]],
) -> tuple[tuple[_KnowledgeValue, ...], tuple[CatalogSource, ...]]:
    rows = connection.execute(
        """
        SELECT pages.page_id, pages.kind, pages.title, pages.normalized_title,
            pages.lifecycle_state, pages.stale_after, revisions.revision_id,
            revisions.revision_number, revisions.content_markdown,
            revisions.provenance_state, verifications.actor, verifications.verified_at
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        LEFT JOIN knowledge_page_verifications AS verifications
            ON verifications.revision_id = revisions.revision_id
            AND verifications.invalidated_at IS NULL
        WHERE pages.lifecycle_state IN ('stable', 'deprecated')
        ORDER BY pages.kind, pages.normalized_title, pages.page_id
        """
    ).fetchall()
    values: list[_KnowledgeValue] = []
    revision_nodes: dict[str, str] = {}
    for row in rows:
        page_id, kind, revision_id = str(row[0]), str(row[1]), str(row[6])
        node_id = f"page:{page_id}"
        metadata = {
            "revision_id": revision_id,
            "revision_number": int(row[7]),
            "provenance": str(row[9]),
            "stale_after": str(row[5]) if row[5] is not None else None,
            "verified_by": str(row[10]) if row[10] is not None else None,
            "verified_at": str(row[11]) if row[11] is not None else None,
        }
        values.append(
            _KnowledgeValue(
                node=CatalogNode(
                    node_id=node_id,
                    parent_node_id=_group_id(kind),
                    order=0,
                    depth=2,
                    kind=kind,
                    authority="user_revision",
                    authority_id=page_id,
                    title=str(row[2]),
                    normalized_title=str(row[3]),
                    search_text=_search_text(str(row[2]), kind),
                    lifecycle_state=str(row[4]),
                    availability=None,
                    metadata_json=_json(metadata),
                ),
                relative_path=f"{kind}/{page_id}.md",
                content_markdown=str(row[8]),
            )
        )
        revision_nodes[revision_id] = node_id
    source_rows = connection.execute(
        """
        SELECT sources.revision_id, sources.source_id, sources.evidence_id
        FROM knowledge_page_revision_sources AS sources
        JOIN knowledge_pages AS pages ON pages.current_revision_id = sources.revision_id
        WHERE pages.lifecycle_state IN ('stable', 'deprecated')
        ORDER BY sources.revision_id, sources.source_id, sources.claim_text
        """
    ).fetchall()
    sources = _mapped_sources(source_rows, revision_nodes, occurrences)
    return tuple(values), sources


def _generated_items_in(
    connection: sqlite3.Connection,
    occurrences: dict[str, tuple[str, str]],
) -> tuple[tuple[_KnowledgeValue, ...], tuple[CatalogSource, ...]]:
    rows = connection.execute(
        """
        SELECT state.current_generation_id, items.item_key, items.kind, items.title,
            items.normalized_title, items.content_markdown, items.source_document_id,
            items.provenance_state, items.entity_subtype, items.aliases_json, items.tags_json,
            items.analysis_provenance_json
        FROM knowledge_generation_state AS state
        JOIN knowledge_generation_items AS items
            ON items.generation_id = state.current_generation_id
        WHERE state.singleton = 1
        ORDER BY items.kind, items.normalized_title, items.item_key
        """
    ).fetchall()
    values: list[_KnowledgeValue] = []
    item_nodes: dict[str, str] = {}
    generation_id: int | None = None
    for row in rows:
        generation_id, item_key, kind = int(row[0]), str(row[1]), str(row[2])
        node_id = f"generated:{item_key}"
        aliases = decode_knowledge_labels(row[9])
        tags = decode_knowledge_labels(row[10])
        metadata = {
            "generation_id": generation_id,
            "origin_document_id": str(row[6]),
            "provenance": str(row[7]),
            "entity_subtype": str(row[8]) if row[8] is not None else None,
            "aliases": list(aliases),
            "tags": list(tags),
            "analysis_provenance": _json_object(row[11]),
        }
        values.append(
            _KnowledgeValue(
                node=CatalogNode(
                    node_id=node_id,
                    parent_node_id=_group_id(kind),
                    order=0,
                    depth=2,
                    kind=kind,
                    authority="published_generation",
                    authority_id=item_key,
                    title=str(row[3]),
                    normalized_title=str(row[4]),
                    search_text=_search_text(str(row[3]), kind, *aliases, *tags),
                    lifecycle_state="stable",
                    availability=None,
                    metadata_json=_json(metadata),
                ),
                relative_path=f"generated/{kind}/{item_key}.md",
                content_markdown=str(row[5]),
            )
        )
        item_nodes[item_key] = node_id
    if generation_id is None:
        return tuple(values), ()
    source_rows = connection.execute(
        """
        SELECT sources.item_key, sources.source_id, sources.evidence_id
        FROM knowledge_generation_item_sources AS sources
        WHERE sources.generation_id = ?
        ORDER BY sources.item_key, sources.source_id, sources.claim_text
        """,
        (generation_id,),
    ).fetchall()
    return tuple(values), _mapped_sources(source_rows, item_nodes, occurrences)


def _source_documents_in(
    connection: sqlite3.Connection,
) -> tuple[tuple[CatalogNode, ...], tuple[CatalogSource, ...]]:
    rows = connection.execute(
        """
        SELECT document_id, display_name, source_format, availability, asset_sha256, created_at
        FROM source_documents ORDER BY display_name, document_id
        """
    ).fetchall()
    nodes: list[CatalogNode] = []
    for row in rows:
        document_id = str(row[0])
        nodes.append(
            CatalogNode(
                node_id=f"document:{document_id}",
                parent_node_id="catalog:documents",
                order=0,
                depth=2,
                kind="source_document",
                authority="source_document",
                authority_id=document_id,
                title=str(row[1]),
                normalized_title=_normalize(str(row[1])),
                search_text=_search_text(str(row[1]), str(row[2])),
                lifecycle_state=None,
                availability=str(row[3]),
                metadata_json=_json(
                    {
                        "source_format": str(row[2]),
                        "asset_sha256": str(row[4]),
                        "created_at": str(row[5]),
                    }
                ),
            )
        )
    source_rows = connection.execute(
        """
        SELECT occurrences.document_id, occurrences.evidence_id, documents.availability,
            MIN(occurrences.ordinal) AS first_ordinal
        FROM evidence_occurrences AS occurrences
        JOIN source_documents AS documents ON documents.document_id = occurrences.document_id
        GROUP BY occurrences.document_id, occurrences.evidence_id, documents.availability
        ORDER BY occurrences.document_id, first_ordinal, occurrences.evidence_id
        """
    ).fetchall()
    sources = tuple(
        CatalogSource(
            node_id=f"document:{row[0]}",
            source_id=f"document:{row[1]}",
            evidence_id=str(row[1]),
            document_id=str(row[0]),
            availability=str(row[2]),
            order=int(row[3]),
        )
        for row in source_rows
    )
    return tuple(nodes), sources


def _preferred_occurrences_in(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT evidence_id, document_id, availability FROM (
            SELECT occurrences.evidence_id, occurrences.document_id, documents.availability,
                ROW_NUMBER() OVER (
                    PARTITION BY occurrences.evidence_id
                    ORDER BY (documents.availability = 'available') DESC,
                        documents.created_at, documents.document_id, occurrences.ordinal
                ) AS occurrence_rank
            FROM evidence_occurrences AS occurrences
            JOIN source_documents AS documents ON documents.document_id = occurrences.document_id
        ) WHERE occurrence_rank = 1
        """
    ).fetchall()
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def _mapped_sources(
    rows: list[tuple[object, ...]],
    node_ids: dict[str, str],
    occurrences: dict[str, tuple[str, str]],
) -> tuple[CatalogSource, ...]:
    values: list[CatalogSource] = []
    seen: set[tuple[str, str]] = set()
    order_by_node: defaultdict[str, int] = defaultdict(int)
    for owner_id, source_id, evidence_id in rows:
        node_id = node_ids.get(str(owner_id))
        occurrence = occurrences.get(str(evidence_id))
        key = node_id or "", str(evidence_id)
        if node_id is None or occurrence is None or key in seen:
            continue
        seen.add(key)
        values.append(
            CatalogSource(
                node_id=node_id,
                source_id=str(source_id),
                evidence_id=str(evidence_id),
                document_id=occurrence[0],
                availability=occurrence[1],
                order=order_by_node[node_id],
            )
        )
        order_by_node[node_id] += 1
    return tuple(values)


def _ordered_nodes(leaves: tuple[CatalogNode, ...]) -> tuple[CatalogNode, ...]:
    fixed = (
        CatalogNode(
            "catalog:root",
            None,
            0,
            0,
            "root",
            "system",
            "root",
            "OpenKB Catalog",
            "openkb catalog",
            "openkb catalog",
            None,
            None,
            "{}",
        ),
        CatalogNode(
            "catalog:concepts",
            "catalog:root",
            1,
            1,
            "group",
            "system",
            "concepts",
            "Concepts",
            "concepts",
            "concepts",
            None,
            None,
            "{}",
        ),
        CatalogNode(
            "catalog:entities",
            "catalog:root",
            2,
            1,
            "group",
            "system",
            "entities",
            "Entities",
            "entities",
            "entities",
            None,
            None,
            "{}",
        ),
        CatalogNode(
            "catalog:procedures",
            "catalog:root",
            3,
            1,
            "group",
            "system",
            "procedures",
            "Procedures",
            "procedures",
            "procedures",
            None,
            None,
            "{}",
        ),
        CatalogNode(
            "catalog:documents",
            "catalog:root",
            4,
            1,
            "group",
            "system",
            "documents",
            "Source Documents",
            "source documents",
            "source documents",
            None,
            None,
            "{}",
        ),
    )
    ordered = sorted(
        leaves,
        key=lambda item: (
            {"concept": 0, "entity": 1, "procedure": 2, "source_document": 3}[item.kind],
            item.normalized_title,
            item.node_id,
        ),
    )
    return fixed + tuple(
        CatalogNode(**{**node.__dict__, "order": index})
        for index, node in enumerate(ordered, start=len(fixed))
    )


def _knowledge_links(
    values: tuple[_KnowledgeValue, ...],
    nodes: tuple[CatalogNode, ...],
    sources: tuple[CatalogSource, ...],
    routes: dict[tuple[str, str, str], str],
) -> tuple[CatalogLink, ...]:
    node_by_path = {value.relative_path: value.node.node_id for value in values}
    node_by_id = {node.node_id: node for node in nodes}
    sources_by_node: defaultdict[str, list[CatalogSource]] = defaultdict(list)
    for source in sources:
        if source.availability == "available":
            sources_by_node[source.node_id].append(source)
    links: dict[tuple[str, str, str], CatalogLink] = {}

    for value in values:
        source_route_value = _catalog_route(value.node, routes)
        source_bindings = sources_by_node[value.node.node_id]
        if source_route_value is None or not source_bindings:
            continue
        by_document: defaultdict[str, list[CatalogSource]] = defaultdict(list)
        for source in source_bindings:
            by_document[source.document_id].append(source)
        for document_id, bindings in sorted(by_document.items()):
            target_node = node_by_id.get(f"document:{document_id}")
            target_route_value = (
                _catalog_route(target_node, routes) if target_node is not None else None
            )
            if target_node is None or target_route_value is None:
                continue
            relationship_sources = tuple(
                _relationship_source("supporting", source) for source in bindings
            )
            link = CatalogLink(
                from_node_id=value.node.node_id,
                to_node_id=target_node.node_id,
                source_route=source_route_value,
                target_route=target_route_value,
                relation_kind="supported_by",
                provenance="knowledge_source_binding",
                lifecycle_eligible=True,
                source_bindings=relationship_sources,
                weight=0.25,
            )
            links[(link.from_node_id, link.to_node_id, link.relation_kind)] = link

        for match in _MARKDOWN_LINK.finditer(value.content_markdown):
            target = _resolved_link(value.relative_path, match.group(1))
            target_node_id = node_by_path.get(target) if target is not None else None
            target_node = node_by_id.get(target_node_id or "")
            target_route_value = (
                _catalog_route(target_node, routes) if target_node is not None else None
            )
            target_bindings = sources_by_node[target_node_id or ""]
            if (
                target_node is None
                or target_node.node_id == value.node.node_id
                or target_route_value is None
                or not target_bindings
            ):
                continue
            relationship_sources = tuple(
                sorted(
                    (
                        *(_relationship_source("source", source) for source in source_bindings),
                        *(_relationship_source("target", source) for source in target_bindings),
                    ),
                    key=lambda item: (
                        item.binding_role,
                        item.document_id,
                        item.evidence_id,
                        item.source_id,
                    ),
                )
            )
            link = CatalogLink(
                from_node_id=value.node.node_id,
                to_node_id=target_node.node_id,
                source_route=source_route_value,
                target_route=target_route_value,
                relation_kind="references",
                provenance="published_markdown_with_source_bindings",
                lifecycle_eligible=True,
                source_bindings=relationship_sources,
            )
            links[(link.from_node_id, link.to_node_id, link.relation_kind)] = link
    return tuple(links[key] for key in sorted(links))


def _catalog_route(
    node: CatalogNode | None,
    routes: dict[tuple[str, str, str], str],
) -> str | None:
    if node is None:
        return None
    kind = "source" if node.kind == "source_document" else node.kind
    return routes.get((node.authority, kind, node.authority_id))


def _relationship_source(binding_role: str, source: CatalogSource) -> CatalogRelationshipSource:
    return CatalogRelationshipSource(
        binding_role=binding_role,
        source_id=source.source_id,
        evidence_id=source.evidence_id,
        document_id=source.document_id,
        availability=source.availability,
    )


def _resolved_link(source_path: str, raw_target: str) -> str | None:
    target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = unquote(parsed.path)
    if path.startswith("/"):
        normalized = posixpath.normpath(path.lstrip("/"))
    else:
        normalized = posixpath.normpath(
            str(PurePosixPath(source_path).parent / PurePosixPath(path))
        )
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _snapshot_digest(
    nodes: tuple[CatalogNode, ...],
    sources: tuple[CatalogSource, ...],
    links: tuple[CatalogLink, ...],
) -> str:
    payload = {
        "nodes": [node.__dict__ for node in nodes],
        "sources": [source.__dict__ for source in sources],
        "links": [asdict(link) for link in links],
    }
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _group_id(kind: str) -> str:
    return {
        "concept": "catalog:concepts",
        "entity": "catalog:entities",
        "procedure": "catalog:procedures",
    }[kind]


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _search_text(*values: str) -> str:
    return _normalize(" ".join(value for value in values if value))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return dict(payload) if isinstance(payload, dict) else None
