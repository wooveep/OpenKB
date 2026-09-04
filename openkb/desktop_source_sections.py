"""Canonical Evidence reads for bounded logical source sections."""

from __future__ import annotations

import json
import sqlite3

from openkb.desktop_answer_types import DesktopEvidenceRef
from openkb.desktop_scoped_evidence import ScopedEvidenceView

SOURCE_SECTION_MAX_CHARACTERS = 12_000
FULL_SOURCE_MAX_CHARACTERS = 6_000
SOURCE_OCCURRENCE_CONTEXT_KEY = "_openkb_source_occurrences"
SOURCE_BLOCK_KIND_CONTEXT_KEY = "_openkb_source_block_kind"


def source_section_evidence_in(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    terms: tuple[str, ...] = (),
    scoped_view: ScopedEvidenceView | None = None,
) -> tuple[DesktopEvidenceRef, ...]:
    """Read one logical section as separately bound canonical EvidenceRefs."""
    occurrences = source_occurrences_in(connection, evidence_id, scoped_view=scoped_view)
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
    occurrence_contexts = _repeated_occurrence_contexts(selected)
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
                locator=_locator_with_occurrence_contexts(
                    {
                        **_json_object(str(row[4])),
                        SOURCE_BLOCK_KIND_CONTEXT_KEY: str(row[1]),
                    },
                    occurrence_contexts.get(selected_evidence_id, ()),
                ),
                excerpt=str(row[2]).strip(),
                channels=("knowledge_navigation_source_window",),
            )
        )
    return tuple(references)


def bounded_source_text(rows: list[tuple[object, ...]], anchor_ordinal: int) -> str:
    """Compatibility helper used to verify logical whole-block selection."""
    return "\n\n".join(str(row[2]).strip() for row in _bounded_source_rows(rows, anchor_ordinal))


def source_occurrences_in(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    scoped_view: ScopedEvidenceView | None = None,
) -> list[tuple[object, ...]]:
    if scoped_view is not None:
        cte, parameters = scoped_view.sql_cte()
        return connection.execute(
            f"""
            {cte}
            SELECT document_id, display_name, ordinal, heading_path,
                locator_json, occurrence_rank
            FROM scoped_evidence_occurrences
            WHERE evidence_id = ? ORDER BY occurrence_rank
            """,
            (*parameters, evidence_id),
        ).fetchall()
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
    phase_outline = _phase_diverse_block_indexes(logical, anchor_path)
    if phase_outline:
        order = (*phase_outline, *(index for index in order if index not in phase_outline))
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


def _phase_diverse_block_indexes(
    rows: tuple[tuple[object, ...], ...],
    anchor_path: tuple[str, ...],
) -> tuple[int, ...]:
    """Read one substantive block per child phase before spending depth on any one."""
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, row in enumerate(rows):
        path = _heading_path(str(row[3]))
        if len(path) <= len(anchor_path) or path[: len(anchor_path)] != anchor_path:
            continue
        groups.setdefault(path, []).append(index)
    if len(groups) < 2:
        return ()
    ranked_groups = tuple(
        tuple(
            sorted(
                indexes,
                key=lambda index: (str(rows[index][1]) == "heading", index),
            )
        )
        for _path, indexes in sorted(groups.items(), key=lambda item: (len(item[0]), item[1][0]))
    )
    selected: list[int] = []
    for depth in range(max(len(indexes) for indexes in ranked_groups)):
        selected.extend(indexes[depth] for indexes in ranked_groups if depth < len(indexes))
    return tuple(selected)


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


def _repeated_occurrence_contexts(
    rows: tuple[tuple[object, ...], ...],
) -> dict[str, tuple[dict[str, object], ...]]:
    contexts: dict[str, list[dict[str, object]]] = {}
    for index, row in enumerate(rows):
        evidence_id = str(row[5])
        context: dict[str, object] = {"ordinal": int(str(row[0]))}
        previous_id = _previous_distinct_evidence_id(rows, index, evidence_id)
        if previous_id:
            context["previous_evidence_id"] = previous_id
        contexts.setdefault(evidence_id, []).append(context)
    return {
        evidence_id: tuple(values) for evidence_id, values in contexts.items() if len(values) > 1
    }


def _previous_distinct_evidence_id(
    rows: tuple[tuple[object, ...], ...], index: int, evidence_id: str
) -> str:
    for previous in reversed(rows[:index]):
        previous_id = str(previous[5])
        if previous_id != evidence_id:
            return previous_id
    return ""


def _locator_with_occurrence_contexts(
    locator: dict[str, object], contexts: tuple[dict[str, object], ...]
) -> dict[str, object]:
    if not contexts:
        return locator
    return {**locator, SOURCE_OCCURRENCE_CONTEXT_KEY: [dict(item) for item in contexts]}
