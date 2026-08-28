"""Explicit, idempotent adoption of generated knowledge into user-owned drafts."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from openkb.desktop_knowledge_generations import KnowledgeGenerationSource
from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange
from openkb.desktop_knowledge_sources import (
    DesktopKnowledgeSourceMapEntry,
    generation_source_map_in,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_AUTO_RECONCILIATION_CONFIDENCE = 0.9


@dataclass(frozen=True)
class DesktopKnowledgeAdoptionResult:
    status: str
    generation_id: int
    item_key: str
    page_id: str | None = None
    candidates: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "generation_id": self.generation_id,
            "item_key": self.item_key,
            "page_id": self.page_id,
            "origin": {
                "generation_id": self.generation_id,
                "item_key": self.item_key,
            },
            "candidates": [dict(candidate) for candidate in self.candidates],
        }


@dataclass(frozen=True)
class _RecordedAdoptionRequest:
    result: DesktopKnowledgeAdoptionResult
    decision: str | None
    candidate_page_id: str | None


class DesktopKnowledgeAdoptionService:
    """Create a Working Draft while retaining immutable generated lineage."""

    def __init__(self, kb_dir: Path) -> None:
        resolved = kb_dir.expanduser().resolve()
        self._kb_dir = resolved
        self._state_dir = desktop_state_dir(resolved)
        self._database_path = desktop_state_database_path(resolved)

    def adopt(
        self,
        *,
        generation_id: int,
        item_key: str,
        request_id: str,
        decision: str | None = None,
        candidate_page_id: str | None = None,
    ) -> DesktopKnowledgeAdoptionResult:
        if generation_id < 1 or not item_key or not request_id:
            raise ValueError("knowledge_adoption_invalid")
        if decision not in {None, "create_new", "use_existing"}:
            raise ValueError("knowledge_adoption_decision_invalid")
        if (decision == "use_existing") != (candidate_page_id is not None):
            raise ValueError("knowledge_adoption_candidate_invalid")
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                repeated = _request_result_in(connection, request_id)
                if repeated is not None:
                    if (
                        repeated.result.generation_id != generation_id
                        or repeated.result.item_key != item_key
                        or repeated.decision != decision
                        or repeated.candidate_page_id != candidate_page_id
                    ):
                        raise ValueError("knowledge_adoption_request_conflict")
                    connection.rollback()
                    return repeated.result
                generated = connection.execute(
                    """
                    SELECT kind, title, normalized_title, content_markdown,
                        content_sha256, source_document_id, entity_subtype,
                        aliases_json, tags_json, analysis_provenance_json
                    FROM knowledge_generation_items
                    WHERE generation_id = ? AND item_key = ?
                    """,
                    (generation_id, item_key),
                ).fetchone()
                if generated is None:
                    raise ValueError("knowledge_workspace_item_not_found")
                matches = _user_matches_in(
                    connection,
                    kind=str(generated[0]),
                    normalized_title=str(generated[2]),
                )
                existing_origin = connection.execute(
                    """
                    SELECT page_id FROM knowledge_origin_references
                    WHERE generation_id = ? AND item_key = ?
                    """,
                    (generation_id, item_key),
                ).fetchone()
                if existing_origin is not None:
                    origin_page_id = str(existing_origin[0])
                    pending = _pending_reconciliation_in(
                        connection,
                        generated=generated,
                        target_page_id=origin_page_id,
                    )
                    candidate = next(
                        (match for match in matches if match["page_id"] == origin_page_id),
                        None,
                    )
                    result = (
                        DesktopKnowledgeAdoptionResult(
                            "reconciliation_required",
                            generation_id,
                            item_key,
                            candidates=(candidate,),
                        )
                        if pending and candidate is not None
                        else DesktopKnowledgeAdoptionResult(
                            "already_adopted",
                            generation_id,
                            item_key,
                            origin_page_id,
                        )
                    )
                    _record_request_in(
                        connection,
                        request_id,
                        result,
                        decision=decision,
                        candidate_page_id=candidate_page_id,
                    )
                    connection.commit()
                    return result
                exact = tuple(match for match in matches if match["match"] == "exact")
                high_confidence = tuple(
                    match
                    for match in matches
                    if _match_confidence(match) >= _AUTO_RECONCILIATION_CONFIDENCE
                )
                selected = next(
                    (
                        match
                        for match in matches
                        if match["page_id"] == candidate_page_id
                    ),
                    None,
                )
                if decision == "use_existing" and selected is None:
                    raise ValueError("knowledge_adoption_candidate_not_found")
                target = selected if decision == "use_existing" else None
                if decision is None and len(exact) == 1:
                    target = exact[0]
                elif decision is None and not exact and len(high_confidence) == 1:
                    target = high_confidence[0]
                if target is not None:
                    DesktopKnowledgeReconciliationService(
                        self._kb_dir
                    ).record_adoption_match_in(
                        connection,
                        document_id=str(generated[5]),
                        change=_incoming_change_in(
                            connection,
                            generation_id=generation_id,
                            item_key=item_key,
                            generated=generated,
                        ),
                        target_page_id=str(target["page_id"]),
                        observed_generation_id=generation_id,
                    )
                    _record_origin_in(
                        connection,
                        generation_id=generation_id,
                        item_key=item_key,
                        page_id=str(target["page_id"]),
                    )
                    result = DesktopKnowledgeAdoptionResult(
                        "reconciliation_required",
                        generation_id,
                        item_key,
                        candidates=(target,),
                    )
                    _record_request_in(
                        connection,
                        request_id,
                        result,
                        decision=decision,
                        candidate_page_id=candidate_page_id,
                    )
                    connection.commit()
                    return result
                if decision is None and matches:
                    result = DesktopKnowledgeAdoptionResult(
                        "choice_required",
                        generation_id,
                        item_key,
                        candidates=matches,
                    )
                    _record_request_in(
                        connection,
                        request_id,
                        result,
                        decision=decision,
                        candidate_page_id=candidate_page_id,
                    )
                    connection.commit()
                    return result
                page_id = _create_draft_in(
                    connection,
                    generation_id=generation_id,
                    item_key=item_key,
                    generated=generated,
                )
                result = DesktopKnowledgeAdoptionResult("adopted", generation_id, item_key, page_id)
                _record_request_in(
                    connection,
                    request_id,
                    result,
                    decision=decision,
                    candidate_page_id=candidate_page_id,
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _request_result_in(
    connection: sqlite3.Connection, request_id: str
) -> _RecordedAdoptionRequest | None:
    row = connection.execute(
        """
        SELECT status, generation_id, item_key, page_id, candidates_json,
            decision, candidate_page_id
        FROM knowledge_adoption_requests WHERE request_id = ?
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    candidates = json.loads(str(row[4]))
    return _RecordedAdoptionRequest(
        DesktopKnowledgeAdoptionResult(
            str(row[0]),
            int(row[1]),
            str(row[2]),
            str(row[3]) if row[3] is not None else None,
            tuple(dict(candidate) for candidate in candidates if isinstance(candidate, dict)),
        ),
        str(row[5]) if row[5] is not None else None,
        str(row[6]) if row[6] is not None else None,
    )


def _incoming_change_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    item_key: str,
    generated: tuple[object, ...],
) -> IncomingKnowledgeChange:
    sources = tuple(
        KnowledgeGenerationSource(source.source_id, source.evidence_id, source.claim_text)
        for source in _unique_available_sources(
            generation_source_map_in(connection, generation_id, item_key)
        )
    )
    return IncomingKnowledgeChange(
        source_block_id=None,
        kind=str(generated[0]),
        is_kind_explicit=True,
        title=str(generated[1]),
        normalized_title=str(generated[2]),
        content_markdown=str(generated[3]),
        content_sha256=str(generated[4]),
        entity_subtype=str(generated[6]) if generated[6] is not None else None,
        aliases=decode_knowledge_labels(generated[7]),
        tags=decode_knowledge_labels(generated[8]),
        sources=sources,
        analysis_provenance_json=(
            str(generated[9]) if generated[9] is not None else None
        ),
    )


def _create_draft_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    item_key: str,
    generated: tuple[object, ...],
) -> str:
    page_id = uuid.uuid4().hex
    now = _timestamp()
    connection.execute(
        """
        INSERT INTO knowledge_page_working_drafts (
            page_id, kind, title, normalized_title, content_markdown,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            page_id,
            str(generated[0]),
            str(generated[1]),
            str(generated[2]),
            str(generated[3]),
            now,
            now,
        ),
    )
    for source in _unique_available_sources(
        generation_source_map_in(connection, generation_id, item_key)
    ):
        connection.execute(
            """
            INSERT INTO knowledge_page_working_sources (
                page_id, source_id, evidence_id, claim_text, document_id,
                document_name, section, locator_json, excerpt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                source.source_id,
                source.evidence_id,
                source.claim_text,
                source.document_id,
                source.document_name,
                source.section,
                json.dumps(
                    source.locator,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                source.excerpt,
                now,
            ),
        )
    _record_origin_in(
        connection,
        generation_id=generation_id,
        item_key=item_key,
        page_id=page_id,
        now=now,
    )
    connection.execute(
        """
        INSERT INTO knowledge_page_ui_state (singleton, last_page_id)
        VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET
            last_page_id = excluded.last_page_id
        """,
        (page_id,),
    )
    return page_id


def _record_origin_in(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    item_key: str,
    page_id: str,
    now: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_origin_references (
            generation_id, item_key, page_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (generation_id, item_key, page_id, now or _timestamp()),
    )


def _record_request_in(
    connection: sqlite3.Connection,
    request_id: str,
    result: DesktopKnowledgeAdoptionResult,
    *,
    decision: str | None,
    candidate_page_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_adoption_requests (
            request_id, generation_id, item_key, status, page_id,
            candidates_json, decision, candidate_page_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            result.generation_id,
            result.item_key,
            result.status,
            result.page_id,
            json.dumps(
                result.candidates,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            decision,
            candidate_page_id,
            _timestamp(),
        ),
    )


def _pending_reconciliation_in(
    connection: sqlite3.Connection,
    *,
    generated: tuple[object, ...],
    target_page_id: str,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM knowledge_reconciliation_candidates
            WHERE document_id = ? AND kind = ? AND normalized_title = ?
                AND content_sha256 = ? AND target_page_id = ?
                AND status = 'pending_conflict' AND resolution_status IS NULL
            LIMIT 1
            """,
            (
                str(generated[5]),
                str(generated[0]),
                str(generated[2]),
                str(generated[4]),
                target_page_id,
            ),
        ).fetchone()
        is not None
    )


def _user_matches_in(
    connection: sqlite3.Connection,
    *,
    kind: str,
    normalized_title: str,
) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        """
        WITH user_items AS (
            SELECT drafts.page_id, drafts.kind, drafts.title, drafts.normalized_title,
                CASE WHEN pages.page_id IS NULL THEN 'draft' ELSE 'unpublished_changes' END
                    AS publication_state
            FROM knowledge_page_working_drafts AS drafts
            LEFT JOIN knowledge_pages AS pages ON pages.page_id = drafts.page_id
            UNION ALL
            SELECT pages.page_id, pages.kind, pages.title, pages.normalized_title, 'published'
            FROM knowledge_pages AS pages
            WHERE NOT EXISTS (
                SELECT 1 FROM knowledge_page_working_drafts AS drafts
                WHERE drafts.page_id = pages.page_id
            )
        )
        SELECT page_id, title, normalized_title, publication_state
        FROM user_items WHERE kind = ?
        ORDER BY title COLLATE NOCASE, page_id
        """,
        (kind,),
    ).fetchall()
    matches: list[dict[str, object]] = []
    for row in rows:
        candidate_normalized = str(row[2])
        confidence = SequenceMatcher(None, normalized_title, candidate_normalized).ratio()
        if candidate_normalized == normalized_title:
            match = "exact"
            confidence = 1.0
        elif confidence >= 0.72:
            match = "possible"
        else:
            continue
        matches.append(
            {
                "page_id": str(row[0]),
                "title": str(row[1]),
                "publication_state": str(row[3]),
                "match": match,
                "confidence": round(confidence, 3),
            }
        )
    return tuple(matches)


def _match_confidence(match: dict[str, object]) -> float:
    value = match.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _unique_available_sources(
    sources: tuple[DesktopKnowledgeSourceMapEntry, ...],
) -> tuple[DesktopKnowledgeSourceMapEntry, ...]:
    selected: dict[tuple[str, str], DesktopKnowledgeSourceMapEntry] = {}
    for source in sources:
        key = (source.source_id, source.claim_text)
        if source.availability == "available" and key not in selected:
            selected[key] = source
    return tuple(selected.values())


def _timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()
