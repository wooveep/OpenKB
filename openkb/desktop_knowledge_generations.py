"""Published derived-knowledge generations and their Markdown projections."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    materialize_okf_projection,
    stage_okf_projection_in,
)


@dataclass(frozen=True)
class KnowledgeGenerationChange:
    """One selected derived Concept or Entity value for a generation."""

    document_id: str
    kind: str
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str
    entity_subtype: str | None = None


def normalized_knowledge_content(value: str) -> str:
    """Return the stable text form used by derived knowledge comparisons."""
    return "\n".join(
        " ".join(line.split()) for line in value.splitlines() if line.strip()
    ).casefold()


def knowledge_content_sha256(value: str) -> str:
    """Fingerprint a derived knowledge value without retaining its text."""
    return hashlib.sha256(normalized_knowledge_content(value).encode("utf-8")).hexdigest()


def current_generation_id_in(connection: sqlite3.Connection) -> int | None:
    """Return the current immutable generation snapshot, if one was published."""
    row = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    return int(row[0]) if row is not None else None


def publish_generation_changes_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int | None,
    changes: tuple[KnowledgeGenerationChange, ...],
    now: str,
) -> int:
    """Create one snapshot with all selected changes, inside the caller's transaction."""
    if not changes:
        raise ValueError("A published knowledge generation needs at least one change.")
    cursor = connection.execute(
        """
        INSERT INTO knowledge_generations (parent_generation_id, created_at)
        VALUES (?, ?)
        """,
        (current_generation_id, now),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Knowledge generation insert did not return an identifier.")
    generation_id = int(cursor.lastrowid)
    if current_generation_id is not None:
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype
            )
            SELECT ?, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype
            FROM knowledge_generation_items WHERE generation_id = ?
            """,
            (generation_id, current_generation_id),
        )
    for change in changes:
        _upsert_generation_change_in(connection, generation_id, change, now)
    connection.execute(
        """
        INSERT INTO knowledge_generation_state (singleton, current_generation_id)
        VALUES (1, ?)
        ON CONFLICT(singleton) DO UPDATE SET current_generation_id = excluded.current_generation_id
        """,
        (generation_id,),
    )
    return generation_id


def materialize_current_generation(kb_dir: Path) -> None:
    """Restore the complete disposable OKF projection."""
    materialize_okf_projection(kb_dir)


def materialize_generation_in(
    connection: sqlite3.Connection, kb_dir: Path, generation_id: int | None
) -> None:
    """Rebuild the visible projection for the current committed generation."""
    staged = stage_generation_projection_in(connection, kb_dir, generation_id)
    try:
        activate_generation_projection(kb_dir, staged)
    finally:
        discard_generation_projection_staging(staged)


def stage_generation_projection_in(
    connection: sqlite3.Connection, kb_dir: Path, _generation_id: int | None
) -> Path:
    """Stage the complete bundle containing the transactional generation."""
    return stage_okf_projection_in(connection, kb_dir)


def activate_generation_projection(kb_dir: Path, staged: Path) -> None:
    """Activate the complete bundle after the generation transaction commits."""
    activate_okf_projection(kb_dir, staged)


def discard_generation_projection_staging(staged: Path) -> None:
    """Delete a hidden complete-bundle projection."""
    discard_okf_projection_staging(staged)


def _upsert_generation_change_in(
    connection: sqlite3.Connection,
    generation_id: int,
    change: KnowledgeGenerationChange,
    now: str,
) -> None:
    existing = connection.execute(
        """
        SELECT item_key FROM knowledge_generation_items
        WHERE generation_id = ? AND kind = ? AND normalized_title = ?
        """,
        (generation_id, change.kind, change.normalized_title),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state, entity_subtype
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_unmapped', ?)
            """,
            (
                generation_id,
                uuid.uuid4().hex,
                change.kind,
                change.title,
                change.normalized_title,
                change.content_markdown,
                change.content_sha256,
                change.document_id,
                now,
                change.entity_subtype,
            ),
        )
        return
    connection.execute(
        """
        UPDATE knowledge_generation_items
        SET title = ?, content_markdown = ?, content_sha256 = ?,
            source_document_id = ?, created_at = ?, provenance_state = 'legacy_unmapped',
            entity_subtype = COALESCE(?, entity_subtype)
        WHERE generation_id = ? AND item_key = ?
        """,
        (
            change.title,
            change.content_markdown,
            change.content_sha256,
            change.document_id,
            now,
            change.entity_subtype,
            generation_id,
            str(existing[0]),
        ),
    )
