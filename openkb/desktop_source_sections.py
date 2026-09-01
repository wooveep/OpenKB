"""Canonical Evidence reads for bounded logical source sections."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_answer_types import DesktopEvidenceRef

SOURCE_SECTION_MAX_CHARACTERS = 12_000
FULL_SOURCE_MAX_CHARACTERS = 6_000


def source_section_evidence_in(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    terms: tuple[str, ...] = (),
) -> tuple[DesktopEvidenceRef, ...]:
    """Read one logical section as separately bound canonical EvidenceRefs."""
    occurrences = source_occurrences_in(connection, evidence_id)
    if not occurrences:
        return ()
    occurrence = min(occurrences, key=lambda item: source_occurrence_sort_key(item, terms))
    document_id, document_name, anchor_ordinal = (
        str(occurrence[0]),
        str(occurrence[1]),
        int(str(occurrence[2])),
    )
    rows = connection.execute(
        """
        SELECT blocks.ordinal, blocks.kind, blocks.text, blocks.heading_path,
            blocks.locator_json, occurrences.evidence_id
        FROM document_ir_blocks AS blocks
        JOIN evidence_occurrences AS occurrences
          ON occurrences.document_id = blocks.document_id
         AND occurrences.block_id = blocks.block_id
        WHERE blocks.document_id = ?
        ORDER BY blocks.ordinal
        """,
        (document_id,),
    ).fetchall()
    selected = _bounded_source_rows(rows, anchor_ordinal)
    references: list[DesktopEvidenceRef] = []
    seen: set[str] = set()
    for row in selected:
        selected_evidence_id = str(row[5])
        if selected_evidence_id in seen:
            continue
        seen.add(selected_evidence_id)
        references.append(
            DesktopEvidenceRef(
                evidence_id=selected_evidence_id,
                document_id=document_id,
                document_name=document_name,
                section=section_from_heading_path(str(row[3])),
                locator=_json_object(str(row[4])),
                excerpt=str(row[2]).strip(),
                channels=("knowledge_navigation_source_window",),
            )
        )
    return tuple(references)


def bounded_source_text(rows: list[tuple[object, ...]], anchor_ordinal: int) -> str:
    """Compatibility helper used to verify logical whole-block selection."""
    return "\n\n".join(str(row[2]).strip() for row in _bounded_source_rows(rows, anchor_ordinal))


def source_occurrences_in(
    connection: sqlite3.Connection, evidence_id: str
) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        SELECT occurrences.document_id, documents.display_name, blocks.ordinal,
            blocks.heading_path, blocks.locator_json, documents.created_at
        FROM evidence_occurrences AS occurrences
        JOIN source_documents AS documents ON documents.document_id = occurrences.document_id
        JOIN document_ir_blocks AS blocks ON blocks.block_id = occurrences.block_id
        WHERE occurrences.evidence_id = ? AND documents.availability = 'available'
        ORDER BY documents.created_at, documents.document_id, occurrences.ordinal
        """,
        (evidence_id,),
    ).fetchall()


def source_occurrence_sort_key(
    row: tuple[object, ...], terms: tuple[str, ...]
) -> tuple[bool, int, str, str, int]:
    section = section_from_heading_path(str(row[3]))
    return (
        is_administrative_section(section),
        -term_match_count(section, terms),
        str(row[5]),
        str(row[0]),
        int(str(row[2])),
    )


def section_from_heading_path(value: str) -> str:
    path = _heading_path(value)
    return " / ".join(path) if path else "Document"


def term_match_count(text: str, terms: tuple[str, ...]) -> int:
    normalized = text.casefold()
    return sum(1 for term in terms if term in normalized)


def is_administrative_section(section: str) -> bool:
    normalized = section.casefold()
    return any(
        marker in normalized
        for marker in ("修订记录", "revision history", "目录", "table of contents")
    )


def _bounded_source_rows(
    rows: list[tuple[object, ...]], anchor_ordinal: int
) -> tuple[tuple[object, ...], ...]:
    blocks = tuple(row for row in rows if str(row[2]).strip())
    if _character_count(blocks) <= FULL_SOURCE_MAX_CHARACTERS:
        return blocks
    anchor = next((row for row in blocks if int(str(row[0])) == anchor_ordinal), None)
    if anchor is None:
        return ()
    anchor_path = _heading_path(str(anchor[3]))
    logical = tuple(
        row
        for row in blocks
        if (path := _heading_path(str(row[3]))) == anchor_path
        or (anchor_path and path[: len(anchor_path)] == anchor_path)
    )
    if not logical:
        logical = (anchor,)
    if _character_count(logical) <= SOURCE_SECTION_MAX_CHARACTERS:
        return logical
    anchor_index = next(
        (index for index, row in enumerate(logical) if int(str(row[0])) == anchor_ordinal),
        0,
    )
    heading_index = next(
        (index for index, row in enumerate(logical) if str(row[1]) == "heading"),
        None,
    )
    order = _nearby_block_indexes(len(logical), anchor_index)
    if heading_index is not None:
        order = (heading_index, *(index for index in order if index != heading_index))
    required = {anchor_index}
    if heading_index is not None:
        required.add(heading_index)
    selected: dict[int, tuple[object, ...]] = {}
    used = 0
    for index in order:
        row = logical[index]
        text = str(row[2]).strip()
        separator = 2 if selected else 0
        if (
            index not in required
            and selected
            and used + separator + len(text) > SOURCE_SECTION_MAX_CHARACTERS
        ):
            continue
        ordinal = int(str(row[0]))
        selected[ordinal] = row
        used += separator + len(text)
    return tuple(selected[ordinal] for ordinal in sorted(selected))


def _character_count(rows: tuple[tuple[object, ...], ...]) -> int:
    if not rows:
        return 0
    return sum(len(str(row[2]).strip()) for row in rows) + (len(rows) - 1) * 2


def _nearby_block_indexes(length: int, anchor_index: int) -> tuple[int, ...]:
    values = [anchor_index]
    for distance in range(1, length):
        right = anchor_index + distance
        left = anchor_index - distance
        if right < length:
            values.append(right)
        if left >= 0:
            values.append(left)
    return tuple(values)


def _heading_path(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return ()
    return tuple(decoded)


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
