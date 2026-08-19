"""SQLite-authoritative drafts and published Concept/Entity pages for Desktop."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock

DesktopKnowledgePagePublicationState = Literal["draft", "unpublished_changes", "published"]
_PAGE_KINDS = frozenset({"concept", "entity"})
logger = logging.getLogger(__name__)

_PAGE_STATE_CTE = """
WITH page_ids AS (
    SELECT page_id FROM knowledge_pages
    UNION
    SELECT page_id FROM knowledge_page_working_drafts
),
page_state AS (
    SELECT page_ids.page_id, COALESCE(pages.kind, drafts.kind) AS kind,
        COALESCE(drafts.title, revisions.title) AS title,
        CASE
            WHEN pages.page_id IS NULL THEN 'draft'
            WHEN drafts.page_id IS NOT NULL THEN 'unpublished_changes'
            ELSE 'published'
        END AS publication_state,
        revisions.revision_number AS published_revision_number,
        COALESCE(drafts.updated_at, pages.updated_at) AS updated_at,
        COALESCE(
            pages.materialized_path,
            'knowledge-pages/' || drafts.kind || '/' || page_ids.page_id || '.md'
        ) AS materialized_path,
        revisions.title AS published_title,
        revisions.content_markdown AS published_content_markdown,
        revisions.created_at AS published_at,
        drafts.title AS draft_title,
        drafts.content_markdown AS draft_content_markdown,
        drafts.updated_at AS draft_updated_at
    FROM page_ids
    LEFT JOIN knowledge_pages AS pages ON pages.page_id = page_ids.page_id
    LEFT JOIN knowledge_page_revisions AS revisions
        ON revisions.revision_id = pages.current_revision_id
    LEFT JOIN knowledge_page_working_drafts AS drafts
        ON drafts.page_id = page_ids.page_id
)
"""


class DesktopKnowledgePageError(RuntimeError):
    """A stable domain error for Desktop Knowledge Page operations."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DesktopKnowledgePublishedRevision:
    revision_number: int
    title: str
    content_markdown: str
    published_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "revision_number": self.revision_number,
            "title": self.title,
            "content_markdown": self.content_markdown,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgeWorkingDraft:
    title: str
    content_markdown: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "content_markdown": self.content_markdown,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgePageSummary:
    """Small page record used by the Desktop Workbench navigation list."""

    page_id: str
    kind: str
    title: str
    publication_state: DesktopKnowledgePagePublicationState
    published_revision_number: int | None
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "page_id": self.page_id,
            "kind": self.kind,
            "title": self.title,
            "publication_state": self.publication_state,
            "published_revision_number": self.published_revision_number,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgePage(DesktopKnowledgePageSummary):
    """One page with explicitly separated published and editable values."""

    materialized_path: str
    published_revision: DesktopKnowledgePublishedRevision | None
    working_draft: DesktopKnowledgeWorkingDraft | None

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "materialized_path": self.materialized_path,
            "published_revision": (
                self.published_revision.as_dict() if self.published_revision else None
            ),
            "working_draft": self.working_draft.as_dict() if self.working_draft else None,
        }


class DesktopKnowledgePageService:
    """Autosave one Working Draft and publish immutable user revisions explicitly."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)

    def list_pages(self) -> tuple[DesktopKnowledgePageSummary, ...]:
        self._require_database()
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                rows = connection.execute(
                    f"""
                    {_PAGE_STATE_CTE}
                    SELECT * FROM page_state
                    ORDER BY kind, updated_at DESC, title COLLATE NOCASE
                    """
                ).fetchall()
            finally:
                connection.close()
        return tuple(_summary(_page_from_row(row)) for row in rows)

    def selected_page_id(self) -> str | None:
        self._require_database()
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT last_page_id FROM knowledge_page_ui_state WHERE singleton = 1"
                ).fetchone()
                if row is None or row[0] is None:
                    return None
                page_id = str(row[0])
                return page_id if self._page_exists_in(connection, page_id) else None
            finally:
                connection.close()

    def get_page(self, page_id: str) -> DesktopKnowledgePage:
        self._require_database()
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                return self._page_in(connection, page_id)
            finally:
                connection.close()

    def select_page(self, page_id: str) -> DesktopKnowledgePage:
        self._require_database()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    page = self._page_in(connection, page_id)
                    _select_page_in(connection, page_id)
                return page
            finally:
                connection.close()

    def save_draft(
        self,
        *,
        page_id: str | None,
        kind: str,
        title: str,
        content_markdown: str,
    ) -> DesktopKnowledgePage:
        """Create or replace the page's sole Working Draft without publishing it."""
        self._require_database()
        normalized_kind = _require_kind(kind)
        display_title, normalized_title = _normalize_title(title)
        now = _timestamp()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                resolved_page_id = page_id or uuid.uuid4().hex
                if page_id is not None:
                    self._require_existing_kind(connection, page_id, normalized_kind)
                self._require_unique_title(
                    connection,
                    normalized_kind,
                    normalized_title,
                    page_id=resolved_page_id,
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_page_working_drafts (
                        page_id, kind, title, normalized_title, content_markdown,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(page_id) DO UPDATE SET
                        title = excluded.title,
                        normalized_title = excluded.normalized_title,
                        content_markdown = excluded.content_markdown,
                        updated_at = excluded.updated_at
                    """,
                    (
                        resolved_page_id,
                        normalized_kind,
                        display_title,
                        normalized_title,
                        content_markdown,
                        now,
                        now,
                    ),
                )
                _select_page_in(connection, resolved_page_id)
                page = self._page_in(connection, resolved_page_id)
                connection.commit()
                return page
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def publish(self, page_id: str) -> DesktopKnowledgePage:
        """Atomically advance the current revision and consume its Working Draft."""
        self._require_database()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            staged: Path | None = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                draft = connection.execute(
                    """
                    SELECT kind, title, normalized_title, content_markdown
                    FROM knowledge_page_working_drafts WHERE page_id = ?
                    """,
                    (page_id,),
                ).fetchone()
                if draft is None:
                    raise DesktopKnowledgePageError(
                        "knowledge_page_draft_not_found",
                        "Save a Working Draft before publishing this knowledge page.",
                    )
                kind, title, normalized_title, content_markdown = (str(value) for value in draft)
                self._require_unique_title(connection, kind, normalized_title, page_id=page_id)
                now = _timestamp()
                revision_number = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(revision_number), 0) + 1
                        FROM knowledge_page_revisions WHERE page_id = ?
                        """,
                        (page_id,),
                    ).fetchone()[0]
                )
                revision_id = uuid.uuid4().hex
                existing = connection.execute(
                    "SELECT materialized_path FROM knowledge_pages WHERE page_id = ?",
                    (page_id,),
                ).fetchone()
                materialized_path = (
                    str(existing[0]) if existing is not None else _materialized_path(kind, page_id)
                )
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO knowledge_pages (
                            page_id, kind, title, normalized_title, materialized_path,
                            current_revision_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            page_id,
                            kind,
                            title,
                            normalized_title,
                            materialized_path,
                            revision_id,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO knowledge_page_revisions (
                        revision_id, page_id, revision_number, title,
                        content_markdown, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (revision_id, page_id, revision_number, title, content_markdown, now),
                )
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE knowledge_pages
                        SET title = ?, normalized_title = ?, current_revision_id = ?, updated_at = ?
                        WHERE page_id = ?
                        """,
                        (title, normalized_title, revision_id, now, page_id),
                    )
                connection.execute(
                    "DELETE FROM knowledge_page_working_drafts WHERE page_id = ?", (page_id,)
                )
                page = self._page_in(connection, page_id)
                staged = stage_knowledge_page_projection(self.kb_dir, page)
                connection.commit()
            except BaseException:
                connection.rollback()
                if staged is not None:
                    discard_knowledge_page_projection_staging(staged)
                raise
            finally:
                connection.close()
            try:
                activate_knowledge_page_projection(self.kb_dir, page, staged)
            except Exception:
                logger.exception("Could not activate Desktop user knowledge Markdown projection.")
            finally:
                discard_knowledge_page_projection_staging(staged)
            return page

    def materialize_current_pages(self) -> None:
        """Rebuild published Markdown only; Working Drafts never enter the projection."""
        self._require_database()
        with kb_ingest_lock(self.state_dir):
            _discard_abandoned_projection_staging(self.kb_dir)
            connection = self._connect()
            try:
                rows = connection.execute(
                    f"""
                    {_PAGE_STATE_CTE}
                    SELECT * FROM page_state WHERE published_revision_number IS NOT NULL
                    ORDER BY kind, page_id
                    """
                ).fetchall()
            finally:
                connection.close()
            for row in rows:
                page = _page_from_row(row)
                atomic_write_text(self.kb_dir / page.materialized_path, _render_markdown(page))

    def _require_existing_kind(
        self, connection: sqlite3.Connection, page_id: str, kind: str
    ) -> None:
        row = connection.execute(
            """
            SELECT kind FROM knowledge_pages WHERE page_id = ?
            UNION ALL
            SELECT kind FROM knowledge_page_working_drafts WHERE page_id = ?
            LIMIT 1
            """,
            (page_id, page_id),
        ).fetchone()
        if row is None:
            raise DesktopKnowledgePageError(
                "knowledge_page_not_found", f"Page not found: {page_id}"
            )
        if str(row[0]) != kind:
            raise DesktopKnowledgePageError(
                "knowledge_page_kind_immutable",
                "A knowledge page cannot change between Concept and Entity.",
            )

    def _require_unique_title(
        self,
        connection: sqlite3.Connection,
        kind: str,
        normalized_title: str,
        *,
        page_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT page_id FROM (
                SELECT page_id, kind, normalized_title FROM knowledge_pages
                UNION ALL
                SELECT page_id, kind, normalized_title FROM knowledge_page_working_drafts
            )
            WHERE kind = ? AND normalized_title = ? AND page_id != ?
            LIMIT 1
            """,
            (kind, normalized_title, page_id),
        ).fetchone()
        if row is not None:
            raise DesktopKnowledgePageError(
                "knowledge_page_title_conflict",
                "A page with this title already exists in the selected page type.",
            )

    def _page_in(self, connection: sqlite3.Connection, page_id: str) -> DesktopKnowledgePage:
        row = connection.execute(
            f"""
            {_PAGE_STATE_CTE}
            SELECT * FROM page_state WHERE page_id = ?
            """,
            (page_id,),
        ).fetchone()
        if row is None:
            raise DesktopKnowledgePageError(
                "knowledge_page_not_found", f"Page not found: {page_id}"
            )
        return _page_from_row(row)

    def _page_exists_in(self, connection: sqlite3.Connection, page_id: str) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM knowledge_pages WHERE page_id = ?
                UNION ALL
                SELECT 1 FROM knowledge_page_working_drafts WHERE page_id = ?
                LIMIT 1
                """,
                (page_id, page_id),
            ).fetchone()
            is not None
        )

    def _require_database(self) -> None:
        if not self.database_path.is_file():
            raise DesktopKnowledgePageError(
                "desktop_knowledge_base_not_found",
                f"Not a Desktop Knowledge Base: {self.kb_dir}",
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def stage_knowledge_page_projection(kb_dir: Path, page: DesktopKnowledgePage) -> Path:
    """Render a publication before its SQLite transaction commits."""
    staging_root = kb_dir / "knowledge-pages" / ".page-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = staging_root / f"{uuid.uuid4().hex}.md"
    atomic_write_text(staged, _render_markdown(page))
    return staged


def activate_knowledge_page_projection(
    kb_dir: Path, page: DesktopKnowledgePage, staged: Path
) -> None:
    target = kb_dir / page.materialized_path
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, target)


def discard_knowledge_page_projection_staging(staged: Path) -> None:
    staged.unlink(missing_ok=True)


def _discard_abandoned_projection_staging(kb_dir: Path) -> None:
    staging_root = kb_dir / "knowledge-pages" / ".page-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)


def _page_from_row(row: tuple[object, ...]) -> DesktopKnowledgePage:
    published = (
        DesktopKnowledgePublishedRevision(
            revision_number=int(str(row[4])),
            title=str(row[7]),
            content_markdown=str(row[8]),
            published_at=str(row[9]),
        )
        if row[4] is not None
        else None
    )
    draft = (
        DesktopKnowledgeWorkingDraft(
            title=str(row[10]),
            content_markdown=str(row[11]),
            updated_at=str(row[12]),
        )
        if row[10] is not None
        else None
    )
    return DesktopKnowledgePage(
        page_id=str(row[0]),
        kind=str(row[1]),
        title=str(row[2]),
        publication_state=cast(DesktopKnowledgePagePublicationState, str(row[3])),
        published_revision_number=int(str(row[4])) if row[4] is not None else None,
        updated_at=str(row[5]),
        materialized_path=str(row[6]),
        published_revision=published,
        working_draft=draft,
    )


def _summary(page: DesktopKnowledgePage) -> DesktopKnowledgePageSummary:
    return DesktopKnowledgePageSummary(
        page_id=page.page_id,
        kind=page.kind,
        title=page.title,
        publication_state=page.publication_state,
        published_revision_number=page.published_revision_number,
        updated_at=page.updated_at,
    )


def _select_page_in(connection: sqlite3.Connection, page_id: str) -> None:
    connection.execute(
        "UPDATE knowledge_page_ui_state SET last_page_id = ? WHERE singleton = 1",
        (page_id,),
    )


def _materialized_path(kind: str, page_id: str) -> str:
    return (Path("knowledge-pages") / kind / f"{page_id}.md").as_posix()


def _require_kind(kind: str) -> str:
    if kind not in _PAGE_KINDS:
        raise DesktopKnowledgePageError(
            "knowledge_page_kind_invalid", "Knowledge page kind must be concept or entity."
        )
    return kind


def _normalize_title(title: str) -> tuple[str, str]:
    display_title, normalized_title = normalize_knowledge_title(title)
    if not display_title:
        raise DesktopKnowledgePageError(
            "knowledge_page_title_required", "A knowledge page title is required."
        )
    return display_title, normalized_title


def _render_markdown(page: DesktopKnowledgePage) -> str:
    published = page.published_revision
    if published is None:
        raise DesktopKnowledgePageError(
            "knowledge_page_not_published", "A Working Draft cannot be materialized."
        )
    frontmatter = "\n".join(
        (
            "---",
            f"page_id: {json.dumps(page.page_id, ensure_ascii=False)}",
            f"kind: {json.dumps(page.kind, ensure_ascii=False)}",
            f"title: {json.dumps(published.title, ensure_ascii=False)}",
            f"revision: {published.revision_number}",
            'authority: "user_revision"',
            f"updated_at: {json.dumps(published.published_at, ensure_ascii=False)}",
            "---",
        )
    )
    body = published.content_markdown.rstrip("\n")
    return f"{frontmatter}\n\n# {published.title}\n\n{body}\n"


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
