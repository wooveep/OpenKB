"""Render one immutable, reader-friendly wiki from a pinned SQLite snapshot."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import posixpath
import re
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from openkb.knowledge.analysis.inventory import (
    ROUTE_INDEX_KINDS,
    DesktopKnowledgeRoute,
    eligible_knowledge_routes_in,
    index_route,
    route_kind_spec,
    source_section_anchor,
)
from openkb.knowledge.export.portable_wiki_preview import portable_wiki_snapshot_in
from openkb.knowledge.export.portable_wiki_validation import (
    portable_wiki_snapshot_id,
    validate_portable_wiki,
)
from openkb.knowledge.pages.metadata import decode_knowledge_labels
from openkb.knowledge.pages.relationships import generation_relationships_in
from openkb.locks import atomic_write_text
from openkb.retrieval.navigation.routes import knowledge_route

_ANCHOR_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class PortableWikiExport:
    source_image_count: int


@dataclass(frozen=True)
class _Document:
    document_id: str
    title: str
    source_format: str
    source_route: str
    summary_route: str


@dataclass(frozen=True)
class _EvidenceLocation:
    evidence_id: str
    document_id: str


@dataclass(frozen=True)
class _KnowledgePage:
    route: str
    kind: str
    authority: str
    identity: str
    title: str
    content_markdown: str
    provenance_state: str
    lifecycle_state: str
    evidence_ids: tuple[str, ...]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RelatedKnowledge:
    title: str
    route: str


@dataclass(frozen=True)
class _RouteEntry:
    route: str
    path: str
    kind: str
    authority: str
    identity: str
    title: str
    anchor: str | None = None


@dataclass(frozen=True)
class _SourceImage:
    source_image_id: str
    document_id: str
    image_sha256: str
    media_type: str
    storage_path: str
    display_name: str
    alt_text: str | None


def render_portable_wiki_in(
    connection: sqlite3.Connection, kb_dir: Path, staging: Path
) -> PortableWikiExport:
    """Materialize a generic Markdown wiki without copying complete Raw Assets."""
    inventory = eligible_knowledge_routes_in(connection)
    documents = _documents_in(connection, inventory)
    evidence_locations = _evidence_locations_in(connection)
    images = _source_images_in(connection)
    image_resources = _copy_source_images(kb_dir, staging, images)
    user_pages = _user_pages_in(connection)
    generated_pages = _generated_pages_in(connection)
    pages = _assign_page_routes((*user_pages, *generated_pages), inventory)
    related = _related_knowledge_in(connection, inventory)
    source_paths = {document.document_id: f"{document.source_route}.md" for document in documents}

    content_routes = tuple(
        _inventory_route_entry(item, source_paths=source_paths) for item in inventory
    )
    routes = tuple(
        sorted(
            (*content_routes, *_index_route_entries(content_routes)),
            key=lambda entry: (entry.route, entry.identity),
        )
    )
    for document in documents:
        _write_source_page(
            connection,
            staging,
            document,
            images=tuple(image for image in images if image.document_id == document.document_id),
            image_resources=image_resources,
        )
        _write_summary_page(
            connection,
            staging,
            document,
            evidence_locations=evidence_locations,
            source_paths=source_paths,
        )
    for page in pages:
        _write_knowledge_page(
            staging,
            page,
            evidence_locations=evidence_locations,
            source_paths=source_paths,
            related=related.get(page.identity, ())
            if page.authority == "published_generation"
            else (),
        )

    atomic_write_text(staging / "index.md", _index_markdown(content_routes))
    for kind in ROUTE_INDEX_KINDS:
        entries = tuple(entry for entry in content_routes if entry.kind == kind)
        if entries:
            atomic_write_text(
                staging / f"{index_route(kind)}.md",
                _kind_index_markdown(kind, entries),
            )
    checksums = _checksums_in(staging)
    snapshot = portable_wiki_snapshot_in(connection)
    manifest = {
        "format": "openkb-portable-wiki-v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "snapshot": snapshot,
        "snapshot_id": portable_wiki_snapshot_id(snapshot),
        "routes": [asdict(entry) for entry in routes],
        "aliases": _aliases(pages),
        "source_images": [
            {
                "source_image_id": image.source_image_id,
                "document_id": image.document_id,
                "display_name": image.display_name,
                "media_type": image.media_type,
                "alt_text": image.alt_text,
                "resource": image_resources[image.source_image_id],
                "sha256": image.image_sha256,
            }
            for image in images
        ],
        "checksums": checksums,
    }
    atomic_write_text(
        staging / "wiki-manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    validate_portable_wiki(staging)
    return PortableWikiExport(source_image_count=len(set(image_resources.values())))


def _documents_in(
    connection: sqlite3.Connection,
    inventory: tuple[DesktopKnowledgeRoute, ...],
) -> tuple[_Document, ...]:
    rows = connection.execute(
        """
        SELECT document_id, display_name, source_format
        FROM source_documents
        WHERE availability = 'available'
        ORDER BY display_name, created_at, document_id
        """
    ).fetchall()
    routes = {(item.kind, item.identity): item.route for item in inventory}
    return tuple(
        _Document(
            document_id=str(row[0]),
            title=str(row[1]),
            source_format=str(row[2]),
            source_route=routes[("source", str(row[0]))],
            summary_route=routes[("summary", str(row[0]))],
        )
        for row in rows
    )


def _evidence_locations_in(
    connection: sqlite3.Connection,
) -> dict[str, _EvidenceLocation]:
    rows = connection.execute(
        """
        SELECT occurrences.evidence_id, occurrences.document_id
        FROM evidence_occurrences AS occurrences
        JOIN source_documents AS documents
          ON documents.document_id = occurrences.document_id
        WHERE documents.availability = 'available'
        ORDER BY occurrences.evidence_id, documents.created_at,
            occurrences.document_id, occurrences.ordinal
        """
    ).fetchall()
    locations: dict[str, _EvidenceLocation] = {}
    for row in rows:
        evidence_id = str(row[0])
        locations.setdefault(
            evidence_id,
            _EvidenceLocation(evidence_id, str(row[1])),
        )
    return locations


def _user_pages_in(connection: sqlite3.Connection) -> tuple[_KnowledgePage, ...]:
    rows = connection.execute(
        """
        SELECT pages.page_id, pages.kind, pages.title, revisions.content_markdown,
            revisions.provenance_state, pages.lifecycle_state, revisions.revision_id
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
          ON revisions.revision_id = pages.current_revision_id
        WHERE pages.lifecycle_state = 'stable'
          AND revisions.provenance_state = 'source_backed'
        ORDER BY pages.kind, pages.normalized_title, pages.page_id
        """
    ).fetchall()
    evidence_by_revision = _user_evidence_in(connection)
    return tuple(
        _KnowledgePage(
            route=knowledge_route(str(row[1]), "user_revision", str(row[2]), str(row[0])),
            kind=str(row[1]),
            authority="user_revision",
            identity=str(row[0]),
            title=str(row[2]),
            content_markdown=str(row[3]),
            provenance_state=str(row[4]),
            lifecycle_state=str(row[5]),
            evidence_ids=evidence_by_revision.get(str(row[6]), ()),
        )
        for row in rows
    )


def _user_evidence_in(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    rows = connection.execute(
        """
        SELECT sources.revision_id, sources.evidence_id
        FROM knowledge_page_revision_sources AS sources
        JOIN knowledge_pages AS pages ON pages.current_revision_id = sources.revision_id
        ORDER BY sources.revision_id, sources.source_id, sources.claim_text
        """
    ).fetchall()
    return _group_evidence(rows)


def _generated_pages_in(connection: sqlite3.Connection) -> tuple[_KnowledgePage, ...]:
    snapshot = connection.execute(
        """
        SELECT state.current_generation_id, generations.qualification_state
        FROM knowledge_generation_state AS state
        JOIN knowledge_generations AS generations
          ON generations.generation_id = state.current_generation_id
        WHERE state.singleton = 1
        """
    ).fetchone()
    if snapshot is None or str(snapshot[1]) != "qualified":
        return ()
    generation_id = int(snapshot[0])
    rows = connection.execute(
        """
        SELECT item_key, kind, title, content_markdown, provenance_state, aliases_json
        FROM knowledge_generation_items
        WHERE generation_id = ? AND provenance_state = 'source_backed'
        ORDER BY kind, normalized_title, item_key
        """,
        (generation_id,),
    ).fetchall()
    evidence = _generated_evidence_in(connection, generation_id)
    return tuple(
        _KnowledgePage(
            route=knowledge_route(str(row[1]), "published_generation", str(row[2]), str(row[0])),
            kind=str(row[1]),
            authority="published_generation",
            identity=str(row[0]),
            title=str(row[2]),
            content_markdown=str(row[3]),
            provenance_state=str(row[4]),
            lifecycle_state="stable",
            evidence_ids=evidence.get(str(row[0]), ()),
            aliases=decode_knowledge_labels(row[5]),
        )
        for row in rows
    )


def _generated_evidence_in(
    connection: sqlite3.Connection, generation_id: int
) -> dict[str, tuple[str, ...]]:
    rows = connection.execute(
        """
        SELECT item_key, evidence_id
        FROM knowledge_generation_item_sources
        WHERE generation_id = ?
        ORDER BY item_key, source_id, claim_text
        """,
        (generation_id,),
    ).fetchall()
    return _group_evidence(rows)


def _group_evidence(rows: list[tuple[object, ...]]) -> dict[str, tuple[str, ...]]:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for identity_value, evidence_value in rows:
        evidence_id = str(evidence_value)
        if evidence_id not in grouped[str(identity_value)]:
            grouped[str(identity_value)].append(evidence_id)
    return {identity: tuple(values) for identity, values in grouped.items()}


def _assign_page_routes(
    pages: tuple[_KnowledgePage, ...],
    inventory: tuple[DesktopKnowledgeRoute, ...],
) -> tuple[_KnowledgePage, ...]:
    assigned = {(item.authority, item.kind, item.identity): item.route for item in inventory}
    return tuple(
        _KnowledgePage(
            route=assigned[(page.authority, page.kind, page.identity)],
            kind=page.kind,
            authority=page.authority,
            identity=page.identity,
            title=page.title,
            content_markdown=page.content_markdown,
            provenance_state=page.provenance_state,
            lifecycle_state=page.lifecycle_state,
            evidence_ids=page.evidence_ids,
            aliases=page.aliases,
        )
        for page in pages
        if (page.authority, page.kind, page.identity) in assigned
    )


def _related_knowledge_in(
    connection: sqlite3.Connection,
    inventory: tuple[DesktopKnowledgeRoute, ...],
) -> dict[str, tuple[_RelatedKnowledge, ...]]:
    snapshot = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    if snapshot is None:
        return {}
    routes = {
        (item.kind, item.identity): item.route
        for item in inventory
        if item.authority == "published_generation"
    }
    grouped: defaultdict[str, list[_RelatedKnowledge]] = defaultdict(list)
    for relationship in generation_relationships_in(connection, int(snapshot[0])):
        target_route = routes.get((relationship.target_kind, relationship.target_item_key))
        if target_route is None:
            continue
        grouped[relationship.source_item_key].append(
            _RelatedKnowledge(relationship.target_title, target_route)
        )
    return {identity: tuple(values) for identity, values in grouped.items()}


def _write_knowledge_page(
    staging: Path,
    page: _KnowledgePage,
    *,
    evidence_locations: dict[str, _EvidenceLocation],
    source_paths: dict[str, str],
    related: tuple[_RelatedKnowledge, ...],
) -> None:
    path = staging / f"{page.route}.md"
    authority = (
        "Qualified generated knowledge"
        if page.authority == "published_generation"
        else "User-maintained knowledge"
    )
    content = (
        f"# {page.title}\n\n"
        f"> {authority} · {page.lifecycle_state} · {page.provenance_state}\n\n"
        f"{page.content_markdown.strip()}"
    )
    if related:
        page_path = f"{page.route}.md"
        links = "\n".join(
            f"- [{relation.title}]({_relative_page_link(page_path, relation.route)})"
            for relation in related
        )
        content = f"{content}\n\n## Related knowledge\n\n{links}"
    content = _append_sources(
        content,
        page.evidence_ids,
        page_path=f"{page.route}.md",
        evidence_locations=evidence_locations,
        source_paths=source_paths,
    )
    _write_markdown(path, content)


def _relative_page_link(page_path: str, target_route: str) -> str:
    return posixpath.relpath(f"{target_route}.md", posixpath.dirname(page_path))


def _write_summary_page(
    connection: sqlite3.Connection,
    staging: Path,
    document: _Document,
    *,
    evidence_locations: dict[str, _EvidenceLocation],
    source_paths: dict[str, str],
) -> None:
    rows = connection.execute(
        """
        SELECT units.unit_ordinal, units.label, units.unit_text, sources.evidence_id
        FROM document_summary_units AS units
        JOIN document_summary_unit_sources AS sources
          ON sources.document_id = units.document_id
         AND sources.unit_ordinal = units.unit_ordinal
        WHERE units.document_id = ?
        ORDER BY units.unit_ordinal, sources.evidence_id
        """,
        (document.document_id,),
    ).fetchall()
    page_path = f"{document.summary_route}.md"
    if rows:
        units: defaultdict[int, list[tuple[object, ...]]] = defaultdict(list)
        for row in rows:
            units[int(row[0])].append(row)
        lines = [f"# {document.title}", "", "> Document Summary · source-backed"]
        evidence_ids: list[str] = []
        for _ordinal, values in sorted(units.items()):
            label = str(values[0][1])
            lines.extend(("", f"## {label}", "", f"- {values[0][2]}"))
            evidence_ids.extend(str(value[3]) for value in values)
    else:
        lines, evidence_ids = _structural_summary_in(connection, document)
    content = _append_sources(
        "\n".join(lines),
        tuple(dict.fromkeys(evidence_ids)),
        page_path=page_path,
        evidence_locations=evidence_locations,
        source_paths=source_paths,
    )
    _write_markdown(staging / page_path, content)


def _structural_summary_in(
    connection: sqlite3.Connection, document: _Document
) -> tuple[list[str], list[str]]:
    rows = connection.execute(
        """
        SELECT blocks.heading_path, occurrences.evidence_id
        FROM document_ir_blocks AS blocks
        LEFT JOIN evidence_occurrences AS occurrences
          ON occurrences.document_id = blocks.document_id
         AND occurrences.block_id = blocks.block_id
        WHERE blocks.document_id = ?
        ORDER BY blocks.ordinal
        """,
        (document.document_id,),
    ).fetchall()
    sections: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    evidence_ids: list[str] = []
    fallback_evidence_id: str | None = None
    for heading_json, evidence_value in rows:
        if fallback_evidence_id is None and evidence_value is not None:
            fallback_evidence_id = str(evidence_value)
        heading = _heading_text(str(heading_json))
        if heading and heading not in seen:
            seen.add(heading)
            sections.append((heading, str(evidence_value) if evidence_value is not None else None))
            if evidence_value is not None:
                evidence_ids.append(str(evidence_value))
    lines = [f"# {document.title}", "", "> Document Summary · structural fallback", ""]
    lines.extend(("## Document structure", ""))
    lines.extend(f"- {heading}" for heading, _evidence_id in sections)
    if not sections:
        lines.append("- No section headings were retained in DocumentIR.")
        if fallback_evidence_id is not None:
            evidence_ids.append(fallback_evidence_id)
    return lines, evidence_ids


def _write_source_page(
    connection: sqlite3.Connection,
    staging: Path,
    document: _Document,
    *,
    images: tuple[_SourceImage, ...],
    image_resources: dict[str, str],
) -> None:
    rows = connection.execute(
        """
        SELECT blocks.ordinal, blocks.kind, blocks.text, blocks.heading_path,
            occurrences.evidence_id
        FROM document_ir_blocks AS blocks
        LEFT JOIN evidence_occurrences AS occurrences
          ON occurrences.document_id = blocks.document_id
         AND occurrences.block_id = blocks.block_id
        WHERE blocks.document_id = ? ORDER BY blocks.ordinal
        """,
        (document.document_id,),
    ).fetchall()
    lines = [
        "---",
        f"document_id: {json.dumps(document.document_id, ensure_ascii=False)}",
        f"source_format: {json.dumps(document.source_format, ensure_ascii=False)}",
        'availability: "available"',
        "---",
        "",
        f"# {document.title}",
    ]
    anchored: set[str] = set()
    anchored_sections: set[str] = set()
    for row in rows:
        kind, value = str(row[1]), str(row[2]).strip()
        if not value:
            continue
        section_anchor = source_section_anchor(document.document_id, str(row[3]))
        if str(row[3]).strip() not in {"", "[]"} and section_anchor not in anchored_sections:
            anchored_sections.add(section_anchor)
            lines.extend(("", f'<a id="{section_anchor}"></a>'))
        evidence_id = str(row[4]) if row[4] is not None else None
        if evidence_id is not None and evidence_id not in anchored:
            anchored.add(evidence_id)
            lines.extend(("", f'<a id="{_evidence_anchor(evidence_id)}"></a>'))
        lines.extend(("", _render_block(kind, value, str(row[3]))))
    if images:
        lines.extend(("", "## Images", ""))
        for image in images:
            resource = image_resources[image.source_image_id]
            relative = posixpath.relpath(resource, posixpath.dirname(f"{document.source_route}.md"))
            alt = image.alt_text or image.display_name
            lines.append(f"![{alt}]({relative})")
    _write_markdown(staging / f"{document.source_route}.md", "\n".join(lines))


def _render_block(kind: str, value: str, heading_path_json: str) -> str:
    if kind != "code":
        value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"Image: \1", value)
    if kind == "heading":
        level = min(6, max(2, len(_heading_parts(heading_path_json)) + 1))
        return f"{'#' * level} {value}"
    if kind == "code":
        fence = "````" if "```" in value else "```"
        return f"{fence}\n{value}\n{fence}"
    if kind == "figure":
        return f"> {value or 'Image: source figure'}"
    return value


def _append_sources(
    content: str,
    evidence_ids: tuple[str, ...],
    *,
    page_path: str,
    evidence_locations: dict[str, _EvidenceLocation],
    source_paths: dict[str, str],
) -> str:
    lines: list[str] = []
    for evidence_id in evidence_ids:
        location = evidence_locations.get(evidence_id)
        if location is None or location.document_id not in source_paths:
            continue
        relative = posixpath.relpath(
            source_paths[location.document_id], posixpath.dirname(page_path)
        )
        lines.append(f"- [{evidence_id[:12]}]({relative}#{_evidence_anchor(evidence_id)})")
    if not lines:
        return content.rstrip() + "\n"
    return f"{content.rstrip()}\n\n## Sources\n\n" + "\n".join(dict.fromkeys(lines)) + "\n"


def _source_images_in(connection: sqlite3.Connection) -> tuple[_SourceImage, ...]:
    rows = connection.execute(
        """
        SELECT images.source_image_id, images.document_id, images.image_sha256,
            images.media_type, images.storage_path, images.display_name, images.alt_text
        FROM source_images AS images
        JOIN source_documents AS documents ON documents.document_id = images.document_id
        WHERE documents.availability = 'available'
        ORDER BY images.document_id, images.ordinal, images.source_image_id
        """
    ).fetchall()
    return tuple(
        _SourceImage(
            source_image_id=str(row[0]),
            document_id=str(row[1]),
            image_sha256=str(row[2]),
            media_type=str(row[3]),
            storage_path=str(row[4]),
            display_name=str(row[5]),
            alt_text=str(row[6]) if row[6] is not None else None,
        )
        for row in rows
    )


def _copy_source_images(
    kb_dir: Path, staging: Path, images: tuple[_SourceImage, ...]
) -> dict[str, str]:
    resources: dict[str, str] = {}
    for image in images:
        source = _stored_file(kb_dir, image.storage_path)
        if not source.is_file():
            raise ValueError(f"Portable Wiki source image is missing: {image.display_name}")
        suffix = source.suffix.lower() or ".bin"
        relative = (Path("images") / f"{image.image_sha256}{suffix}").as_posix()
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        resources[image.source_image_id] = relative
    return resources


def _aliases(pages: tuple[_KnowledgePage, ...]) -> list[dict[str, str]]:
    return sorted(
        (
            {"alias": alias, "identity": page.identity, "route": page.route}
            for page in pages
            for alias in page.aliases
        ),
        key=lambda value: (value["alias"].casefold(), value["route"]),
    )


def _index_markdown(routes: tuple[_RouteEntry, ...]) -> str:
    lines = [
        "# OpenKB Portable Wiki",
        "",
        "This immutable export mirrors OpenKB's semantic Knowledge Navigation routes.",
    ]
    present = {entry.kind for entry in routes}
    for kind in ROUTE_INDEX_KINDS:
        if kind not in present:
            continue
        spec = route_kind_spec(kind)
        lines.extend(("", f"- [{spec.title}]({index_route(kind)}.md)"))
    return "\n".join(lines).rstrip() + "\n"


def _kind_index_markdown(kind: str, routes: tuple[_RouteEntry, ...]) -> str:
    heading = route_kind_spec(kind).title
    page_path = f"{index_route(kind)}.md"
    lines = [f"# {heading}", "", "Current eligible routes in this snapshot.", ""]
    for entry in routes:
        relative = posixpath.relpath(entry.path, posixpath.dirname(page_path))
        if entry.anchor is not None:
            relative = f"{relative}#{entry.anchor}"
        lines.append(f"- [{entry.title}]({relative})")
    return "\n".join(lines).rstrip() + "\n"


def _index_route_entries(routes: tuple[_RouteEntry, ...]) -> tuple[_RouteEntry, ...]:
    entries = [
        _route_entry(
            index_route(),
            kind="index",
            authority="navigation_index",
            identity="index:root",
            title="OpenKB Portable Wiki",
        )
    ]
    present = {entry.kind for entry in routes}
    entries.extend(
        _route_entry(
            index_route(kind),
            kind="index",
            authority="navigation_index",
            identity=f"index:{kind}",
            title=route_kind_spec(kind).title,
        )
        for kind in ROUTE_INDEX_KINDS
        if kind in present
    )
    return tuple(entries)


def _route_entry(
    route: str,
    *,
    kind: str,
    authority: str,
    identity: str,
    title: str,
    path: str | None = None,
    anchor: str | None = None,
) -> _RouteEntry:
    return _RouteEntry(route, path or f"{route}.md", kind, authority, identity, title, anchor)


def _inventory_route_entry(
    item: DesktopKnowledgeRoute, *, source_paths: dict[str, str]
) -> _RouteEntry:
    if item.authority != "source_section":
        return _route_entry(
            item.route,
            kind=item.kind,
            authority=item.authority,
            identity=item.identity,
            title=item.title,
        )
    try:
        metadata = json.loads(item.metadata_json)
    except json.JSONDecodeError as error:
        raise ValueError("Source section route metadata is invalid.") from error
    document_id = metadata.get("document_id") if isinstance(metadata, dict) else None
    anchor = metadata.get("anchor") if isinstance(metadata, dict) else None
    if not isinstance(document_id, str) or not isinstance(anchor, str):
        raise ValueError("Source section route metadata is incomplete.")
    path = source_paths.get(document_id)
    if path is None:
        raise ValueError("Source section route has no Portable Source View.")
    return _route_entry(
        item.route,
        kind=item.kind,
        authority=item.authority,
        identity=item.identity,
        title=item.title,
        path=path,
        anchor=anchor,
    )


def _checksums_in(staging: Path) -> dict[str, str]:
    return {
        path.relative_to(staging).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path.name != "wiki-manifest.json"
    }


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content.rstrip() + "\n")


def _stored_file(kb_dir: Path, storage_path: str) -> Path:
    relative = Path(storage_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Portable Wiki source image has an invalid stored path.")
    return kb_dir / relative


def _evidence_anchor(evidence_id: str) -> str:
    token = _ANCHOR_UNSAFE.sub("-", evidence_id).strip("-")
    return f"evidence-{token or hashlib.sha256(evidence_id.encode()).hexdigest()[:16]}"


def _heading_text(value: str) -> str:
    return " / ".join(_heading_parts(value))


def _heading_parts(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return ()
    return tuple(item for item in parsed if item)
