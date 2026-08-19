"""Revision-bound human review state for Desktop Knowledge Pages."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

from openkb.desktop_knowledge_sources import (
    DesktopKnowledgeSourceMapEntry,
    publication_diagnostics_in,
    revision_source_map_in,
)

DesktopKnowledgeVerificationState = Literal["unverified", "human_reviewed"]
DesktopKnowledgeVerificationReason = Literal[
    "publish_required",
    "working_draft_not_verifiable",
    "not_verified",
    "revision_changed",
    "publication_gate_blocked",
    "legacy_unmapped_not_verifiable",
]
LOCAL_HUMAN_ACTOR = "local_user"


@dataclass(frozen=True)
class DesktopKnowledgeVerificationStatus:
    state: DesktopKnowledgeVerificationState
    can_verify: bool
    reason: DesktopKnowledgeVerificationReason | None
    actor: str | None = None
    verified_at: str | None = None
    revision_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "can_verify": self.can_verify,
            "reason": self.reason,
            "actor": self.actor,
            "verified_at": self.verified_at,
            "revision_id": self.revision_id,
        }


def verification_status_in(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    revision_id: str | None,
    content_markdown: str,
    provenance_state: str,
    source_map: tuple[DesktopKnowledgeSourceMapEntry, ...],
    has_working_draft: bool,
) -> DesktopKnowledgeVerificationStatus:
    """Project the current revision's review and next valid user action."""
    if revision_id is None:
        return DesktopKnowledgeVerificationStatus("unverified", False, "publish_required")
    verification = connection.execute(
        """
        SELECT actor, verified_at FROM knowledge_page_verifications
        WHERE revision_id = ? AND invalidated_at IS NULL
        """,
        (revision_id,),
    ).fetchone()
    diagnostics = publication_diagnostics_in(connection, content_markdown, source_map)
    reason: DesktopKnowledgeVerificationReason | None = None
    if has_working_draft:
        reason = "working_draft_not_verifiable"
    elif provenance_state == "legacy_unmapped":
        reason = "legacy_unmapped_not_verifiable"
    elif diagnostics:
        reason = "publication_gate_blocked"
    if verification is not None:
        return DesktopKnowledgeVerificationStatus(
            "human_reviewed",
            False,
            reason,
            actor=str(verification[0]),
            verified_at=str(verification[1]),
            revision_id=revision_id,
        )
    if reason is not None:
        return DesktopKnowledgeVerificationStatus("unverified", False, reason)
    previous = connection.execute(
        """
        SELECT 1 FROM knowledge_page_verifications AS verifications
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = verifications.revision_id
        WHERE revisions.page_id = ? LIMIT 1
        """,
        (page_id,),
    ).fetchone()
    return DesktopKnowledgeVerificationStatus(
        "unverified", True, "revision_changed" if previous is not None else "not_verified"
    )


def verify_current_revision_in(
    connection: sqlite3.Connection, *, page_id: str, verified_at: str
) -> None:
    """Record an explicit local-human review after re-running the Publication Gate."""
    if connection.execute(
        "SELECT 1 FROM knowledge_page_working_drafts WHERE page_id = ?", (page_id,)
    ).fetchone() is not None:
        raise ValueError("knowledge_verification_requires_current_publication")
    row = connection.execute(
        """
        SELECT pages.current_revision_id, revisions.content_markdown,
            revisions.provenance_state
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE pages.page_id = ?
        """,
        (page_id,),
    ).fetchone()
    if row is None:
        raise ValueError("knowledge_verification_requires_current_publication")
    revision_id, content_markdown, provenance_state = (str(value) for value in row)
    if provenance_state == "legacy_unmapped":
        raise ValueError("knowledge_verification_legacy_unmapped")
    source_map = revision_source_map_in(connection, revision_id)
    if publication_diagnostics_in(connection, content_markdown, source_map):
        raise ValueError("knowledge_verification_blocked")
    if connection.execute(
        """
        SELECT 1 FROM knowledge_page_verifications
        WHERE revision_id = ? AND invalidated_at IS NULL
        """,
        (revision_id,),
    ).fetchone() is not None:
        return
    connection.execute(
        """
        INSERT INTO knowledge_page_verifications (
            verification_id, revision_id, verification_kind, actor, verified_at
        ) VALUES (?, ?, 'human_reviewed', ?, ?)
        """,
        (uuid.uuid4().hex, revision_id, LOCAL_HUMAN_ACTOR, verified_at),
    )
