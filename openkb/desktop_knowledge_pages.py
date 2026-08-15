"""SQLite-authoritative Concept and Entity pages for OpenKB Desktop.

Desktop imports may discover knowledge, but they never write this module's
tables.  A submitted user revision is therefore the authority for a page;
Markdown under ``knowledge-pages/`` is a disposable, readable projection.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock

_PAGE_KINDS = frozenset({"concept", "entity"})


class DesktopKnowledgePageError(RuntimeError):
    """A stable domain error for Desktop Knowledge Page operations."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DesktopKnowledgePageSummary:
    """Small page record used by the Desktop Workbench navigation list."""

    page_id: str
    kind: str
    title: str
    revision_number: int
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "page_id": self.page_id,
            "kind": self.kind,
            "title": self.title,
            "revision_number": self.revision_number,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class DesktopKnowledgePage(DesktopKnowledgePageSummary):
    """The current user revision of one Concept or Entity page."""

    content_markdown: str
    materialized_path: str

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "content_markdown": self.content_markdown,
            "materialized_path": self.materialized_path,
        }


class DesktopKnowledgePageService:
    """Read and revise user-owned knowledge pages for one Desktop knowledge base."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)

    def list_pages(self) -> tuple[DesktopKnowledgePageSummary, ...]:
        """Return current revisions, grouped by kind then most recently revised."""
        self._require_database()
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT pages.page_id, pages.kind, revisions.title,
                        revisions.revision_number, pages.updated_at
                    FROM knowledge_pages AS pages
                    JOIN knowledge_page_revisions AS revisions
                        ON revisions.revision_id = pages.current_revision_id
                    ORDER BY pages.kind, pages.updated_at DESC, revisions.title COLLATE NOCASE
                    """
                ).fetchall()
            finally:
                connection.close()
        return tuple(
            DesktopKnowledgePageSummary(
                page_id=str(row[0]),
                kind=str(row[1]),
                title=str(row[2]),
                revision_number=int(row[3]),
                updated_at=str(row[4]),
            )
            for row in rows
        )

    def get_page(self, page_id: str) -> DesktopKnowledgePage:
        """Return the current user revision or a stable not-found error."""
        self._require_database()
        with kb_read_lock(self.state_dir):
            connection = self._connect()
            try:
                page = self._page_in(connection, page_id)
            finally:
                connection.close()
        return page

    def save_page(
        self,
        *,
        page_id: str | None,
        kind: str,
        title: str,
        content_markdown: str,
    ) -> DesktopKnowledgePage:
        """Append a user revision and rewrite only its derived Markdown projection."""
        self._require_database()
        normalized_kind = _require_kind(kind)
        display_title, normalized_title = _normalize_title(title)
        now = _timestamp()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if page_id is None:
                    page = self._create_page_in(
                        connection,
                        kind=normalized_kind,
                        title=display_title,
                        normalized_title=normalized_title,
                        content_markdown=content_markdown,
                        now=now,
                    )
                else:
                    page = self._revise_page_in(
                        connection,
                        page_id=page_id,
                        kind=normalized_kind,
                        title=display_title,
                        normalized_title=normalized_title,
                        content_markdown=content_markdown,
                        now=now,
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            self._materialize_page(page)
        return page

    def materialize_current_pages(self) -> None:
        """Rebuild derived Markdown from the SQLite authority after opening a KB."""
        self._require_database()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT pages.page_id, pages.kind, revisions.title,
                        revisions.revision_number, revisions.content_markdown,
                        pages.materialized_path, pages.updated_at
                    FROM knowledge_pages AS pages
                    JOIN knowledge_page_revisions AS revisions
                        ON revisions.revision_id = pages.current_revision_id
                    ORDER BY pages.kind, pages.page_id
                    """
                ).fetchall()
            finally:
                connection.close()
            for row in rows:
                self._materialize_page(_page_from_row(row))

    def _create_page_in(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        title: str,
        normalized_title: str,
        content_markdown: str,
        now: str,
    ) -> DesktopKnowledgePage:
        self._require_unique_title(connection, kind, normalized_title, page_id=None)
        page_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        materialized_path = (Path("knowledge-pages") / kind / f"{page_id}.md").as_posix()
        connection.execute(
            """
            INSERT INTO knowledge_pages (
                page_id, kind, title, normalized_title, materialized_path,
                current_revision_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (page_id, kind, title, normalized_title, materialized_path, revision_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO knowledge_page_revisions (
                revision_id, page_id, revision_number, title, content_markdown, created_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (revision_id, page_id, title, content_markdown, now),
        )
        return DesktopKnowledgePage(
            page_id=page_id,
            kind=kind,
            title=title,
            revision_number=1,
            content_markdown=content_markdown,
            materialized_path=materialized_path,
            updated_at=now,
        )

    def _revise_page_in(
        self,
        connection: sqlite3.Connection,
        *,
        page_id: str,
        kind: str,
        title: str,
        normalized_title: str,
        content_markdown: str,
        now: str,
    ) -> DesktopKnowledgePage:
        row = connection.execute(
            """
            SELECT kind, materialized_path, current_revision_id
            FROM knowledge_pages WHERE page_id = ?
            """,
            (page_id,),
        ).fetchone()
        if row is None:
            raise DesktopKnowledgePageError(
                "knowledge_page_not_found", f"Page not found: {page_id}"
            )
        existing_kind = str(row[0])
        if existing_kind != kind:
            raise DesktopKnowledgePageError(
                "knowledge_page_kind_immutable",
                "A knowledge page cannot change between Concept and Entity.",
            )
        self._require_unique_title(connection, kind, normalized_title, page_id=page_id)
        revision_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM knowledge_page_revisions "
                "WHERE page_id = ?",
                (page_id,),
            ).fetchone()[0]
        )
        revision_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO knowledge_page_revisions (
                revision_id, page_id, revision_number, title, content_markdown, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (revision_id, page_id, revision_number, title, content_markdown, now),
        )
        connection.execute(
            """
            UPDATE knowledge_pages
            SET title = ?, normalized_title = ?, current_revision_id = ?, updated_at = ?
            WHERE page_id = ?
            """,
            (title, normalized_title, revision_id, now, page_id),
        )
        return DesktopKnowledgePage(
            page_id=page_id,
            kind=kind,
            title=title,
            revision_number=revision_number,
            content_markdown=content_markdown,
            materialized_path=str(row[1]),
            updated_at=now,
        )

    def _require_unique_title(
        self,
        connection: sqlite3.Connection,
        kind: str,
        normalized_title: str,
        *,
        page_id: str | None,
    ) -> None:
        row = connection.execute(
            """
            SELECT page_id FROM knowledge_pages
            WHERE kind = ? AND normalized_title = ? AND page_id != COALESCE(?, '')
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
            """
            SELECT pages.page_id, pages.kind, revisions.title,
                revisions.revision_number, revisions.content_markdown,
                pages.materialized_path, pages.updated_at
            FROM knowledge_pages AS pages
            JOIN knowledge_page_revisions AS revisions
                ON revisions.revision_id = pages.current_revision_id
            WHERE pages.page_id = ?
            """,
            (page_id,),
        ).fetchone()
        if row is None:
            raise DesktopKnowledgePageError(
                "knowledge_page_not_found", f"Page not found: {page_id}"
            )
        return _page_from_row(row)

    def _materialize_page(self, page: DesktopKnowledgePage) -> None:
        atomic_write_text(
            self.kb_dir / page.materialized_path,
            _render_markdown(page),
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


def _page_from_row(row: tuple[str | int, ...]) -> DesktopKnowledgePage:
    return DesktopKnowledgePage(
        page_id=str(row[0]),
        kind=str(row[1]),
        title=str(row[2]),
        revision_number=int(row[3]),
        content_markdown=str(row[4]),
        materialized_path=str(row[5]),
        updated_at=str(row[6]),
    )


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
    frontmatter = "\n".join(
        (
            "---",
            f"page_id: {json.dumps(page.page_id, ensure_ascii=False)}",
            f"kind: {json.dumps(page.kind, ensure_ascii=False)}",
            f"title: {json.dumps(page.title, ensure_ascii=False)}",
            f"revision: {page.revision_number}",
            'authority: "user_revision"',
            f"updated_at: {json.dumps(page.updated_at, ensure_ascii=False)}",
            "---",
        )
    )
    body = page.content_markdown.rstrip("\n")
    return f"{frontmatter}\n\n# {page.title}\n\n{body}\n"


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
