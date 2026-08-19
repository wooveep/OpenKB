"""Published derived-knowledge generations and their Markdown projections."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import atomic_write_text, kb_ingest_lock


@dataclass(frozen=True)
class KnowledgeGenerationChange:
    """One selected derived Concept or Entity value for a generation."""

    document_id: str
    kind: str
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str


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
                provenance_state
            )
            SELECT ?, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at,
                provenance_state
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
    """Restore the disposable Markdown projection after opening a knowledge base."""
    resolved = kb_dir.expanduser().resolve()
    with kb_ingest_lock(desktop_state_dir(resolved)):
        connection = sqlite3.connect(desktop_state_database_path(resolved))
        try:
            generation_id = current_generation_id_in(connection)
            _discard_abandoned_projection_staging(resolved)
            materialize_generation_in(connection, resolved, generation_id)
        finally:
            connection.close()


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
    connection: sqlite3.Connection, kb_dir: Path, generation_id: int | None
) -> Path:
    """Render a generation into a hidden directory before it becomes visible."""
    staging_root = _projection_staging_root(kb_dir)
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = staging_root / uuid.uuid4().hex
    staged.mkdir()
    try:
        rows = (
            ()
            if generation_id is None
            else connection.execute(
                """
            SELECT item_key, kind, title, content_markdown, source_document_id, created_at,
                provenance_state
            FROM knowledge_generation_items
            WHERE generation_id = ?
            ORDER BY kind, item_key
            """,
                (generation_id,),
            ).fetchall()
        )
        for row in rows:
            item_key, kind, title, content, document_id, created_at, provenance_state = (
                str(value) for value in row
            )
            path = Path(kind) / f"{item_key}.md"
            atomic_write_text(
                staged / path,
                _render_generation_markdown(
                    generation_id=_required_generation_id(generation_id),
                    item_key=item_key,
                    kind=kind,
                    title=title,
                    content_markdown=content,
                    source_document_id=document_id,
                    created_at=created_at,
                    provenance_state=provenance_state,
                ),
            )
        return staged
    except BaseException:
        discard_generation_projection_staging(staged)
        raise


def activate_generation_projection(kb_dir: Path, staged: Path) -> None:
    """Swap a fully rendered hidden directory into the visible projection path."""
    target = _projection_root(kb_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".generated-backup-{uuid.uuid4().hex}"
    moved_current = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_current = True
        os.replace(staged, target)
    except BaseException:
        if moved_current and not target.exists() and backup.exists():
            os.replace(backup, target)
        raise
    finally:
        if backup.exists() and target.exists():
            shutil.rmtree(backup, ignore_errors=True)


def discard_generation_projection_staging(staged: Path) -> None:
    """Delete a hidden projection that was not (or is no longer) needed."""
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)


def _discard_abandoned_projection_staging(kb_dir: Path) -> None:
    staging_root = _projection_staging_root(kb_dir)
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


def _projection_root(kb_dir: Path) -> Path:
    return kb_dir / "knowledge-pages" / "generated"


def _projection_staging_root(kb_dir: Path) -> Path:
    return kb_dir / "knowledge-pages" / ".generated-staging"


def _required_generation_id(generation_id: int | None) -> int:
    if generation_id is None:
        raise RuntimeError("Cannot render a generated item without a generation identifier.")
    return generation_id


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
                provenance_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_unmapped')
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
            ),
        )
        return
    connection.execute(
        """
        UPDATE knowledge_generation_items
        SET title = ?, content_markdown = ?, content_sha256 = ?,
            source_document_id = ?, created_at = ?, provenance_state = 'legacy_unmapped'
        WHERE generation_id = ? AND item_key = ?
        """,
        (
            change.title,
            change.content_markdown,
            change.content_sha256,
            change.document_id,
            now,
            generation_id,
            str(existing[0]),
        ),
    )


def _render_generation_markdown(
    *,
    generation_id: int,
    item_key: str,
    kind: str,
    title: str,
    content_markdown: str,
    source_document_id: str,
    created_at: str,
    provenance_state: str,
) -> str:
    frontmatter = "\n".join(
        (
            "---",
            f"item_key: {json.dumps(item_key, ensure_ascii=False)}",
            f"kind: {json.dumps(kind, ensure_ascii=False)}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"generation: {generation_id}",
            'authority: "published_generation"',
            f"openkb.provenance: {json.dumps(provenance_state)}",
            f"openkb.origin_document_id: {json.dumps(source_document_id, ensure_ascii=False)}",
            f"published_at: {json.dumps(created_at, ensure_ascii=False)}",
            "---",
        )
    )
    return f"{frontmatter}\n\n# {title}\n\n{content_markdown.rstrip()}\n"
