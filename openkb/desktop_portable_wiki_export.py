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
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_knowledge_routes import knowledge_route, source_route, summary_route
from openkb.locks import atomic_write_text

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
class _RouteEntry:
    route: str
    path: str
    kind: str
    authority: str
    identity: str
    title: str


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
    documents = _documents_in(connection)
    evidence_locations = _evidence_locations_in(connection)
    images = _source_images_in(connection)
    image_resources = _copy_source_images(kb_dir, staging, images)
    user_pages = _user_pages_in(connection)
    generated_pages = _generated_pages_in(connection)
    pages = _assign_page_routes((*user_pages, *generated_pages))

    route_entries: list[_RouteEntry] = []
    for document in documents:
        route_entries.extend(
            (
                _route_entry(
                    document.summary_route,
                    kind="summary",
                    authority="document_summary",
                    identity=document.document_id,
                    title=document.title,
                ),
                _route_entry(
                    document.source_route,
                    kind="source",
                    authority="source_document",
                    identity=document.document_id,
                    title=document.title,
                ),
            )
        )
    route_entries.extend(
        _route_entry(
            page.route,
            kind=page.kind,
            authority=page.authority,
            identity=page.identity,
            title=page.title,
        )
        for page in pages
    )
    routes = tuple(sorted(route_entries, key=lambda entry: (entry.route, entry.identity)))
    source_paths = {document.document_id: f"{document.source_route}.md" for document in documents}

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
        )

    atomic_write_text(staging / "index.md", _index_markdown(routes))
    checksums = _checksums_in(staging)
    manifest = {
        "format": "openkb-portable-wiki-v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "snapshot": _snapshot_in(connection),
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
    return PortableWikiExport(source_image_count=len(set(image_resources.values())))


def _documents_in(connection: sqlite3.Connection) -> tuple[_Document, ...]:
    rows = connection.execute(
        """
        SELECT document_id, display_name, source_format
        FROM source_documents
        WHERE availability = 'available'
        ORDER BY display_name, created_at, document_id
        """
    ).fetchall()
    summary_routes = _unique_routes(
        (str(row[0]), summary_route(str(row[1]), str(row[0]))) for row in rows
    )
    source_routes = _unique_routes(
        (str(row[0]), source_route(str(row[1]), str(row[0]))) for row in rows
    )
    return tuple(
        _Document(
            document_id=str(row[0]),
            title=str(row[1]),
            source_format=str(row[2]),
            source_route=source_routes[str(row[0])],
            summary_route=summary_routes[str(row[0])],
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
        WHERE pages.lifecycle_state IN ('stable', 'deprecated')
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


def _assign_page_routes(pages: tuple[_KnowledgePage, ...]) -> tuple[_KnowledgePage, ...]:
    keys = tuple(f"{page.authority}:{page.identity}" for page in pages)
    assigned = _unique_routes((key, page.route) for key, page in zip(keys, pages, strict=True))
    return tuple(
        _KnowledgePage(
            route=assigned[key],
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
        for key, page in zip(keys, pages, strict=True)
    )


def _unique_routes(candidates: Iterable[tuple[str, str]]) -> dict[str, str]:
    values: tuple[tuple[str, str], ...] = tuple(candidates)
    result: dict[str, str] = {}
    used: set[str] = set()
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


def _write_knowledge_page(
    staging: Path,
    page: _KnowledgePage,
    *,
    evidence_locations: dict[str, _EvidenceLocation],
    source_paths: dict[str, str],
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
    content = _append_sources(
        content,
        page.evidence_ids,
        page_path=f"{page.route}.md",
        evidence_locations=evidence_locations,
        source_paths=source_paths,
    )
    _write_markdown(path, content)


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
        SELECT units.unit_ordinal, units.role, units.unit_text, sources.evidence_id
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
            role = str(values[0][1]).replace("_", " ").title()
            lines.extend(("", f"## {role}", "", f"- {values[0][2]}"))
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
    for row in rows:
        kind, value = str(row[1]), str(row[2]).strip()
        if not value:
            continue
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
    if kind == "heading":
        level = min(6, max(2, len(_heading_parts(heading_path_json)) + 1))
        return f"{'#' * level} {value}"
    if kind == "code":
        fence = "````" if "```" in value else "```"
        return f"{fence}\n{value}\n{fence}"
    if kind == "figure":
        return f"> Image: {value}"
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


def _snapshot_in(connection: sqlite3.Connection) -> dict[str, object]:
    knowledge = connection.execute(
        """
        SELECT state.current_generation_id, generations.qualification_state,
            generations.synthesis_schema_version
        FROM knowledge_generation_state AS state
        JOIN knowledge_generations AS generations
          ON generations.generation_id = state.current_generation_id
        WHERE state.singleton = 1
        """
    ).fetchone()
    catalog = connection.execute(
        """
        SELECT state.current_generation_id, state.source_revision,
            generations.snapshot_digest
        FROM knowledge_catalog_state AS state
        LEFT JOIN knowledge_catalog_generations AS generations
          ON generations.generation_id = state.current_generation_id
        WHERE state.singleton = 1
        """
    ).fetchone()
    return {
        "knowledge_generation_id": int(knowledge[0]) if knowledge is not None else None,
        "knowledge_qualification_state": str(knowledge[1]) if knowledge is not None else None,
        "knowledge_synthesis_schema_version": (
            str(knowledge[2]) if knowledge is not None and knowledge[2] is not None else None
        ),
        "catalog_generation_id": (
            str(catalog[0]) if catalog is not None and catalog[0] is not None else None
        ),
        "catalog_source_revision": int(catalog[1]) if catalog is not None else None,
        "catalog_snapshot_digest": (
            str(catalog[2]) if catalog is not None and catalog[2] is not None else None
        ),
    }


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
    headings = (
        ("summary", "Document summaries"),
        ("procedure", "Procedures"),
        ("concept", "Concepts"),
        ("entity", "Entities"),
        ("source", "Source documents"),
    )
    lines = [
        "# OpenKB Portable Wiki",
        "",
        "This immutable export mirrors OpenKB's semantic Knowledge Navigation routes.",
    ]
    for kind, heading in headings:
        values = tuple(entry for entry in routes if entry.kind == kind)
        if not values:
            continue
        lines.extend(("", f"## {heading}", ""))
        for entry in values:
            authority = {
                "published_generation": "generated",
                "user_revision": "user",
            }.get(entry.authority)
            label = f" ({authority})" if authority else ""
            lines.append(f"- [{entry.title}]({entry.path}){label}")
    return "\n".join(lines).rstrip() + "\n"


def _route_entry(
    route: str, *, kind: str, authority: str, identity: str, title: str
) -> _RouteEntry:
    return _RouteEntry(route, f"{route}.md", kind, authority, identity, title)


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
