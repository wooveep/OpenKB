"""Read-only count and size estimate for one Portable Wiki snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from openkb.knowledge.analysis.inventory import ROUTE_INDEX_KINDS, eligible_knowledge_routes_in
from openkb.knowledge.export.portable_wiki_validation import portable_wiki_snapshot_id


@dataclass(frozen=True)
class PortableWikiPreview:
    document_count: int
    estimated_size_bytes: int
    snapshot_id: str


def portable_wiki_preview_in(connection: sqlite3.Connection, kb_dir: Path) -> PortableWikiPreview:
    """Estimate one snapshot without writing or copying its projection."""
    inventory = eligible_knowledge_routes_in(connection)
    documents = {item.identity for item in inventory if item.authority == "source_document"}
    text_bytes = int(
        connection.execute(
            """
            SELECT
                COALESCE((SELECT SUM(LENGTH(CAST(blocks.text AS BLOB)))
                          FROM document_ir_blocks AS blocks
                          JOIN source_documents AS documents
                            ON documents.document_id = blocks.document_id
                          WHERE documents.availability = 'available'), 0)
              + COALESCE((SELECT SUM(LENGTH(CAST(unit_text AS BLOB)))
                          FROM document_summary_units AS units
                          JOIN source_documents AS documents
                            ON documents.document_id = units.document_id
                          WHERE documents.availability = 'available'), 0)
              + COALESCE((SELECT SUM(LENGTH(CAST(revisions.content_markdown AS BLOB)))
                          FROM knowledge_pages AS pages
                          JOIN knowledge_page_revisions AS revisions
                            ON revisions.revision_id = pages.current_revision_id
                          WHERE pages.lifecycle_state = 'stable'
                            AND revisions.provenance_state = 'source_backed'), 0)
              + COALESCE((SELECT SUM(LENGTH(CAST(items.content_markdown AS BLOB)))
                          FROM knowledge_generation_items AS items
                          JOIN knowledge_generation_state AS state
                            ON state.current_generation_id = items.generation_id
                          JOIN knowledge_generations AS generations
                            ON generations.generation_id = items.generation_id
                          WHERE state.singleton = 1
                            AND generations.qualification_state = 'qualified'
                            AND items.provenance_state = 'source_backed'), 0)
            """
        ).fetchone()[0]
    )
    image_bytes = 0
    seen_resources: set[str] = set()
    rows = connection.execute(
        """
        SELECT images.storage_path
        FROM source_images AS images
        JOIN source_documents AS documents ON documents.document_id = images.document_id
        WHERE documents.availability = 'available'
        ORDER BY images.document_id, images.ordinal, images.source_image_id
        """
    ).fetchall()
    for (storage_path_value,) in rows:
        storage_path = str(storage_path_value)
        if storage_path in seen_resources:
            continue
        seen_resources.add(storage_path)
        path = _stored_file(kb_dir, storage_path)
        if path.is_file():
            image_bytes += path.stat().st_size
    route_overhead = (len(inventory) + len(ROUTE_INDEX_KINDS) + 1) * 512
    return PortableWikiPreview(
        document_count=len(documents),
        estimated_size_bytes=max(4096, text_bytes + image_bytes + route_overhead),
        snapshot_id=portable_wiki_snapshot_id(portable_wiki_snapshot_in(connection)),
    )


def portable_wiki_snapshot_in(connection: sqlite3.Connection) -> dict[str, object]:
    """Describe the complete eligible Portable Wiki read view in one digestible value."""
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
        SELECT state.source_revision
        FROM knowledge_catalog_state AS state
        WHERE state.singleton = 1
        """
    ).fetchone()
    inventory = eligible_knowledge_routes_in(connection)
    route_material = [
        (
            item.route,
            item.kind,
            item.authority,
            item.identity,
            item.metadata_json,
            item.snapshot_token,
        )
        for item in inventory
    ]
    image_material = connection.execute(
        """
        SELECT images.source_image_id, images.document_id, images.image_sha256,
            images.storage_path
        FROM source_images AS images
        JOIN source_documents AS documents ON documents.document_id = images.document_id
        WHERE documents.availability = 'available'
        ORDER BY images.source_image_id
        """
    ).fetchall()
    return {
        "knowledge_generation_id": int(knowledge[0]) if knowledge is not None else None,
        "knowledge_qualification_state": (str(knowledge[1]) if knowledge is not None else None),
        "knowledge_synthesis_schema_version": (
            str(knowledge[2]) if knowledge is not None and knowledge[2] is not None else None
        ),
        "catalog_source_revision": int(catalog[0]) if catalog is not None else None,
        "eligible_view_digest": _snapshot_material_digest(route_material),
        "source_image_digest": _snapshot_material_digest(image_material),
    }


def _snapshot_material_digest(rows: object) -> str:
    encoded = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stored_file(kb_dir: Path, storage_path: str) -> Path:
    relative = Path(storage_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Portable Wiki source image has an invalid stored path.")
    return kb_dir / relative
