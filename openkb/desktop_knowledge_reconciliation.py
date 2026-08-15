"""Reconcile imported Concept/Entity candidates without changing document identity.

This is intentionally a small, deterministic first pass.  It extracts bounded
section candidates from Document IR, keeps published derived knowledge as
generation snapshots, and leaves incompatible changes in the review queue.
User-owned Knowledge Page revisions remain separate SQLite authority.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_import_types import DesktopKnowledgeReconciliationConflict
from openkb.desktop_knowledge_titles import normalize_knowledge_title
from openkb.desktop_workspace import desktop_state_database_path

_MAX_CANDIDATES_PER_DOCUMENT = 32
_MAX_CANDIDATE_CHARACTERS = 24_000
_KIND_PREFIX = re.compile(
    r"^\s*(?:(?P<english>concept|entity)|(?P<chinese>概念|实体))\s*[:：]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_FIELD_PATTERN = re.compile(r"^\s*(?:[-*+]\s*)?(?P<key>[^:：\n]{1,80})\s*[:：]\s*\S")


@dataclass(frozen=True)
class _IncomingChange:
    source_block_id: str | None
    kind: str
    is_kind_explicit: bool
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str


@dataclass(frozen=True)
class _Baseline:
    kind: str
    baseline_id: str
    title: str
    content_markdown: str


class DesktopKnowledgeReconciliationService:
    """Persist compatible additions and isolate conflicts from published knowledge."""

    def __init__(self, kb_dir: Path) -> None:
        self._database_path = desktop_state_database_path(kb_dir.expanduser().resolve())

    def record_document_changes(
        self, document_id: str, blocks: tuple[DocumentIRBlock, ...]
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Classify one published document's deterministic incoming knowledge changes.

        The import worker already owns the knowledge-base ingestion lock when it
        calls this method.  As with D3 candidate recording, this method only owns
        a short SQLite transaction and must not acquire that file lock again.
        """
        connection = self._connect()
        try:
            with connection:
                document = connection.execute(
                    """
                    SELECT display_name FROM source_documents
                    WHERE document_id = ? AND availability = 'available'
                    """,
                    (document_id,),
                ).fetchone()
                if document is None:
                    return ()
                document_name = str(document[0])
                conflicts: list[DesktopKnowledgeReconciliationConflict] = []
                for parsed_change in _extract_changes(blocks, document_name):
                    current_generation_id = _current_generation_id_in(connection)
                    for change in _resolved_kinds_in(
                        connection, current_generation_id, parsed_change
                    ):
                        conflict = self._reconcile_change_in(
                            connection,
                            document_id=document_id,
                            document_name=document_name,
                            change=change,
                        )
                        if conflict is not None:
                            conflicts.append(conflict)
                return tuple(conflicts)
        finally:
            connection.close()

    def list_conflicts(self) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Return pending conflicts only; compatible additions never need review."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT candidates.candidate_id, candidates.document_id, documents.display_name,
                    candidates.kind, candidates.title, candidates.content_markdown,
                    candidates.baseline_kind, candidates.baseline_title,
                    candidates.baseline_content_markdown, candidates.observed_generation_id
                FROM knowledge_reconciliation_candidates AS candidates
                JOIN source_documents AS documents ON documents.document_id = candidates.document_id
                WHERE candidates.status = 'pending_conflict'
                    AND documents.availability = 'available'
                ORDER BY candidates.created_at DESC, candidates.candidate_id
                """
            ).fetchall()
            return tuple(_conflict_from_row(row) for row in rows)
        finally:
            connection.close()

    def current_generation_id(self) -> int | None:
        """Expose the stable current generation for transport and focused checks."""
        connection = self._connect()
        try:
            return _current_generation_id_in(connection)
        finally:
            connection.close()

    def _reconcile_change_in(
        self,
        connection: sqlite3.Connection,
        *,
        document_id: str,
        document_name: str,
        change: _IncomingChange,
    ) -> DesktopKnowledgeReconciliationConflict | None:
        current_generation_id = _current_generation_id_in(connection)
        baselines = _baselines_in(connection, current_generation_id, change)
        relationships = tuple(
            _relationship(change.content_markdown, baseline.content_markdown)
            for baseline in baselines
        )
        conflict_index = next(
            (index for index, value in enumerate(relationships) if value == "conflict"), None
        )
        now = _timestamp()

        if conflict_index is not None:
            conflict_baseline = baselines[conflict_index]
            candidate = _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification="conflict",
                status="pending_conflict",
                baseline=conflict_baseline,
                observed_generation_id=current_generation_id,
                now=now,
            )
            return DesktopKnowledgeReconciliationConflict(
                candidate_id=candidate,
                document_id=document_id,
                document_name=document_name,
                kind=change.kind,
                title=change.title,
                content_markdown=change.content_markdown,
                baseline_kind=conflict_baseline.kind,
                baseline_title=conflict_baseline.title,
                baseline_content_markdown=conflict_baseline.content_markdown,
                observed_generation_id=current_generation_id,
            )

        has_addition = not baselines or any(
            value == "compatible_addition" for value in relationships
        )
        baseline_for_record = baselines[0] if baselines else None
        if has_addition:
            published_generation_id = _advance_generation_in(
                connection,
                current_generation_id=current_generation_id,
                document_id=document_id,
                change=change,
                now=now,
            )
            _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification="compatible_addition",
                status="auto_reconciled",
                baseline=baseline_for_record,
                observed_generation_id=published_generation_id,
                now=now,
            )
        else:
            _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification="duplicate",
                status="auto_reconciled",
                baseline=baseline_for_record,
                observed_generation_id=current_generation_id,
                now=now,
            )
        return None

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                "Open a Desktop Knowledge Base before reconciling imported knowledge.",
            )
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _extract_changes(
    blocks: tuple[DocumentIRBlock, ...], document_name: str
) -> tuple[_IncomingChange, ...]:
    headings = tuple(
        (index, block) for index, block in enumerate(blocks) if block.kind == "heading"
    )
    candidates: list[tuple[str | None, str, bool, str, str]] = []
    if headings:
        for position, (index, heading) in enumerate(headings[:_MAX_CANDIDATES_PER_DOCUMENT]):
            end = headings[position + 1][0] if position + 1 < len(headings) else len(blocks)
            body = _bounded_content(
                block.text for block in blocks[index + 1 : end] if block.kind != "heading"
            )
            if body:
                kind, title, is_kind_explicit = _kind_and_title(heading.text)
                candidates.append((heading.block_id, kind, is_kind_explicit, title, body))
    else:
        body = _bounded_content(block.text for block in blocks)
        if body:
            candidates.append((None, "concept", False, Path(document_name).stem, body))
    return _merge_changes(candidates)


def _merge_changes(
    values: list[tuple[str | None, str, bool, str, str]],
) -> tuple[_IncomingChange, ...]:
    merged: dict[tuple[str, str, bool], tuple[str | None, str, bool, str, list[str]]] = {}
    for source_block_id, kind, is_kind_explicit, untrusted_title, content in values:
        title, normalized_title = _normalize_title(untrusted_title)
        if not title:
            continue
        key = kind, normalized_title, is_kind_explicit
        if key not in merged:
            merged[key] = (source_block_id, kind, is_kind_explicit, title, [content])
        elif content not in merged[key][4]:
            merged[key][4].append(content)
    changes: list[_IncomingChange] = []
    for source_block_id, kind, is_kind_explicit, title, contents in merged.values():
        content_markdown = _bounded_content(contents)
        if not content_markdown:
            continue
        _, normalized_title = _normalize_title(title)
        changes.append(
            _IncomingChange(
                source_block_id=source_block_id,
                kind=kind,
                is_kind_explicit=is_kind_explicit,
                title=title,
                normalized_title=normalized_title,
                content_markdown=content_markdown,
                content_sha256=_content_sha256(content_markdown),
            )
        )
    return tuple(changes)


def _kind_and_title(value: str) -> tuple[str, str, bool]:
    match = _KIND_PREFIX.match(value)
    if match is None:
        return "concept", value, False
    english = match.group("english")
    chinese = match.group("chinese")
    kind = (
        "entity"
        if (english is not None and english.casefold() == "entity") or chinese == "实体"
        else "concept"
    )
    return kind, str(match.group("title")), True


def _normalize_title(value: str) -> tuple[str, str]:
    return normalize_knowledge_title(value)


def _bounded_content(values: Iterable[object]) -> str:
    parts: list[str] = []
    remaining = _MAX_CANDIDATE_CHARACTERS
    for value in values:
        if not isinstance(value, str):
            continue
        content = value.strip()
        if not content:
            continue
        content = content[:remaining]
        parts.append(content)
        remaining -= len(content)
        if remaining <= 0:
            break
    return "\n\n".join(parts)


def _content_sha256(value: str) -> str:
    return hashlib.sha256(_normalized_content(value).encode("utf-8")).hexdigest()


def _normalized_content(value: str) -> str:
    return "\n".join(
        " ".join(line.split()) for line in value.splitlines() if line.strip()
    ).casefold()


def _content_units(value: str) -> frozenset[str]:
    return frozenset(part for part in _normalized_content(value).split("\n") if part)


def _relationship(incoming: str, baseline: str) -> str:
    if _content_sha256(incoming) == _content_sha256(baseline):
        return "duplicate"
    incoming_units = _content_units(incoming)
    baseline_units = _content_units(baseline)
    if incoming_units and incoming_units <= baseline_units:
        return "duplicate"
    if _is_compatible_structured_addition(incoming_units, baseline_units):
        return "compatible_addition"
    return "conflict"


def _is_compatible_structured_addition(
    incoming_units: frozenset[str], baseline_units: frozenset[str]
) -> bool:
    """Accept only additions that introduce new explicit fields.

    A line-set superset alone cannot distinguish an added fact from a
    contradictory replacement.  Requiring every retained and added statement
    to use a ``Field: value`` form makes the automatic path intentionally
    narrow: an existing field with a different value, or arbitrary prose,
    remains a reviewable conflict.
    """
    if not baseline_units or not baseline_units < incoming_units:
        return False
    baseline_fields = tuple(_field_key(unit) for unit in baseline_units)
    additional_fields = tuple(_field_key(unit) for unit in incoming_units - baseline_units)
    if any(field is None for field in (*baseline_fields, *additional_fields)):
        return False
    known_fields = tuple(str(field) for field in baseline_fields)
    added_fields = tuple(str(field) for field in additional_fields)
    return (
        len(set(known_fields)) == len(known_fields)
        and len(set(added_fields)) == len(added_fields)
        and all(field not in known_fields for field in added_fields)
    )


def _field_key(value: str) -> str | None:
    match = _FIELD_PATTERN.match(value)
    if match is None:
        return None
    key, _ = normalize_knowledge_title(str(match.group("key")))
    return key or None


def _resolved_kinds_in(
    connection: sqlite3.Connection,
    generation_id: int | None,
    change: _IncomingChange,
) -> tuple[_IncomingChange, ...]:
    """Match an unprefixed heading to established Concept/Entity identities."""
    if change.is_kind_explicit:
        return (change,)
    rows = connection.execute(
        """
        SELECT kind FROM knowledge_pages
        WHERE normalized_title = ?
        UNION
        SELECT kind FROM knowledge_generation_items
        WHERE generation_id = ? AND normalized_title = ?
        ORDER BY kind
        """,
        (change.normalized_title, generation_id, change.normalized_title),
    ).fetchall()
    kinds = tuple(str(row[0]) for row in rows)
    if not kinds:
        return (change,)
    return tuple(
        _IncomingChange(
            source_block_id=change.source_block_id,
            kind=kind,
            is_kind_explicit=True,
            title=change.title,
            normalized_title=change.normalized_title,
            content_markdown=change.content_markdown,
            content_sha256=change.content_sha256,
        )
        for kind in kinds
    )


def _baselines_in(
    connection: sqlite3.Connection, generation_id: int | None, change: _IncomingChange
) -> tuple[_Baseline, ...]:
    baselines: list[_Baseline] = []
    user = connection.execute(
        """
        SELECT revisions.revision_id, revisions.title, revisions.content_markdown
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE pages.kind = ? AND pages.normalized_title = ?
        """,
        (change.kind, change.normalized_title),
    ).fetchone()
    if user is not None:
        baselines.append(_Baseline("user_revision", str(user[0]), str(user[1]), str(user[2])))
    if generation_id is not None:
        published = connection.execute(
            """
            SELECT item_key, title, content_markdown
            FROM knowledge_generation_items
            WHERE generation_id = ? AND kind = ? AND normalized_title = ?
            """,
            (generation_id, change.kind, change.normalized_title),
        ).fetchone()
        if published is not None:
            baselines.append(
                _Baseline(
                    "published_generation", str(published[0]), str(published[1]), str(published[2])
                )
            )
    return tuple(baselines)


def _advance_generation_in(
    connection: sqlite3.Connection,
    *,
    current_generation_id: int | None,
    document_id: str,
    change: _IncomingChange,
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO knowledge_generations (parent_generation_id, created_at)
        VALUES (?, ?)
        """,
        (current_generation_id, now),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Knowledge generation insert did not return an identifier.")
    generation_id = cursor.lastrowid
    if current_generation_id is not None:
        connection.execute(
            """
            INSERT INTO knowledge_generation_items (
                generation_id, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at
            )
            SELECT ?, item_key, kind, title, normalized_title,
                content_markdown, content_sha256, source_document_id, created_at
            FROM knowledge_generation_items WHERE generation_id = ?
            """,
            (generation_id, current_generation_id),
        )
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
                content_markdown, content_sha256, source_document_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                uuid.uuid4().hex,
                change.kind,
                change.title,
                change.normalized_title,
                change.content_markdown,
                change.content_sha256,
                document_id,
                now,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE knowledge_generation_items
            SET title = ?, content_markdown = ?, content_sha256 = ?,
                source_document_id = ?, created_at = ?
            WHERE generation_id = ? AND item_key = ?
            """,
            (
                change.title,
                change.content_markdown,
                change.content_sha256,
                document_id,
                now,
                generation_id,
                str(existing[0]),
            ),
        )
    connection.execute(
        """
        INSERT INTO knowledge_generation_state (singleton, current_generation_id)
        VALUES (1, ?)
        ON CONFLICT(singleton) DO UPDATE SET current_generation_id = excluded.current_generation_id
        """,
        (generation_id,),
    )
    return generation_id


def _insert_candidate_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    change: _IncomingChange,
    classification: str,
    status: str,
    baseline: _Baseline | None,
    observed_generation_id: int | None,
    now: str,
) -> str:
    candidate_id = uuid.uuid4().hex
    source_block_id = change.source_block_id
    if source_block_id is not None:
        source_block = connection.execute(
            """
            SELECT 1 FROM document_ir_blocks
            WHERE block_id = ? AND document_id = ?
            """,
            (source_block_id, document_id),
        ).fetchone()
        if source_block is None:
            # D1 versions reuse their canonical document's IR.  The incoming
            # transient block has no row of its own, but the version must still
            # be reconciled against published knowledge.
            source_block_id = None
    connection.execute(
        """
        INSERT INTO knowledge_reconciliation_candidates (
            candidate_id, document_id, source_block_id, kind, title, normalized_title,
            content_markdown, content_sha256, classification, status,
            baseline_kind, baseline_id, baseline_title, baseline_content_markdown,
            observed_generation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            document_id,
            source_block_id,
            change.kind,
            change.title,
            change.normalized_title,
            change.content_markdown,
            change.content_sha256,
            classification,
            status,
            baseline.kind if baseline is not None else None,
            baseline.baseline_id if baseline is not None else None,
            baseline.title if baseline is not None else None,
            baseline.content_markdown if baseline is not None else None,
            observed_generation_id,
            now,
        ),
    )
    return candidate_id


def _current_generation_id_in(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1"
    ).fetchone()
    return int(row[0]) if row is not None else None


def _conflict_from_row(row: tuple[object, ...]) -> DesktopKnowledgeReconciliationConflict:
    return DesktopKnowledgeReconciliationConflict(
        candidate_id=str(row[0]),
        document_id=str(row[1]),
        document_name=str(row[2]),
        kind=str(row[3]),
        title=str(row[4]),
        content_markdown=str(row[5]),
        baseline_kind=str(row[6]),
        baseline_title=str(row[7]),
        baseline_content_markdown=str(row[8]),
        observed_generation_id=int(str(row[9])) if row[9] is not None else None,
    )


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
