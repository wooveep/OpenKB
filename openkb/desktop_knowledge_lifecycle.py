"""User-controlled lifecycle mutations for published Desktop Knowledge Pages."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from openkb.desktop_knowledge_page_errors import DesktopKnowledgePageError
from openkb.desktop_knowledge_page_projection import (
    discard_knowledge_page_projection_staging,
    restore_knowledge_page_projection_deletion,
    stage_knowledge_page_projection_deletion,
)
from openkb.desktop_knowledge_verification import invalidate_current_verification_in
from openkb.locks import kb_ingest_lock

if TYPE_CHECKING:
    from openkb.desktop_knowledge_pages import DesktopKnowledgePage

DesktopKnowledgeLifecycleState = Literal["draft", "stable", "deprecated"]
_LOCAL_ACTOR = "local_user"
logger = logging.getLogger(__name__)


class DesktopKnowledgeLifecycleMixin:
    """Deep lifecycle actions mixed into the page service without widening its core module."""

    kb_dir: Path
    state_dir: Path

    def _require_database(self) -> None:
        raise NotImplementedError

    def _connect(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _page_in(
        self, connection: sqlite3.Connection, page_id: str
    ) -> DesktopKnowledgePage:
        raise NotImplementedError

    def set_stale_after(self, page_id: str, stale_after: str | None) -> DesktopKnowledgePage:
        """Set or clear the explicit staleness threshold without changing page identity."""
        normalized = _normalize_stale_after(stale_after)
        return self._change_lifecycle(
            page_id,
            target_state=None,
            target_stale_after=normalized,
            event_type="stale_after_changed",
        )

    def deprecate(self, page_id: str) -> DesktopKnowledgePage:
        """Retain a published page and its history while removing it from default routing."""
        return self._change_lifecycle(
            page_id,
            target_state="deprecated",
            target_stale_after=_UNCHANGED,
            event_type="deprecated",
        )

    def restore(self, page_id: str) -> DesktopKnowledgePage:
        """Restore the same stable Page ID after deprecation."""
        return self._change_lifecycle(
            page_id,
            target_state="stable",
            target_stale_after=_UNCHANGED,
            event_type="restored",
        )

    def permanent_delete(self, page_id: str, *, confirmation_page_id: str) -> None:
        """Delete a deprecated page after exact identity confirmation; never touch sources."""
        self._require_database()
        if confirmation_page_id != page_id:
            raise DesktopKnowledgePageError(
                "knowledge_page_delete_confirmation_invalid",
                "Confirm the exact Knowledge Page before permanent deletion.",
            )
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            staged: Path | None = None
            relative_path: str | None = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT materialized_path, lifecycle_state, stale_after
                    FROM knowledge_pages WHERE page_id = ?
                    """,
                    (page_id,),
                ).fetchone()
                draft_exists = connection.execute(
                    "SELECT 1 FROM knowledge_page_working_drafts WHERE page_id = ?",
                    (page_id,),
                ).fetchone()
                if row is None and draft_exists is None:
                    raise DesktopKnowledgePageError(
                        "knowledge_page_not_found", f"Page not found: {page_id}"
                    )
                previous_state: DesktopKnowledgeLifecycleState = "draft"
                previous_stale_after: str | None = None
                if row is not None:
                    relative_path = str(row[0])
                    previous_state = cast(DesktopKnowledgeLifecycleState, str(row[1]))
                    previous_stale_after = str(row[2]) if row[2] is not None else None
                    if previous_state != "deprecated":
                        raise DesktopKnowledgePageError(
                            "knowledge_page_deprecation_required",
                            "Deprecate this Knowledge Page before permanent deletion.",
                        )
                    staged = stage_knowledge_page_projection_deletion(
                        self.kb_dir, relative_path
                    )
                now = _timestamp()
                _record_event(
                    connection,
                    page_id=page_id,
                    event_type="permanently_deleted",
                    previous_state=previous_state,
                    new_state=None,
                    previous_stale_after=previous_stale_after,
                    new_stale_after=None,
                    occurred_at=now,
                )
                connection.execute(
                    "DELETE FROM knowledge_page_working_drafts WHERE page_id = ?", (page_id,)
                )
                connection.execute("DELETE FROM knowledge_pages WHERE page_id = ?", (page_id,))
                connection.execute(
                    """
                    UPDATE knowledge_page_ui_state SET last_page_id = NULL
                    WHERE singleton = 1 AND last_page_id = ?
                    """,
                    (page_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                if relative_path is not None:
                    restore_knowledge_page_projection_deletion(
                        self.kb_dir, relative_path, staged
                    )
                raise
            finally:
                connection.close()
            if staged is not None:
                try:
                    discard_knowledge_page_projection_staging(staged)
                except OSError:
                    logger.warning(
                        "knowledge_page_projection_cleanup_deferred page_id=%s staged=%s",
                        page_id,
                        staged,
                        exc_info=True,
                    )

    def _change_lifecycle(
        self,
        page_id: str,
        *,
        target_state: Literal["stable", "deprecated"] | None,
        target_stale_after: str | None | object,
        event_type: Literal["stale_after_changed", "deprecated", "restored"],
    ) -> DesktopKnowledgePage:
        self._require_database()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT lifecycle_state, stale_after FROM knowledge_pages WHERE page_id = ?
                    """,
                    (page_id,),
                ).fetchone()
                if row is None:
                    raise DesktopKnowledgePageError(
                        "knowledge_page_publication_required",
                        "Publish this Knowledge Page before changing its lifecycle.",
                    )
                previous_state = str(row[0])
                previous_stale_after = str(row[1]) if row[1] is not None else None
                next_state = target_state or previous_state
                next_stale_after = (
                    previous_stale_after
                    if target_stale_after is _UNCHANGED
                    else cast(str | None, target_stale_after)
                )
                if (next_state, next_stale_after) == (
                    previous_state,
                    previous_stale_after,
                ):
                    page = self._page_in(connection, page_id)
                    connection.commit()
                    return page
                now = _timestamp()
                connection.execute(
                    """
                    UPDATE knowledge_pages
                    SET lifecycle_state = ?, stale_after = ?, updated_at = ?
                    WHERE page_id = ?
                    """,
                    (next_state, next_stale_after, now, page_id),
                )
                invalidate_current_verification_in(
                    connection,
                    page_id=page_id,
                    invalidated_at=now,
                    reason="lifecycle_changed",
                )
                _record_event(
                    connection,
                    page_id=page_id,
                    event_type=event_type,
                    previous_state=previous_state,
                    new_state=next_state,
                    previous_stale_after=previous_stale_after,
                    new_stale_after=next_stale_after,
                    occurred_at=now,
                )
                page = self._page_in(connection, page_id)
                connection.commit()
                return page
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()


_UNCHANGED = object()


def _normalize_stale_after(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise DesktopKnowledgePageError(
            "knowledge_page_stale_after_invalid",
            "stale_after must be an ISO-8601 timestamp with a time zone.",
        ) from error
    if parsed.tzinfo is None:
        raise DesktopKnowledgePageError(
            "knowledge_page_stale_after_invalid",
            "stale_after must include a time zone.",
        )
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _record_event(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    event_type: str,
    previous_state: str,
    new_state: str | None,
    previous_stale_after: str | None,
    new_stale_after: str | None,
    occurred_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_page_lifecycle_events (
            event_id, page_id, event_type, previous_lifecycle_state,
            new_lifecycle_state, previous_stale_after, new_stale_after, actor, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            page_id,
            event_type,
            previous_state,
            new_state,
            previous_stale_after,
            new_stale_after,
            _LOCAL_ACTOR,
            occurred_at,
        ),
    )


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
