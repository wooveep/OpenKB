"""User-confirmed D3 Document Version Candidates for the Desktop SQLite authority."""

from __future__ import annotations

import datetime as dt
import difflib
import re
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_import_types import DesktopDocumentVersionCandidate
from openkb.desktop_lexical import cjk_bigrams, is_cjk_text
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_MAX_PROFILE_CHARACTERS = 12_000
_MAX_LEXICAL_TERMS = 256
_MAX_COMPARISON_DOCUMENTS = 96
_MAX_PROFILE_BLOCKS = 64
_MAX_CANDIDATES = 8
_MIN_LEXICAL_SCORE = 0.30
_MIN_CHARACTER_SCORE = 0.35
_TERM_PATTERN = re.compile(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+")
_PENDING = "pending"
_DECISIONS = {"link_to_candidate", "keep_separate"}


class DesktopDocumentVersionService:
    """Use document text only; D3 candidates never auto-link sources or consult knowledge pages."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._state_dir = desktop_state_dir(self._kb_dir)

    def record_candidates(
        self, document_id: str, blocks: tuple[DocumentIRBlock, ...]
    ) -> tuple[DesktopDocumentVersionCandidate, ...]:
        """Persist bounded lexical/character candidates after an import has published.

        The import worker already owns the KB ingestion lock.  This method therefore
        opens its own short SQLite transaction but does not acquire that lock again.
        """
        profile = _profile_text(block.text for block in blocks)
        terms = _lexical_terms(profile)
        if not profile or len(terms) < 2:
            return ()
        connection = self._connect()
        try:
            with connection:
                document = connection.execute(
                    """
                    SELECT document_content_fingerprints.normalized_body_sha256
                    FROM source_documents
                    LEFT JOIN document_content_fingerprints
                        ON document_content_fingerprints.document_id = source_documents.document_id
                    WHERE source_documents.document_id = ?
                        AND source_documents.availability = 'available'
                    """,
                    (document_id,),
                ).fetchone()
                if document is None:
                    return ()
                normalized_body_sha256 = str(document[0]) if document[0] is not None else None
                candidates = _candidate_profiles(connection, document_id)
                scored = _scored_candidates(profile, terms, normalized_body_sha256, candidates)
                for candidate in scored:
                    connection.execute(
                        """
                        INSERT INTO document_version_candidates (
                            candidate_id, document_id, candidate_document_id,
                            lexical_score, character_score, reason, status,
                            resolution, created_at, resolved_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, 'lexical_character_similarity', 'pending', NULL, ?, NULL
                        )
                        ON CONFLICT(document_id, candidate_document_id) DO UPDATE SET
                            lexical_score = excluded.lexical_score,
                            character_score = excluded.character_score,
                            reason = excluded.reason,
                            created_at = excluded.created_at
                        WHERE document_version_candidates.status = 'pending'
                        """,
                        (
                            uuid.uuid4().hex,
                            document_id,
                            candidate.document_id,
                            candidate.lexical_score,
                            candidate.character_score,
                            _timestamp(),
                        ),
                    )
                return self._pending_candidates_in(connection, document_id=document_id)
        finally:
            connection.close()

    def list_candidates(self) -> tuple[DesktopDocumentVersionCandidate, ...]:
        """Return only still-actionable candidates for the Desktop review queue."""
        connection = self._connect()
        try:
            return self._pending_candidates_in(connection)
        finally:
            connection.close()

    def resolve_candidate(
        self, candidate_id: str, decision: str
    ) -> DesktopDocumentVersionCandidate:
        """Link an incoming document only after an explicit user decision."""
        if decision not in _DECISIONS:
            raise DesktopImportError(
                "invalid_document_version_decision", "Choose how to resolve the version candidate."
            )
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT document_version_candidates.document_id,
                        document_version_candidates.candidate_document_id,
                        candidate_members.source_id,
                        incoming.availability, matched.availability,
                        document_version_candidates.status
                    FROM document_version_candidates
                    JOIN source_documents AS incoming
                        ON incoming.document_id = document_version_candidates.document_id
                    JOIN source_documents AS matched
                        ON matched.document_id = document_version_candidates.candidate_document_id
                    JOIN document_version_members AS candidate_members
                        ON candidate_members.document_id = matched.document_id
                    WHERE document_version_candidates.candidate_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
                if row is None:
                    raise DesktopImportError(
                        "document_version_candidate_not_found",
                        "The selected document version candidate was not found.",
                    )
                document_id = str(row[0])
                candidate_source_id = str(row[2])
                if str(row[5]) != _PENDING:
                    raise DesktopImportError(
                        "document_version_candidate_resolved",
                        "The selected document version candidate has already been resolved.",
                    )
                if str(row[3]) != "available" or str(row[4]) != "available":
                    raise DesktopImportError(
                        "document_version_candidate_unavailable",
                        "Both documents must remain available to create a version relationship.",
                    )
                now = _timestamp()
                if decision == "link_to_candidate":
                    source_id = candidate_source_id
                    resolution = "linked_existing_source"
                    other_resolution = "other_candidate_selected"
                else:
                    source_id = document_id
                    resolution = "kept_independent"
                    other_resolution = "kept_independent"
                    connection.execute(
                        """
                        INSERT INTO document_version_sources (source_id, created_at)
                        VALUES (?, ?)
                        ON CONFLICT(source_id) DO NOTHING
                        """,
                        (source_id, now),
                    )
                connection.execute(
                    """
                    UPDATE document_version_members
                    SET source_id = ?, linked_at = ?
                    WHERE document_id = ?
                    """,
                    (source_id, now, document_id),
                )
                connection.execute(
                    """
                    UPDATE document_version_candidates
                    SET status = ?, resolution = ?, resolved_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        "accepted" if decision == "link_to_candidate" else "rejected",
                        resolution,
                        now,
                        candidate_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE document_version_candidates
                    SET status = 'dismissed', resolution = ?, resolved_at = ?
                    WHERE document_id = ? AND candidate_id <> ? AND status = 'pending'
                    """,
                    (other_resolution, now, document_id, candidate_id),
                )
                connection.commit()
                candidate = self._candidate_by_id_in(connection, candidate_id)
                if candidate is None:
                    raise RuntimeError("Resolved document version candidate disappeared.")
                return candidate
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                "Open a Desktop Knowledge Base before reviewing document versions.",
            )
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _pending_candidates_in(
        self, connection: sqlite3.Connection, *, document_id: str | None = None
    ) -> tuple[DesktopDocumentVersionCandidate, ...]:
        clauses = [
            "document_version_candidates.status = 'pending'",
            "incoming.availability = 'available'",
            "matched.availability = 'available'",
        ]
        parameters: list[object] = []
        if document_id is not None:
            clauses.append("document_version_candidates.document_id = ?")
            parameters.append(document_id)
        rows = connection.execute(
            f"""
            SELECT document_version_candidates.candidate_id,
                document_version_candidates.document_id, incoming.display_name,
                document_version_candidates.candidate_document_id, matched.display_name,
                document_version_candidates.lexical_score,
                document_version_candidates.character_score,
                document_version_candidates.reason, document_version_candidates.status
            FROM document_version_candidates
            JOIN source_documents AS incoming
                ON incoming.document_id = document_version_candidates.document_id
            JOIN source_documents AS matched
                ON matched.document_id = document_version_candidates.candidate_document_id
            WHERE {" AND ".join(clauses)}
            ORDER BY document_version_candidates.created_at DESC,
                document_version_candidates.candidate_id
            """,
            parameters,
        ).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    def _candidate_by_id_in(
        self, connection: sqlite3.Connection, candidate_id: str
    ) -> DesktopDocumentVersionCandidate | None:
        row = connection.execute(
            """
            SELECT document_version_candidates.candidate_id,
                document_version_candidates.document_id, incoming.display_name,
                document_version_candidates.candidate_document_id, matched.display_name,
                document_version_candidates.lexical_score,
                document_version_candidates.character_score,
                document_version_candidates.reason, document_version_candidates.status
            FROM document_version_candidates
            JOIN source_documents AS incoming
                ON incoming.document_id = document_version_candidates.document_id
            JOIN source_documents AS matched
                ON matched.document_id = document_version_candidates.candidate_document_id
            WHERE document_version_candidates.candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        return _candidate_from_row(row) if row is not None else None


class _CandidateProfile:
    def __init__(self, document_id: str, text: str, normalized_body_sha256: str | None) -> None:
        self.document_id = document_id
        self.text = text
        self.normalized_body_sha256 = normalized_body_sha256


class _ScoredCandidate:
    def __init__(self, document_id: str, lexical_score: float, character_score: float) -> None:
        self.document_id = document_id
        self.lexical_score = lexical_score
        self.character_score = character_score

    @property
    def score(self) -> float:
        return (self.lexical_score * 0.65) + (self.character_score * 0.35)


def _candidate_profiles(
    connection: sqlite3.Connection, document_id: str
) -> tuple[_CandidateProfile, ...]:
    rows = connection.execute(
        """
        SELECT document_ir_blocks.document_id,
            document_content_fingerprints.normalized_body_sha256
        FROM document_ir_blocks
        JOIN source_documents ON source_documents.document_id = document_ir_blocks.document_id
        LEFT JOIN document_content_fingerprints
            ON document_content_fingerprints.document_id = source_documents.document_id
        WHERE source_documents.availability = 'available'
            AND document_ir_blocks.document_id <> ?
        GROUP BY document_ir_blocks.document_id
        ORDER BY source_documents.created_at DESC, document_ir_blocks.document_id
        LIMIT ?
        """,
        (document_id, _MAX_COMPARISON_DOCUMENTS),
    ).fetchall()
    document_ids = tuple(str(row[0]) for row in rows)
    if not document_ids:
        return ()
    return tuple(
        _CandidateProfile(
            document_id=str(row[0]),
            text=_candidate_profile_text(connection, str(row[0])),
            normalized_body_sha256=str(row[1]) if row[1] is not None else None,
        )
        for row in rows
    )


def _candidate_profile_text(connection: sqlite3.Connection, document_id: str) -> str:
    """Read a bounded prefix of a candidate before any D3 scoring occurs."""
    rows = connection.execute(
        """
        SELECT substr(text, 1, ?)
        FROM document_ir_blocks
        WHERE document_id = ?
        ORDER BY ordinal
        LIMIT ?
        """,
        (_MAX_PROFILE_CHARACTERS, document_id, _MAX_PROFILE_BLOCKS),
    )
    return _profile_text(row[0] for row in rows)


def _scored_candidates(
    text: str,
    terms: frozenset[str],
    normalized_body_sha256: str | None,
    candidates: tuple[_CandidateProfile, ...],
) -> tuple[_ScoredCandidate, ...]:
    scored: list[_ScoredCandidate] = []
    for candidate in candidates:
        if normalized_body_sha256 and candidate.normalized_body_sha256 == normalized_body_sha256:
            continue
        candidate_terms = _lexical_terms(candidate.text)
        lexical_score = _jaccard_score(terms, candidate_terms)
        if lexical_score < _MIN_LEXICAL_SCORE:
            continue
        character_score = difflib.SequenceMatcher(
            None, text, candidate.text, autojunk=False
        ).ratio()
        if character_score < _MIN_CHARACTER_SCORE:
            continue
        scored.append(_ScoredCandidate(candidate.document_id, lexical_score, character_score))
    scored.sort(key=lambda candidate: (-candidate.score, candidate.document_id))
    return tuple(scored[:_MAX_CANDIDATES])


def _profile_text(values: Iterable[object]) -> str:
    parts: list[str] = []
    remaining = _MAX_PROFILE_CHARACTERS
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = " ".join(value.split())
        if not normalized:
            continue
        parts.append(normalized[:remaining])
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    return "\n".join(parts)


def _lexical_terms(value: str) -> frozenset[str]:
    terms: list[str] = []
    for match in _TERM_PATTERN.finditer(value.casefold()):
        token = match.group(0)
        token_values = cjk_bigrams(token) if is_cjk_text(token) else (token,)
        for item in token_values:
            if item not in terms:
                terms.append(item)
            if len(terms) == _MAX_LEXICAL_TERMS:
                return frozenset(terms)
    return frozenset(terms)


def _jaccard_score(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _candidate_from_row(row: tuple[object, ...]) -> DesktopDocumentVersionCandidate:
    return DesktopDocumentVersionCandidate(
        candidate_id=str(row[0]),
        document_id=str(row[1]),
        document_name=str(row[2]),
        candidate_document_id=str(row[3]),
        candidate_document_name=str(row[4]),
        lexical_score=float(str(row[5])),
        character_score=float(str(row[6])),
        reason=str(row[7]),
        status=str(row[8]),
    )


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
