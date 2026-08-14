"""Filesystem and SQLite helpers for independently retained Desktop source images."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openkb.desktop_import_artifacts import SourceImage
from openkb.locks import atomic_write_bytes, kb_ingest_lock


def source_image_storage_path(image: SourceImage) -> str:
    """Return the stable derived location for one independently stored image."""
    extension = image.extension.lower()
    if not extension.startswith(".") or "/" in extension or "\\" in extension:
        extension = ".bin"
    return (Path("derived") / "source-images" / f"{image.image_sha256}{extension}").as_posix()


def write_source_images(
    kb_dir: Path, state_dir: Path, source_images: tuple[SourceImage, ...]
) -> None:
    """Write extracted bytes before their Document IR checkpoint commits."""
    if not source_images:
        return
    with kb_ingest_lock(state_dir):
        for image in source_images:
            if image.content:
                atomic_write_bytes(kb_dir / source_image_storage_path(image), image.content)


def persist_source_images(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    source_images: tuple[SourceImage, ...],
    created_at: str,
) -> None:
    """Attach saved source images to the newly available document in one transaction."""
    connection.executemany(
        """
        INSERT INTO source_images (
            source_image_id, document_id, ordinal, image_sha256, byte_size,
            media_type, storage_path, display_name, alt_text, locator_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                image.image_id,
                document_id,
                image.ordinal,
                image.image_sha256,
                image.byte_size,
                image.media_type,
                source_image_storage_path(image),
                image.filename,
                image.alt_text,
                json.dumps(image.locator, ensure_ascii=False),
                created_at,
            )
            for image in source_images
        ],
    )
