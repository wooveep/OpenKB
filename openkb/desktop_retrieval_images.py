"""Resolve source images that align exactly with selected original EvidenceRefs."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from openkb.desktop_answer_types import DesktopAnswerSourceImage, DesktopEvidenceRef
from openkb.desktop_source_image_locator import source_image_matches_evidence


def source_images_for_evidence(
    connection: sqlite3.Connection,
    evidence: tuple[DesktopEvidenceRef, ...],
    kb_dir: Path,
) -> tuple[DesktopAnswerSourceImage, ...]:
    """Select only original images that share an exact source location with a citation."""
    document_ids = tuple(dict.fromkeys(reference.document_id for reference in evidence))
    if not document_ids:
        return ()
    placeholders = ", ".join("?" for _ in document_ids)
    rows = connection.execute(
        f"""
        SELECT source_images.source_image_id, source_images.document_id,
            source_documents.display_name, source_images.display_name,
            source_images.media_type, source_images.storage_path, source_images.alt_text,
            source_images.locator_json
        FROM source_images
        JOIN source_documents ON source_documents.document_id = source_images.document_id
        WHERE source_documents.availability = 'available'
            AND source_images.document_id IN ({placeholders})
        ORDER BY source_images.document_id, source_images.ordinal
        """,
        document_ids,
    ).fetchall()
    images_by_document: defaultdict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        images_by_document[str(row[1])].append(row)

    selected: list[DesktopAnswerSourceImage] = []
    selected_ids: set[str] = set()
    for reference in evidence:
        for row in images_by_document[reference.document_id]:
            source_image_id = str(row[0])
            if source_image_id in selected_ids:
                continue
            locator = _json_object(str(row[7]))
            if not source_image_matches_evidence(source_image_id, locator, reference.locator):
                continue
            file_path = kb_dir / str(row[5])
            if not file_path.is_file():
                continue
            selected_ids.add(source_image_id)
            selected.append(
                DesktopAnswerSourceImage(
                    source_image_id=source_image_id,
                    evidence_id=reference.evidence_id,
                    document_id=str(row[1]),
                    document_name=str(row[2]),
                    name=str(row[3]),
                    media_type=str(row[4]),
                    file_path=str(file_path),
                    alt_text=str(row[6]) if row[6] is not None else None,
                    locator={**locator, "source_image_id": source_image_id},
                )
            )
    return tuple(selected)


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}
